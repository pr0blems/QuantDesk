from __future__ import annotations

import json
import math

import pytest
from sqlalchemy import event, text
from sqlalchemy.engine import Engine

import quantdesk_v2.backtest as backtest_module
from quantdesk_v2.backtest import BacktestRepository, BacktestUnavailable

BASE_TS = 1_700_000_000
HOUR = 3_600


def _build_repository(
    engine: Engine, tmp_path, closes: list[float], *, milliseconds: bool = True
):
    symbols = tmp_path / "symbols.json"
    symbols.write_text(
        json.dumps(
            {
                "symbols": [
                    {
                        "symbol": "TESTUSDT",
                        "pair": "TESTUSDT",
                        "underlyingType": "EQUITY",
                    },
                    {"symbol": "EMPTYUSDT", "underlyingType": "EQUITY"},
                ]
            }
        ),
        encoding="utf-8",
    )
    rows = []
    for index, close in enumerate(closes):
        timestamp = BASE_TS + index * HOUR
        stored_timestamp = timestamp * 1_000 if milliseconds else timestamp
        open_price = closes[index - 1] if index else close
        rows.append(
            (
                "TESTUSDT",
                "1h",
                stored_timestamp,
                open_price,
                max(open_price, close) + 0.25,
                min(open_price, close) - 0.25,
                close,
                100,
            )
        )
    with engine.begin() as connection:
        statement = text(
            """
            INSERT INTO klines(symbol,tf,open_time,open,high,low,close,volume)
            VALUES(:symbol,:tf,:open_time,:open,:high,:low,:close,:volume)
            """
        )
        payload = [
            {
                "symbol": row[0],
                "tf": row[1],
                "open_time": row[2],
                "open": row[3],
                "high": row[4],
                "low": row[5],
                "close": row[6],
                "volume": row[7],
            }
            for row in rows
        ]
        for start in range(0, len(payload), 5_000):
            connection.execute(statement, payload[start : start + 5_000])
    return BacktestRepository(engine, symbols), engine


@pytest.fixture
def repository_factory(mysql_test_engine: Engine, tmp_path):
    def build(closes: list[float], *, milliseconds: bool = True):
        return _build_repository(
            mysql_test_engine,
            tmp_path,
            closes,
            milliseconds=milliseconds,
        )

    return build


