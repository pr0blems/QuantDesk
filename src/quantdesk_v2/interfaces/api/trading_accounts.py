"""Paper and live trading account HTTP routes."""

# FastAPI dependency markers intentionally remain defaults so the extracted
# handlers retain the exact public signatures of the former router.
# ruff: noqa: B008

from __future__ import annotations

import math
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ...binance_client import BinanceAccountClientError
from ...binance_rate_limit import REST_RATE_LIMITER
from ...database import get_db
from ...dependencies import get_current_user
from ...market_config import TRADFI_UNIVERSE_KEY, tradfi_live_symbols
from ...models import (
    LiveOrderIntent,
    LiveTradingAccount,
    PaperAccount,
    StrategyDeployment,
    StrategyRevision,
    User,
    UserStrategy,
    utcnow,
)
from ...monitor import MonitorUnavailable
from ...schemas import (
    DashboardPerformanceOut,
    LiveAccountArmRequest,
    LiveAccountCreateRequest,
    LiveAccountStatusUpdate,
    LiveAccountStrategyUpdate,
    PaperAccountCreateRequest,
    PaperAccountStatusUpdate,
    PaperAccountStrategyUpdate,
)
from ...security import CredentialCipher, SecurityError
from ...strategy_artifacts import add_run_manifest
from ...strategy_catalog import get_user_strategy
from ...strategy_evaluator import StrategyEvaluationError, resolve_builtin_strategy_timeframe
from ...strategy_lifecycle import (
    LIVE_ELIGIBLE_STATUSES,
    PAPER_ELIGIBLE_STATUSES,
    current_strategy_revision,
)
from .common import add_audit_log, monitor_repository, require_expected_user

router = APIRouter()
_audit = add_audit_log
_monitor = monitor_repository
_require_expected_user = require_expected_user

def _paper_account_record(
    db: Session, user_id: int, public_id: str | None = None
) -> PaperAccount | None:
    statement = select(PaperAccount).where(
        PaperAccount.user_id == user_id,
        PaperAccount.status != "archived",
    )
    if public_id:
        statement = statement.where(PaperAccount.public_id == public_id)
    else:
        statement = statement.order_by(
            PaperAccount.status.asc(), PaperAccount.created_at.asc(), PaperAccount.id.asc()
        )
    return db.scalar(statement.limit(1))


def _paper_strategy_snapshots(snapshot: dict[str, Any] | None) -> list[dict[str, Any]]:
    selected = snapshot if isinstance(snapshot, dict) else {}
    bundled = selected.get("strategy_snapshots")
    if isinstance(bundled, list):
        normalized = [item for item in bundled if isinstance(item, dict)]
        if normalized:
            return normalized
    return [selected] if selected else []


def _paper_account_out(account: PaperAccount) -> dict[str, Any]:
    snapshot = account.strategy_snapshot_json or {}
    snapshots = _paper_strategy_snapshots(snapshot)
    strategy_ids = [str(item["public_id"]) for item in snapshots if item.get("public_id")]
    strategy_names = [str(item["name"]) for item in snapshots if item.get("name")]
    return {
        "id": account.public_id,
        "name": account.name,
        "status": account.status,
        "strategy_id": snapshot.get("public_id"),
        "strategy_name": " + ".join(strategy_names) or snapshot.get("name"),
        "strategy_ids": strategy_ids,
        "strategy_names": strategy_names,
        "strategies": [
            {
                "id": item.get("public_id"),
                "name": item.get("name"),
                "engine_key": item.get("engine_key"),
                "strategy_kind": item.get("strategy_kind"),
                "version": item.get("version"),
            }
            for item in snapshots
        ],
        "combination_mode": "all",
        "engine_key": snapshot.get("engine_key"),
        "initial_balance": float(account.initial_balance),
        "balance": float(account.balance),
        "config": account.config_json,
        "started_at": account.started_at,
        "last_tick_at": account.last_tick_at,
        "created_at": account.created_at,
        "updated_at": account.updated_at,
    }


def _execution_strategy_snapshot(strategy: UserStrategy) -> dict[str, Any]:
    """Freeze the strategy inputs used by paper and live deployments."""

    snapshot = {
        "public_id": strategy.public_id,
        "name": strategy.name,
        "engine_key": strategy.engine_key,
        "strategy_kind": strategy.strategy_kind,
        "version": strategy.version,
        "spec_schema_version": strategy.spec_schema_version,
        "spec": strategy.spec_json,
        "spec_hash": strategy.spec_hash,
        "source_language": getattr(strategy, "source_language", None),
        "source_code": getattr(strategy, "source_code", None),
        "source_hash": getattr(strategy, "source_hash", None),
        "source_runtime_version": getattr(strategy, "source_runtime_version", None),
        "source_validation": getattr(strategy, "source_validation_json", None),
        "parameters": strategy.parameters_json,
        "risk_defaults": strategy.risk_defaults_json,
    }
    if strategy.strategy_kind not in {"full_strategy", "source_strategy"}:
        try:
            snapshot["timeframe"] = resolve_builtin_strategy_timeframe(
                strategy.parameters_json,
                strategy.risk_defaults_json,
            )
        except StrategyEvaluationError as exc:
            raise HTTPException(
                status_code=409,
                detail="strategy execution timeframe is invalid",
            ) from exc
    return snapshot


def _execution_strategy_bundle(strategies: list[UserStrategy]) -> dict[str, Any]:
    snapshots = [_execution_strategy_snapshot(strategy) for strategy in strategies]
    if not snapshots:
        raise HTTPException(status_code=422, detail="at least one paper strategy is required")
    return {
        **snapshots[0],
        "combination_mode": "all",
        "strategy_snapshots": snapshots,
    }


