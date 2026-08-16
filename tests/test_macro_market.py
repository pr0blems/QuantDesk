from datetime import UTC, datetime

from quantdesk_v2.macro_market import (
    apply_market_adjustment,
    macro_event_calendar,
    opportunity_market_context,
    sector_key,
)


def _market_snapshot() -> dict:
    return {
        "available": True,
        "indices": [
            {"key": "NDX", "change_percent": -2.0},
            {"key": "SPX", "change_percent": -1.2},
        ],
        "market_rsi": 31.5,
        "vix": {"available": True, "value": 34.0},
        "breadth": {
            "available": True,
            "advancers": 100,
            "decliners": 220,
            "advance_decline_ratio": 0.45,
        },
        "sectors": [
            {"key": "TECH", "label": "科技", "change_percent": -2.4},
            {"key": "CRYPTO", "label": "加密关联", "change_percent": -3.1},
        ],
        "sentiment": {"direction": "bear", "label": "恐慌"},
        "events": {"risk_level": "critical"},
    }


def test_macro_event_calendar_marks_imminent_fomc_as_critical() -> None:
    calendar = macro_event_calendar(datetime(2026, 9, 16, 16, 0, tzinfo=UTC))

    assert calendar["risk_level"] == "critical"
    assert calendar["next_event"]["event_type"] == "FOMC"
    assert calendar["next_event"]["hours_until"] == 2.0


def test_long_signal_is_penalized_and_marked_divergent_in_fear_regime() -> None:
    context = opportunity_market_context(
        _market_snapshot(),
        direction="long",
        symbol="PLTR",
        sector="Technology",
    )

    assert context["resonance"] == "divergent"
    assert context["adjustment"] == -20
    assert apply_market_adjustment(82.0, context) == 62.0
    assert {factor["key"] for factor in context["factors"]} >= {
        "ndx_trend",
        "vix",
        "breadth",
        "sector",
        "macro_event",
    }


def test_short_signal_can_benefit_from_bear_regime_but_adjustment_is_capped() -> None:
    context = opportunity_market_context(
        _market_snapshot(),
        direction="short",
        symbol="MSTR",
    )

    assert context["sector_key"] == "CRYPTO"
    assert context["resonance"] == "resonant"
    assert context["adjustment"] == 10
    assert apply_market_adjustment(96.0, context) == 100.0


def test_sector_mapping_uses_symbol_and_company_profile() -> None:
    assert sector_key("MSTR") == "CRYPTO"
    assert sector_key("NVDA", industry="Semiconductors") == "SEMIS"
    assert sector_key("PLTR", sector="Technology") == "TECH"
    assert sector_key("KO", sector="Consumer Defensive") == "BROAD"
