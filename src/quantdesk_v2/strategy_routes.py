from __future__ import annotations

import copy
import hashlib
import hmac
import math
import uuid
from collections.abc import Mapping
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from .database import get_db
from .dependencies import get_current_user
from .models import (
    AuditLog,
    StrategyRevision,
    StrategyTemplate,
    User,
    UserStrategy,
    utcnow,
)
from .schemas import (
    StrategyAiApplyRequest,
    StrategyAiPreviewRequest,
    StrategyCreateRequest,
    StrategyUpdateRequest,
)
from .strategy_ai import StrategyAiError, generate_strategy_preview
from .strategy_catalog import (
    DEFAULT_RISK,
    StrategyParameterError,
    ensure_system_templates,
    ensure_user_default_strategies,
    get_user_strategy,
    serialize_user_strategy,
    strategy_snapshot,
    validate_strategy_parameters,
)

router = APIRouter(prefix="/api/v2/strategies", tags=["strategies"])
MAX_ACTIVE_STRATEGIES = 100

_RISK_RULES: dict[str, tuple[float, float, bool]] = {
    "position_size_pct": (0.01, 100.0, False),
    "leverage": (1.0, 20.0, True),
    "fee_bps": (0.0, 1_000.0, False),
    "slippage_bps": (0.0, 1_000.0, False),
    "stop_loss_pct": (0.0, 99.9, False),
    "take_profit_pct": (0.0, 99.9, False),
    "max_holding_bars": (0.0, 50_000.0, True),
}


def _client_ip(request: Request) -> str | None:
    return request.client.host[:45] if request.client else None


def _audit(
    db: Session,
    request: Request,
    action: str,
    user_id: int,
    resource_id: str,
    *,
    metadata: dict[str, Any] | None = None,
) -> None:
    db.add(
        AuditLog(
            user_id=user_id,
            action=action,
            resource_type="user_strategy",
            resource_id=resource_id,
            ip_address=_client_ip(request),
            metadata_json=metadata,
        )
    )


def _template_response(template: StrategyTemplate) -> dict[str, Any]:
    return {
        "template_key": template.template_key,
        "name": template.name,
        "category": template.category,
        "description": template.description,
        "engine_key": template.engine_key,
        "parameter_schema": copy.deepcopy(template.parameter_schema_json),
        "parameters": copy.deepcopy(template.parameters_json),
        "risk_defaults": copy.deepcopy(template.risk_defaults_json),
        "version": template.version,
    }


def _normalize_risk_defaults(
    values: Mapping[str, Any] | None,
    *,
    base: Mapping[str, Any] | None = None,
    require_same_keys: bool = False,
) -> dict[str, int | float]:
    if values is None:
        values = {}
    if not isinstance(values, Mapping):
        raise ValueError("风险参数必须是对象")
    source = dict(base or DEFAULT_RISK)
    if require_same_keys and set(values) != set(source):
        raise ValueError("风险参数字段必须保持不变")
    unknown = sorted(set(values) - set(_RISK_RULES))
    if unknown:
        raise ValueError(f"未知风险参数：{', '.join(unknown)}")
    source.update(values)
    if set(source) != set(_RISK_RULES):
        raise ValueError("风险参数配置不完整")

    normalized: dict[str, int | float] = {}
    for key, (minimum, maximum, integer) in _RISK_RULES.items():
        raw = source[key]
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            raise ValueError(f"风险参数 {key} 必须是数字")
        numeric = float(raw)
        if not math.isfinite(numeric) or not minimum <= numeric <= maximum:
            raise ValueError(f"风险参数 {key} 超出允许范围")
        if integer:
            if not numeric.is_integer():
                raise ValueError(f"风险参数 {key} 必须是整数")
            normalized[key] = int(numeric)
        else:
            normalized[key] = int(numeric) if numeric.is_integer() else numeric
    return normalized


def _locked_user_strategy(db: Session, user_id: int, public_id: str) -> UserStrategy | None:
    return db.scalar(
        select(UserStrategy)
        .where(
            UserStrategy.user_id == user_id,
            UserStrategy.public_id == public_id,
        )
        .with_for_update()
    )


