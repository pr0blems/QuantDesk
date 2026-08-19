"""Auditable AI interpretation for deterministic macro admission controls."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from threading import Lock
from typing import Any

from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from .ai_model_config import get_global_ai_model_config
from .models import AdminSetting
from .security import CredentialCipher, SecurityError
from .strategy_ai import (
    StrategyAiError,
    _chat_http_transport,
    _chat_output_text,
    _strict_json_bytes,
    _strict_json_text,
    _validate_chat_configuration,
)

MACRO_AI_SETTING_KEY = "macro_ai_analysis_v1"
MACRO_AI_MIN_REFRESH_SECONDS = 15 * 60
MACRO_AI_MAX_AGE_HOURS = 6
_ANALYSIS_LOCK = Lock()


def _compact_macro_input(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    curve = snapshot.get("treasury_curve") or {}
    banks = snapshot.get("central_banks") or {}
    retreat = snapshot.get("capital_retreat") or {}
    policy = snapshot.get("entry_policy") or {}
    events = snapshot.get("events") or {}
    return {
        "captured_at": snapshot.get("captured_at"),
        "market_session": snapshot.get("market_session"),
        "entry_policy": {
            "state": policy.get("state"),
            "label": policy.get("label"),
            "threshold_delta": policy.get("threshold_delta"),
            "long_position_multiplier": policy.get("long_position_multiplier"),
            "short_position_multiplier": policy.get("short_position_multiplier"),
            "pause_new_trend_longs": policy.get("pause_new_trend_longs"),
            "reasons": policy.get("reasons") or [],
        },
        "treasury_curve": {
            "as_of": curve.get("as_of"),
            "source": curve.get("source"),
            "stale": bool(curve.get("stale")),
            "nominal": curve.get("nominal") or [],
            "real_10y": curve.get("real_10y") or {},
            "breakeven_10y": curve.get("breakeven_10y") or {},
            "curves": curve.get("curves") or [],
            "shock": curve.get("shock") or {},
        },
        "central_banks": {
            "as_of": banks.get("as_of"),
            "source": banks.get("source"),
            "stale": bool(banks.get("stale")),
            "rows": banks.get("rows") or [],
            "spreads": banks.get("spreads") or [],
        },
        "capital_retreat": {
            "confirmed": bool(retreat.get("confirmed")),
            "met_count": retreat.get("met_count"),
            "required_count": retreat.get("required_count"),
            "checks": retreat.get("checks") or [],
        },
        "market": {
            "sentiment": snapshot.get("sentiment") or {},
            "vix": snapshot.get("vix") or {},
            "move": snapshot.get("move") or {},
            "breadth": snapshot.get("breadth") or {},
            "market_tide": snapshot.get("market_tide") or {},
        },
        "events": {
            "risk_level": events.get("risk_level"),
            "next_event": events.get("next_event"),
        },
        "sector_impacts": snapshot.get("sector_impacts") or [],
        "data_sources": snapshot.get("data_sources") or [],
        "execution_price_rule": "Binance mapped contract is always the execution and settlement price",
    }


def _semantic_fingerprint(snapshot: Mapping[str, Any]) -> str:
    payload = deepcopy(_compact_macro_input(snapshot))
    # Fast-changing timestamps and exact tick values must not cause an AI call
    # every few seconds. The deterministic gate still reacts immediately.
    payload.pop("captured_at", None)
    session = payload.get("market_session") or {}
    # Session identity is material; the live countdown and timestamps are not.
    payload["market_session"] = {
        "key": session.get("key"),
        "allows_new_entries": session.get("allows_new_entries"),
        "upstream_confirmed": session.get("upstream_confirmed"),
    }
    retreat = payload.get("capital_retreat") or {}
    retreat["checks"] = [
        {
            "key": item.get("key"),
            "available": item.get("available"),
            "met": item.get("met"),
        }
        for item in (retreat.get("checks") or [])
        if isinstance(item, Mapping)
    ]
    market = payload.get("market") or {}
    for key in ("vix", "move"):
        value = market.get(key) or {}
        numeric = value.get("value")
        try:
            value["value_bucket"] = round(float(numeric) / 2) * 2
        except (TypeError, ValueError, OverflowError):
            value["value_bucket"] = None
        for volatile in ("value", "source_timestamp", "change_percent"):
            value.pop(volatile, None)
    breadth = market.get("breadth") or {}
    try:
        breadth["ratio_bucket"] = round(float(breadth.get("advance_decline_ratio")), 1)
    except (TypeError, ValueError, OverflowError):
        breadth["ratio_bucket"] = None
    for volatile in ("advancers", "decliners", "unchanged", "total", "advance_decline_ratio"):
        breadth.pop(volatile, None)
    market_tide = market.get("market_tide") or {}
    for volatile in (
        "timestamp",
        "time",
        "raw",
        "points",
        "history",
        "directional_value",
        "net_volume",
        "net_flow",
        "signed_flow",
    ):
        market_tide.pop(volatile, None)
    payload["sector_impacts"] = [
        {
            "key": item.get("key"),
            "state": item.get("state"),
            "adjustment_bucket": (
                round(float(item.get("adjustment") or 0) / 5) * 5
            ),
        }
        for item in (payload.get("sector_impacts") or [])
        if isinstance(item, Mapping)
    ]
    sources = payload.get("data_sources") or []
    for source in sources:
        if isinstance(source, dict):
            source.pop("last_success_at", None)
            source.pop("next_refresh_at", None)
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def macro_analysis_fingerprint(snapshot: Mapping[str, Any]) -> str:
    return _semantic_fingerprint(snapshot)


def _parse_datetime(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def macro_analysis_state(db: Session, snapshot: Mapping[str, Any]) -> dict[str, Any]:
    setting = db.get(AdminSetting, MACRO_AI_SETTING_KEY)
    stored = dict(setting.value_json) if setting is not None and isinstance(setting.value_json, Mapping) else {}
    fingerprint = _semantic_fingerprint(snapshot)
    generated_at = _parse_datetime(stored.get("generated_at"))
    expired = generated_at is None or datetime.now(UTC) - generated_at >= timedelta(hours=MACRO_AI_MAX_AGE_HOURS)
    needs_refresh = stored.get("fingerprint") != fingerprint or expired
    return {
        "available": bool(stored.get("analysis")),
        "configured": get_global_ai_model_config(db) is not None,
        "needs_refresh": needs_refresh,
        "fingerprint": fingerprint,
        "analysis": stored.get("analysis"),
        "generated_at": stored.get("generated_at"),
        "trigger_reason": stored.get("trigger_reason"),
        "model": stored.get("model"),
        "provider": stored.get("provider"),
        "input_as_of": stored.get("input_as_of"),
        "last_attempt_at": stored.get("last_attempt_at"),
        "last_error": stored.get("last_error"),
        "advisory_only": True,
    }


def _normalize_analysis(raw: Mapping[str, Any]) -> dict[str, Any]:
    def text_list(value: Any, *, limit: int = 8) -> list[str]:
        if isinstance(value, Mapping):
            items = [f"{key}：{item}" for key, item in value.items()]
        elif isinstance(value, (list, tuple, set)):
            items = list(value)
        elif value in (None, ""):
            items = []
        else:
            items = [value]
        return [str(item)[:220] for item in items[:limit]]

    regime = str(raw.get("regime") or "neutral")[:40]
    if regime not in {"supportive", "neutral", "tightening", "shock", "event_risk", "insufficient_data"}:
        regime = "neutral"
    try:
        confidence = max(0.0, min(100.0, float(raw.get("confidence") or 0)))
    except (TypeError, ValueError, OverflowError):
        confidence = 0.0
    if 0 < confidence <= 1:
        confidence *= 100
    sector_impacts: list[dict[str, str]] = []
    raw_sector_impacts = raw.get("sector_impacts") or []
    if isinstance(raw_sector_impacts, Mapping):
        raw_sector_impacts = [
            {"sector": key, **(dict(value) if isinstance(value, Mapping) else {"reason": value})}
            for key, value in raw_sector_impacts.items()
        ]
    for item in raw_sector_impacts:
        if not isinstance(item, Mapping):
            continue
        direction = str(item.get("direction") or "neutral")
        if direction not in {"positive", "negative", "neutral"}:
            direction = "neutral"
        sector_impacts.append(
            {
                "sector": str(item.get("sector") or "其他")[:40],
                "direction": direction,
                "reason": str(item.get("reason") or "")[:240],
            }
        )
    return {
        "headline": str(raw.get("headline") or "宏观环境待进一步确认")[:100],
        "regime": regime,
        "confidence": round(confidence, 1),
        "summary": str(raw.get("summary") or "")[:800],
        "rate_analysis": str(raw.get("rate_analysis") or "")[:500],
        "central_bank_analysis": str(raw.get("central_bank_analysis") or "")[:500],
        "liquidity_analysis": str(raw.get("liquidity_analysis") or "")[:500],
        "sector_impacts": sector_impacts[:8],
        "trading_constraints": text_list(raw.get("trading_constraints")),
        "risks": text_list(raw.get("risks")),
        "data_limitations": text_list(raw.get("data_limitations")),
    }


def _call_model(
    provider_code: str,
    model_name: str,
    api_key: str,
    snapshot: Mapping[str, Any],
) -> dict[str, Any]:
    provider, endpoint = _validate_chat_configuration(
        provider_code=provider_code,
        api_key=api_key,
        model_name=model_name,
        timeout_seconds=45.0,
    )
    request_payload: dict[str, Any] = {
        "model": model_name,
        "messages": [
            {
                "role": "system",
                "content": (
                    "你是 QuantDesk 的宏观风控解释器。只根据给出的结构化快照输出审计友好的结论，"
                    "不得补造缺失数据，不得声称保证盈利，不得改变确定性的准入规则。"
                    "Binance 映射合约始终是交易和结算主价格。输出一个 JSON 对象，字段必须为："
                    "headline, regime, confidence, summary, rate_analysis, central_bank_analysis, "
                    "liquidity_analysis, sector_impacts, trading_constraints, risks, data_limitations。"
                    "regime 仅可为 supportive/neutral/tightening/shock/event_risk/insufficient_data；"
                    "sector_impacts 每项含 sector,direction,reason，direction 仅可为 positive/negative/neutral。"
                    "只给结论与可核验依据，不输出隐藏思维链。"
                ),
            },
            {
                "role": "user",
                "content": json.dumps(
                    _compact_macro_input(snapshot),
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            },
        ],
        "stream": False,
        "response_format": {"type": "json_object"},
        "temperature": 0.1,
    }
    if provider == "deepseek":
        request_payload["thinking"] = {"type": "disabled"}
        request_payload["max_tokens"] = 1_600
    else:
        request_payload["max_completion_tokens"] = 1_600
    body = json.dumps(request_payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    status, response_body = _chat_http_transport(
        endpoint,
        body,
        {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        45.0,
    )
    if status in {401, 403}:
        raise StrategyAiError("not_configured")
    if status in {408, 504}:
        raise StrategyAiError("timeout")
    if not 200 <= status < 300:
        raise StrategyAiError("upstream")
    payload = _strict_json_bytes(response_body)
    parsed = _strict_json_text(_chat_output_text(payload))
    if not isinstance(parsed, Mapping):
        raise StrategyAiError("invalid_output")
    return _normalize_analysis(parsed)


def refresh_macro_analysis(
    engine: Engine,
    master_key: str,
    snapshot: Mapping[str, Any],
    *,
    force: bool = False,
) -> dict[str, Any]:
    """Refresh one global macro analysis, deduplicated across request threads."""

    if not _ANALYSIS_LOCK.acquire(blocking=False):
        session_factory = sessionmaker(bind=engine, expire_on_commit=False)
        with session_factory() as db:
            state = macro_analysis_state(db, snapshot)
            state["refreshing"] = True
            return state
    try:
        session_factory = sessionmaker(bind=engine, expire_on_commit=False)
        with session_factory() as db:
            state = macro_analysis_state(db, snapshot)
            previous_attempt = _parse_datetime(state.get("last_attempt_at"))
            cooldown = previous_attempt is not None and datetime.now(UTC) - previous_attempt < timedelta(seconds=MACRO_AI_MIN_REFRESH_SECONDS)
            if not force and (not state["needs_refresh"] or cooldown):
                return state
            model = get_global_ai_model_config(db)
            if model is None:
                state["last_error"] = "DeepSeek 全局模型尚未配置"
                return state
            setting = db.get(AdminSetting, MACRO_AI_SETTING_KEY)
            stored = dict(setting.value_json) if setting is not None and isinstance(setting.value_json, Mapping) else {}
            attempt_at = datetime.now(UTC).isoformat()
            stored["last_attempt_at"] = attempt_at
            stored["last_error"] = None
            if setting is None:
                setting = AdminSetting(key=MACRO_AI_SETTING_KEY, value_json=stored, version=1, updated_by=model.user_id)
                db.add(setting)
            else:
                setting.value_json = stored
                setting.version = int(setting.version or 0) + 1
                setting.updated_by = model.user_id
            db.commit()
            try:
                api_key = CredentialCipher(master_key).decrypt(model.api_key_encrypted)
                analysis = _call_model(model.provider_code, model.model_name, api_key, snapshot)
            except (SecurityError, StrategyAiError, OSError, TimeoutError, ValueError) as exc:
                setting = db.get(AdminSetting, MACRO_AI_SETTING_KEY)
                failed = dict(setting.value_json) if setting is not None else stored
                failed["last_attempt_at"] = attempt_at
                failed["last_error"] = getattr(exc, "category", None) or type(exc).__name__
                if setting is not None:
                    setting.value_json = failed
                    setting.version = int(setting.version or 0) + 1
                    db.commit()
                return macro_analysis_state(db, snapshot)
            generated_at = datetime.now(UTC).isoformat()
            trigger_reason = "手动刷新" if force else "宏观快照发生实质变化或分析超过 6 小时"
            value = {
                "fingerprint": state["fingerprint"],
                "analysis": analysis,
                "generated_at": generated_at,
                "last_attempt_at": generated_at,
                "last_error": None,
                "trigger_reason": trigger_reason,
                "model": model.model_name,
                "provider": model.provider_code,
                "input_as_of": snapshot.get("captured_at"),
            }
            setting = db.get(AdminSetting, MACRO_AI_SETTING_KEY)
            if setting is None:
                setting = AdminSetting(key=MACRO_AI_SETTING_KEY, value_json=value, version=1, updated_by=model.user_id)
                db.add(setting)
            else:
                setting.value_json = value
                setting.version = int(setting.version or 0) + 1
                setting.updated_by = model.user_id
            db.commit()
            return macro_analysis_state(db, snapshot)
    finally:
        _ANALYSIS_LOCK.release()