def _update_candle(
    engine: Engine,
    timestamp: int,
    *,
    open_price: float,
    high: float,
    low: float,
    close: float,
) -> None:
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                UPDATE klines
                SET open=:open_price, high=:high, low=:low, close=:close
                WHERE open_time=:open_time
                """
            ),
            {
                "open_price": open_price,
                "high": high,
                "low": low,
                "close": close,
                "open_time": timestamp,
            },
        )


def _config(length: int, **overrides):
    config = {
        "strategy_id": "ma_cross",
        "symbol": "TESTUSDT",
        "timeframe": "1h",
        "start_ts": BASE_TS,
        "end_ts": BASE_TS + (length - 1) * HOUR,
        "initial_capital": 10_000,
        "position_size_pct": 100,
        "leverage": 1,
        "fee_bps": 4,
        "slippage_bps": 2,
        "stop_loss_pct": 0,
        "take_profit_pct": 0,
        "max_holding_bars": 0,
        "params": {"fast_period": 2, "slow_period": 3},
    }
    config.update(overrides)
    return config


def _assert_json_safe(value) -> None:
    if isinstance(value, dict):
        for child in value.values():
            _assert_json_safe(child)
    elif isinstance(value, list):
        for child in value:
            _assert_json_safe(child)
    elif isinstance(value, float):
        assert math.isfinite(value)


def test_catalog_lists_templates_symbols_and_unix_second_bounds(repository_factory) -> None:
    repository, _ = repository_factory([100, 101, 102, 103])

    catalog = repository.catalog()

    assert {item["id"] for item in catalog["strategies"]} == {
        "multi_factor",
        "ma_cross",
        "macd_momentum",
        "rsi_reversal",
        "bollinger_reversion",
    }
    assert len(catalog["symbols"]) == 2
    available = catalog["symbols"][0]["timeframes"][0]
    assert available == {
        "timeframe": "1h",
        "start_ts": BASE_TS,
        "end_ts": BASE_TS + 3 * HOUR,
        "bars": 4,
    }
    assert catalog["symbols"][1]["available"] is False
    assert catalog["timeframes"] == ["1h"]
    assert catalog["bounds"]["TESTUSDT"]["1h"]["start_ts"] == BASE_TS
    assert catalog["limits"]["timestamp_unit"] == "seconds"


def test_specialized_market_loader_reuses_clean_binance_candles(repository_factory) -> None:
    repository, _ = repository_factory([100, 101, 102, 103])

    candles, quality = repository.load_market_candles(
        "TESTUSDT",
        "1h",
        BASE_TS,
        BASE_TS + 3 * HOUR,
    )

    assert [candle.close for candle in candles] == [100, 101, 102, 103]
    assert quality["source"] == "binance_fapi"
    assert quality["actual_bars"] == 4


def test_signal_is_filled_at_next_open_and_costs_are_charged_both_sides(
    repository_factory,
) -> None:
    closes = [10, 10, 10, 12, 13, 14]
    repository, _ = repository_factory(closes)

    result = repository.run(_config(len(closes)))

    assert result["trades"][0]["entry_ts"] == BASE_TS + 4 * HOUR
    assert result["trades"][0]["exit_ts"] == BASE_TS + 5 * HOUR
    assert result["trades"][0]["fees"] > 0
    assert result["trades"][0]["initial_margin"] > 0
    assert result["trades"][0]["leverage"] == 1
    assert result["trades"][0]["remaining_available_balance"] >= 0
    assert result["trades"][0]["available_balance_after_close"] == pytest.approx(
        result["account"]["final_equity"]
    )
    assert result["trades"][0]["margin_return_pct"] == result["trades"][0]["return_pct"]
    assert math.isfinite(result["trades"][0]["account_return_pct"])
    assert result["account"]["total_fees"] == result["trades"][0]["fees"]
    assert result["metrics"]["trade_count"] == 1
    assert math.isfinite(result["metrics"]["annualized_return_pct"])
    assert len(result["data_quality"]["assumptions"]) == 8
    assert set(result) == {
        "account",
        "metrics",
        "equity_curve",
        "price_candles",
        "trades",
        "data_quality",
    }
    assert len(result["price_candles"]) == len(closes)
    assert result["price_candles"][0]["ts"] == BASE_TS
    _assert_json_safe(result)


def test_same_bar_stop_and_take_uses_conservative_stop(repository_factory) -> None:
    closes = [10, 10, 10, 12, 100, 100]
    repository, database = repository_factory(closes)
    _update_candle(
        database,
        (BASE_TS + 4 * HOUR) * 1_000,
        open_price=100,
        high=110,
        low=90,
        close=100,
    )

    result = repository.run(
        _config(
            len(closes),
            fee_bps=0,
            slippage_bps=0,
            stop_loss_pct=5,
            take_profit_pct=5,
        )
    )

    trade = result["trades"][0]
    assert trade["exit_reason"] == "stop_loss"
    assert trade["exit_price"] == 95
    assert trade["net_pnl"] < 0
    assert trade["exit_decision"] == {
        "version": "unified_exit_decision_v1",
        "policy_version": "unified_exit_v1",
        "mode": "backtest",
        "reason": "stop_loss",
        "source": "price_barrier",
        "priority": 20,
        "trigger_price": 95.0,
        "observed_at": BASE_TS + 5 * HOUR,
        "execution_price": 95.0,
    }


def test_binance_contract_filters_reject_subminimum_order(repository_factory) -> None:
    closes = [10, 10, 10, 12, 13, 14]
    repository, _ = repository_factory(closes)
    rules = {
        "source": "test_exchange_info",
        "symbol": "TESTUSDT",
        "tick_size": 0.01,
        "market_step_size": 0.1,
        "min_quantity": 0.1,
        "max_quantity": 1_000,
        "min_notional": 1_000_000,
        "max_leverage": 20,
        "maintenance_margin_rate": 0.005,
        "maintenance_amount": 0,
        "liquidation_fee_rate": 0.0125,
        "market_take_bound": 0.05,
    }

    result = repository.run(_config(len(closes), contract_rules=rules))

    assert result["trades"] == []
    assert result["metrics"]["rejected_order_count"] >= 1
    assert result["metrics"]["contract_rules"]["source"] == "test_exchange_info"


def test_liquidation_uses_fixed_mmr_and_adverse_exit_precedes_take_profit(
    repository_factory,
) -> None:
    closes = [100, 100, 100, 110, 100, 100]
    repository, database = repository_factory(closes)
    _update_candle(
        database,
        (BASE_TS + 4 * HOUR) * 1_000,
        open_price=110,
        high=111,
        low=90,
        close=110,
    )

    result = repository.run(
        _config(
            len(closes),
            fee_bps=0,
            slippage_bps=0,
            leverage=20,
            stop_loss_pct=10,
            take_profit_pct=0.5,
        )
    )

    trade = result["trades"][0]
    expected_liquidation_price = 110 * (1 - 1 / 20) / (1 - 0.005)
    assert trade["exit_reason"] == "liquidation"
    assert trade["liquidation_price"] == pytest.approx(expected_liquidation_price)
    assert trade["exit_price"] == pytest.approx(expected_liquidation_price)
    assert result["metrics"]["liquidation_count"] == 1
    assert result["metrics"]["maintenance_margin_rate_pct"] == 0.5
    assert result["metrics"]["liquidation_model"] == "binance_isolated_contract_snapshot_ohlc"
    assert any("一级维持保证金率" in item for item in result["data_quality"]["assumptions"])


def test_stop_above_liquidation_is_executed_first(repository_factory) -> None:
    closes = [100, 100, 100, 110, 100, 100]
    repository, database = repository_factory(closes)
    _update_candle(
        database,
        (BASE_TS + 4 * HOUR) * 1_000,
        open_price=110,
        high=111,
        low=90,
        close=110,
    )

    result = repository.run(
        _config(
            len(closes),
            fee_bps=0,
            slippage_bps=0,
            leverage=20,
            stop_loss_pct=2,
        )
    )

    assert result["trades"][0]["exit_reason"] == "stop_loss"
    assert result["trades"][0]["exit_price"] == pytest.approx(107.8)
    assert result["metrics"]["liquidation_count"] == 0


def test_gap_through_liquidation_uses_worse_open_before_strategy_exit(
    repository_factory,
) -> None:
    closes = [100, 100, 100, 110, 100, 100]
    repository, database = repository_factory(closes)
    _update_candle(
        database,
        (BASE_TS + 4 * HOUR) * 1_000,
        open_price=110,
        high=111,
        low=109,
        close=110,
    )

    result = repository.run(_config(len(closes), fee_bps=0, slippage_bps=0, leverage=20))

    trade = result["trades"][0]
    assert trade["exit_ts"] == BASE_TS + 5 * HOUR
    assert trade["exit_reason"] == "liquidation"
    assert trade["exit_price"] == 100
    assert trade["exit_price"] < trade["liquidation_price"]


def test_short_position_profits_when_price_falls(repository_factory) -> None:
    closes = [12, 12, 12, 10, 9, 8]
    repository, _ = repository_factory(closes)

    result = repository.run(_config(len(closes), fee_bps=0, slippage_bps=0, leverage=2))

    trade = result["trades"][0]
    assert trade["side"] == "short"
    assert trade["net_pnl"] > 0
    assert trade["leverage"] == 2


def test_small_valid_trade_values_are_not_rounded_to_zero(repository_factory) -> None:
    closes = [100_000_000, 100_000_000, 100_000_000, 120_000_000, 130_000_000, 140_000_000]
    repository, _ = repository_factory(closes)

    result = repository.run(
        _config(
            len(closes),
            initial_capital=1,
            position_size_pct=0.01,
            fee_bps=0,
            slippage_bps=0,
        )
    )

    trade = result["trades"][0]
    expected_quantity = 0.0001 / 120_000_000
    assert trade["quantity"] == pytest.approx(expected_quantity)
    assert 0 < trade["quantity"] < 1e-8
    assert 0 < trade["gross_pnl"] < 0.0001
    assert 0 < trade["net_pnl"] < 0.0001
    assert result["metrics"]["net_profit"] > 0


def test_sub_cent_prices_remain_nonzero_in_trade_output(repository_factory) -> None:
    closes = [1.2e-9, 1.1e-9, 1e-9, 1.4e-9, 1.5e-9, 1.6e-9]
    repository, database = repository_factory(closes)
    for index, close in enumerate(closes):
        open_price = closes[index - 1] if index else close
        _update_candle(
            database,
            (BASE_TS + index * HOUR) * 1_000,
            open_price=open_price,
            high=max(open_price, close) * 1.01,
            low=min(open_price, close) * 0.99,
            close=close,
        )

    result = repository.run(
        _config(
            len(closes),
            initial_capital=1,
            position_size_pct=0.01,
            fee_bps=0,
            slippage_bps=0,
        )
    )

    trade = result["trades"][0]
    assert 0 < trade["entry_price"] < 1e-8
    assert 0 < trade["exit_price"] < 1e-8
    assert trade["net_pnl"] > 0


def test_integral_float_strategy_periods_are_accepted(repository_factory) -> None:
    repository, _ = repository_factory([10, 10, 10, 12, 13, 14])

    result = repository.run(_config(6, params={"fast_period": 2.0, "slow_period": 3.0}))

    assert result["metrics"]["trade_count"] == 1


def test_repository_executes_read_only_market_queries(repository_factory) -> None:
    repository, database = repository_factory([100, 101, 102])
    statements: list[str] = []

    def record_statement(
        _connection, _cursor, statement, _parameters, _context, _executemany
    ) -> None:
        statements.append(statement.strip())

    event.listen(database, "before_cursor_execute", record_statement)
    try:
        repository.catalog()
        repository.run(_config(3))
    finally:
        event.remove(database, "before_cursor_execute", record_statement)

    assert statements
    assert all(statement.upper().startswith("SELECT") for statement in statements)


@pytest.mark.parametrize(
    ("strategy_id", "params"),
    [
        (
            "multi_factor",
            {"fast_period": 5, "slow_period": 12, "rsi_period": 6, "threshold": 2},
        ),
        ("ma_cross", {"fast_period": 5, "slow_period": 12}),
        (
            "macd_momentum",
            {"fast_period": 5, "slow_period": 12, "signal_period": 4},
        ),
        ("rsi_reversal", {"period": 6, "oversold": 35, "overbought": 65}),
        ("bollinger_reversion", {"period": 10, "stddev": 1.5}),
    ],
)
def test_all_strategies_return_json_safe_results(
    repository_factory, strategy_id, params
) -> None:
    closes = [100 + math.sin(index / 3) * 8 + index * 0.03 for index in range(180)]
    repository, _ = repository_factory(closes)

    result = repository.run(_config(len(closes), strategy_id=strategy_id, params=params))

    assert result["data_quality"]["bars_used"] == len(closes)
    _assert_json_safe(result)
    json.dumps(result, allow_nan=False)


def test_curve_is_evenly_sampled_and_preserves_first_and_last(repository_factory) -> None:
    closes = [100 + index * 0.01 for index in range(1_601)]
    repository, _ = repository_factory(closes)

    result = repository.run(_config(len(closes)))

    assert len(result["equity_curve"]) == 1_500
    assert result["equity_curve"][0]["ts"] == BASE_TS
    assert result["equity_curve"][-1]["ts"] == BASE_TS + 1_600 * HOUR
    assert result["data_quality"]["equity_curve_truncated"] is True


def test_equity_drawdown_uses_constant_time_running_peak(
    repository_factory, monkeypatch
) -> None:
    closes = [100 + index * 0.01 for index in range(2_000)]
    repository, _ = repository_factory(closes)
    original_max = max

    def reject_long_list_scan(*values, **kwargs):
        if len(values) == 1 and isinstance(values[0], list) and len(values[0]) > 100:
            raise AssertionError("engine rescanned a growing list while processing a candle")
        return original_max(*values, **kwargs)

    monkeypatch.setattr("builtins.max", reject_long_list_scan)

    result = repository.run(_config(len(closes)))

    assert result["data_quality"]["bars_used"] == len(closes)
    assert len(result["equity_curve"]) == 1_500


def test_annualization_ends_at_last_candle_close(repository_factory, monkeypatch) -> None:
    closes = [10, 10, 10, 12, 13, 14]
    repository, _ = repository_factory(closes)
    durations: list[int] = []
    original_annualized_return = backtest_module._annualized_return

    def record_duration(initial, final, duration_seconds):
        durations.append(duration_seconds)
        return original_annualized_return(initial, final, duration_seconds)

    monkeypatch.setattr(backtest_module, "_annualized_return", record_duration)

    repository.run(_config(len(closes), position_size_pct=0.01))

    assert durations == [len(closes) * HOUR]


def test_trade_response_is_bounded_but_keeps_json_safe_server_payload(
    repository_factory,
) -> None:
    closes = [95 if index % 2 == 0 else 105 for index in range(6_010)]
    repository, _ = repository_factory(closes)

    result = repository.run(_config(len(closes), fee_bps=0, slippage_bps=0, position_size_pct=0.01))

    assert result["data_quality"]["trades_truncated"] is True
    assert len(result["trades"]) == 5_000
    assert len(result["_all_trades"]) == result["metrics"]["trade_count"]
    assert result["trades"] == result["_all_trades"][-5_000:]
    json.dumps(result, allow_nan=False)


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"symbol": "UNKNOWN"}, "unknown backtest symbol"),
        ({"timeframe": "1hour"}, "暂不支持该回测周期"),
        ({"leverage": 21}, "leverage must be between"),
        ({"start_ts": BASE_TS + HOUR, "end_ts": BASE_TS}, "start_ts must be earlier"),
        ({"params": {"fast_period": 20, "slow_period": 10}}, "fast_period must be smaller"),
        (
            {"params": {"fast_period": 2, "slow_period": 3, "future_period": 4}},
            "unknown strategy params",
        ),
    ],
)
def test_strict_config_validation(repository_factory, changes, message) -> None:
    repository, _ = repository_factory([100, 101, 102, 103])

    with pytest.raises(BacktestUnavailable, match=message):
        repository.run(_config(4, **changes))


def test_more_than_fifty_thousand_bars_is_rejected(repository_factory) -> None:
    closes = [100 + index * 0.0001 for index in range(50_001)]
    repository, _ = repository_factory(closes, milliseconds=False)

    with pytest.raises(BacktestUnavailable, match="50000 bar limit"):
        repository.run(_config(len(closes)))