def _active_paper_strategies(
    db: Session, user_id: int, strategy_ids: list[str]
) -> list[UserStrategy]:
    selected: list[UserStrategy] = []
    for strategy_id in strategy_ids:
        strategy = get_user_strategy(db, user_id, strategy_id)
        if strategy is None or strategy.status != "active":
            raise HTTPException(status_code=404, detail="未找到可用策略")
        revision = current_strategy_revision(db, strategy)
        if revision is None:
            raise HTTPException(status_code=409, detail="当前策略版本缺少不可变修订记录")
        if revision.lifecycle_status not in PAPER_ELIGIBLE_STATUSES:
            raise HTTPException(
                status_code=409,
                detail={
                    "message": "当前策略修订尚未取得模拟盘资格",
                    "lifecycle_status": revision.lifecycle_status,
                    "required_statuses": sorted(PAPER_ELIGIBLE_STATUSES),
                },
            )
        selected.append(strategy)
    return selected


def _strategy_revision_for_deployment(
    db: Session, user_id: int, strategy: UserStrategy
) -> StrategyRevision:
    revision = db.scalar(
        select(StrategyRevision).where(
            StrategyRevision.user_strategy_id == strategy.id,
            StrategyRevision.user_id == user_id,
            StrategyRevision.version == strategy.version,
        )
    )
    if revision is None:
        raise HTTPException(status_code=409, detail="strategy revision is unavailable")
    return revision


def _paper_deployment_name(
    account_name: str, strategy_name: str, strategy_count: int
) -> str:
    if strategy_count == 1:
        return account_name
    return f"{account_name} · {strategy_name}"[:100]


def _paper_response(data: dict) -> dict:
    return {**data, "permissions": {"can_reset": True}}


def _live_account_record(
    db: Session, user_id: int, account_id: str | None
) -> LiveTradingAccount | None:
    query = select(LiveTradingAccount).where(
        LiveTradingAccount.user_id == user_id,
        LiveTradingAccount.status != "archived",
    )
    if account_id:
        query = query.where(LiveTradingAccount.public_id == account_id)
    query = query.order_by(LiveTradingAccount.created_at, LiveTradingAccount.id)
    return next(
        (
            account
            for account in db.scalars(query).all()
            if str((account.config_json or {}).get("execution_scope") or "")
            != "ai_monitor"
        ),
        None,
    )


def _live_account_out(account: LiveTradingAccount, *, enabled: bool) -> dict[str, Any]:
    snapshot = account.strategy_snapshot_json or {}
    return {
        "id": account.public_id,
        "name": account.name,
        "status": account.status,
        "strategy_id": snapshot.get("public_id"),
        "strategy_name": snapshot.get("name"),
        "engine_key": snapshot.get("engine_key"),
        "config": account.config_json,
        "credential_version": account.credential_version,
        "armed_at": account.armed_at,
        "last_tick_at": account.last_tick_at,
        "last_error_code": account.last_error_code,
        "system_enabled": enabled,
        "created_at": account.created_at,
        "updated_at": account.updated_at,
    }


_LIVE_RISK_DEFAULTS: dict[str, Any] = {
    "risk_per_trade_pct": 0.5,
    "max_total_risk_pct": 4,
    "max_cluster_positions": 2,
    "risk_max_leverage": 10,
    "liquidation_buffer_pct": 1.5,
    "daily_loss_limit_pct": 2,
    "max_drawdown_pct": 6,
    "short_risk_multiplier": 0.5,
    "max_ticker_age_seconds": 120,
    "max_signal_age_seconds": 18_000,
    "block_high_risk_products": True,
    "round_trip_cost_bps": 16,
    "max_high_risk_positions": 1,
    "high_risk_multiplier": 0.5,
    "signal_valid_bars": 1,
}


