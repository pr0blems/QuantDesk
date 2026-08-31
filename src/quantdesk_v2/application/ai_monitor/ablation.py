"""Stable application port for frozen market-signal ablation classification."""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any

_QUOTE_HALT_CHECK_CODES = {
    "price_available": "QUOTE_PRICE_MISSING",
    "quote_fresh": "REFERENCE_QUOTE_STALE",
    "spread_acceptable": "REFERENCE_SPREAD_TOO_WIDE",
    "quote_sane": "REFERENCE_QUOTE_INVALID",
    "not_halted": "SYMBOL_HALTED_OR_COOLDOWN",
}


def _finite_number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _ablation_domain(item: Mapping[str, Any], key: str) -> dict[str, Any]:
    flow = dict(item.get("flow") or {})
    domains = dict(flow.get("domains") or {})
    source = dict(domains.get(key) or flow.get(key) or {})
    score_components = dict(item.get("score_components") or {})
    score = _finite_number(source.get("score"))
    if score is None:
        score = _finite_number(score_components.get(key))
    available = bool(source.get("available", score is not None)) and score is not None
    return {
        "available": available,
        "fresh": bool(source.get("fresh", available)),
        "score": score,
        "hard_conflict": bool(source.get("hard_conflict", False)),
    }


def classify_ablation_signal_state(item: Mapping[str, Any]) -> dict[str, Any]:
    """Classify one settled signal using immutable signal-time evidence only."""

    gate = dict(item.get("gate_summary") or {})
    checks = {
        str(key): bool(value) for key, value in dict(gate.get("checks") or {}).items()
    }
    quote = dict(item.get("quote") or {})
    quality = dict(item.get("data_quality") or {})
    quote_fields_available = (
        _finite_number(quote.get("quote_age_ms")) is not None
        and (
            (
                _finite_number(quote.get("bid")) is not None
                and _finite_number(quote.get("ask")) is not None
            )
            or _finite_number(quote.get("spread_bps")) is not None
        )
    )
    quote_available = bool(quality.get("quote_available", quote_fields_available))
    quote_check_keys = set(_QUOTE_HALT_CHECK_CODES)
    quote_halt_available = quote_available and quote_check_keys <= set(checks)
    quote_reasons = [
        code
        for key, code in _QUOTE_HALT_CHECK_CODES.items()
        if key in checks and not checks[key]
    ]
    quote_halt_selected = quote_halt_available and not quote_reasons

    option_flow = _ablation_domain(item, "option_flow")
    gex = _ablation_domain(item, "gex")
    institutional_flow = _ablation_domain(item, "institutional_flow")
    option_available = quote_halt_available and option_flow["available"]
    option_reasons = list(quote_reasons)
    if option_flow["available"] and option_flow["hard_conflict"]:
        option_reasons.append("OPTION_FLOW_DIRECTION_CONFLICT")
    option_selected = option_available and quote_halt_selected and not option_reasons

    event_check_available = "event_window_clear" in checks
    full_available = bool(
        option_available
        and gex["available"]
        and institutional_flow["available"]
        and event_check_available
    )
    full_reasons = [
        str(reason).strip().upper()
        for reason in gate.get("blocking_reasons", [])
        if str(reason).strip()
    ]
    if gex["available"] and gex["hard_conflict"]:
        full_reasons.append("GEX_DIRECTION_CONFLICT")
    if institutional_flow["available"] and institutional_flow["hard_conflict"]:
        full_reasons.append("INSTITUTIONAL_FLOW_DIRECTION_CONFLICT")
    full_selected = full_available and bool(gate.get("passed")) and not full_reasons

    return {
        "baseline": {"available": True, "selected": True, "reasons": []},
        "quote_halt": {
            "available": quote_halt_available,
            "selected": quote_halt_selected,
            "reasons": sorted(set(quote_reasons)),
        },
        "option_flow": {
            "available": option_available,
            "selected": option_selected,
            "reasons": sorted(set(option_reasons)),
        },
        "full": {
            "available": full_available,
            "selected": full_selected,
            "reasons": sorted(set(full_reasons)),
        },
        "modules": {
            "quote_halt": quote_halt_available,
            "option_flow": option_flow["available"],
            "gex": gex["available"],
            "institutional_flow": institutional_flow["available"],
            "event_window": event_check_available,
        },
    }