def _record_revision(
    db: Session,
    strategy: UserStrategy,
    *,
    source: str,
    summary: str,
) -> None:
    db.add(
        StrategyRevision(
            user_strategy_id=strategy.id,
            user_id=strategy.user_id,
            version=strategy.version,
            change_source=source,
            change_summary=summary[:500],
            snapshot_json=strategy_snapshot(strategy),
            created_at=utcnow(),
        )
    )


def _apply_edit(
    db: Session,
    strategy: UserStrategy,
    editable: Mapping[str, Any],
    *,
    expected_version: int,
    source: str,
    summary: str,
) -> None:
    if strategy.status != "active":
        raise HTTPException(status_code=409, detail="strategy is archived")
    if strategy.version != expected_version:
        raise HTTPException(
            status_code=409,
            detail={
                "message": "strategy version conflict; reload before saving",
                "current_version": strategy.version,
            },
        )
    try:
        parameters = validate_strategy_parameters(
            strategy.engine_key,
            editable["parameters"],
        )
        risks = _normalize_risk_defaults(
            editable["risk_defaults"],
            base=strategy.risk_defaults_json,
            require_same_keys=True,
        )
    except (KeyError, StrategyParameterError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None

    strategy.name = str(editable["name"]).strip()
    strategy.description = str(editable["description"]).strip()
    strategy.category = str(editable["category"]).strip()
    strategy.parameters_json = parameters
    strategy.risk_defaults_json = risks
    strategy.version += 1
    strategy.updated_at = utcnow()
    _record_revision(db, strategy, source=source, summary=summary)


def _commit_or_conflict(db: Session) -> None:
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail="strategy changed concurrently; reload before saving",
        ) from None
    except SQLAlchemyError:
        db.rollback()
        raise HTTPException(status_code=503, detail="strategy could not be saved") from None


