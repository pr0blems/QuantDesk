"""Authenticated control plane for hierarchical trading kill switches."""

from __future__ import annotations

import re
import uuid
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .database import get_db
from .dependencies import get_current_user
from .models import (
    AuditLog,
    LiveTradingAccount,
    StrategyRevision,
    TradingControlEvent,
    TradingControlLatch,
    User,
    UserStrategy,
    utcnow,
)
from .trading_controls import (
    CONTROL_SCOPE_TYPES,
    account_symbol_scope_key,
    normalize_service_key,
    normalize_symbol,
    strategy_revision_scope_key,
    user_owner_scope,
)

router = APIRouter(prefix="/api/v2/risk", tags=["risk-controls"])

ScopeType = Literal[
    "global",
    "account",
    "strategy_revision",
    "symbol",
    "data_source",
    "broker_connection",
]


class KillSwitchCommandRequest(BaseModel):
    command_id: uuid.UUID
    action: Literal["engage", "release"]
    scope_type: ScopeType
    scope_key: str = Field(min_length=1, max_length=191)
    expected_version: int = Field(ge=0)
    reason_code: str = Field(
        min_length=3,
        max_length=64,
        pattern=r"^[a-z][a-z0-9_]{2,63}$",
    )
    reason: str = Field(min_length=10, max_length=500)
    confirmed: Literal[True]


def _client_ip(request: Request) -> str | None:
    return request.client.host[:45] if request.client else None


def _require_expected_identity(request: Request, user: User) -> None:
    expected = request.headers.get("X-QuantDesk-User-ID", "").strip()
    if not expected:
        raise HTTPException(status_code=428, detail="执行风险控制前必须确认当前用户")
    try:
        expected_user_id = int(expected)
    except ValueError:
        raise HTTPException(status_code=400, detail="确认用户标识无效") from None
    if expected_user_id != user.id:
        raise HTTPException(status_code=409, detail="登录用户已变化，请重新确认")


def _latch_out(latch: TradingControlLatch, *, idempotent: bool = False) -> dict[str, Any]:
    return {
        "id": latch.public_id,
        "owner_scope": latch.owner_scope,
        "scope_type": latch.scope_type,
        "scope_key": latch.scope_key,
        "engaged": latch.engaged,
        "reason_code": latch.reason_code,
        "reason": latch.reason_text,
        "version": latch.version,
        "changed_at": latch.changed_at,
        "idempotent": idempotent,
    }


def _account_for_scope(
    db: Session,
    user: User,
    account_public_id: str,
) -> LiveTradingAccount:
    account_id = account_public_id.strip().lower()
    account = db.scalar(
        select(LiveTradingAccount).where(LiveTradingAccount.public_id == account_id)
    )
    if account is None or (not user.is_admin and account.user_id != user.id):
        raise HTTPException(status_code=404, detail="未找到可控制的实盘账户")
    return account


def _resolve_scope(
    db: Session,
    user: User,
    scope_type: str,
    raw_scope_key: str,
) -> tuple[int | None, str, str, dict[str, Any]]:
    if scope_type not in CONTROL_SCOPE_TYPES:
        raise HTTPException(status_code=422, detail="不支持的风险控制层级")
    key = raw_scope_key.strip()
    if scope_type == "global":
        if not user.is_admin:
            raise HTTPException(status_code=403, detail="只有管理员可以控制全局熔断")
        if key != "*":
            raise HTTPException(status_code=422, detail="全局熔断的 scope_key 必须为 *")
        return None, "global", "*", {}
    if scope_type in {"data_source", "broker_connection"}:
        if not user.is_admin:
            raise HTTPException(status_code=403, detail="只有管理员可以控制基础设施熔断")
        try:
            normalized = normalize_service_key(key)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None
        return None, "global", normalized, {}
    if scope_type == "account":
        account = _account_for_scope(db, user, key)
        return (
            account.user_id,
            user_owner_scope(account.user_id),
            account.public_id.lower(),
            {"live_account_id": account.id},
        )
    if scope_type == "strategy_revision":
        match = re.fullmatch(r"([0-9a-fA-F-]{36})@([1-9][0-9]*)", key)
        if match is None:
            raise HTTPException(
                status_code=422,
                detail="策略 revision 控制键格式必须为 strategy_public_id@version",
            )
        strategy_public_id = match.group(1).lower()
        version = int(match.group(2))
        row = db.execute(
            select(UserStrategy, StrategyRevision)
            .join(
                StrategyRevision,
                StrategyRevision.user_strategy_id == UserStrategy.id,
            )
            .where(
                UserStrategy.public_id == strategy_public_id,
                StrategyRevision.version == version,
            )
        ).one_or_none()
        if row is None:
            raise HTTPException(status_code=404, detail="未找到可控制的策略 revision")
        strategy, revision = row
        if not user.is_admin and strategy.user_id != user.id:
            raise HTTPException(status_code=404, detail="未找到可控制的策略 revision")
        owner_id = strategy.user_id
        return (
            owner_id,
            user_owner_scope(owner_id),
            strategy_revision_scope_key(strategy_public_id, version),
            {
                "strategy_id": strategy.id,
                "strategy_revision_id": revision.id,
            },
        )
    if scope_type == "symbol":
        if ":" not in key:
            raise HTTPException(
                status_code=422,
                detail="品种控制键格式必须为 account_public_id:SYMBOL",
            )
        account_key, raw_symbol = key.rsplit(":", 1)
        try:
            symbol = normalize_symbol(raw_symbol)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None
        if account_key == "*":
            if not user.is_admin:
                raise HTTPException(status_code=403, detail="只有管理员可以全局冻结品种")
            return None, "global", f"*:{symbol}", {"symbol": symbol}
        account = _account_for_scope(db, user, account_key)
        return (
            account.user_id,
            user_owner_scope(account.user_id),
            account_symbol_scope_key(account.public_id, symbol),
            {"live_account_id": account.id, "symbol": symbol},
        )
    raise HTTPException(status_code=422, detail="不支持的风险控制层级")


