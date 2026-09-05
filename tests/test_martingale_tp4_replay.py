from __future__ import annotations

import math
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from quantdesk_v2.application.martingale_tp4.replay import (
    ReplayCosts,
    ReplayDataError,
    assess_market_adaptive_box_state,
    assess_replay_coverage,
    build_box_levels,
    calculate_wilder_atr,
    dataset_sha256,
    run_bar_replay,
)
from quantdesk_v2.domain.martingale_tp4 import (
    MartingaleTp4Config,
    Mq4Inputs,
    strategy_parameters_from_mq4,
)
from quantdesk_v2.domain.martingale_tp4_engine import EnginePolicy
from quantdesk_v2.tiger_market_data import TigerBar


def _bar(
    index: int,
    *,
    timeframe: str = "15m",
    open_price: str = "100",
    high: str = "100.1",
    low: str = "99.9",
    close: str = "100",
    volume: str = "1000",
) -> TigerBar:
    duration = 86_400_000 if timeframe == "1d" else 900_000
    open_time = 1_700_000_000_000 + index * duration
    return TigerBar(
        symbol="AMD",
        timeframe=timeframe,
        trade_session="regular",
        adjustment="none",
        open_time=open_time,
        close_time=open_time + duration,
        open=Decimal(open_price),
        high=Decimal(high),
        low=Decimal(low),
        close=Decimal(close),
        volume=Decimal(volume),
        amount=None,
        received_at=datetime.now(UTC),
    )


def _config(inputs: Mq4Inputs) -> MartingaleTp4Config:
    return MartingaleTp4Config.model_validate(
        {
            "market_data": {
                "underlying_symbol": "AMD",
                "contract_symbol": "AMDUSDT",
            },
            "parameters": strategy_parameters_from_mq4(inputs).model_dump(mode="json"),
            "live_risk": {
                "max_cycle_loss_pct": "2",
                "max_cycle_margin_pct": "20",
                "minimum_liquidation_buffer_pct": "8",
                "daily_loss_limit_pct": "5",
            },
        }
    )


def test_wilder_atr_uses_only_closed_daily_bars() -> None:
    daily = (
        _bar(0, timeframe="1d", open_price="9", high="10", low="8", close="9"),
        _bar(1, timeframe="1d", open_price="10", high="11", low="9", close="10"),
        _bar(2, timeframe="1d", open_price="11", high="13", low="10", close="12"),
    )

    values = calculate_wilder_atr(daily, 2)

    assert values[0][1] == Decimal("2")
    assert values[1][1] == Decimal("2.5")


def test_market_adaptive_box_detects_low_volume_narrow_range() -> None:
    active = tuple(
        _bar(index, high="100.1", low="99.9", volume="1000")
        for index in range(20)
    )
    contracted = tuple(
        _bar(index, high="100.02", low="99.98", volume="100")
        for index in range(20, 25)
    )

    state = assess_market_adaptive_box_state(
        (*active, *contracted),
        daily_atr=Decimal("2"),
    )

    assert state is not None
    assert state.status == "low_volume_range"
    assert state.volume_ratio < Decimal("0.80")
    assert state.range_ratio < Decimal("0.80")
    assert state.factor_multiplier == Decimal("1.60")


def test_market_adaptive_setting_round_trips_from_mq4_inputs() -> None:
    parameters = strategy_parameters_from_mq4(
        Mq4Inputs(AutoBoxRange=True, AutoBoxRangeMarketAdaptive=True)
    )

    assert parameters.box.auto_range is True
    assert parameters.box.market_adaptive is True


def test_box_level_is_causal_and_excludes_the_current_breakout_bar() -> None:
    config = _config(
        Mq4Inputs(
            AutoBoxRange=False,
            BoxLength=2,
            BoxRange="100",
            BoxBufferPips="0",
            Start_Hour=0,
            End_Hour=0,
        )
    )
    signal = (
        _bar(0, high="101.5", low="99.9"),
        _bar(1),
        _bar(2),
        _bar(3, open_price="100", high="105", low="99.9", close="104"),
    )
    daily = (_bar(0, timeframe="1d"), _bar(1, timeframe="1d"))

    levels = build_box_levels(signal, daily, config, point_size=Decimal("0.01"))

    assert levels[:3] == (None, None, None)
    assert levels[3] is not None
    assert levels[3].high == Decimal("100.9")
    assert levels[3].low == Decimal("99.9")


