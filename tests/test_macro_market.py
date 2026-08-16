from datetime import UTC, datetime

from quantdesk_v2.macro_market import (
    apply_market_adjustment,
    macro_event_calendar,
    opportunity_market_context,
    sector_key,
    us_market_session,
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


def test_us_market_session_distinguishes_extended_and_regular_hours() -> None:
    premarket = us_market_session(datetime(2026, 8, 17, 12, 0, tzinfo=UTC))
    regular = us_market_session(
        datetime(2026, 8, 17, 15, 0, tzinfo=UTC),
        upstream_sessions=("regular",),
    )
    postmarket = us_market_session(datetime(2026, 8, 17, 21, 0, tzinfo=UTC))

    assert premarket["key"] == "premarket"
    assert premarket["allows_new_entries"] is False
    assert premarket["countdown_label"] == "距正式开盘"
    assert premarket["countdown_target_at"].startswith("2026-08-17T09:30:00")
    assert regular["key"] == "regular"
    assert regular["upstream_confirmed"] is True
    assert regular["allows_new_entries"] is True
    assert regular["countdown_label"] == "距常规收盘"
    assert regular["countdown_target_at"].startswith("2026-08-17T16:00:00")
    assert postmarket["key"] == "postmarket"


def test_market_session_countdown_skips_weekend_and_nyse_holiday() -> None:
    weekend = us_market_session(datetime(2026, 8, 16, 12, 0, tzinfo=UTC))
    independence_observed = us_market_session(datetime(2026, 7, 3, 14, 0, tzinfo=UTC))

    assert weekend["key"] == "closed"
    assert weekend["next_open_at"].startswith("2026-08-17T09:30:00")
    assert independence_observed["key"] == "closed"
    assert independence_observed["next_open_at"].startswith("2026-07-06T09:30:00")


def test_closed_session_applies_a_transparent_liquidity_discount() -> None:
    snapshot = {**_market_snapshot(), "market_session": {"key": "closed"}}

    context = opportunity_market_context(
        snapshot,
        direction="short",
        symbol="MSTR",
    )

    assert any(item["key"] == "market_session" for item in context["factors"])