@router.get("/kill-switches")
def list_kill_switches(
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
    engaged_only: Annotated[bool, Query()] = False,
) -> dict[str, Any]:
    statement = select(TradingControlLatch).where(
        or_(
            TradingControlLatch.owner_scope == "global",
            TradingControlLatch.owner_user_id == user.id,
        )
    )
    if engaged_only:
        statement = statement.where(TradingControlLatch.engaged.is_(True))
    rows = db.scalars(
        statement.order_by(
            TradingControlLatch.engaged.desc(),
            TradingControlLatch.scope_type,
            TradingControlLatch.changed_at.desc(),
        )
    ).all()
    return {"items": [_latch_out(row) for row in rows]}


@router.post("/kill-switch")
def transition_kill_switch(
    payload: KillSwitchCommandRequest,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
) -> dict[str, Any]:
    _require_expected_identity(request, user)
    owner_user_id, owner_scope, scope_key, scope_metadata = _resolve_scope(
        db,
        user,
        payload.scope_type,
        payload.scope_key,
    )
    command_id = str(payload.command_id)
    existing_event = db.scalar(
        select(TradingControlEvent).where(TradingControlEvent.command_id == command_id)
    )
    if existing_event is not None:
        if existing_event.actor_user_id != user.id:
            raise HTTPException(status_code=409, detail="风险控制命令标识已经被占用")
        latch = db.get(TradingControlLatch, existing_event.latch_id)
        if latch is None:
            raise HTTPException(status_code=503, detail="风险控制审计链不完整")
        return _latch_out(latch, idempotent=True)

    latch = db.scalar(
        select(TradingControlLatch)
        .where(
            TradingControlLatch.owner_scope == owner_scope,
            TradingControlLatch.scope_type == payload.scope_type,
            TradingControlLatch.scope_key == scope_key,
        )
        .with_for_update()
    )
    if latch is None:
        if payload.action == "release":
            raise HTTPException(status_code=409, detail="该层级尚未启用熔断")
        if payload.expected_version != 0:
            raise HTTPException(status_code=409, detail="风险控制版本已变化，请刷新后重试")
        latch = TradingControlLatch(
            public_id=str(uuid.uuid4()),
            owner_user_id=owner_user_id,
            owner_scope=owner_scope,
            scope_type=payload.scope_type,
            scope_key=scope_key,
            engaged=False,
            version=0,
            changed_by_user_id=user.id,
            changed_at=utcnow(),
            created_at=utcnow(),
        )
        db.add(latch)
        db.flush()
    if latch.version != payload.expected_version:
        raise HTTPException(status_code=409, detail="风险控制版本已变化，请刷新后重试")
    target_engaged = payload.action == "engage"
    if latch.engaged is target_engaged:
        state_label = "已启用" if target_engaged else "已解除"
        raise HTTPException(status_code=409, detail=f"该层级熔断{state_label}")

    resulting_version = latch.version + 1
    latch.engaged = target_engaged
    latch.reason_code = payload.reason_code
    latch.reason_text = payload.reason.strip()
    latch.version = resulting_version
    latch.changed_by_user_id = user.id
    latch.changed_at = utcnow()
    db.add(
        TradingControlEvent(
            public_id=str(uuid.uuid4()),
            command_id=command_id,
            latch_id=latch.id,
            actor_user_id=user.id,
            action=payload.action,
            expected_version=payload.expected_version,
            resulting_version=resulting_version,
            reason_code=payload.reason_code,
            reason_text=payload.reason.strip(),
            metadata_json=scope_metadata or None,
            created_at=utcnow(),
        )
    )
    db.add(
        AuditLog(
            user_id=user.id,
            action=f"risk.kill_switch.{payload.action}",
            resource_type="trading_control_latch",
            resource_id=latch.public_id,
            ip_address=_client_ip(request),
            metadata_json={
                "command_id": command_id,
                "owner_scope": owner_scope,
                "scope_type": payload.scope_type,
                "scope_key": scope_key,
                "expected_version": payload.expected_version,
                "resulting_version": resulting_version,
                "reason_code": payload.reason_code,
                **scope_metadata,
            },
        )
    )
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="风险控制状态冲突"
        ) from None
    db.refresh(latch)
    return _latch_out(latch)
