from copy import deepcopy

from quantdesk_v2.macro_ai import _normalize_analysis, macro_analysis_fingerprint


def _snapshot() -> dict:
    return {
        "captured_at": "2026-08-19T10:00:00+00:00",
        "market_session": {
            "key": "regular",
            "allows_new_entries": True,
            "upstream_confirmed": True,
            "countdown_seconds": 1800,
            "countdown_target_at": "2026-08-19T16:00:00-04:00",
        },
        "entry_policy": {
            "state": "normal",
            "threshold_delta": 0,
            "long_position_multiplier": 1.0,
            "short_position_multiplier": 1.0,
            "reasons": [],
        },
        "treasury_curve": {"as_of": "2026-08-18", "nominal": []},
        "central_banks": {"as_of": "2026-08-18", "rows": []},
        "capital_retreat": {
            "confirmed": False,
            "met_count": 0,
            "required_count": 2,
            "checks": [{"key": "breadth", "available": True, "met": False, "detail": "A/D 1.01"}],
        },
        "sentiment": {"direction": "neutral", "score": 52.4},
        "vix": {"value": 18.1, "source_timestamp": 1, "change_percent": 0.2},
        "move": {"value": 91.3, "source_timestamp": 1, "change_percent": -0.1},
        "breadth": {"advancers": 101, "decliners": 99, "advance_decline_ratio": 1.02},
        "market_tide": {"bias": "neutral", "net_flow": 1200, "timestamp": 1},
        "events": {"risk_level": "normal"},
        "sector_impacts": [{"key": "TECH", "state": "neutral", "adjustment": 1.2}],
        "data_sources": [
            {
                "key": "treasury",
                "status": "healthy",
                "last_success_at": "2026-08-19T09:00:00+00:00",
                "next_refresh_at": "2026-08-19T15:00:00+00:00",
            }
        ],
    }


def test_macro_ai_fingerprint_ignores_clock_and_small_tick_noise() -> None:
    first = _snapshot()
    original = deepcopy(first)
    second = deepcopy(first)
    second["captured_at"] = "2026-08-19T10:00:05+00:00"
    second["market_session"]["countdown_seconds"] = 1795
    second["vix"]["value"] = 18.4
    second["breadth"].update({"advancers": 102, "decliners": 98, "advance_decline_ratio": 1.04})
    second["market_tide"].update({"net_flow": 1300, "timestamp": 2})
    second["data_sources"][0]["last_success_at"] = "2026-08-19T10:00:00+00:00"

    assert macro_analysis_fingerprint(first) == macro_analysis_fingerprint(second)
    assert first == original


def test_macro_ai_fingerprint_changes_when_admission_policy_changes() -> None:
    first = _snapshot()
    second = deepcopy(first)
    second["entry_policy"].update(
        {
            "state": "tightening",
            "threshold_delta": 5,
            "long_position_multiplier": 0.6,
        }
    )

    assert macro_analysis_fingerprint(first) != macro_analysis_fingerprint(second)


def test_macro_ai_normalization_enforces_safe_enums_and_limits() -> None:
    normalized = _normalize_analysis(
        {
            "headline": "利率压力",
            "regime": "invented",
            "confidence": 120,
            "sector_impacts": [
                {"sector": "科技", "direction": "down", "reason": "实际利率上升"}
            ],
        }
    )

    assert normalized["regime"] == "neutral"
    assert normalized["confidence"] == 100.0
    assert normalized["sector_impacts"][0]["direction"] == "neutral"


def test_macro_ai_normalization_accepts_object_shaped_model_lists() -> None:
    normalized = _normalize_analysis(
        {
            "sector_impacts": {
                "科技": {"direction": "negative", "reason": "实际利率上升"}
            },
            "trading_constraints": {"threshold": "提高 5 分", "position": "降至 60%"},
            "risks": "长端利率继续上行",
        }
    )

    assert normalized["sector_impacts"] == [
        {"sector": "科技", "direction": "negative", "reason": "实际利率上升"}
    ]
    assert normalized["trading_constraints"] == ["threshold：提高 5 分", "position：降至 60%"]
    assert normalized["risks"] == ["长端利率继续上行"]


def test_macro_ai_normalization_accepts_fractional_confidence() -> None:
    assert _normalize_analysis({"confidence": 0.72})["confidence"] == 72.0