def test_auto_replay_opens_on_box_breakout_and_closes_at_basket_tp() -> None:
    config = _config(
        Mq4Inputs(
            AutoBoxRange=False,
            BoxLength=2,
            BoxRange="100",
            BoxBufferPips="0",
            Start_Hour=0,
            End_Hour=0,
            TrailStart=0,
        )
    )
    signal = (
        _bar(0, high="101.5", low="99.9"),
        _bar(1),
        _bar(2),
        _bar(3, open_price="100", high="101.2", low="99.9", close="101"),
        _bar(4, open_price="101", high="102.4", low="100.8", close="102"),
    )
    daily = (_bar(0, timeframe="1d"), _bar(1, timeframe="1d"))

    result = run_bar_replay(
        config,
        signal,
        daily,
        initial_capital=Decimal("10000"),
        point_size=Decimal("0.01"),
        costs=ReplayCosts(
            fee_bps=Decimal("0"),
            slippage_bps=Decimal("0"),
            synthetic_spread_points=Decimal("0"),
        ),
    )

    assert len(result.cycles) == 1
    assert result.cycles[0].exit_reason == "basket_take_profit"
    assert result.cycles[0].realized_pnl == Decimal("0.012")
    assert result.cycles[0].leverage == 1
    assert result.cycles[0].peak_initial_margin == Decimal("1.0120")
    assert result.cycles[0].minimum_available_balance == Decimal("9998.9880")
    assert result.cycles[0].available_balance_after_close == Decimal("10000.0120")
    assert result.cycles[0].long_leg_count == 1
    assert result.cycles[0].short_leg_count == 0
    assert [item.action for item in result.fills] == ["open", "close_all"]
    assert result.metrics["winning_cycles"] == 1
    assert math.isfinite(float(result.metrics["annualized_return_pct"]))
    assert math.isfinite(float(result.metrics["sharpe_ratio"]))


def test_stateful_box_is_invalidated_after_a_closed_bar_breaks_its_boundary() -> None:
    config = _config(
        Mq4Inputs(
            AutoBoxRange=False,
            BoxLength=2,
            BoxRange="100",
            BoxBufferPips="0",
        )
    )
    signal = (
        _bar(0, high="101.5", low="99.9"),
        _bar(1),
        _bar(2),
        _bar(3, high="101.2", low="99.9", close="101"),
        _bar(4),
    )

    levels = build_box_levels(signal, (), config, point_size=Decimal("0.01"))

    assert levels[3] is not None
    assert levels[4] is None


def test_grid_replay_requires_and_records_manual_first_leg() -> None:
    config = _config(
        Mq4Inputs(
            ChooseTrading="grid",
            AutoBoxRange=False,
            BoxLength=2,
            TrailStart=0,
            TP="1000",
        )
    )
    signal = tuple(_bar(index) for index in range(4))
    daily = (_bar(0, timeframe="1d"), _bar(1, timeframe="1d"))

    with pytest.raises(ReplayDataError, match="manual first-leg"):
        run_bar_replay(
            config,
            signal,
            daily,
            initial_capital=Decimal("10000"),
            point_size=Decimal("0.01"),
        )

    result = run_bar_replay(
        config,
        signal,
        daily,
        initial_capital=Decimal("10000"),
        point_size=Decimal("0.01"),
        costs=ReplayCosts(fee_bps=Decimal("0"), slippage_bps=Decimal("0")),
        manual_entry_direction="buy",
        manual_entry_time=signal[2].open_time,
    )

    assert result.fills[0].reason_code == "manual_cycle_entry"
    assert result.cycles[0].exit_reason == "end_of_data"


def test_replay_is_deterministic_and_hash_changes_with_market_data() -> None:
    config = _config(Mq4Inputs(AutoBoxRange=False, BoxLength=2, Start_Hour=0, End_Hour=0))
    signal = tuple(_bar(index) for index in range(4))
    changed = (*signal[:-1], _bar(3, close="100.01", high="100.1", low="99.9"))
    daily = (_bar(0, timeframe="1d"), _bar(1, timeframe="1d"))

    first = run_bar_replay(
        config,
        signal,
        daily,
        initial_capital=Decimal("10000"),
        point_size=Decimal("0.01"),
    )
    second = run_bar_replay(
        config,
        signal,
        daily,
        initial_capital=Decimal("10000"),
        point_size=Decimal("0.01"),
    )

    assert first.run_sha256 == second.run_sha256
    assert first.audit_payload() == second.audit_payload()
    assert dataset_sha256(signal, daily) != dataset_sha256(changed, daily)


def test_replay_hash_records_the_selected_engine_policy() -> None:
    config = _config(Mq4Inputs(AutoBoxRange=False, BoxLength=2, Start_Hour=0, End_Hour=0))
    signal = tuple(_bar(index) for index in range(4))
    daily = (_bar(0, timeframe="1d"), _bar(1, timeframe="1d"))

    compatibility = run_bar_replay(
        config,
        signal,
        daily,
        initial_capital=Decimal("10000"),
        point_size=Decimal("0.01"),
    )
    safe = run_bar_replay(
        config,
        signal,
        daily,
        initial_capital=Decimal("10000"),
        point_size=Decimal("0.01"),
        engine_policy=EnginePolicy.LIVE_SAFE,
    )

    assert compatibility.engine_policy == "research_compatibility"
    assert safe.engine_policy == "live_safe"
    assert compatibility.run_sha256 != safe.run_sha256


