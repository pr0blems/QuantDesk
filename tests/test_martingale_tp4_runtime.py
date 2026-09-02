from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from quantdesk_v2.application.martingale_tp4.runtime import (
    binance_quote_from_snapshot,
    binance_tick_size,
    build_shadow_config,
    current_shadow_box,
    shadow_event_id,
    tiger_quote_from_snapshot,
)
from quantdesk_v2.strategy_catalog import ENGINE_PARAMETER_SCHEMAS
from quantdesk_v2.tiger_market_data import TigerBar

NOW = datetime(2026, 9, 3, 15, 0, tzinfo=UTC)


def _catalog_parameters(**overrides: int | float) -> dict[str, int | float]:
    values = {
        item["key"]: item["default"]
        for item in ENGINE_PARAMETER_SCHEMAS["martingale_tp4"]
    }
    values.update(overrides)
    return values


def _bar(index: int, low: str, high: str, close: str) -> TigerBar:
    open_time = int((NOW - timedelta(minutes=(4 - index) * 15)).timestamp() * 1000)
    return TigerBar(
        symbol="AMD",
        timeframe="15m",
        trade_session="regular",
        adjustment="none",
        open_time=open_time,
        close_time=open_time + 900_000,
        open=Decimal(close),
        high=Decimal(high),
        low=Decimal(low),
        close=Decimal(close),
        volume=Decimal("100"),
        amount=None,
        received_at=NOW,
    )


def test_runtime_config_restores_revision_and_keeps_independent_cycle_guard() -> None:
    config = build_shadow_config(
        {
            "engine_key": "martingale_tp4",
            "parameters": _catalog_parameters(ChooseTrading=2, Lot=0.25),
        },
        {
            "position_size_pct": 1,
            "leverage": 2,
            "stop_loss_pct": 0,
        },
        underlying_symbol="amd",
        contract_symbol="amdusdt",
    )

    assert config.parameters.mode == "grid"
    assert config.parameters.sizing.initial_lot == Decimal("0.25")
    assert config.live_risk.max_cycle_loss_pct == Decimal("1")
    assert config.live_risk.max_cycle_margin_pct == Decimal("10")
    assert config.market_data.underlying_symbol == "AMD"
    assert config.market_data.contract_symbol == "AMDUSDT"


def test_runtime_requires_exact_binance_tick_metadata() -> None:
    assert binance_tick_size(
        {
            "filters": [
                {"filterType": "LOT_SIZE", "stepSize": "0.01"},
                {"filterType": "PRICE_FILTER", "tickSize": "0.001"},
            ]
        }
    ) == Decimal("0.001")
    assert binance_tick_size({"pricePrecision": 2}) == Decimal("0.01")
    with pytest.raises(ValueError, match="tick size is unavailable"):
        binance_tick_size({})


def test_runtime_quote_adapters_preserve_source_identity() -> None:
    tiger_snapshot = {
        "symbol": "AMD",
        "best_bid": 100.01,
        "best_ask": 100.02,
        "source_timestamp": int(NOW.timestamp()),
    }
    binance_snapshot = {
        "symbol": "AMDUSDT",
        "best_bid": 99.99,
        "best_ask": 100.00,
        "mid_price": 99.995,
        "captured_at": int(NOW.timestamp()),
        "last_update_id": 12345,
    }

    tiger = tiger_quote_from_snapshot(tiger_snapshot)
    binance = binance_quote_from_snapshot(binance_snapshot)

    assert tiger.bid == Decimal("100.01")
    assert binance.ask == Decimal("100.0")
    assert tiger.observed_at == NOW
    assert binance.observed_at == NOW
    assert shadow_event_id(tiger_snapshot, binance_snapshot).endswith("binance:12345")


def test_current_shadow_box_uses_only_closed_bar_history() -> None:
    config = build_shadow_config(
        {
            "engine_key": "martingale_tp4",
            "parameters": _catalog_parameters(
                AutoBoxRange=0,
                BoxLength=2,
                BoxRange=500,
                BoxBufferPips=0,
            ),
        },
        {"position_size_pct": 1, "stop_loss_pct": 1},
        underlying_symbol="AMD",
        contract_symbol="AMDUSDT",
    )
    bars = (
        _bar(0, "90", "90", "90"),
        _bar(1, "100", "100", "100"),
        _bar(2, "100", "101", "100.5"),
        _bar(3, "100", "101", "100.5"),
    )

    context = current_shadow_box(
        bars,
        (),
        config,
        point_size=Decimal("0.01"),
        current_price=Decimal("100.5"),
        evaluated_at=NOW,
    )

    assert context.reason_code is None
    assert context.level is not None
    assert context.level.high == Decimal("101")
    assert context.level.low == Decimal("96")


def test_current_shadow_box_fails_closed_when_closed_bars_are_stale() -> None:
    config = build_shadow_config(
        {
            "engine_key": "martingale_tp4",
            "parameters": _catalog_parameters(AutoBoxRange=0, BoxLength=2),
        },
        {"position_size_pct": 1, "stop_loss_pct": 1},
        underlying_symbol="AMD",
        contract_symbol="AMDUSDT",
    )
    stale = tuple(
        replace(
            (bar := _bar(index, "99", "101", "100")),
            open_time=bar.open_time - 86_400_000,
            close_time=bar.close_time - 86_400_000,
        )
        for index in range(4)
    )

    context = current_shadow_box(
        stale,
        (),
        config,
        point_size=Decimal("0.01"),
        current_price=Decimal("100"),
        evaluated_at=NOW,
    )

    assert context.level is None
    assert context.reason_code == "tiger_closed_bars_stale"
