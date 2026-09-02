from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import ValidationError

from quantdesk_v2.domain.martingale_tp4 import (
    MartingaleTp4Config,
    preview_configuration_risk,
)


def _config(**parameter_overrides: object) -> MartingaleTp4Config:
    parameters: dict[str, object] = {
        "mode": "grid",
        "new_cycle": True,
        "sizing": {
            "method": "fixed_quantity",
            "initial_value": "0.01",
            "balance_unit": "10000",
            "multiplier": "2",
            "max_leg_quantity": "100",
            "max_legs": 4,
        },
        "ladder": {
            "distance_method": "tick_multiple",
            "distance_value": "150",
            "grid_drift_leg": 4,
        },
        "take_profit": {
            "method": "tick_multiple",
            "tiers": [
                {"min_legs": 1, "value": "100"},
                {"min_legs": 2, "value": "80"},
                {"min_legs": 4, "value": "50"},
            ],
        },
        "stop": {"basket_loss_amount": "0", "catastrophe_stop_pct": None},
        "trailing": {"enabled": False, "start": "600", "distance": "100"},
        "overlap": {"enabled": True, "min_legs": 4, "coverage_percent": "111"},
        "session": {"timezone": "UTC", "start_hour": 1, "end_hour": 23},
        "box": {
            "timeframe": "15m",
            "minimum_bars": 22,
            "range_method": "daily_atr",
            "daily_atr_period": 30,
            "daily_atr_factor": "0.2",
            "buffer_method": "tick_multiple",
            "buffer_value": "5",
        },
    }
    parameters.update(parameter_overrides)
    return MartingaleTp4Config.model_validate(
        {
            "schema_version": 1,
            "strategy_kind": "basket_strategy",
            "engine_key": "martingale_tp4",
            "market": "BINANCE_TRADIFI_PERPETUAL",
            "market_data": {
                "signal_source": "tiger",
                "underlying_symbol": "amd",
                "trade_sessions": ["regular"],
                "adjustment": "none",
                "execution_source": "binance",
                "contract_symbol": "amdusdt",
                "maximum_tiger_age_seconds": 15,
                "maximum_binance_age_seconds": 5,
                "maximum_clock_skew_seconds": 10,
                "maximum_basis_bps": "100",
            },
            "parameters": parameters,
            "live_risk": {
                "max_cycle_loss_pct": "1",
                "max_cycle_margin_pct": "10",
                "max_cycle_notional": None,
                "minimum_liquidation_buffer_pct": "8",
                "daily_loss_limit_pct": "3",
                "additions_enabled": True,
            },
        }
    )


def test_configuration_preserves_operator_parameters_and_normalizes_symbols() -> None:
    config = _config()

    assert config.market_data.underlying_symbol == "AMD"
    assert config.market_data.contract_symbol == "AMDUSDT"
    assert config.parameters.sizing.multiplier == Decimal("2")
    assert config.parameters.take_profit.tiers[-1].min_legs == 4


def test_configuration_rejects_unreachable_cross_parameter_values() -> None:
    with pytest.raises(ValidationError, match="grid_drift_leg"):
        _config(
            ladder={
                "distance_method": "tick_multiple",
                "distance_value": "150",
                "grid_drift_leg": 5,
            }
        )


def test_risk_preview_makes_exponential_growth_explicit() -> None:
    preview = preview_configuration_risk(
        _config(),
        account_equity=Decimal("10000"),
        reference_price=Decimal("100"),
        leverage=Decimal("10"),
    )

    assert preview.leg_quantities == (
        Decimal("0.01"),
        Decimal("0.02"),
        Decimal("0.04"),
        Decimal("0.08"),
    )
    assert preview.cumulative_quantity == Decimal("0.15")
    assert preview.gross_notional == Decimal("15.00")
    assert preview.estimated_initial_margin == Decimal("1.50")
    assert "exponential_position_growth" in preview.warnings
    assert "catastrophe_stop_disabled" in preview.warnings


def test_equity_ratio_sizing_is_still_configurable() -> None:
    config = _config(
        sizing={
            "method": "equity_ratio",
            "initial_value": "0.01",
            "balance_unit": "10000",
            "multiplier": "1",
            "max_leg_quantity": "10",
            "max_legs": 4,
        }
    )
    preview = preview_configuration_risk(
        config,
        account_equity=Decimal("20000"),
        reference_price=Decimal("100"),
        leverage=Decimal("10"),
    )

    assert preview.leg_quantities[0] == Decimal("0.02")
