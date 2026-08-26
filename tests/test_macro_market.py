from datetime import UTC, datetime, timedelta

from quantdesk_v2 import macro_market
from quantdesk_v2.macro_market import (
    MacroMarketService,
    _bis_policy_metric,
    apply_market_adjustment,
    capital_retreat_snapshot,
    global_central_bank_matrix,
    macro_entry_policy,
    macro_event_calendar,
    opportunity_market_context,
    sector_key,
    treasury_curve_snapshot,
    us_market_session,
)


class _CountingUnusualWhalesClient:
    def __init__(self) -> None:
        self.state_calls = 0
        self.tide_calls = 0

    def configured(self) -> bool:
        return True

    def stock_states(self, symbols):
        self.state_calls += 1
        return {str(symbol): {"available": True} for symbol in symbols}

    def market_tide(self):
        self.tide_calls += 1
        return {"available": True, "source": "unusual_whales_market_tide"}


def test_bis_policy_metric_derives_latest_rate_and_last_action() -> None:
    metric = _bis_policy_metric(
        [
            {"TIME_PERIOD": "2026-07-01", "OBS_VALUE": "3.50"},
            {"TIME_PERIOD": "2026-07-29", "OBS_VALUE": "3.75"},
            {"TIME_PERIOD": "2026-08-18", "OBS_VALUE": "3.75"},
        ],
        key="FED",
        label="美联储",
    )

    assert metric["available"] is True
    assert metric["policy_rate"] == "3.750%"
    assert metric["as_of"] == "2026-08-18"
    assert metric["last_action_bps"] == 25.0
    assert "上调" in metric["last_action"]


def test_central_bank_matrix_marks_partial_bis_coverage() -> None:
    matrix = global_central_bank_matrix(
        datetime(2026, 8, 19, tzinfo=UTC),
        {
            "FED": {
                "available": True,
                "policy_rate": "3.750%",
                "midpoint": 3.75,
                "last_action": "2026-07-29 上调至 3.750%",
                "as_of": "2026-08-18",
                "source_url": "https://data.bis.org/topics/CBPOL",
            }
        },
    )

    assert matrix["coverage"] == {"available": 1, "required": 3}
    assert matrix["rows"][0]["rate_status"] == "live"
    assert matrix["rows"][1]["rate_status"] == "fallback"


def test_unusual_whales_macro_snapshot_is_cached_for_five_minutes() -> None:
    client = _CountingUnusualWhalesClient()
    service = MacroMarketService(
        object(),  # type: ignore[arg-type]
        unusual_whales_client=client,  # type: ignore[arg-type]
        unusual_whales_cache_seconds=5 * 60,
    )

    first = service._unusual_whales_snapshot(["QQQ", "SPY"])
    second = service._unusual_whales_snapshot(["QQQ", "SPY"])

    assert first[2] is True
    assert second[2] is True
    assert client.state_calls == 1
    assert client.tide_calls == 1


def test_disabling_unusual_whales_stops_macro_api_calls() -> None:
    client = _CountingUnusualWhalesClient()
    service = MacroMarketService(
        object(),  # type: ignore[arg-type]
        unusual_whales_client=client,  # type: ignore[arg-type]
    )
    service.set_unusual_whales_enabled(False)

    states, tide, configured = service._unusual_whales_snapshot(["QQQ"])

    assert configured is False
    assert states == {}
    assert tide["source"] == "disabled"
    assert client.state_calls == 0
    assert client.tide_calls == 0


def test_closed_market_reuses_cache_without_calling_unusual_whales() -> None:
    client = _CountingUnusualWhalesClient()
    service = MacroMarketService(
        object(),  # type: ignore[arg-type]
        unusual_whales_client=client,  # type: ignore[arg-type]
    )

    states, tide, configured = service._unusual_whales_snapshot(
        ["QQQ"],
        allow_refresh=False,
    )

    assert configured is True
    assert states == {}
    assert tide["available"] is False
    assert client.state_calls == 0
    assert client.tide_calls == 0


