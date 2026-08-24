from __future__ import annotations

import copy
import hashlib
import hmac
import json
import math
import uuid
from collections.abc import Mapping
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from .ai_model_config import get_global_ai_model_config
from .database import get_db
from .dependencies import get_current_user
from .models import (
    AuditLog,
    StrategyDeployment,
    StrategyRevision,
    StrategySignal,
    StrategyTemplate,
    User,
    UserStrategy,
    utcnow,
)
from .schemas import (
    StrategyAiApplyRequest,
    StrategyAiPreviewRequest,
    StrategyCodeAiPreviewRequest,
    StrategyCodeUpdateRequest,
    StrategyCodeValidateRequest,
    StrategyCreateRequest,
    StrategyUpdateRequest,
)
from .security import CredentialCipher, SecurityError
from .strategy_ai import (
    StrategyAiError,
    generate_strategy_code_preview,
    generate_strategy_preview,
    generate_user_model_strategy_code_preview,
    generate_user_model_strategy_preview,
)
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
from .strategy_runtime import (
    INDICATOR_BY_KEY,
    INDICATOR_CATALOG,
    build_indicator_composite_spec,
    full_strategy_parameter_schema,
    indicator_composite_parameter_schema,
    strategy_spec_hash,
    validate_strategy_spec,
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
        "template_kind": template.template_kind,
        "spec_schema_version": template.spec_schema_version,
        "spec": copy.deepcopy(template.spec_json),
        "implementation_version": template.implementation_version,
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


def _normalize_schema_parameters(
    schema: list[dict[str, Any]], values: Mapping[str, Any]
) -> dict[str, int | float]:
    if not isinstance(values, Mapping):
        raise ValueError("策略参数必须是对象")
    definitions = {str(item["key"]): item for item in schema}
    unknown = sorted(set(values) - set(definitions))
    missing = sorted(set(definitions) - set(values))
    if unknown:
        raise ValueError(f"未知策略参数：{', '.join(unknown)}")
    if missing:
        raise ValueError(f"缺少策略参数：{', '.join(missing)}")
    normalized: dict[str, int | float] = {}
    for key, definition in definitions.items():
        raw = values[key]
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            raise ValueError(f"策略参数 {key} 必须是数字")
        numeric = float(raw)
        if not math.isfinite(numeric):
            raise ValueError(f"策略参数 {key} 必须是有限数字")
        if definition.get("type") == "integer":
            if not numeric.is_integer():
                raise ValueError(f"策略参数 {key} 必须是整数")
            value: int | float = int(numeric)
        else:
            value = int(numeric) if numeric.is_integer() else numeric
        if value < definition["min"] or value > definition["max"]:
            raise ValueError(
                f"策略参数 {key} 必须在 {definition['min']} 到 {definition['max']} 之间"
            )
        normalized[key] = value
    return normalized


def _indicator_ai_snapshot() -> dict[str, Any]:
    parameters: dict[str, int | float] = {
        "confirmation_threshold": 60,
        "signal_valid_bars": 2,
        "timeframe_minutes": 60,
        "allow_long": 1,
        "allow_short": 1,
    }
    default_keys = {"ema", "adx", "volume_ratio"}
    for indicator in INDICATOR_CATALOG:
        key = indicator["key"]
        parameters[f"{key}_weight"] = 1 if key in default_keys else 0
        for definition in indicator["parameters"]:
            parameters[f"{key}_{definition['key']}"] = definition["default"]
    return {
        "name": "AI 指标组合策略",
        "description": "由多个标准指标形成方向共识，并经过风险参数约束。",
        "category": "指标组合",
        "version": 1,
        "parameters": parameters,
        "risk_defaults": copy.deepcopy(DEFAULT_RISK),
    }


def _indicator_model_prompt(prompt: str) -> str:
    catalog_note = "；".join(
        f"{item['key']}={item['name']}（{item['role']}）" for item in INDICATOR_CATALOG
    )
    return (
        f"可选指标：{catalog_note}。"
        "请通过每个 指标key_weight 选择指标：0 表示不选，0.1 到 5 表示选择及权重；"
        "至少选择两个且至少一个 directional。timeframe_minutes 只能是 15、60 或 240；"
        "allow_long 和 allow_short 只能是 0 或 1。根据需求自动调整所选指标参数和风险参数。"
        f"\n用户需求：{prompt}"
    )


def _local_indicator_selection(parameters: dict[str, int | float], prompt: str) -> None:
    keywords = {
        "ema": ("ema", "均线"),
        "macd": ("macd", "动量"),
        "rsi": ("rsi", "超买", "超卖"),
        "bollinger": ("boll", "布林"),
        "adx": ("adx", "趋势强度"),
        "donchian": ("donchian", "唐奇安", "通道突破"),
        "volume_ratio": ("成交量", "量比", "放量"),
        "atr": ("atr", "波动率"),
    }
    normalized = prompt.lower()
    compact = "".join(normalized.split())
    selected = {
        key for key, words in keywords.items() if any(word in normalized for word in words)
    }
    if selected:
        for key in INDICATOR_BY_KEY:
            parameters[f"{key}_weight"] = 1 if key in selected else 0
    if "15m" in compact or "15分钟" in compact:
        parameters["timeframe_minutes"] = 15
    elif "4h" in compact or "4小时" in compact:
        parameters["timeframe_minutes"] = 240
    elif "1h" in compact or "1小时" in compact:
        parameters["timeframe_minutes"] = 60
    wants_long = any(word in normalized for word in ("只做多", "仅做多", "多头策略"))
    wants_short = any(word in normalized for word in ("只做空", "仅做空", "空头策略"))
    if wants_long != wants_short:
        parameters["allow_long"] = int(wants_long)
        parameters["allow_short"] = int(wants_short)


def _indicator_draft_from_proposed(
    proposed: Mapping[str, Any], *, prompt: str, local: bool
) -> dict[str, Any]:
    raw_parameters = proposed.get("parameters")
    if not isinstance(raw_parameters, Mapping):
        raise StrategyAiError("invalid_output")
    parameters = dict(raw_parameters)
    if local:
        _local_indicator_selection(parameters, prompt)
    selected_keys = [
        key
        for key in INDICATOR_BY_KEY
        if float(parameters.get(f"{key}_weight", 0)) >= 0.1
    ]
    if not any(INDICATOR_BY_KEY[key]["role"] == "directional" for key in selected_keys):
        selected_keys.insert(0, "ema")
    if len(selected_keys) < 2:
        selected_keys.append("volume_ratio" if "volume_ratio" not in selected_keys else "adx")
    selected_keys = list(dict.fromkeys(selected_keys))[:8]
    selections = []
    for key in selected_keys:
        indicator = INDICATOR_BY_KEY[key]
        raw_weight = float(parameters.get(f"{key}_weight", 1))
        weight = min(5.0, max(0.1, raw_weight))
        indicator_parameters: dict[str, int | float] = {}
        for definition in indicator["parameters"]:
            raw = parameters.get(f"{key}_{definition['key']}", definition["default"])
            value = min(float(definition["max"]), max(float(definition["min"]), float(raw)))
            if definition["type"] == "integer":
                value = int(round(value))
            elif value.is_integer():
                value = int(value)
            indicator_parameters[definition["key"]] = value
        selections.append({"key": key, "weight": weight, "parameters": indicator_parameters})
    timeframe_value = int(round(float(parameters.get("timeframe_minutes", 60))))
    timeframe = {15: "15m", 60: "1h", 240: "4h"}.get(timeframe_value, "1h")
    directions = []
    if float(parameters.get("allow_long", 1)) >= 0.5:
        directions.append("long")
    if float(parameters.get("allow_short", 1)) >= 0.5:
        directions.append("short")
    if not directions:
        directions = ["long", "short"]
    risk_defaults = _normalize_risk_defaults(
        proposed.get("risk_defaults"), base=DEFAULT_RISK, require_same_keys=True
    )
    return {
        "name": str(proposed.get("name") or "AI 指标组合策略")[:80],
        "description": str(proposed.get("description") or "")[:600],
        "category": str(proposed.get("category") or "指标组合")[:32],
        "timeframe": timeframe,
        "directions": directions,
        "confirmation_threshold": min(
            100.0, max(1.0, float(parameters.get("confirmation_threshold", 60)))
        ),
        "signal_valid_bars": min(
            10, max(1, int(round(float(parameters.get("signal_valid_bars", 2)))))
        ),
        "indicators": selections,
        "risk_defaults": risk_defaults,
    }


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
            spec_schema_version=strategy.spec_schema_version,
            spec_json=copy.deepcopy(strategy.spec_json),
            spec_hash=strategy.spec_hash,
            validation_json=(
                {"valid": True, "engine": "strategy_runtime_v1"}
                if strategy.strategy_kind == "full_strategy"
                else {"valid": True, "legacy": True}
            ),
            published_at=utcnow() if strategy.lifecycle_status == "published" else None,
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
        current_spec = strategy.spec_json if isinstance(strategy.spec_json, dict) else {}
        if current_spec.get("strategy_type") == "indicator_composite":
            parameters = _normalize_schema_parameters(
                strategy.parameter_schema_json,
                editable["parameters"],
            )
        else:
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
    if strategy.strategy_kind == "full_strategy":
        spec = copy.deepcopy(strategy.spec_json or {})
        spec["parameters"] = copy.deepcopy(parameters)
        try:
            strategy.spec_json = validate_strategy_spec(spec)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None
        strategy.spec_schema_version = int(strategy.spec_json["schema_version"])
        strategy.spec_hash = strategy_spec_hash(strategy.spec_json)
    strategy.version += 1
    strategy.updated_at = utcnow()
    _record_revision(db, strategy, source=source, summary=summary)


def _validated_code_spec(
    strategy: UserStrategy, spec_value: Mapping[str, Any]
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    if strategy.strategy_kind != "full_strategy" or strategy.engine_key != "strategy_dsl":
        raise HTTPException(
            status_code=409,
            detail="code editing is available only for full strategy DSL revisions",
        )
    try:
        spec = validate_strategy_spec(spec_value)
        if spec["strategy_type"] == "indicator_composite":
            indicator_keys = [str(item["key"]) for item in spec["indicators"]]
            parameter_schema = indicator_composite_parameter_schema(indicator_keys)
        else:
            parameter_schema = full_strategy_parameter_schema()
    except (KeyError, TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    return spec, parameter_schema


def _apply_code_edit(
    db: Session,
    strategy: UserStrategy,
    *,
    expected_version: int,
    name: str,
    description: str,
    category: str,
    spec_value: Mapping[str, Any],
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
    spec, parameter_schema = _validated_code_spec(strategy, spec_value)
    strategy.name = name
    strategy.description = description
    strategy.category = category
    strategy.spec_json = spec
    strategy.spec_schema_version = int(spec["schema_version"])
    strategy.spec_hash = strategy_spec_hash(spec)
    strategy.parameter_schema_json = parameter_schema
    strategy.parameters_json = copy.deepcopy(spec["parameters"])
    strategy.version += 1
    strategy.updated_at = utcnow()
    _record_revision(db, strategy, source=source, summary=summary)


def _code_validation_response(spec: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "valid": True,
        "legacy": False,
        "strategy_type": spec["strategy_type"],
        "schema_version": spec["schema_version"],
        "spec_hash": strategy_spec_hash(spec),
        "normalized_spec": copy.deepcopy(dict(spec)),
        "data_requirements": {
            "market": spec["market"],
            "timeframes": copy.deepcopy(spec["timeframes"]),
            "closed_bar_only": True,
        },
        "warnings": [],
    }


def _strategy_ai_http_error(exc: StrategyAiError) -> HTTPException:
    status_by_category = {
        "not_configured": 503,
        "timeout": 504,
        "upstream": 502,
        "invalid_output": 502,
    }
    return HTTPException(
        status_code=status_by_category[exc.category],
        detail={"message": "AI strategy preview failed", "category": exc.category},
    )


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


@router.get("/indicators/catalog")
def list_indicator_catalog(_: User = Depends(get_current_user)) -> dict[str, Any]:
    return {
        "items": copy.deepcopy(list(INDICATOR_CATALOG)),
        "engine": "strategy_runtime_v1",
        "defaults": {
            "timeframe": "1h",
            "directions": ["long", "short"],
            "confirmation_threshold": 60,
            "signal_valid_bars": 2,
            "risk_defaults": copy.deepcopy(DEFAULT_RISK),
        },
    }


@router.post("/compose/ai-preview")
def preview_indicator_composition(
    payload: StrategyAiPreviewRequest,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict[str, Any]:
    settings = request.app.state.settings
    user_model = get_global_ai_model_config(db, legacy_fallback_user_id=user.id)
    user_model_runtime: tuple[str, str, str] | None = None
    if user_model is not None:
        try:
            api_key = CredentialCipher(
                settings.credential_master_key.get_secret_value()
            ).decrypt(user_model.api_key_encrypted)
        except SecurityError:
            db.rollback()
            raise HTTPException(
                status_code=503,
                detail={"message": "AI strategy preview failed", "category": "not_configured"},
            ) from None
        user_model_runtime = (user_model.provider_code, user_model.model_name, api_key)
    db.rollback()
    model_prompt = _indicator_model_prompt(payload.prompt)
    snapshot = _indicator_ai_snapshot()
    safety_identifier = (
        "qd_"
        + hmac.new(
            settings.jwt_secret.get_secret_value().encode("utf-8"),
            f"strategy-compose-user:{user.id}".encode(),
            hashlib.sha256,
        ).hexdigest()[:32]
    )
    try:
        if user_model_runtime is not None:
            provider_code, model_name, api_key = user_model_runtime
            preview = generate_user_model_strategy_preview(
                snapshot,
                model_prompt,
                provider_code=provider_code,
                api_key=api_key,
                model_name=model_name,
                timeout_seconds=settings.openai_strategy_timeout_seconds,
            )
        else:
            preview = generate_strategy_preview(
                snapshot,
                model_prompt,
                settings.openai_api_key.get_secret_value(),
                settings.openai_strategy_model,
                settings.openai_strategy_timeout_seconds,
                safety_identifier,
            )
        proposed = preview.get("proposed")
        if not isinstance(proposed, Mapping):
            raise StrategyAiError("invalid_output")
        draft = _indicator_draft_from_proposed(
            proposed,
            prompt=payload.prompt,
            local=preview.get("provider") == "local_semantic",
        )
        # Run the same executable DSL validator used by save, backtest and paper.
        build_indicator_composite_spec(
            draft["indicators"],
            timeframe=draft["timeframe"],
            directions=draft["directions"],
            confirmation_threshold=draft["confirmation_threshold"],
            signal_valid_bars=draft["signal_valid_bars"],
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
    except ValueError as exc:
        raise HTTPException(status_code=502, detail=f"AI 指标方案校验失败：{exc}") from None
    indicator_names = [INDICATOR_BY_KEY[item["key"]]["name"] for item in draft["indicators"]]
    return {
        "provider": preview["provider"],
        "summary": preview.get("summary") or "AI 已生成受约束的指标组合。",
        "changes": [
            {"path": "indicators", "before": "未选择", "after": " + ".join(indicator_names)},
            {"path": "timeframe", "before": "1h", "after": draft["timeframe"]},
            {
                "path": "confirmation_threshold",
                "before": 60,
                "after": draft["confirmation_threshold"],
            },
        ],
        "draft": draft,
    }


@router.get("/deployments")
def list_strategy_deployments(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict[str, Any]:
    rows = db.scalars(
        select(StrategyDeployment)
        .where(StrategyDeployment.user_id == user.id)
        .order_by(StrategyDeployment.created_at.desc(), StrategyDeployment.id.desc())
    ).all()
    return {
        "items": [
            {
                "id": row.public_id,
                "name": row.name,
                "mode": row.mode,
                "status": row.status,
                "strategy_id": row.strategy_id,
                "strategy_revision_id": row.strategy_revision_id,
                "target_account_id": row.target_account_id,
                "last_evaluated_bar_time": row.last_evaluated_bar_time,
                "last_error_code": row.last_error_code,
                "started_at": row.started_at.isoformat() + "Z" if row.started_at else None,
                "created_at": row.created_at.isoformat() + "Z",
                "updated_at": row.updated_at.isoformat() + "Z",
            }
            for row in rows
        ]
    }


@router.get("/signals")
def list_strategy_signals(
    limit: int = 100,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict[str, Any]:
    bounded_limit = min(max(int(limit), 1), 500)
    rows = db.scalars(
        select(StrategySignal)
        .where(StrategySignal.user_id == user.id)
        .order_by(StrategySignal.created_at.desc(), StrategySignal.id.desc())
        .limit(bounded_limit)
    ).all()
    return {
        "items": [
            {
                "id": row.public_id,
                "deployment_id": row.deployment_id,
                "strategy_revision_id": row.strategy_revision_id,
                "symbol": row.symbol,
                "timeframe": row.timeframe,
                "signal_bar_time": row.signal_bar_time,
                "decision": row.decision,
                "confidence": float(row.confidence) if row.confidence is not None else None,
                "status": row.status,
                "reason_codes": row.reason_codes_json,
                "evidence": row.evidence_json,
                "risk_decision": row.risk_decision_json,
                "created_at": row.created_at.isoformat() + "Z",
            }
            for row in rows
        ]
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


@router.get("/{public_id}/revisions")
def list_strategy_revisions(
    public_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict[str, Any]:
    strategy = get_user_strategy(db, user.id, public_id)
    if strategy is None:
        raise HTTPException(status_code=404, detail="strategy not found")
    rows = db.scalars(
        select(StrategyRevision)
        .where(
            StrategyRevision.user_id == user.id,
            StrategyRevision.user_strategy_id == strategy.id,
        )
        .order_by(StrategyRevision.version.desc())
    ).all()
    return {
        "items": [
            {
                "version": row.version,
                "change_source": row.change_source,
                "change_summary": row.change_summary,
                "spec_schema_version": row.spec_schema_version,
                "spec_hash": row.spec_hash,
                "validation": row.validation_json,
                "published_at": row.published_at.isoformat() + "Z" if row.published_at else None,
                "created_at": row.created_at.isoformat() + "Z",
            }
            for row in rows
        ]
    }


@router.post("/{public_id}/validate")
def validate_full_strategy(
    public_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict[str, Any]:
    strategy = get_user_strategy(db, user.id, public_id)
    if strategy is None:
        raise HTTPException(status_code=404, detail="strategy not found")
    if strategy.strategy_kind != "full_strategy" or not strategy.spec_json:
        return {
            "valid": True,
            "legacy": True,
            "warnings": ["这是旧版指标信号，只保留兼容运行；建议迁移到完整策略。"],
        }
    try:
        spec = validate_strategy_spec(strategy.spec_json)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    return _code_validation_response(spec)


@router.post("/{public_id}/code/validate")
def validate_strategy_code(
    public_id: str,
    payload: StrategyCodeValidateRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict[str, Any]:
    strategy = get_user_strategy(db, user.id, public_id)
    if strategy is None or strategy.status != "active":
        raise HTTPException(status_code=404, detail="strategy not found")
    spec, _ = _validated_code_spec(strategy, payload.spec)
    return _code_validation_response(spec)


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

    now = utcnow()
    if payload.indicators is not None:
        try:
            risks = _normalize_risk_defaults(payload.risk_defaults, base=DEFAULT_RISK)
            strategy_spec, parameter_schema, parameters = build_indicator_composite_spec(
                [item.model_dump() for item in payload.indicators],
                timeframe=payload.timeframe,
                directions=payload.directions,
                confirmation_threshold=payload.confirmation_threshold,
                signal_valid_bars=payload.signal_valid_bars,
            )
            strategy_spec["risk"]["max_leverage"] = int(risks["leverage"])
            strategy_spec["exit"]["max_holding_bars"] = max(
                1, int(risks["max_holding_bars"])
            )
            stop_loss = float(risks["stop_loss_pct"])
            take_profit = float(risks["take_profit_pct"])
            if stop_loss > 0 and take_profit > 0:
                strategy_spec["exit"]["take_profit_r"] = min(
                    20.0, max(0.1, take_profit / stop_loss)
                )
            strategy_spec = validate_strategy_spec(strategy_spec)
        except (StrategyParameterError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None
        engine_key = "strategy_dsl"
        strategy_kind = "full_strategy"
    else:
        template_key = payload.template_key or "trend_pullback_continuation_v1"
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
                payload.parameters
                if payload.parameters is not None
                else template.parameters_json,
            )
            risks = _normalize_risk_defaults(
                payload.risk_defaults,
                base=template.risk_defaults_json,
            )
        except (StrategyParameterError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None
        strategy_spec = copy.deepcopy(template.spec_json)
        if strategy_spec is not None:
            strategy_spec["parameters"] = copy.deepcopy(parameters)
            try:
                strategy_spec = validate_strategy_spec(strategy_spec)
            except ValueError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from None
        parameter_schema = copy.deepcopy(template.parameter_schema_json)
        engine_key = template.engine_key
        strategy_kind = (
            "full_strategy" if template.template_kind == "strategy" else "legacy_signal"
        )
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
        engine_key=engine_key,
        strategy_kind=strategy_kind,
        lifecycle_status="published",
        spec_schema_version=(int(strategy_spec["schema_version"]) if strategy_spec else None),
        spec_json=strategy_spec,
        spec_hash=strategy_spec_hash(strategy_spec) if strategy_spec else None,
        risk_level="medium",
        parameter_schema_json=parameter_schema,
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


@router.put("/{public_id}/code")
def update_strategy_code(
    public_id: str,
    payload: StrategyCodeUpdateRequest,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict[str, Any]:
    strategy = _locked_user_strategy(db, user.id, public_id)
    if strategy is None:
        raise HTTPException(status_code=404, detail="strategy not found")
    _apply_code_edit(
        db,
        strategy,
        expected_version=payload.version,
        name=payload.name,
        description=payload.description,
        category=payload.category,
        spec_value=payload.spec,
        source="manual",
        summary="手工修改完整策略代码",
    )
    _audit(
        db,
        request,
        "strategy.code_update",
        user.id,
        strategy.public_id,
        metadata={
            "version": strategy.version,
            "source": "manual",
            "spec_hash": strategy.spec_hash,
        },
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
    settings = request.app.state.settings
    user_model = get_global_ai_model_config(db, legacy_fallback_user_id=user.id)
    user_model_runtime: tuple[str, str, str] | None = None
    if user_model is not None:
        try:
            api_key = CredentialCipher(
                settings.credential_master_key.get_secret_value()
            ).decrypt(user_model.api_key_encrypted)
        except SecurityError:
            db.rollback()
            raise HTTPException(
                status_code=503,
                detail={
                    "message": "AI strategy preview failed",
                    "category": "not_configured",
                },
            ) from None
        user_model_runtime = (
            user_model.provider_code,
            user_model.model_name,
            api_key,
        )
    # Release the SQL connection before the external inference request. Applying
    # the preview always rechecks ownership and version in a separate transaction.
    db.rollback()
    safety_identifier = (
        "qd_"
        + hmac.new(
            settings.jwt_secret.get_secret_value().encode("utf-8"),
            f"strategy-user:{user.id}".encode(),
            hashlib.sha256,
        ).hexdigest()[:32]
    )
    try:
        if user_model_runtime is not None:
            provider_code, model_name, api_key = user_model_runtime
            return generate_user_model_strategy_preview(
                editable,
                payload.prompt,
                provider_code=provider_code,
                api_key=api_key,
                model_name=model_name,
                timeout_seconds=settings.openai_strategy_timeout_seconds,
            )
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


@router.post("/{public_id}/code/ai-preview")
def preview_ai_strategy_code_edit(
    public_id: str,
    payload: StrategyCodeAiPreviewRequest,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict[str, Any]:
    strategy = get_user_strategy(db, user.id, public_id)
    if strategy is None or strategy.status != "active":
        raise HTTPException(status_code=404, detail="strategy not found")
    if strategy.strategy_kind != "full_strategy" or not strategy.spec_json:
        raise HTTPException(
            status_code=409,
            detail="code editing is available only for full strategy DSL revisions",
        )
    input_spec, _ = _validated_code_spec(strategy, payload.spec)
    editable = strategy_snapshot(strategy)
    editable["spec"] = input_spec
    settings = request.app.state.settings
    user_model = get_global_ai_model_config(db, legacy_fallback_user_id=user.id)
    user_model_runtime: tuple[str, str, str] | None = None
    if user_model is not None:
        try:
            api_key = CredentialCipher(
                settings.credential_master_key.get_secret_value()
            ).decrypt(user_model.api_key_encrypted)
        except SecurityError:
            db.rollback()
            raise HTTPException(
                status_code=503,
                detail={
                    "message": "AI strategy preview failed",
                    "category": "not_configured",
                },
            ) from None
        user_model_runtime = (
            user_model.provider_code,
            user_model.model_name,
            api_key,
        )
    db.rollback()
    safety_identifier = (
        "qd_"
        + hmac.new(
            settings.jwt_secret.get_secret_value().encode("utf-8"),
            f"strategy-code-user:{user.id}".encode(),
            hashlib.sha256,
        ).hexdigest()[:32]
    )
    try:
        if user_model_runtime is not None:
            provider_code, model_name, api_key = user_model_runtime
            preview = generate_user_model_strategy_code_preview(
                editable,
                payload.prompt,
                provider_code=provider_code,
                api_key=api_key,
                model_name=model_name,
                timeout_seconds=settings.openai_strategy_timeout_seconds,
            )
        else:
            preview = generate_strategy_code_preview(
                editable,
                payload.prompt,
                settings.openai_api_key.get_secret_value(),
                settings.openai_strategy_model,
                settings.openai_strategy_timeout_seconds,
                safety_identifier,
            )
    except StrategyAiError as exc:
        raise _strategy_ai_http_error(exc) from None
    proposed = preview.get("proposed_spec")
    if not isinstance(proposed, Mapping):
        raise _strategy_ai_http_error(StrategyAiError("invalid_output"))
    spec, _ = _validated_code_spec(strategy, proposed)
    preview["proposed_spec"] = spec
    preview["strategy_code"] = json.dumps(spec, ensure_ascii=False, indent=2)
    preview["spec_hash"] = strategy_spec_hash(spec)
    return preview


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
        strategy.lifecycle_status = "retired"
        strategy.version += 1
        strategy.updated_at = utcnow()
        _record_revision(db, strategy, source="manual", summary="归档用户策略")
        _audit(db, request, "strategy.archive", user.id, strategy.public_id)
        _commit_or_conflict(db)
    return serialize_user_strategy(strategy)