def _live_risk_config(
    payload: LiveAccountCreateRequest | LiveAccountStrategyUpdate,
    existing: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return the execution risk envelope stored with every live snapshot."""

    config = {
        "leverage": payload.leverage,
        "max_positions": payload.max_positions,
        # Retained as a hard margin ceiling for compatibility. Position size is
        # now further reduced by risk_per_trade_pct and the actual stop distance.
        "position_size_pct": payload.position_size_pct,
        "margin_cap": payload.margin_cap,
    }
    previous = existing or {}
    for key, default in _LIVE_RISK_DEFAULTS.items():
        submitted = getattr(payload, key, None)
        config[key] = submitted if submitted is not None else previous.get(key, default)
    return config


def _binance_permissions_include_trade(user: User) -> bool:
    permissions = user.binance_permissions or {}
    requested = permissions.get("requested") if isinstance(permissions, dict) else None
    return isinstance(requested, list) and "TRADE" in requested


def _lock_and_revalidate_live_arm(
    db: Session,
    *,
    user_id: int,
    account_id: str,
    confirmation_name: str,
    credential_version: int,
    encrypted_key: str,
    encrypted_secret: str,
) -> LiveTradingAccount:
    """Serialize arming and recheck mutable state after the Binance preflight."""

    locked_user = db.scalar(select(User).where(User.id == user_id).with_for_update())
    if locked_user is None:
        raise HTTPException(status_code=404, detail="user not found")
    if (
        not locked_user.binance_credentials_configured
        or not _binance_permissions_include_trade(locked_user)
        or locked_user.binance_key_version != credential_version
        or locked_user.binance_api_key_encrypted != encrypted_key
        or locked_user.binance_api_secret_encrypted != encrypted_secret
    ):
        raise HTTPException(
            status_code=409,
            detail="Binance credentials changed during preflight; retry arming",
        )

    account = db.scalar(
        select(LiveTradingAccount)
        .where(
            LiveTradingAccount.user_id == user_id,
            LiveTradingAccount.public_id == account_id,
            LiveTradingAccount.status != "archived",
        )
        .with_for_update()
    )
    if account is None:
        raise HTTPException(status_code=404, detail="live account not found")
    if account.name != confirmation_name:
        raise HTTPException(
            status_code=409,
            detail="live-trading confirmation did not match",
        )

    active = db.scalar(
        select(LiveTradingAccount.id)
        .where(
            LiveTradingAccount.user_id == user_id,
            LiveTradingAccount.status == "active",
            LiveTradingAccount.id != account.id,
        )
        .limit(1)
    )
    if active is not None:
        raise HTTPException(status_code=409, detail="pause the other live deployment first")
    unresolved = db.scalar(
        select(LiveOrderIntent.id)
        .where(
            LiveOrderIntent.user_id == user_id,
            LiveOrderIntent.live_account_id == account.id,
            LiveOrderIntent.status == "unknown",
        )
        .limit(1)
    )
    if unresolved is not None:
        raise HTTPException(
            status_code=409,
            detail="resolve unknown Binance order states before arming",
        )
    return account

@router.get("/paper/accounts")
def list_paper_accounts(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list[dict[str, Any]]:
    accounts = db.scalars(
        select(PaperAccount)
        .where(PaperAccount.user_id == user.id, PaperAccount.status != "archived")
        .order_by(PaperAccount.created_at, PaperAccount.id)
    ).all()
    return [_paper_account_out(account) for account in accounts]


@router.post("/paper/accounts", status_code=status.HTTP_201_CREATED)
def create_paper_account(
    payload: PaperAccountCreateRequest,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict[str, Any]:
    existing_count = db.scalar(
        select(func.count(PaperAccount.id)).where(
            PaperAccount.user_id == user.id, PaperAccount.status != "archived"
        )
    )
    if int(existing_count or 0) >= 20:
        raise HTTPException(status_code=409, detail="paper account limit reached")
    strategies = _active_paper_strategies(db, user.id, payload.strategy_ids or [])
    primary_strategy = strategies[0]
    revisions = [
        _strategy_revision_for_deployment(db, user.id, strategy)
        for strategy in strategies
    ]
    risk = dict(primary_strategy.risk_defaults_json or {})
    config: dict[str, Any] = {
        "leverage": risk.get("leverage", 20),
        "max_positions": risk.get("max_positions", 15),
        "position_size_pct": risk.get("position_size_pct", 10),
        "margin_cap": risk.get("margin_cap", 0.8),
        "fee_bps": risk.get("fee_bps", 5),
        "slippage_bps": risk.get("slippage_bps", 3),
        "stop_loss_pct": risk.get("stop_loss_pct", 3),
        "take_profit_pct": risk.get("take_profit_pct", 5),
        "max_holding_bars": risk.get("max_holding_bars", 12),
        # Every paper deployment uses the immutable strategy-event path.
        "signal_mode": "strategy_event_v2",
        "strategy_combination_mode": "all",
    }
    for key in ("leverage", "max_positions", "position_size_pct", "margin_cap"):
        value = getattr(payload, key)
        if value is not None:
            config[key] = value
    # Paper leverage is an exact future-entry setting. Persist the same value as
    # the sizing ceiling so risk controls may reduce quantity, but not leverage.
    config["risk_max_leverage"] = min(int(config["leverage"]), 20)
    account = PaperAccount(
        user_id=user.id,
        strategy_id=primary_strategy.id,
        name=payload.name,
        initial_balance=Decimal(str(payload.initial_balance)),
        balance=Decimal(str(payload.initial_balance)),
        config_json=config,
        strategy_snapshot_json=_execution_strategy_bundle(strategies),
    )
    db.add(account)
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="paper account name already exists") from exc
    db.execute(
        text(
            """INSERT INTO paper_account_balance_checkpoints(
                   paper_account_id,user_id,baseline_balance,baseline_execution_id,
                   expected_balance,last_execution_id
               ) VALUES(:account_id,:user_id,:balance,NULL,:balance,NULL)"""
        ),
        {
            "account_id": account.id,
            "user_id": user.id,
            "balance": account.balance,
        },
    )
    for strategy, revision in zip(strategies, revisions, strict=True):
        deployment = StrategyDeployment(
            public_id=str(uuid.uuid4()),
            user_id=user.id,
            strategy_id=strategy.id,
            strategy_revision_id=revision.id,
            mode="paper",
            target_account_id=account.id,
            name=_paper_deployment_name(account.name, strategy.name, len(strategies)),
            status="running",
            runtime_state_json={"combination_mode": "all"},
            started_at=utcnow(),
        )
        db.add(deployment)
        add_run_manifest(
            db,
            deployment,
            revision,
            data_set_id=f"paper-account:{account.public_id}",
            extra={"account_name": account.name, "combination_mode": "all"},
        )
    _audit(db, request, "paper.account.create", user.id, "paper_account", account.public_id)
    db.commit()
    db.refresh(account)
    return _paper_account_out(account)


@router.patch("/paper/accounts/{account_id}")
def update_paper_account_status(
    account_id: str,
    payload: PaperAccountStatusUpdate,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict[str, Any]:
    account = _paper_account_record(db, user.id, account_id)
    if account is None:
        raise HTTPException(status_code=404, detail="paper account not found")
    if payload.status == "archived":
        open_count = db.scalar(
            text(
                """SELECT COUNT(*) FROM paper_positions
                   WHERE paper_account_id=:account_id AND user_id=:user_id"""
            ),
            {"account_id": account.id, "user_id": user.id},
        )
        if int(open_count or 0):
            raise HTTPException(status_code=409, detail="close positions before archiving")
    if payload.status is not None:
        account.status = payload.status
    if payload.name is not None:
        account.name = payload.name
    deployments = db.scalars(
        select(StrategyDeployment).where(
            StrategyDeployment.user_id == user.id,
            StrategyDeployment.mode == "paper",
            StrategyDeployment.target_account_id == account.id,
        )
    ).all()
    strategy_names = {
        strategy.id: strategy.name
        for strategy in db.scalars(
            select(UserStrategy).where(
                UserStrategy.user_id == user.id,
                UserStrategy.id.in_([item.strategy_id for item in deployments] or [-1]),
            )
        ).all()
    }
    selected_public_ids = {
        str(item["public_id"])
        for item in _paper_strategy_snapshots(account.strategy_snapshot_json)
        if item.get("public_id")
    }
    selected_strategy_ids = {
        strategy.id
        for strategy in db.scalars(
            select(UserStrategy).where(
                UserStrategy.user_id == user.id,
                UserStrategy.public_id.in_(selected_public_ids or {""}),
            )
        ).all()
    }
    for deployment in deployments:
        if deployment.status == "stopped":
            continue
        if payload.status is not None:
            if payload.status == "archived" or deployment.strategy_id not in selected_strategy_ids:
                deployment.status = "stopped"
            else:
                deployment.status = "running" if payload.status == "active" else "paused"
        if payload.name is not None:
            deployment.name = _paper_deployment_name(
                payload.name,
                strategy_names.get(deployment.strategy_id, "策略"),
                len(deployments),
            )
        deployment.updated_at = utcnow()
    action = "paper.account.rename" if payload.name is not None else "paper.account.status"
    _audit(db, request, action, user.id, "paper_account", account.public_id)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="paper account name already exists") from exc
    db.refresh(account)
    return _paper_account_out(account)


@router.put("/paper/accounts/{account_id}/strategy")
def update_paper_account_strategy(
    account_id: str,
    payload: PaperAccountStrategyUpdate,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict[str, Any]:
    """Atomically replace the paper strategy snapshot and future-entry limits."""

    _require_expected_user(request, user)
    account = _paper_account_record(db, user.id, account_id)
    if account is None:
        raise HTTPException(status_code=404, detail="paper account not found")
    strategies = _active_paper_strategies(db, user.id, payload.strategy_ids or [])
    primary_strategy = strategies[0]
    revisions = [
        _strategy_revision_for_deployment(db, user.id, strategy)
        for strategy in strategies
    ]
    risk = dict(primary_strategy.risk_defaults_json or {})
    config = dict(account.config_json or {})
    config.update(
        {
            "leverage": payload.leverage,
            "risk_max_leverage": payload.leverage,
            "max_positions": payload.max_positions,
            "position_size_pct": payload.position_size_pct,
            "margin_cap": payload.margin_cap,
            "fee_bps": risk.get("fee_bps", config.get("fee_bps", 5)),
            "slippage_bps": risk.get("slippage_bps", config.get("slippage_bps", 3)),
            "stop_loss_pct": risk.get("stop_loss_pct", config.get("stop_loss_pct", 3)),
            "take_profit_pct": risk.get("take_profit_pct", config.get("take_profit_pct", 5)),
            "max_holding_bars": risk.get("max_holding_bars", config.get("max_holding_bars", 12)),
            "signal_mode": "strategy_event_v2",
            "strategy_combination_mode": "all",
        }
    )
    account.strategy_id = primary_strategy.id
    account.config_json = config
    account.strategy_snapshot_json = _execution_strategy_bundle(strategies)

    deployments = db.scalars(
        select(StrategyDeployment).where(
            StrategyDeployment.user_id == user.id,
            StrategyDeployment.mode == "paper",
            StrategyDeployment.target_account_id == account.id,
        )
    ).all()
    deployments_by_strategy: dict[int, list[StrategyDeployment]] = {}
    for deployment in deployments:
        deployments_by_strategy.setdefault(deployment.strategy_id, []).append(deployment)
    risk_override = {
        "leverage": payload.leverage,
        "risk_max_leverage": payload.leverage,
        "max_positions": payload.max_positions,
        "position_size_pct": payload.position_size_pct,
        "margin_cap": payload.margin_cap,
    }
    selected_deployments: set[int] = set()
    deployment_status = "paused" if account.status == "paused" else "running"
    for strategy, revision in zip(strategies, revisions, strict=True):
        matching = deployments_by_strategy.get(strategy.id) or []
        deployment = next(
            (
                item
                for item in matching
                if item.strategy_revision_id == revision.id and item.status != "stopped"
            ),
            None,
        )
        if deployment is None:
            for old_deployment in matching:
                old_deployment.status = "stopped"
                old_deployment.updated_at = utcnow()
            deployment = StrategyDeployment(
                public_id=str(uuid.uuid4()),
                user_id=user.id,
                strategy_id=strategy.id,
                strategy_revision_id=revision.id,
                mode="paper",
                target_account_id=account.id,
                name=_paper_deployment_name(account.name, strategy.name, len(strategies)),
                status=deployment_status,
                runtime_state_json={"combination_mode": "all"},
                started_at=utcnow(),
            )
            db.add(deployment)
            add_run_manifest(
                db,
                deployment,
                revision,
                data_set_id=f"paper-account:{account.public_id}",
                extra={"account_name": account.name, "combination_mode": "all"},
            )
        else:
            deployment.name = _paper_deployment_name(
                account.name, strategy.name, len(strategies)
            )
            deployment.status = deployment_status
            deployment.runtime_state_json = {"combination_mode": "all"}
        deployment.risk_override_json = risk_override
        deployment.last_error_code = None
        deployment.updated_at = utcnow()
        selected_deployments.add(id(deployment))
    for deployment in deployments:
        if id(deployment) not in selected_deployments:
            deployment.status = "stopped"
            deployment.updated_at = utcnow()
    _audit(
        db,
        request,
        "paper.account.strategy.update",
        user.id,
        "paper_account",
        account.public_id,
    )
    db.commit()
    db.refresh(account)
    return _paper_account_out(account)


@router.get("/paper")
def paper_account(
    request: Request,
    account_id: str | None = Query(default=None, min_length=36, max_length=36),
    timezone_offset_minutes: int = Query(default=0, ge=-720, le=840),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    account = _paper_account_record(db, user.id, account_id)
    if account is None:
        raise HTTPException(status_code=404, detail="paper account not found")
    return _paper_response(_monitor(request).paper(user.id, account.id, timezone_offset_minutes))


@router.get("/dashboard/performance", response_model=DashboardPerformanceOut)
def dashboard_performance(
    request: Request,
    response: Response,
    month: str | None = Query(default=None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    account_id: str | None = Query(default=None, min_length=36, max_length=36),
    timezone_offset_minutes: int = Query(default=0, ge=-720, le=840),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> DashboardPerformanceOut:
    response.headers["Cache-Control"] = "private, no-store"
    local_now = datetime.now(UTC) + timedelta(minutes=timezone_offset_minutes)
    selected_month = month or local_now.strftime("%Y-%m")
    selected_year = int(selected_month[:4])
    if not 2000 <= selected_year <= 2100:
        raise HTTPException(status_code=422, detail="performance month is out of range")
    account = _paper_account_record(db, user.id, account_id)
    if account is None:
        raise HTTPException(status_code=404, detail="paper account not found")
    try:
        return DashboardPerformanceOut.model_validate(
            _monitor(request).paper_performance(
                user.id, account.id, selected_month, timezone_offset_minutes
            )
        )
    except MonitorUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from None


@router.post("/paper/reset")
def reset_paper_account(
    request: Request,
    account_id: str | None = Query(default=None, min_length=36, max_length=36),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    account = _paper_account_record(db, user.id, account_id)
    if account is None:
        raise HTTPException(status_code=404, detail="paper account not found")
    data = _monitor(request).reset_paper(user.id, account.id)
    _audit(db, request, "paper.account.reset", user.id, "paper_account", account.public_id)
    db.commit()
    return _paper_response(data)




@router.get("/live/accounts")
def list_live_accounts(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict[str, Any]:
    account_rows = db.scalars(
        select(LiveTradingAccount)
        .where(
            LiveTradingAccount.user_id == user.id,
            LiveTradingAccount.status != "archived",
        )
        .order_by(LiveTradingAccount.created_at, LiveTradingAccount.id)
    ).all()
    accounts = [
        account
        for account in account_rows
        if str((account.config_json or {}).get("execution_scope") or "")
        != "ai_monitor"
    ]
    enabled = request.app.state.settings.binance_live_trading_enabled
    universe = tradfi_live_symbols()
    return {
        "items": [_live_account_out(account, enabled=enabled) for account in accounts],
        "system_enabled": enabled,
        "credentials_configured": user.binance_credentials_configured,
        "trade_permission_requested": _binance_permissions_include_trade(user),
        "universe": {
            "key": TRADFI_UNIVERSE_KEY,
            "count": len(universe),
            "label": "Binance TradFi 股票及传统资产合约池",
        },
    }


@router.post("/live/accounts", status_code=status.HTTP_201_CREATED)
def create_live_account(
    payload: LiveAccountCreateRequest,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict[str, Any]:
    _require_expected_user(request, user)
    if not user.binance_credentials_configured:
        raise HTTPException(status_code=409, detail="configure Binance credentials first")
    if not _binance_permissions_include_trade(user):
        raise HTTPException(status_code=409, detail="Binance TRADE permission was not requested")
    existing_accounts = db.scalars(
        select(LiveTradingAccount).where(
            LiveTradingAccount.user_id == user.id,
            LiveTradingAccount.status != "archived",
        )
    ).all()
    existing_count = sum(
        1
        for account in existing_accounts
        if str((account.config_json or {}).get("execution_scope") or "")
        != "ai_monitor"
    )
    if existing_count >= 10:
        raise HTTPException(status_code=409, detail="live account limit reached")
    strategy = get_user_strategy(db, user.id, payload.strategy_id)
    if strategy is None or strategy.status != "active":
        raise HTTPException(status_code=404, detail="未找到可用策略")
    revision = db.scalar(
        select(StrategyRevision).where(
            StrategyRevision.user_strategy_id == strategy.id,
            StrategyRevision.user_id == user.id,
            StrategyRevision.version == strategy.version,
        )
    )
    if revision is None:
        raise HTTPException(status_code=409, detail="当前策略版本缺少不可变修订记录")
    if revision.lifecycle_status not in LIVE_ELIGIBLE_STATUSES:
        raise HTTPException(
            status_code=409,
            detail={
                "message": "当前策略修订尚未取得实盘资格",
                "lifecycle_status": revision.lifecycle_status,
                "required_statuses": sorted(LIVE_ELIGIBLE_STATUSES),
            },
        )
    universe = tradfi_live_symbols()
    if not universe:
        raise HTTPException(status_code=503, detail="TradFi trading universe is unavailable")
    risk = dict(strategy.risk_defaults_json or {})
    risk_config = _live_risk_config(payload)
    config = {
        "symbols": universe,
        "universe_key": TRADFI_UNIVERSE_KEY,
        "universe_count": len(universe),
        **risk_config,
        "stop_loss_pct": max(0.1, min(float(risk.get("stop_loss_pct", 3)), 20)),
        "take_profit_pct": max(0.1, min(float(risk.get("take_profit_pct", 5)), 50)),
        "max_holding_bars": max(0, min(int(risk.get("max_holding_bars", 12)), 1_000)),
    }
    account = LiveTradingAccount(
        user_id=user.id,
        strategy_id=strategy.id,
        name=payload.name,
        status="paused",
        config_json=config,
        strategy_snapshot_json=_execution_strategy_snapshot(strategy),
        credential_version=user.binance_key_version,
    )
    db.add(account)
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="live account name already exists") from exc
    deployment = StrategyDeployment(
        public_id=str(uuid.uuid4()),
        user_id=user.id,
        strategy_id=strategy.id,
        strategy_revision_id=revision.id,
        mode="live",
        target_account_id=account.id,
        name=account.name,
        status="paused",
        universe_override_json={
            "universe_key": TRADFI_UNIVERSE_KEY,
            "symbols": universe,
        },
        risk_override_json=risk_config,
        runtime_state_json={},
    )
    db.add(deployment)
    add_run_manifest(
        db,
        deployment,
        revision,
        data_set_id=f"live-account:{account.public_id}",
        extra={"credential_version": account.credential_version},
    )
    _audit(db, request, "live.account.create", user.id, "live_account", account.public_id)
    db.commit()
    db.refresh(account)
    return _live_account_out(
        account, enabled=request.app.state.settings.binance_live_trading_enabled
    )


@router.post("/live/accounts/{account_id}/arm")
def arm_live_account(
    account_id: str,
    payload: LiveAccountArmRequest,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict[str, Any]:
    _require_expected_user(request, user)
    if not request.app.state.settings.binance_live_trading_enabled:
        raise HTTPException(status_code=503, detail="server live-trading switch is disabled")
    account = _live_account_record(db, user.id, account_id)
    if account is None:
        raise HTTPException(status_code=404, detail="live account not found")
    if payload.confirmation_name != account.name or not payload.acknowledge_real_funds:
        raise HTTPException(status_code=409, detail="live-trading confirmation did not match")
    if not user.binance_credentials_configured or not _binance_permissions_include_trade(user):
        raise HTTPException(status_code=409, detail="Binance TRADE credentials are required")
    active = db.scalar(
        select(LiveTradingAccount.id).where(
            LiveTradingAccount.user_id == user.id,
            LiveTradingAccount.status == "active",
            LiveTradingAccount.id != account.id,
        )
    )
    if active is not None:
        raise HTTPException(status_code=409, detail="pause the other live deployment first")
    unresolved = db.scalar(
        select(func.count(LiveOrderIntent.id)).where(
            LiveOrderIntent.user_id == user.id,
            LiveOrderIntent.live_account_id == account.id,
            LiveOrderIntent.status == "unknown",
        )
    )
    if int(unresolved or 0):
        raise HTTPException(
            status_code=409,
            detail="resolve unknown Binance order states before arming",
        )

    encrypted_key = user.binance_api_key_encrypted or ""
    encrypted_secret = user.binance_api_secret_encrypted or ""
    credential_version = user.binance_key_version
    symbols = tradfi_live_symbols()
    if not symbols:
        raise HTTPException(status_code=503, detail="TradFi trading universe is unavailable")
    db.rollback()
    cipher = CredentialCipher(request.app.state.settings.credential_master_key.get_secret_value())
    try:
        api_key = cipher.decrypt(encrypted_key)
        api_secret = cipher.decrypt(encrypted_secret)
        snapshot = request.app.state.binance_service.account(
            api_key, api_secret, force_refresh=True
        )
        if snapshot.account_type != "UM_FUTURE":
            raise HTTPException(
                status_code=409,
                detail="实盘启用失败：当前风险模型不支持 Binance 统一账户，请使用标准 USD-M 合约账户。",
            )
        position_mode = request.app.state.binance_trading_client.position_mode(api_key, api_secret)
        eligible_symbols: list[str] = []
        for symbol in symbols:
            try:
                request.app.state.binance_trading_client.symbol_rules(symbol)
            except BinanceAccountClientError as exc:
                if exc.category == "unsupported_symbol":
                    continue
                raise
            eligible_symbols.append(symbol)
        if not eligible_symbols:
            raise HTTPException(
                status_code=409,
                detail="实盘启用失败：当前 TradFi 品种池没有可交易的 Binance USD-M 合约。",
            )
    except SecurityError:
        raise HTTPException(
            status_code=409, detail="Binance credentials cannot be decrypted"
        ) from None
    except BinanceAccountClientError as exc:
        reason = {
            "authentication": "API 密钥认证失败或没有合约交易权限",
            "timestamp": "本机时间与 Binance 服务器时间不同步",
            "rate_limit": "Binance 请求频率受限",
            "timeout": "连接 Binance 超时",
            "network": "无法连接 Binance",
            "upstream": "Binance 服务暂时异常",
            "invalid_response": "Binance 返回了无法识别的数据",
            "rejected": "Binance 拒绝了预检请求",
        }.get(exc.category, exc.category)
        raise HTTPException(status_code=409, detail=f"实盘启用失败：{reason}。") from None
    account = _lock_and_revalidate_live_arm(
        db,
        user_id=user.id,
        account_id=account_id,
        confirmation_name=payload.confirmation_name,
        credential_version=credential_version,
        encrypted_key=encrypted_key,
        encrypted_secret=encrypted_secret,
    )
    locked_user = db.scalar(select(User).where(User.id == user.id).with_for_update())
    if locked_user is None:
        raise HTTPException(status_code=409, detail="user account is unavailable")
    if not locked_user.binance_physical_account_id:
        locked_user.binance_physical_account_id = f"binance-usdm:{uuid.uuid4().hex}"
    account.status = "active"
    account_config = dict(account.config_json or {})
    account_config.update(
        {
            "symbols": symbols,
            "universe_key": TRADFI_UNIVERSE_KEY,
            "universe_count": len(symbols),
            "eligible_symbols": eligible_symbols,
            "eligible_count": len(eligible_symbols),
            "position_mode": position_mode,
            "preexisting_position_count": len(snapshot.positions),
        }
    )
    account.config_json = account_config
    account.credential_version = credential_version
    account.armed_at = utcnow()
    account.last_error_code = None
    deployment = db.scalar(
        select(StrategyDeployment)
        .where(
            StrategyDeployment.user_id == user.id,
            StrategyDeployment.mode == "live",
            StrategyDeployment.target_account_id == account.id,
            StrategyDeployment.status != "stopped",
        )
        .order_by(StrategyDeployment.id.desc())
        .with_for_update()
    )
    if deployment is None:
        raise HTTPException(status_code=409, detail="live deployment is unavailable")
    deployment_revision = db.scalar(
        select(StrategyRevision).where(
            StrategyRevision.id == deployment.strategy_revision_id,
            StrategyRevision.user_id == user.id,
        )
    )
    if (
        deployment_revision is None
        or deployment_revision.lifecycle_status not in LIVE_ELIGIBLE_STATUSES
    ):
        raise HTTPException(
            status_code=409,
            detail={
                "message": "该实盘部署绑定的策略修订尚未取得实盘资格",
                "lifecycle_status": (
                    deployment_revision.lifecycle_status
                    if deployment_revision is not None
                    else None
                ),
                "required_statuses": sorted(LIVE_ELIGIBLE_STATUSES),
            },
        )
    deployment.universe_override_json = {
        "universe_key": TRADFI_UNIVERSE_KEY,
        "symbols": symbols,
        "eligible_symbols": eligible_symbols,
        "position_mode": position_mode,
    }
    deployment.status = "running"
    deployment.started_at = utcnow()
    deployment.last_error_code = None
    _audit(db, request, "live.account.arm", user.id, "live_account", account.public_id)
    db.commit()
    db.refresh(account)
    return _live_account_out(account, enabled=True)


@router.patch("/live/accounts/{account_id}")
def update_live_account_status(
    account_id: str,
    payload: LiveAccountStatusUpdate,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict[str, Any]:
    _require_expected_user(request, user)
    account = _live_account_record(db, user.id, account_id)
    if account is None:
        raise HTTPException(status_code=404, detail="live account not found")
    if payload.status == "archived":
        submitted = db.scalar(
            select(func.count(LiveOrderIntent.id)).where(
                LiveOrderIntent.user_id == user.id,
                LiveOrderIntent.live_account_id == account.id,
                LiveOrderIntent.status.in_(["created", "submitted", "unknown"]),
            )
        )
        if account.status == "active" or int(submitted or 0):
            raise HTTPException(
                status_code=409,
                detail="pause deployment and resolve managed orders before archiving",
            )
    if payload.status is not None:
        account.status = payload.status
    if payload.name is not None:
        account.name = payload.name
    if payload.status == "paused":
        account.last_error_code = None
    deployment = db.scalar(
        select(StrategyDeployment)
        .where(
            StrategyDeployment.user_id == user.id,
            StrategyDeployment.mode == "live",
            StrategyDeployment.target_account_id == account.id,
            StrategyDeployment.status != "stopped",
        )
        .order_by(StrategyDeployment.id.desc())
    )
    if deployment is not None:
        if payload.status is not None:
            deployment.status = "paused" if payload.status == "paused" else "stopped"
        if payload.name is not None:
            deployment.name = payload.name
        deployment.updated_at = utcnow()
    action = "live.account.rename" if payload.name is not None else "live.account.status"
    _audit(db, request, action, user.id, "live_account", account.public_id)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="live account name already exists") from exc
    db.refresh(account)
    return _live_account_out(
        account, enabled=request.app.state.settings.binance_live_trading_enabled
    )


@router.put("/live/accounts/{account_id}/strategy")
def update_live_account_strategy(
    account_id: str,
    payload: LiveAccountStrategyUpdate,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict[str, Any]:
    """Replace the frozen strategy/risk snapshot without executing any exchange action."""
    _require_expected_user(request, user)
    account = _live_account_record(db, user.id, account_id)
    if account is None:
        raise HTTPException(status_code=404, detail="live account not found")
    if account.status != "paused":
        raise HTTPException(
            status_code=409,
            detail="pause the live deployment before adjusting its strategy",
        )
    pending = db.scalar(
        select(func.count(LiveOrderIntent.id)).where(
            LiveOrderIntent.user_id == user.id,
            LiveOrderIntent.live_account_id == account.id,
            LiveOrderIntent.status.in_(["created", "submitted", "unknown"]),
        )
    )
    if int(pending or 0):
        raise HTTPException(
            status_code=409,
            detail="resolve managed orders before adjusting the live strategy",
        )
    strategy = get_user_strategy(db, user.id, payload.strategy_id)
    if strategy is None or strategy.status != "active":
        raise HTTPException(status_code=404, detail="未找到可用策略")
    revision = db.scalar(
        select(StrategyRevision).where(
            StrategyRevision.user_strategy_id == strategy.id,
            StrategyRevision.user_id == user.id,
            StrategyRevision.version == strategy.version,
        )
    )
    if revision is None:
        raise HTTPException(status_code=409, detail="当前策略版本缺少不可变修订记录")
    if revision.lifecycle_status not in LIVE_ELIGIBLE_STATUSES:
        raise HTTPException(
            status_code=409,
            detail={
                "message": "当前策略修订尚未取得实盘资格",
                "lifecycle_status": revision.lifecycle_status,
                "required_statuses": sorted(LIVE_ELIGIBLE_STATUSES),
            },
        )
    universe = tradfi_live_symbols()
    if not universe:
        raise HTTPException(status_code=503, detail="TradFi trading universe is unavailable")

    risk = dict(strategy.risk_defaults_json or {})
    config = dict(account.config_json or {})
    risk_config = _live_risk_config(payload, config)
    config.update(
        {
            "symbols": universe,
            "universe_key": TRADFI_UNIVERSE_KEY,
            "universe_count": len(universe),
            **risk_config,
            "stop_loss_pct": max(0.1, min(float(risk.get("stop_loss_pct", 3)), 20)),
            "take_profit_pct": max(0.1, min(float(risk.get("take_profit_pct", 5)), 50)),
            "max_holding_bars": max(0, min(int(risk.get("max_holding_bars", 12)), 1_000)),
        }
    )
    for stale_key in (
        "eligible_symbols",
        "eligible_count",
        "position_mode",
        "preexisting_position_count",
    ):
        config.pop(stale_key, None)

    account.strategy_id = strategy.id
    account.config_json = config
    account.strategy_snapshot_json = _execution_strategy_snapshot(strategy)
    account.armed_at = None
    account.last_error_code = None

    deployment = db.scalar(
        select(StrategyDeployment)
        .where(
            StrategyDeployment.user_id == user.id,
            StrategyDeployment.mode == "live",
            StrategyDeployment.target_account_id == account.id,
            StrategyDeployment.status != "stopped",
        )
        .order_by(StrategyDeployment.id.desc())
    )
    if deployment is None:
        raise HTTPException(status_code=409, detail="live deployment is unavailable")
    deployment.status = "stopped"
    deployment.updated_at = utcnow()
    replacement = StrategyDeployment(
        public_id=str(uuid.uuid4()),
        user_id=user.id,
        strategy_id=strategy.id,
        strategy_revision_id=revision.id,
        mode="live",
        target_account_id=account.id,
        name=account.name,
        status="paused",
        universe_override_json={
            "universe_key": TRADFI_UNIVERSE_KEY,
            "symbols": universe,
        },
        risk_override_json=risk_config,
        runtime_state_json={
            "replaces_deployment_id": deployment.public_id,
            # The account-level drawdown baseline intentionally survives this
            # deployment replacement and remains authoritative.
            "preserve_account_loss_baseline": True,
        },
        last_evaluated_bar_time=None,
        last_error_code=None,
        started_at=None,
        created_at=utcnow(),
        updated_at=utcnow(),
    )
    db.add(replacement)
    add_run_manifest(
        db,
        replacement,
        revision,
        data_set_id=f"live-account:{account.public_id}",
        extra={
            "credential_version": account.credential_version,
            "replaces_deployment_id": deployment.public_id,
        },
    )
    _audit(
        db,
        request,
        "live.account.strategy.update",
        user.id,
        "live_account",
        account.public_id,
    )
    db.commit()
    db.refresh(account)
    return _live_account_out(
        account, enabled=request.app.state.settings.binance_live_trading_enabled
    )


@router.get("/live")
def live_trading_dashboard(
    request: Request,
    response: Response,
    account_id: str | None = Query(default=None, min_length=36, max_length=36),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict[str, Any]:
    response.headers["Cache-Control"] = "private, no-store"
    account = _live_account_record(db, user.id, account_id)
    if account is None:
        raise HTTPException(status_code=404, detail="live account not found")
    account_out = _live_account_out(
        account, enabled=request.app.state.settings.binance_live_trading_enabled
    )
    intents = db.scalars(
        select(LiveOrderIntent)
        .where(
            LiveOrderIntent.user_id == user.id,
            LiveOrderIntent.live_account_id == account.id,
        )
        .order_by(LiveOrderIntent.created_at.desc(), LiveOrderIntent.id.desc())
        .limit(100)
    ).all()
    intent_items = [
        {
            "id": item.public_id,
            "client_order_id": item.client_order_id,
            "binance_order_id": item.binance_order_id,
            "symbol": item.symbol,
            "action": item.action,
            "side": item.side,
            "position_side": item.position_side,
            "order_type": item.order_type,
            "quantity": float(item.quantity) if item.quantity is not None else None,
            "status": item.status,
            "error_code": item.error_code,
            "strategy_signal_id": item.strategy_signal_id,
            "entry_basis": item.entry_basis_json or {},
            "request": item.request_json or {},
            "response": item.response_json or {},
            "submitted_at": item.submitted_at,
            "created_at": item.created_at,
        }
        for item in intents
    ]

    latest_filled: dict[tuple[str, str], LiveOrderIntent] = {}
    for item in intents:
        key = (item.symbol, item.position_side or "BOTH")
        if item.status == "filled" and item.action in {"open", "close"}:
            latest_filled.setdefault(key, item)

    def positions_with_entry_basis(raw_positions: Any) -> list[dict[str, Any]]:
        result = []
        for raw_position in raw_positions:
            position = dict(raw_position)
            key = (
                str(position.get("symbol") or ""),
                str(position.get("position_side") or "BOTH"),
            )
            managed = latest_filled.get(key)
            if managed is not None and managed.action == "open":
                basis = managed.entry_basis_json or {
                    "schema_version": 1,
                    "availability": "legacy_missing",
                    "reasons": ["该实盘仓位早于开仓依据修复，历史证据不可用"],
                }
                position["entry_basis"] = basis
                position["managed_by_strategy"] = True
            else:
                position["entry_basis"] = {
                    "schema_version": 1,
                    "availability": "external_position",
                    "reasons": ["未找到本系统对应的策略开仓订单，可能是人工或外部仓位"],
                }
                position["managed_by_strategy"] = False
            result.append(position)
        return result

    if not user.binance_credentials_configured:
        return {
            "live_account": account_out,
            "binance": {
                "configured": False,
                "connected": False,
                "error_category": "not_configured",
            },
            "positions": [],
            "open_orders": [],
            "order_intents": intent_items,
        }
    encrypted_key = user.binance_api_key_encrypted or ""
    encrypted_secret = user.binance_api_secret_encrypted or ""
    db.rollback()
    cipher = CredentialCipher(request.app.state.settings.credential_master_key.get_secret_value())
    try:
        api_key = cipher.decrypt(encrypted_key)
        api_secret = cipher.decrypt(encrypted_secret)
        snapshot = request.app.state.binance_service.account(api_key, api_secret)
        open_orders = request.app.state.binance_service.open_orders(
            api_key, api_secret, account_type=snapshot.account_type
        )
    except SecurityError:
        category = "credential_error"
    except BinanceAccountClientError as exc:
        category = exc.category
    else:
        return {
            "live_account": account_out,
            "binance": {
                "configured": True,
                "connected": True,
                "account_type": snapshot.account_type,
                "wallet_balance": float(snapshot.wallet_balance),
                "available_balance": float(snapshot.available_balance),
                "unrealized_pnl": float(snapshot.unrealized_pnl),
                "updated_at": snapshot.updated_at,
                "error_category": None,
            },
            "positions": positions_with_entry_basis(snapshot.positions),
            "open_orders": list(open_orders),
            "order_intents": intent_items,
        }
    binance_error: dict[str, Any] = {
        "configured": True,
        "connected": False,
        "error_category": category,
    }
    if category == "rate_limit":
        rate_limit = REST_RATE_LIMITER.snapshot()
        binance_error.update(
            {
                "retry_at": (
                    datetime.fromtimestamp(rate_limit.retry_at, UTC)
                    if rate_limit.retry_at is not None
                    else None
                ),
                "retry_after_seconds": math.ceil(rate_limit.retry_after_seconds),
                "used_weight": rate_limit.used_weight,
                "weight_limit": rate_limit.weight_limit,
            }
        )
    return {
        "live_account": account_out,
        "binance": binance_error,
        "positions": [],
        "open_orders": [],
        "order_intents": intent_items,
    }
