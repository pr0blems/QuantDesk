from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import pytest

from quantdesk_v2.live_risk import (
    LiveRiskPolicy,
    OpenPositionRisk,
    account_loss_limits,
    atr_risk_position_size,
    closed_bar_signal_freshness,
    estimate_open_position_risk,
    leverage_for_stop_distance,
    liquidation_stop_safety,
    market_data_freshness,
    policy_from_config,
    portfolio_risk_admission,
    signal_freshness,
    symbol_admission,
    symbol_risk_profile,
    tighten_policy_with_strategy,
    total_open_risk,
)


def test_policy_normalizes_existing_margin_setting_without_mutating_input() -> None:
    config = {
        "position_size_pct": 1,
        "risk_per_trade_pct": 99,
        "risk_max_leverage": 50,
        "short_risk_multiplier": "bad",
    }

    policy = policy_from_config(config)

    assert policy.max_margin_per_trade_pct == Decimal("1")
    assert policy.risk_per_trade_pct == Decimal("1.00")
    assert policy.max_leverage == 20
    assert policy.short_risk_multiplier == Decimal("0.50")
    assert config["risk_per_trade_pct"] == 99


def test_policy_preserves_configured_margin_ceiling_up_to_api_limit() -> None:
    policy = policy_from_config({"position_size_pct": 10})

    assert policy.max_margin_per_trade_pct == Decimal("10")


def test_policy_safe_defaults_match_live_api_contract() -> None:
    policy = policy_from_config({})

    assert policy.risk_per_trade_pct == Decimal("0.50")
    assert policy.max_total_risk_pct == Decimal("4.00")
    assert policy.max_cluster_positions == 2
    assert policy.max_ticker_age_seconds == 120
    assert policy.max_signal_age_seconds == 18_000
    assert policy.liquidation_buffer_pct == Decimal("1.50")
    assert policy.block_high_risk_products is True


def test_live_policy_enforces_conservative_round_trip_cost_floor() -> None:
    assert policy_from_config({"round_trip_cost_bps": 0}).round_trip_cost_bps == Decimal(
        "16"
    )


def test_strategy_risk_proposal_can_only_tighten_account_policy() -> None:
    policy = LiveRiskPolicy(
        risk_per_trade_pct=Decimal("0.5"),
        max_margin_per_trade_pct=Decimal("2"),
        max_leverage=10,
    )

    tightened = tighten_policy_with_strategy(
        policy,
        {"risk_per_trade_pct": 0.25, "max_margin_pct": 5, "max_leverage": 4},
    )
    attempted_expansion = tighten_policy_with_strategy(
        policy,
        {"risk_per_trade_pct": 2, "max_margin_pct": 20, "max_leverage": 20},
    )

    assert tightened.risk_per_trade_pct == Decimal("0.25")
    assert tightened.max_margin_per_trade_pct == Decimal("2")
    assert tightened.max_leverage == 4
    assert attempted_expansion == policy


def test_incomplete_strategy_risk_proposal_is_rejected() -> None:
    with pytest.raises(ValueError, match="incomplete"):
        tighten_policy_with_strategy(LiveRiskPolicy(), {"max_leverage": 3})


def test_atr_sizing_equalizes_loss_for_different_stop_distances() -> None:
    policy = replace(
        LiveRiskPolicy(),
        short_risk_multiplier=Decimal("1"),
        round_trip_cost_bps=Decimal("0"),
        max_margin_per_trade_pct=Decimal("5"),
    )
    narrow = atr_risk_position_size(
        equity=10_000,
        available_balance=10_000,
        entry_price=100,
        stop_distance=2,
        requested_leverage=10,
        policy=policy,
    )
    wide = atr_risk_position_size(
        equity=10_000,
        available_balance=10_000,
        entry_price=100,
        stop_distance=8,
        requested_leverage=10,
        policy=policy,
    )

    assert narrow.estimated_loss_at_stop == Decimal("50")
    assert wide.estimated_loss_at_stop == Decimal("50")
    assert narrow.quantity == wide.quantity * 4
    assert narrow.effective_leverage == 10


def test_atr_sizing_caps_leverage_and_respects_remaining_portfolio_risk() -> None:
    policy = replace(
        LiveRiskPolicy(),
        max_leverage=5,
        short_risk_multiplier=Decimal("1"),
        round_trip_cost_bps=Decimal("0"),
    )
    result = atr_risk_position_size(
        equity=10_000,
        available_balance=10_000,
        entry_price=100,
        stop_distance=2,
        requested_leverage=20,
        current_open_risk=390,
        policy=policy,
    )

    assert result.effective_leverage == 5
    assert result.risk_budget == Decimal("10")
    assert result.estimated_loss_at_stop == Decimal("10")
    assert result.limiting_factor == "risk_budget"

    blocked = atr_risk_position_size(
        equity=10_000,
        available_balance=10_000,
        entry_price=100,
        stop_distance=2,
        requested_leverage=20,
        current_open_risk=400,
        policy=policy,
    )
    assert not blocked.allowed
    assert blocked.limiting_factor == "portfolio_risk_cap"


def test_atr_sizing_halves_short_and_high_risk_budget_independently() -> None:
    long = atr_risk_position_size(
        equity=10_000,
        available_balance=10_000,
        entry_price=100,
        stop_distance=10,
        requested_leverage=10,
    )
    short_high_risk = atr_risk_position_size(
        equity=10_000,
        available_balance=10_000,
        entry_price=100,
        stop_distance=10,
        requested_leverage=10,
        direction=-1,
        high_risk=True,
    )

    assert short_high_risk.risk_budget == long.risk_budget / 4