def test_public_macro_proxy_fallback_is_cached_and_keeps_all_cards(monkeypatch) -> None:
    calls: list[str] = []

    def fake_quote(symbol: str, *, retries: int = 3):
        calls.append(symbol)
        assert retries == 1
        return {
            "price": 100.5,
            "previous_close": 100.0,
            "change_pct": 0.5,
            "day_open": 100.1,
            "day_high": 101.0,
            "day_low": 99.5,
            "market_time_ms": 1_787_745_600_000,
            "market_state": "pre_market",
            "status": "ok",
        }

    monkeypatch.setattr(macro_market.underlying_quotes, "fetch_quote", fake_quote)
    service = MacroMarketService(object())  # type: ignore[arg-type]

    first = service._public_proxy_snapshot(realtime_expected=True)
    second = service._public_proxy_snapshot(realtime_expected=True)

    assert set(first) == set(macro_market.MACRO_ASSET_KEYS)
    assert second == first
    assert len(calls) == len(macro_market.MACRO_ASSET_KEYS)
    assert first["US10Y"]["provider_symbol"] == "TLT"
    assert first["US10Y"]["price"] == 100.5
    assert first["US10Y"]["change_percent"] == 0.5
    assert first["US10Y"]["market_time"] == "premarket"
    assert first["US10Y"]["source"] == "yahoo_extended_hours_fallback"


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
    assert context["entry_policy"]["entry_allowed"] is True
    assert "MACRO_DIRECTION_DIVERGENT" not in context["entry_policy"]["blocked_reasons"]
    assert "MACRO_DIRECTION_DIVERGENT" in context["entry_policy"]["warnings"]
    assert context["entry_policy"]["directional_divergence"] is True
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
    assert context["entry_policy"]["entry_allowed"] is True
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


def test_closed_session_keeps_research_enabled_and_live_copy_gated() -> None:
    snapshot = {**_market_snapshot(), "market_session": {"key": "closed"}}

    context = opportunity_market_context(
        snapshot,
        direction="short",
        symbol="MSTR",
    )

    assert any(item["key"] == "market_session" for item in context["factors"])
    assert context["entry_policy"]["entry_allowed"] is True
    assert context["entry_policy"]["simulation_entry_allowed"] is True
    assert context["entry_policy"]["live_entry_allowed"] is False
    assert "NON_REGULAR_US_SESSION" not in context["entry_policy"]["blocked_reasons"]
    assert "NON_REGULAR_US_SESSION" in context["entry_policy"]["live_blocked_reasons"]
    assert "NON_REGULAR_US_SESSION" in context["entry_policy"]["warnings"]


def test_direct_treasury_curve_classifies_long_end_term_premium_pressure() -> None:
    nominal = []
    real = []
    for offset in range(30):
        day = datetime(2026, 8, 18, tzinfo=UTC).date() - timedelta(days=offset)
        nominal.append(
            {
                "Date": day.strftime("%m/%d/%Y"),
                "2 Yr": "4.00",
                "5 Yr": "4.30",
                "10 Yr": str(4.80 - min(offset, 5) * 0.02),
                "30 Yr": str(5.20 - min(offset, 5) * 0.03),
            }
        )
        real.append({"Date": day.strftime("%m/%d/%Y"), "10 Yr": "2.30"})

    curve = treasury_curve_snapshot(nominal, real)
    policy = macro_entry_policy(
        {
            "treasury_curve": curve,
            "capital_retreat": {},
            "events": {},
            "vix": {},
        }
    )

    assert curve["available"] is True
    assert curve["shock"]["regime"] == "term_premium_fiscal"
    assert curve["shock"]["changes_5d_bps"]["2Y"] == 0
    assert curve["shock"]["changes_5d_bps"]["10Y"] == 10
    assert policy["state"] == "tightening"
    assert policy["threshold_delta"] == 5
    assert policy["long_position_multiplier"] == 0.6


def test_capital_retreat_requires_two_available_independent_confirmations() -> None:
    snapshot = {
        "indices": [{"key": "SPX", "change_percent": -0.2}],
        "macro_assets": [
            {"key": "EQUAL_WEIGHT", "change_percent": -1.0},
            {"key": "HIGH_YIELD", "change_percent": -0.8},
            {"key": "DXY", "change_percent": 0.5},
        ],
        "breadth": {"advance_decline_ratio": 0.5},
        "market_structure": {
            "breadth_persistence": {
                "available": True,
                "relative_5d_percent": -0.9,
                "relative_20d_percent": -1.4,
            },
            "credit_dollar": {
                "available": True,
                "high_yield_5d_percent": -1.1,
                "dollar_5d_percent": 0.8,
            },
        },
        "vix": {"change_percent": 5.0},
        "move": {"change_percent": 3.0},
    }

    retreat = capital_retreat_snapshot(snapshot)

    assert retreat["confirmed"] is True
    assert retreat["met_count"] == 2
    assert retreat["available_count"] == 2
    assert retreat["checks"][0]["available"] is False
