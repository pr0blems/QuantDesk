from __future__ import annotations

from quantdesk_v2.application.ai_monitor import classify_ablation_signal_state


def _complete_signal() -> dict[str, object]:
    return {
        "quote": {
            "bid": 99.9,
            "ask": 100.1,
            "spread_bps": 20.0,
            "quote_age_ms": 250,
        },
        "data_quality": {"quote_available": True},
        "flow": {
            "domains": {
                "option_flow": {
                    "available": True,
                    "fresh": True,
                    "score": 75.0,
                    "hard_conflict": False,
                },
                "gex": {
                    "available": True,
                    "fresh": True,
                    "score": 68.0,
                    "hard_conflict": False,
                },
                "institutional_flow": {
                    "available": True,
                    "fresh": True,
                    "score": 62.0,
                    "hard_conflict": False,
                },
            }
        },
        "gate_summary": {
            "passed": True,
            "checks": {
                "price_available": True,
                "quote_fresh": True,
                "spread_acceptable": True,
                "quote_sane": True,
                "not_halted": True,
                "event_window_clear": True,
            },
            "blocking_reasons": [],
        },
    }


def test_complete_frozen_signal_selects_every_nested_ablation_variant() -> None:
    assert classify_ablation_signal_state(_complete_signal()) == {
        "baseline": {"available": True, "selected": True, "reasons": []},
        "quote_halt": {"available": True, "selected": True, "reasons": []},
        "option_flow": {"available": True, "selected": True, "reasons": []},
        "full": {"available": True, "selected": True, "reasons": []},
        "modules": {
            "quote_halt": True,
            "option_flow": True,
            "gex": True,
            "institutional_flow": True,
            "event_window": True,
        },
    }


def test_frozen_signal_preserves_conflict_and_quote_rejection_codes() -> None:
    item = _complete_signal()
    item["gate_summary"] = {
        "passed": False,
        "checks": {
            "price_available": True,
            "quote_fresh": False,
            "spread_acceptable": False,
            "quote_sane": True,
            "not_halted": True,
            "event_window_clear": True,
        },
        "blocking_reasons": [
            "reference_quote_stale",
            "reference_spread_too_wide",
        ],
    }
    domains = item["flow"]["domains"]  # type: ignore[index]
    domains["option_flow"]["hard_conflict"] = True  # type: ignore[index]
    domains["gex"]["hard_conflict"] = True  # type: ignore[index]
    domains["institutional_flow"]["hard_conflict"] = True  # type: ignore[index]

    result = classify_ablation_signal_state(item)

    assert result["quote_halt"] == {
        "available": True,
        "selected": False,
        "reasons": ["REFERENCE_QUOTE_STALE", "REFERENCE_SPREAD_TOO_WIDE"],
    }
    assert result["option_flow"] == {
        "available": True,
        "selected": False,
        "reasons": [
            "OPTION_FLOW_DIRECTION_CONFLICT",
            "REFERENCE_QUOTE_STALE",
            "REFERENCE_SPREAD_TOO_WIDE",
        ],
    }
    assert result["full"] == {
        "available": True,
        "selected": False,
        "reasons": [
            "GEX_DIRECTION_CONFLICT",
            "INSTITUTIONAL_FLOW_DIRECTION_CONFLICT",
            "REFERENCE_QUOTE_STALE",
            "REFERENCE_SPREAD_TOO_WIDE",
        ],
    }


def test_missing_frozen_domains_remain_unavailable_instead_of_neutral() -> None:
    result = classify_ablation_signal_state(
        {
            "quote": {},
            "data_quality": {},
            "flow": {},
            "score_components": {},
            "gate_summary": {},
        }
    )

    assert result["baseline"] == {
        "available": True,
        "selected": True,
        "reasons": [],
    }
    assert result["quote_halt"]["available"] is False
    assert result["option_flow"]["available"] is False
    assert result["full"]["available"] is False
    assert result["modules"] == {
        "quote_halt": False,
        "option_flow": False,
        "gex": False,
        "institutional_flow": False,
        "event_window": False,
    }