def test_zero_short_multiplier_disables_new_short_positions() -> None:
    policy = policy_from_config({"short_risk_multiplier": 0})

    decision = atr_risk_position_size(
        equity=10_000,
        available_balance=10_000,
        entry_price=100,
        stop_distance=5,
        requested_leverage=10,
        direction=-1,
        policy=policy,
    )

    assert not decision.allowed


def test_stop_distance_reduces_leverage_before_entry() -> None:
    policy = replace(LiveRiskPolicy(), max_leverage=20, liquidation_buffer_pct=Decimal("1.5"))

    wide_stop = leverage_for_stop_distance(
        entry_price=100,
        stop_distance=8,
        requested_leverage=20,
        policy=policy,
    )
    narrow_stop = leverage_for_stop_distance(
        entry_price=100,
        stop_distance=2,
        requested_leverage=20,
        policy=policy,
    )

    assert wide_stop == 9
    assert narrow_stop == 20


def test_position_and_portfolio_open_risk_include_exit_cost() -> None:
    position = OpenPositionRisk(
        quantity=Decimal("2"),
        entry_price=Decimal("100"),
        stop_price=Decimal("95"),
        exit_cost_bps=Decimal("10"),
    )
    assert estimate_open_position_risk(position) == Decimal("10.20")
    assert total_open_risk([position, position]) == Decimal("20.40")

    admitted = portfolio_risk_admission(equity=1000, current_risk=35, proposed_risk=5, cap_pct=4)
    assert admitted.allowed
    rejected = portfolio_risk_admission(equity=1000, current_risk=35, proposed_risk=5.01, cap_pct=4)
    assert not rejected.allowed


@pytest.mark.parametrize(
    ("direction", "stop", "liquidation", "expected_reason"),
    [
        (1, 95, 90, "ok"),
        (1, 89, 90, "stop_beyond_liquidation"),
        (-1, 105, 110, "ok"),
        (-1, 111, 110, "stop_beyond_liquidation"),
    ],
)
def test_liquidation_safety_orders_stop_before_liquidation(
    direction: int, stop: int, liquidation: int, expected_reason: str
) -> None:
    decision = liquidation_stop_safety(
        entry_price=100,
        stop_price=stop,
        liquidation_price=liquidation,
        direction=direction,
        min_buffer_pct=1,
    )
    assert decision.reason == expected_reason
    assert decision.safe is (expected_reason == "ok")


def test_liquidation_safety_rejects_missing_or_tight_buffer() -> None:
    missing = liquidation_stop_safety(
        entry_price=100,
        stop_price=95,
        liquidation_price=None,
        direction=1,
    )
    assert not missing.safe
    assert missing.reason == "missing_liquidation_price"

    tight = liquidation_stop_safety(
        entry_price=100,
        stop_price=95,
        liquidation_price=94.5,
        direction=1,
        min_buffer_pct=1,
    )
    assert not tight.safe
    assert tight.reason == "liquidation_buffer_too_small"


def test_market_data_freshness_supports_binance_millisecond_timestamps() -> None:
    observed_ms = 1_700_000_000_000
    assert market_data_freshness(observed_ms, now=1_700_000_030, max_age_seconds=45).fresh
    stale = market_data_freshness(observed_ms, now=1_700_000_050, max_age_seconds=45)
    assert not stale.fresh
    assert stale.reason == "market_data_stale"


def test_signal_freshness_has_distinct_rejection_reason() -> None:
    assert signal_freshness(1000, now=18_999, max_age_seconds=18_000).fresh
    stale = signal_freshness(1000, now=19_001, max_age_seconds=18_000)
    assert not stale.fresh
    assert stale.reason == "signal_stale"


def test_closed_bar_signal_is_not_early_and_expires_after_valid_bar() -> None:
    early = closed_bar_signal_freshness(1000, timeframe_seconds=100, now=1099)
    fresh = closed_bar_signal_freshness(1000, timeframe_seconds=100, now=1150)
    stale = closed_bar_signal_freshness(1000, timeframe_seconds=100, now=1201)

    assert early.reason == "bar_not_closed"
    assert fresh.fresh
    assert stale.reason == "signal_stale"


def test_symbol_groups_cap_correlated_and_high_risk_positions() -> None:
    qcom = symbol_risk_profile("QCOMUSDT")
    soxl = symbol_risk_profile("SOXLUSDT")
    assert "semiconductor" in qcom.correlation_groups
    assert soxl.high_risk
    assert "leveraged_or_inverse" in soxl.flags

    correlation_block = symbol_admission("QCOMUSDT", ["AMDUSDT", "NVDAUSDT"])
    assert not correlation_block.allowed
    assert correlation_block.reason == "correlation_group_cap"

    high_risk_block = symbol_admission("TQQQUSDT", [])
    assert not high_risk_block.allowed
    assert high_risk_block.reason == "high_risk_product_blocked"


def test_group_overrides_support_server_owned_metadata() -> None:
    overrides = {"AAA": ["custom"], "BBB": ["custom"]}
    blocked = symbol_admission(
        "AAAUSDT",
        ["BBBUSDT", "BBBUSDT"],
        group_overrides=overrides,
    )
    assert not blocked.allowed
    assert blocked.reason == "correlation_group_cap"


def test_account_loss_limits_block_new_entries_at_either_threshold() -> None:
    daily = account_loss_limits(
        current_equity=979,
        start_of_day_equity=1000,
        high_watermark_equity=1000,
    )
    assert not daily.allow_new_entries
    assert daily.reasons == ("daily_loss_limit",)

    drawdown = account_loss_limits(
        current_equity=930,
        start_of_day_equity=930,
        high_watermark_equity=1000,
    )
    assert not drawdown.allow_new_entries
    assert drawdown.reasons == ("max_drawdown_limit",)
