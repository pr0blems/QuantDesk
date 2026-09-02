from __future__ import annotations

from decimal import Decimal

import pytest

from quantdesk_v2.domain.martingale_tp4 import (
    MartingaleTp4Config,
    Mq4Inputs,
    dump_mq4_settings_csv,
    mq4_inputs_from_strategy_parameters,
    parse_mq4_settings_csv,
    preview_configuration_risk,
    strategy_parameters_from_catalog_parameters,
    strategy_parameters_from_mq4,
)


def _config(inputs: Mq4Inputs | None = None) -> MartingaleTp4Config:
    return MartingaleTp4Config.model_validate(
        {
            "schema_version": 2,
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
            "parameters": strategy_parameters_from_mq4(inputs or Mq4Inputs()).model_dump(
                mode="json"
            ),
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


def test_default_mq4_parameters_round_trip_without_semantic_drift() -> None:
    original = Mq4Inputs()

    restored = mq4_inputs_from_strategy_parameters(strategy_parameters_from_mq4(original))

    assert restored == original
    assert restored.grid_drift == 100
    assert restored.max_orders == 16
    assert restored.overlap_percent == Decimal("11")
    assert strategy_parameters_from_mq4(restored).overlap.required_coverage_percent == 111


def test_numeric_mq4_mode_values_are_accepted() -> None:
    assert Mq4Inputs.model_validate({"ChooseTrading": 0}).choose_trading == "auto"
    assert Mq4Inputs.model_validate({"ChooseTrading": "1"}).choose_trading == "recovery"
    assert Mq4Inputs.model_validate({"ChooseTrading": 2}).choose_trading == "grid"


def test_legacy_settings_csv_matches_the_exact_source_column_order() -> None:
    original = Mq4Inputs(ChooseTrading="recovery", Lot="0.03", OverlapPercent="17")

    encoded = dump_mq4_settings_csv(original)
    restored = parse_mq4_settings_csv(encoded)

    assert len(encoded.split(",")) == 20
    assert restored.choose_trading == "recovery"
    assert restored.lot == Decimal("0.03")
    assert restored.overlap_percent == Decimal("17")
    # The MQ4 SetSettings function does not persist TP2-4 or MaxSpred, so the
    # parser deliberately restores those values from the supplied defaults.
    assert restored.take_profit2 == Mq4Inputs().take_profit2
    assert restored.max_spread == Mq4Inputs().max_spread


def test_invalid_legacy_settings_csv_is_rejected() -> None:
    with pytest.raises(ValueError, match="exactly 20 columns"):
        parse_mq4_settings_csv("0,0.01,true")


def test_session_window_only_controls_new_auto_cycles() -> None:
    session = strategy_parameters_from_mq4(Mq4Inputs(Start_Hour=22, End_Hour=3)).session

    assert session.allows_new_auto_cycle(23)
    assert session.allows_new_auto_cycle(2)
    assert not session.allows_new_auto_cycle(12)
    assert strategy_parameters_from_mq4(
        Mq4Inputs(Start_Hour=0, End_Hour=0)
    ).session.allows_new_auto_cycle(12)


def test_default_trailing_values_are_configured_but_inactive_for_all_tp_tiers() -> None:
    parameters = strategy_parameters_from_mq4(Mq4Inputs())

    assert not parameters.trailing.active_for_take_profit(parameters.take_profit.base_points)
    assert not parameters.trailing.active_for_take_profit(parameters.take_profit.tier4_points)


def test_risk_preview_uses_exact_autolot_formula_and_reports_unreachable_features() -> None:
    preview = preview_configuration_risk(
        _config(Mq4Inputs(Autolot=True)),
        account_equity=Decimal("20000"),
        reference_price=Decimal("100"),
        leverage=Decimal("10"),
    )

    assert preview.leg_quantities[:4] == (
        Decimal("0.02"),
        Decimal("0.04"),
        Decimal("0.08"),
        Decimal("0.16"),
    )
    assert "grid_drift_unreachable" in preview.warnings
    assert "trailing_inactive_for_all_take_profit_tiers" in preview.warnings
    assert "basket_currency_stop_disabled" in preview.warnings


def test_configuration_normalizes_market_symbols() -> None:
    config = _config()

    assert config.market_data.underlying_symbol == "AMD"
    assert config.market_data.contract_symbol == "AMDUSDT"


def test_strategy_center_parameters_restore_mq4_mode_switches_and_timeframe() -> None:
    parameters = {
        "ChooseTrading": 2,
        "NewCycle": 1,
        "Lot": 0.01,
        "Autolot": 0,
        "Autolotsize": 10000,
        "mm": 2,
        "MaxLot": 100,
        "MaxOrders": 16,
        "GridDrift": 100,
        "MaxSpred": 50,
        "Distance": 150,
        "TP": 100,
        "Kol_Ord_for_TP2": 2,
        "TP2": 80,
        "Kol_Ord_for_TP3": 5,
        "TP3": 50,
        "Kol_Ord_for_TP4": 7,
        "TP4": 30,
        "SL_Dollar": 0,
        "TrailStart": 600,
        "TrailDistance": 100,
        "Overlap": 1,
        "OverlapOrderNumber": 7,
        "OverlapPercent": 11,
        "Start_Hour": 1,
        "End_Hour": 23,
        "Magic": 201800,
        "Section": 1000,
        "ShowStat": 1,
        "ShowButton": 1,
        "ShowMainSetting": 1,
        "BoxLength": 22,
        "BoxTimeFrameMinutes": 60,
        "BoxRange": 30,
        "AutoBoxRange": 1,
        "AutoBoxRangeDailyATRperiod": 30,
        "AutoBoxRangeDailyATRfactor": 0.2,
        "BoxBufferPips": 5,
    }

    restored = strategy_parameters_from_catalog_parameters(parameters)

    assert restored.mode == "grid"
    assert restored.new_cycle is True
    assert restored.box.timeframe == "1h"
    assert restored.overlap.enabled is True