def test_replay_rejects_duplicate_or_foreign_bars() -> None:
    config = _config(Mq4Inputs(AutoBoxRange=False, BoxLength=2))
    duplicate = (_bar(0), _bar(1), _bar(1), _bar(2))
    daily = (_bar(0, timeframe="1d"), _bar(1, timeframe="1d"))

    with pytest.raises(ReplayDataError, match="duplicate"):
        run_bar_replay(
            config,
            duplicate,
            daily,
            initial_capital=Decimal("10000"),
            point_size=Decimal("0.01"),
        )


def test_replay_coverage_fails_closed_on_missing_warmup_and_intraday_gap() -> None:
    signal = (
        _bar(0),
        _bar(2),
        _bar(3),
    )
    daily = (_bar(0, timeframe="1d"),)

    coverage = assess_replay_coverage(
        signal,
        daily,
        evaluation_begin_time=signal[1].open_time,
        evaluation_end_time=signal[-1].close_time,
        required_signal_warmup_bars=2,
        required_daily_warmup_bars=2,
        timezone="UTC",
    )

    assert coverage.status == "blocked"
    assert coverage.intraday_gap_count == 1
    assert set(coverage.reason_codes) == {
        "signal_warmup_incomplete",
        "daily_atr_warmup_incomplete",
        "intraday_bar_gap",
    }


def test_replay_coverage_accepts_inclusive_and_exclusive_contiguous_bars() -> None:
    exclusive = tuple(_bar(index) for index in range(4))
    inclusive = tuple(replace(bar, close_time=bar.close_time - 1) for bar in exclusive)

    for signal in (exclusive, inclusive):
        coverage = assess_replay_coverage(
            signal,
            (),
            evaluation_begin_time=signal[2].open_time,
            evaluation_end_time=signal[-1].close_time,
            required_signal_warmup_bars=2,
            required_daily_warmup_bars=0,
            timezone="UTC",
        )

        assert coverage.status == "usable"
        assert coverage.intraday_gap_count == 0
        assert coverage.reason_codes == ()


def test_fixed_box_replay_does_not_require_daily_atr_bars() -> None:
    config = _config(
        Mq4Inputs(AutoBoxRange=False, BoxLength=2, Start_Hour=0, End_Hour=0)
    )
    signal = tuple(_bar(index) for index in range(4))

    result = run_bar_replay(
        config,
        signal,
        (),
        initial_capital=Decimal("10000"),
        point_size=Decimal("0.01"),
    )

    assert result.daily_bar_count == 0


def test_forced_close_equity_deducts_each_fee_exactly_once() -> None:
    config = _config(
        Mq4Inputs(
            ChooseTrading="grid",
            AutoBoxRange=False,
            BoxLength=2,
            TP="1000",
            TrailStart=0,
        )
    )
    signal = tuple(_bar(index) for index in range(4))

    result = run_bar_replay(
        config,
        signal,
        (),
        initial_capital=Decimal("10000"),
        point_size=Decimal("0.01"),
        costs=ReplayCosts(
            fee_bps=Decimal("10"),
            slippage_bps=Decimal("0"),
            synthetic_spread_points=Decimal("0"),
        ),
        manual_entry_direction="buy",
        manual_entry_time=signal[2].open_time,
    )

    assert result.cycles[0].realized_pnl == Decimal("-0.00200")
    assert result.metrics["final_equity"] == Decimal("9999.99800")
    assert result.equity_curve[-1]["equity"] == result.metrics["final_equity"]
    assert result.equity_curve[-1]["open_legs"] == 0


def test_basket_replay_applies_selected_leverage_to_margin_capacity() -> None:
    config = _config(
        Mq4Inputs(
            ChooseTrading="grid",
            AutoBoxRange=False,
            BoxLength=2,
            Lot="200",
            MaxLot="500",
            TP="1000",
            TrailStart=0,
        )
    )
    signal = tuple(_bar(index) for index in range(4))
    common = {
        "initial_capital": Decimal("10000"),
        "point_size": Decimal("0.01"),
        "costs": ReplayCosts(
            fee_bps=Decimal("0"),
            slippage_bps=Decimal("0"),
            synthetic_spread_points=Decimal("0"),
        ),
        "manual_entry_direction": "buy",
        "manual_entry_time": signal[2].open_time,
    }

    rejected = run_bar_replay(config, signal, (), leverage=1, **common)
    accepted = run_bar_replay(config, signal, (), leverage=2, **common)

    assert rejected.metrics["rejected_order_count"] >= 1
    assert rejected.metrics["open_fill_count"] == 0
    assert accepted.metrics["leverage"] == 2
    assert accepted.metrics["open_fill_count"] == 1