@router.get("")
def list_strategies(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict[str, Any]:
    strategies = ensure_user_default_strategies(db, user.id)
    templates = ensure_system_templates(db)
    db.commit()
    return {
        "items": [serialize_user_strategy(item) for item in strategies],
        "templates": [_template_response(item) for item in templates if item.is_active],
        "limits": {"max_active_strategies": MAX_ACTIVE_STRATEGIES},
    }


@router.get("/{public_id}")
def get_strategy(
    public_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict[str, Any]:
    strategy = get_user_strategy(db, user.id, public_id)
    if strategy is None:
        raise HTTPException(status_code=404, detail="strategy not found")
    return serialize_user_strategy(strategy)


@router.post("", status_code=status.HTTP_201_CREATED)
def create_strategy(
    payload: StrategyCreateRequest,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict[str, Any]:
    ensure_user_default_strategies(db, user.id)
    active_count = db.scalar(
        select(func.count(UserStrategy.id)).where(
            UserStrategy.user_id == user.id,
            UserStrategy.status == "active",
        )
    )
    if int(active_count or 0) >= MAX_ACTIVE_STRATEGIES:
        raise HTTPException(status_code=409, detail="active strategy limit reached")

    template_key = payload.template_key or "trend_breakout"
    template = db.scalar(
        select(StrategyTemplate).where(
            StrategyTemplate.template_key == template_key,
            StrategyTemplate.is_active.is_(True),
        )
    )
    if template is None:
        raise HTTPException(status_code=422, detail="unknown strategy template")
    try:
        parameters = validate_strategy_parameters(
            template.engine_key,
            payload.parameters if payload.parameters is not None else template.parameters_json,
        )
        risks = _normalize_risk_defaults(
            payload.risk_defaults,
            base=template.risk_defaults_json,
        )
    except (StrategyParameterError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None

    now = utcnow()
    strategy = UserStrategy(
        public_id=str(uuid.uuid4()),
        user_id=user.id,
        # Custom copies deliberately do not consume the one-time default-copy key.
        source_template_id=None,
        name=payload.name,
        description=payload.description,
        category=payload.category,
        status="active",
        version=1,
        engine_key=template.engine_key,
        parameter_schema_json=copy.deepcopy(template.parameter_schema_json),
        parameters_json=parameters,
        risk_defaults_json=risks,
        created_via="manual",
        created_at=now,
        updated_at=now,
    )
    db.add(strategy)
    db.flush()
    _record_revision(db, strategy, source="manual", summary="新增用户策略")
    _audit(db, request, "strategy.create", user.id, strategy.public_id)
    _commit_or_conflict(db)
    return serialize_user_strategy(strategy)


@router.put("/{public_id}")
def update_strategy(
    public_id: str,
    payload: StrategyUpdateRequest,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict[str, Any]:
    strategy = _locked_user_strategy(db, user.id, public_id)
    if strategy is None:
        raise HTTPException(status_code=404, detail="strategy not found")
    _apply_edit(
        db,
        strategy,
        payload.model_dump(),
        expected_version=payload.version,
        source="manual",
        summary="手工修改策略配置",
    )
    _audit(
        db,
        request,
        "strategy.update",
        user.id,
        strategy.public_id,
        metadata={"version": strategy.version, "source": "manual"},
    )
    _commit_or_conflict(db)
    return serialize_user_strategy(strategy)


@router.post("/{public_id}/ai-preview")
def preview_ai_strategy_edit(
    public_id: str,
    payload: StrategyAiPreviewRequest,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict[str, Any]:
    strategy = get_user_strategy(db, user.id, public_id)
    if strategy is None or strategy.status != "active":
        raise HTTPException(status_code=404, detail="strategy not found")
    editable = strategy_snapshot(strategy)
    # Release the SQL connection before the external inference request. Applying
    # the preview always rechecks ownership and version in a separate transaction.
    db.rollback()
    settings = request.app.state.settings
    safety_identifier = (
        "qd_"
        + hmac.new(
            settings.jwt_secret.get_secret_value().encode("utf-8"),
            f"strategy-user:{user.id}".encode(),
            hashlib.sha256,
        ).hexdigest()[:32]
    )
    try:
        return generate_strategy_preview(
            editable,
            payload.prompt,
            settings.openai_api_key.get_secret_value(),
            settings.openai_strategy_model,
            settings.openai_strategy_timeout_seconds,
            safety_identifier,
        )
    except StrategyAiError as exc:
        status_by_category = {
            "not_configured": 503,
            "timeout": 504,
            "upstream": 502,
            "invalid_output": 502,
        }
        raise HTTPException(
            status_code=status_by_category[exc.category],
            detail={"message": "AI strategy preview failed", "category": exc.category},
        ) from None


@router.post("/{public_id}/ai-apply")
def apply_ai_strategy_edit(
    public_id: str,
    payload: StrategyAiApplyRequest,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict[str, Any]:
    strategy = _locked_user_strategy(db, user.id, public_id)
    if strategy is None:
        raise HTTPException(status_code=404, detail="strategy not found")
    _apply_edit(
        db,
        strategy,
        payload.proposed.model_dump(),
        expected_version=payload.base_version,
        source="ai",
        summary="确认应用 AI 语义修改预览",
    )
    _audit(
        db,
        request,
        "strategy.ai_apply",
        user.id,
        strategy.public_id,
        metadata={"version": strategy.version, "source": "ai"},
    )
    _commit_or_conflict(db)
    return serialize_user_strategy(strategy)


@router.delete("/{public_id}")
def archive_strategy(
    public_id: str,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict[str, Any]:
    strategy = _locked_user_strategy(db, user.id, public_id)
    if strategy is None:
        raise HTTPException(status_code=404, detail="strategy not found")
    if strategy.status != "archived":
        strategy.status = "archived"
        strategy.version += 1
        strategy.updated_at = utcnow()
        _record_revision(db, strategy, source="manual", summary="归档用户策略")
        _audit(db, request, "strategy.archive", user.id, strategy.public_id)
        _commit_or_conflict(db)
    return serialize_user_strategy(strategy)
