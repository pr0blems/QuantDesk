from __future__ import annotations

import importlib.util
from datetime import date
from pathlib import Path

import pytest
from pydantic import ValidationError
from sqlalchemy import JSON, Numeric

from quantdesk_v2.models import BacktestRun, BacktestTrade
from quantdesk_v2.schemas import BacktestRunRequest


def valid_request(**overrides):
    payload = {
        "strategy_id": "trend_breakout",
        "symbol": "btcusdt",
        "timeframe": "4h",
        "start_date": date(2025, 1, 1),
        "end_date": date(2025, 12, 31),
        "initial_capital": "10000.00",
        "position_size_pct": "10",
        "leverage": "2",
        "fee_bps": "5",
        "slippage_bps": "2",
        "stop_loss_pct": "2",
        "take_profit_pct": "5",
        "max_holding_bars": 120,
        "params": {"lookback": 20.0},
    }
    payload.update(overrides)
    return payload


def test_backtest_request_normalizes_symbol_and_is_json_serializable() -> None:
    request = BacktestRunRequest.model_validate(valid_request())

    assert request.symbol == "BTCUSDT"
    assert request.market_data_source == "binance"
    dumped = request.model_dump(mode="json")
    assert dumped["initial_capital"] == "10000.00"
    assert dumped["start_date"] == "2025-01-01"


@pytest.mark.parametrize(
    "overrides",
    [
        {"start_date": date(2025, 2, 1), "end_date": date(2025, 1, 31)},
        {"start_date": date(2024, 1, 1), "end_date": date(2025, 1, 2)},
        {"position_size_pct": 101},
        {"leverage": 126},
        {"fee_bps": -1},
        {"timeframe": "7m"},
        {"market_data_source": "unknown"},
        {"params": {"lookback": float("inf")}},
        {"unexpected": True},
    ],
)
def test_backtest_request_rejects_invalid_or_unbounded_input(overrides) -> None:
    with pytest.raises(ValidationError):
        BacktestRunRequest.model_validate(valid_request(**overrides))


def test_backtest_models_are_tenant_scoped_commented_and_decimal_safe() -> None:
    run_table = BacktestRun.__table__
    trade_table = BacktestTrade.__table__
    for table in (run_table, trade_table):
        assert table.comment
        assert all(column.comment for column in table.columns)

    assert isinstance(run_table.c.config_json.type, JSON)
    assert isinstance(run_table.c.metrics_json.type, JSON)
    assert isinstance(run_table.c.equity_curve_json.type, JSON)
    assert isinstance(run_table.c.data_quality_json.type, JSON)
    assert isinstance(trade_table.c.metadata_json.type, JSON)

    for column_name in ("initial_capital", "final_equity", "net_profit"):
        assert isinstance(run_table.c[column_name].type, Numeric)
    for column_name in (
        "entry_price",
        "exit_price",
        "quantity",
        "gross_pnl",
        "fees",
        "net_pnl",
        "return_pct",
    ):
        assert isinstance(trade_table.c[column_name].type, Numeric)
    assert trade_table.c.quantity.type.precision == 48
    assert trade_table.c.quantity.type.scale == 18

    run_user_fks = tuple(run_table.c.user_id.foreign_keys)
    trade_run_fk = next(iter(trade_table.c.run_id.foreign_keys))
    trade_user_fk = next(iter(trade_table.c.user_id.foreign_keys))
    # ``user_id`` also participates in tenant-scoped strategy/revision foreign
    # keys.  The direct user ownership edge remains cascading, while immutable
    # strategy facts intentionally use RESTRICT.
    assert any(
        fk.target_fullname == "users.id" and fk.ondelete == "CASCADE"
        for fk in run_user_fks
    )
    assert trade_run_fk.ondelete == "CASCADE"
    assert trade_user_fk.ondelete == "CASCADE"
    assert BacktestRun.trades.property.cascade.delete_orphan
    assert BacktestRun.trades.property.passive_deletes


def test_backtest_migration_follows_monitor_preferences_revision() -> None:
    migration_path = (
        Path(__file__).parents[1] / "migrations" / "versions" / "0004_add_backtest_tables.py"
    )
    spec = importlib.util.spec_from_file_location("backtest_migration_0004", migration_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    assert module.revision == "0004_add_backtest_tables"
    assert module.down_revision == "0003_add_monitor_preferences"


def test_quantity_precision_migration_follows_backtest_tables_revision() -> None:
    migration_path = (
        Path(__file__).parents[1]
        / "migrations"
        / "versions"
        / "0005_expand_backtest_quantity_precision.py"
    )
    spec = importlib.util.spec_from_file_location("backtest_migration_0005", migration_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    assert module.revision == "0005_backtest_qty_precision"
    assert module.down_revision == "0004_add_backtest_tables"
