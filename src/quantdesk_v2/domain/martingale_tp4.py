"""Configuration compatibility layer for the Martingale TP4 strategy.

The model keeps the original MQ4 operator inputs configurable while separating
legacy terminal-only values from deterministic strategy semantics and live-risk
limits.  It has no web, persistence, Tiger, or Binance dependency.
"""

from __future__ import annotations

import csv
import io
from collections.abc import Mapping
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

TradingMode = Literal["auto", "recovery", "grid"]


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class MarketDataConfig(_StrictModel):
    signal_source: Literal["tiger"] = "tiger"
    underlying_symbol: str = Field(min_length=1, max_length=32)
    trade_sessions: tuple[Literal["pre_market", "regular", "after_hours", "overnight"], ...] = (
        Field(default=("regular",), min_length=1, max_length=4)
    )
    adjustment: Literal["none", "forward"] = "none"
    execution_source: Literal["binance"] = "binance"
    contract_symbol: str = Field(min_length=5, max_length=32)
    maximum_tiger_age_seconds: int = Field(default=15, ge=1, le=900)
    maximum_binance_age_seconds: int = Field(default=5, ge=1, le=120)
    maximum_clock_skew_seconds: int = Field(default=10, ge=0, le=300)
    maximum_basis_bps: Decimal = Field(default=Decimal("100"), gt=0, le=10_000)

    @model_validator(mode="after")
    def normalize_symbols(self) -> MarketDataConfig:
        underlying = self.underlying_symbol.strip().upper()
        contract = self.contract_symbol.strip().upper()
        if not underlying.replace(".", "").replace("-", "").isalnum():
            raise ValueError("underlying_symbol contains unsupported characters")
        if not contract.isalnum() or not contract.endswith("USDT"):
            raise ValueError("contract_symbol must be a Binance USDT contract")
        if len(set(self.trade_sessions)) != len(self.trade_sessions):
            raise ValueError("trade_sessions must not contain duplicates")
        object.__setattr__(self, "underlying_symbol", underlying)
        object.__setattr__(self, "contract_symbol", contract)
        return self


class SizingConfig(_StrictModel):
    initial_lot: Decimal = Field(default=Decimal("0.01"), gt=0)
    autolot: bool = False
    autolot_balance_unit: Decimal = Field(default=Decimal("10000"), gt=0)
    lot_multiplier: Decimal = Field(default=Decimal("2"), gt=0, le=100)
    max_lot: Decimal = Field(default=Decimal("100"), gt=0)
    max_orders: int = Field(default=16, ge=1, le=100)


class LadderConfig(_StrictModel):
    distance_points: Decimal = Field(default=Decimal("150"), gt=0)
    grid_drift_order_count: int = Field(default=100, ge=1, le=100_000)


class ExecutionConfig(_StrictModel):
    max_spread_points: Decimal = Field(default=Decimal("50"), ge=0)


class TakeProfitConfig(_StrictModel):
    base_points: Decimal = Field(default=Decimal("100"), gt=0)
    tier2_min_orders: int = Field(default=2, ge=1, le=100_000)
    tier2_points: Decimal = Field(default=Decimal("80"), gt=0)
    tier3_min_orders: int = Field(default=5, ge=1, le=100_000)
    tier3_points: Decimal = Field(default=Decimal("50"), gt=0)
    tier4_min_orders: int = Field(default=7, ge=1, le=100_000)
    tier4_points: Decimal = Field(default=Decimal("30"), gt=0)

    def points_for_order_count(self, order_count: int) -> Decimal:
        """Apply the MQ4 sequential override order exactly."""

        points = self.base_points
        if order_count >= self.tier2_min_orders:
            points = self.tier2_points
        if order_count >= self.tier3_min_orders:
            points = self.tier3_points
        if order_count >= self.tier4_min_orders:
            points = self.tier4_points
        return points


class StopConfig(_StrictModel):
    basket_loss_currency: Decimal = Field(default=Decimal("0"), ge=0)
    catastrophe_stop_pct: Decimal | None = Field(default=None, gt=0, le=100)


class TrailingConfig(_StrictModel):
    start_points: Decimal = Field(default=Decimal("600"), ge=0)
    distance_points: Decimal = Field(default=Decimal("100"), gt=0)

    @model_validator(mode="after")
    def validate_distance(self) -> TrailingConfig:
        if self.start_points and self.distance_points >= self.start_points:
            raise ValueError("trailing distance must be lower than trailing start")
        return self

    def active_for_take_profit(self, take_profit_points: Decimal) -> bool:
        return self.start_points > 0 and self.start_points < take_profit_points


class OverlapConfig(_StrictModel):
    enabled: bool = True
    min_orders: int = Field(default=7, ge=2, le=100_000)
    excess_percent: Decimal = Field(default=Decimal("11"), ge=0, le=1000)

    @property
    def required_coverage_percent(self) -> Decimal:
        return Decimal("100") + self.excess_percent


class SessionConfig(_StrictModel):
    timezone: str = Field(default="exchange", min_length=1, max_length=64)
    start_hour: int = Field(default=1, ge=0, le=23)
    end_hour: int = Field(default=23, ge=0, le=23)

    def allows_new_auto_cycle(self, hour: int) -> bool:
        if not 0 <= hour <= 23:
            raise ValueError("hour must be between 0 and 23")
        if self.start_hour == 0 or self.end_hour == 0 or self.start_hour == self.end_hour:
            return True
        if self.start_hour < self.end_hour:
            return self.start_hour <= hour < self.end_hour
        return hour >= self.start_hour or hour < self.end_hour


class BoxConfig(_StrictModel):
    timeframe: Literal["1m", "5m", "15m", "30m", "1h"] = "15m"
    length: int = Field(default=22, ge=2, le=999)
    fixed_range_points: Decimal = Field(default=Decimal("30"), gt=0)
    auto_range: bool = True
    market_adaptive: bool = False
    daily_atr_period: int = Field(default=30, ge=2, le=365)
    daily_atr_factor: Decimal = Field(default=Decimal("0.2"), gt=0, le=10)
    buffer_points: Decimal = Field(default=Decimal("5"), ge=0)


class LegacyDisplayConfig(_StrictModel):
    show_stat: bool = True
    show_buttons_close: bool = True
    show_main_setting: bool = True


class CompatibilityConfig(_StrictModel):
    magic: int = Field(default=201800, ge=0)
    section_points: Decimal = Field(default=Decimal("1000"), ge=0)
    section_policy: Literal["disabled", "legacy_external_range"] = "disabled"
    display: LegacyDisplayConfig = LegacyDisplayConfig()


class StrategyParameters(_StrictModel):
    mode: TradingMode = "auto"
    new_cycle: bool = True
    sizing: SizingConfig = SizingConfig()
    ladder: LadderConfig = LadderConfig()
    execution: ExecutionConfig = ExecutionConfig()
    take_profit: TakeProfitConfig = TakeProfitConfig()
    stop: StopConfig = StopConfig()
    trailing: TrailingConfig = TrailingConfig()
    overlap: OverlapConfig = OverlapConfig()
    session: SessionConfig = SessionConfig()
    box: BoxConfig = BoxConfig()
    compatibility: CompatibilityConfig = CompatibilityConfig()


class LiveRiskConfig(_StrictModel):
    max_cycle_loss_pct: Decimal = Field(gt=0, le=100)
    max_cycle_margin_pct: Decimal = Field(gt=0, le=100)
    max_cycle_notional: Decimal | None = Field(default=None, gt=0)
    minimum_liquidation_buffer_pct: Decimal = Field(gt=0, le=100)
    daily_loss_limit_pct: Decimal = Field(gt=0, le=100)
    additions_enabled: bool = True


class MartingaleTp4Config(_StrictModel):
    schema_version: Literal[2] = 2
    strategy_kind: Literal["basket_strategy"] = "basket_strategy"
    engine_key: Literal["martingale_tp4"] = "martingale_tp4"
    market: Literal["BINANCE_TRADIFI_PERPETUAL"] = "BINANCE_TRADIFI_PERPETUAL"
    market_data: MarketDataConfig
    parameters: StrategyParameters = StrategyParameters()
    live_risk: LiveRiskConfig


class Mq4Inputs(BaseModel):
    """All source inputs, preserving their original MQL variable names."""

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    choose_trading: TradingMode = Field(default="auto", alias="ChooseTrading")
    new_cycle: bool = Field(default=True, alias="NewCycle")
    lot: Decimal = Field(default=Decimal("0.01"), gt=0, alias="Lot")
    autolot: bool = Field(default=False, alias="Autolot")
    autolot_size: Decimal = Field(default=Decimal("10000"), gt=0, alias="Autolotsize")
    lot_multiplier: Decimal = Field(default=Decimal("2"), gt=0, le=100, alias="mm")
    max_lot: Decimal = Field(default=Decimal("100"), gt=0, alias="MaxLot")
    max_orders: int = Field(default=16, ge=1, le=100, alias="MaxOrders")
    grid_drift: int = Field(default=100, ge=1, le=100_000, alias="GridDrift")
    max_spread: Decimal = Field(default=Decimal("50"), ge=0, alias="MaxSpred")
    distance: Decimal = Field(default=Decimal("150"), gt=0, alias="Distance")
    take_profit: Decimal = Field(default=Decimal("100"), gt=0, alias="TP")
    orders_for_tp2: int = Field(default=2, ge=1, alias="Kol_Ord_for_TP2")
    take_profit2: Decimal = Field(default=Decimal("80"), gt=0, alias="TP2")
    orders_for_tp3: int = Field(default=5, ge=1, alias="Kol_Ord_for_TP3")
    take_profit3: Decimal = Field(default=Decimal("50"), gt=0, alias="TP3")
    orders_for_tp4: int = Field(default=7, ge=1, alias="Kol_Ord_for_TP4")
    take_profit4: Decimal = Field(default=Decimal("30"), gt=0, alias="TP4")
    stop_loss_currency: Decimal = Field(default=Decimal("0"), ge=0, alias="SL_Dollar")
    trail_start: Decimal = Field(default=Decimal("600"), ge=0, alias="TrailStart")
    trail_distance: Decimal = Field(default=Decimal("100"), gt=0, alias="TrailDistance")
    overlap: bool = Field(default=True, alias="Overlap")
    overlap_order_number: int = Field(default=7, ge=2, alias="OverlapOrderNumber")
    overlap_percent: Decimal = Field(default=Decimal("11"), ge=0, alias="OverlapPercent")
    start_hour: int = Field(default=1, ge=0, le=23, alias="Start_Hour")
    end_hour: int = Field(default=23, ge=0, le=23, alias="End_Hour")
    show_stat: bool = Field(default=True, alias="ShowStat")
    show_buttons_close: bool = Field(default=True, alias="ShowButton")
    show_main_setting: bool = Field(default=True, alias="ShowMainSetting")
    magic: int = Field(default=201800, ge=0, alias="Magic")
    section: Decimal = Field(default=Decimal("1000"), ge=0, alias="Section")
    box_length: int = Field(default=22, ge=2, le=999, alias="BoxLength")
    box_timeframe: Literal["1m", "5m", "15m", "30m", "1h"] = Field(
        default="15m", alias="BoxTimeFrame"
    )
    box_range: Decimal = Field(default=Decimal("30"), gt=0, alias="BoxRange")
    auto_box_range: bool = Field(default=True, alias="AutoBoxRange")
    auto_box_range_market_adaptive: bool = Field(
        default=False, alias="AutoBoxRangeMarketAdaptive"
    )
    auto_box_range_daily_atr_period: int = Field(
        default=30, ge=2, le=365, alias="AutoBoxRangeDailyATRperiod"
    )
    auto_box_range_daily_atr_factor: Decimal = Field(
        default=Decimal("0.2"), gt=0, le=10, alias="AutoBoxRangeDailyATRfactor"
    )
    box_buffer_pips: Decimal = Field(default=Decimal("5"), ge=0, alias="BoxBufferPips")

    @field_validator("choose_trading", mode="before")
    @classmethod
    def normalize_trading_mode(cls, value: object) -> object:
        modes = {0: "auto", 1: "recovery", 2: "grid", "0": "auto", "1": "recovery", "2": "grid"}
        return modes.get(value, value)


def strategy_parameters_from_mq4(inputs: Mq4Inputs) -> StrategyParameters:
    return StrategyParameters(
        mode=inputs.choose_trading,
        new_cycle=inputs.new_cycle,
        sizing=SizingConfig(
            initial_lot=inputs.lot,
            autolot=inputs.autolot,
            autolot_balance_unit=inputs.autolot_size,
            lot_multiplier=inputs.lot_multiplier,
            max_lot=inputs.max_lot,
            max_orders=inputs.max_orders,
        ),
        ladder=LadderConfig(
            distance_points=inputs.distance,
            grid_drift_order_count=inputs.grid_drift,
        ),
        execution=ExecutionConfig(max_spread_points=inputs.max_spread),
        take_profit=TakeProfitConfig(
            base_points=inputs.take_profit,
            tier2_min_orders=inputs.orders_for_tp2,
            tier2_points=inputs.take_profit2,
            tier3_min_orders=inputs.orders_for_tp3,
            tier3_points=inputs.take_profit3,
            tier4_min_orders=inputs.orders_for_tp4,
            tier4_points=inputs.take_profit4,
        ),
        stop=StopConfig(basket_loss_currency=inputs.stop_loss_currency),
        trailing=TrailingConfig(
            start_points=inputs.trail_start,
            distance_points=inputs.trail_distance,
        ),
        overlap=OverlapConfig(
            enabled=inputs.overlap,
            min_orders=inputs.overlap_order_number,
            excess_percent=inputs.overlap_percent,
        ),
        session=SessionConfig(start_hour=inputs.start_hour, end_hour=inputs.end_hour),
        box=BoxConfig(
            timeframe=inputs.box_timeframe,
            length=inputs.box_length,
            fixed_range_points=inputs.box_range,
            auto_range=inputs.auto_box_range,
            market_adaptive=inputs.auto_box_range_market_adaptive,
            daily_atr_period=inputs.auto_box_range_daily_atr_period,
            daily_atr_factor=inputs.auto_box_range_daily_atr_factor,
            buffer_points=inputs.box_buffer_pips,
        ),
        compatibility=CompatibilityConfig(
            magic=inputs.magic,
            section_points=inputs.section,
            display=LegacyDisplayConfig(
                show_stat=inputs.show_stat,
                show_buttons_close=inputs.show_buttons_close,
                show_main_setting=inputs.show_main_setting,
            ),
        ),
    )


def mq4_inputs_from_catalog_parameters(parameters: Mapping[str, Any]) -> Mq4Inputs:
    """Restore typed MQ4 inputs from the strategy center's numeric JSON schema."""

    raw = dict(parameters)
    try:
        timeframe_minutes = int(raw.pop("BoxTimeFrameMinutes"))
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("BoxTimeFrameMinutes is required") from exc
    timeframe = {1: "1m", 5: "5m", 15: "15m", 30: "30m", 60: "1h"}.get(
        timeframe_minutes
    )
    if timeframe is None:
        raise ValueError("BoxTimeFrameMinutes must be 1, 5, 15, 30, or 60")
    raw["BoxTimeFrame"] = timeframe
    return Mq4Inputs.model_validate(raw)


def strategy_parameters_from_catalog_parameters(
    parameters: Mapping[str, Any],
) -> StrategyParameters:
    return strategy_parameters_from_mq4(mq4_inputs_from_catalog_parameters(parameters))


def mq4_inputs_from_strategy_parameters(parameters: StrategyParameters) -> Mq4Inputs:
    sizing = parameters.sizing
    take_profit = parameters.take_profit
    box = parameters.box
    compatibility = parameters.compatibility
    return Mq4Inputs(
        ChooseTrading=parameters.mode,
        NewCycle=parameters.new_cycle,
        Lot=sizing.initial_lot,
        Autolot=sizing.autolot,
        Autolotsize=sizing.autolot_balance_unit,
        mm=sizing.lot_multiplier,
        MaxLot=sizing.max_lot,
        MaxOrders=sizing.max_orders,
        GridDrift=parameters.ladder.grid_drift_order_count,
        MaxSpred=parameters.execution.max_spread_points,
        Distance=parameters.ladder.distance_points,
        TP=take_profit.base_points,
        Kol_Ord_for_TP2=take_profit.tier2_min_orders,
        TP2=take_profit.tier2_points,
        Kol_Ord_for_TP3=take_profit.tier3_min_orders,
        TP3=take_profit.tier3_points,
        Kol_Ord_for_TP4=take_profit.tier4_min_orders,
        TP4=take_profit.tier4_points,
        SL_Dollar=parameters.stop.basket_loss_currency,
        TrailStart=parameters.trailing.start_points,
        TrailDistance=parameters.trailing.distance_points,
        Overlap=parameters.overlap.enabled,
        OverlapOrderNumber=parameters.overlap.min_orders,
        OverlapPercent=parameters.overlap.excess_percent,
        Start_Hour=parameters.session.start_hour,
        End_Hour=parameters.session.end_hour,
        ShowStat=compatibility.display.show_stat,
        ShowButton=compatibility.display.show_buttons_close,
        ShowMainSetting=compatibility.display.show_main_setting,
        Magic=compatibility.magic,
        Section=compatibility.section_points,
        BoxLength=box.length,
        BoxTimeFrame=box.timeframe,
        BoxRange=box.fixed_range_points,
        AutoBoxRange=box.auto_range,
        AutoBoxRangeMarketAdaptive=box.market_adaptive,
        AutoBoxRangeDailyATRperiod=box.daily_atr_period,
        AutoBoxRangeDailyATRfactor=box.daily_atr_factor,
        BoxBufferPips=box.buffer_points,
    )


_LEGACY_CSV_FIELDS = (
    "ChooseTrading",
    "Lot",
    "Autolot",
    "Autolotsize",
    "mm",
    "MaxLot",
    "MaxOrders",
    "GridDrift",
    "Distance",
    "TP",
    "SL_Dollar",
    "TrailStart",
    "TrailDistance",
    "Overlap",
    "OverlapOrderNumber",
    "OverlapPercent",
    "Start_Hour",
    "End_Hour",
    "Magic",
    "NewCycle",
)


def parse_mq4_settings_csv(value: str, *, defaults: Mq4Inputs | None = None) -> Mq4Inputs:
    """Parse the exact 20-column CSV emitted by ``SetSettings`` in the EA."""

    rows = list(csv.reader(io.StringIO(value.strip())))
    if len(rows) != 1 or len(rows[0]) != len(_LEGACY_CSV_FIELDS):
        raise ValueError("MQ4 settings CSV must contain exactly 20 columns")
    raw = (defaults or Mq4Inputs()).model_dump(by_alias=True, mode="json")
    raw.update(dict(zip(_LEGACY_CSV_FIELDS, rows[0], strict=True)))
    return Mq4Inputs.model_validate(raw)


def dump_mq4_settings_csv(inputs: Mq4Inputs) -> str:
    raw = inputs.model_dump(by_alias=True, mode="json")
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="")
    writer.writerow([raw[field] for field in _LEGACY_CSV_FIELDS])
    return buffer.getvalue()


class RiskPreview(_StrictModel):
    leg_quantities: tuple[Decimal, ...]
    cumulative_quantity: Decimal
    gross_notional: Decimal
    estimated_initial_margin: Decimal
    warnings: tuple[str, ...]


def preview_configuration_risk(
    config: MartingaleTp4Config,
    *,
    account_equity: Decimal,
    reference_price: Decimal,
    leverage: Decimal,
) -> RiskPreview:
    """Return transparent exposure math without approving a live deployment."""

    if account_equity <= 0:
        raise ValueError("account_equity must be positive")
    if reference_price <= 0:
        raise ValueError("reference_price must be positive")
    if leverage <= 0:
        raise ValueError("leverage must be positive")

    parameters = config.parameters
    sizing = parameters.sizing
    initial = sizing.initial_lot
    if sizing.autolot:
        initial = account_equity / sizing.autolot_balance_unit * sizing.initial_lot
    initial = min(initial, sizing.max_lot)

    quantities: list[Decimal] = []
    for index in range(sizing.max_orders):
        quantity = initial * (sizing.lot_multiplier**index)
        quantities.append(min(quantity, sizing.max_lot))

    cumulative = sum(quantities, Decimal("0"))
    notional = cumulative * reference_price
    margin = notional / leverage
    warnings: list[str] = []
    if sizing.lot_multiplier > 1:
        warnings.append("exponential_position_growth")
    if parameters.stop.basket_loss_currency == 0:
        warnings.append("basket_currency_stop_disabled")
    if parameters.stop.catastrophe_stop_pct is None:
        warnings.append("catastrophe_stop_disabled")
    if (
        parameters.mode in {"auto", "recovery"}
        and parameters.ladder.grid_drift_order_count > sizing.max_orders
    ):
        warnings.append("grid_drift_unreachable")
    if parameters.overlap.enabled and parameters.overlap.min_orders > sizing.max_orders:
        warnings.append("overlap_unreachable")
    if not any(
        parameters.trailing.active_for_take_profit(points)
        for points in (
            parameters.take_profit.base_points,
            parameters.take_profit.tier2_points,
            parameters.take_profit.tier3_points,
            parameters.take_profit.tier4_points,
        )
    ):
        warnings.append("trailing_inactive_for_all_take_profit_tiers")
    if parameters.compatibility.section_policy != "disabled":
        warnings.append("legacy_chart_viewport_dependency")
    if margin / account_equity * Decimal("100") > config.live_risk.max_cycle_margin_pct:
        warnings.append("configured_cycle_margin_limit_exceeded")
    if config.live_risk.max_cycle_notional is not None and notional > config.live_risk.max_cycle_notional:
        warnings.append("configured_cycle_notional_limit_exceeded")

    return RiskPreview(
        leg_quantities=tuple(quantities),
        cumulative_quantity=cumulative,
        gross_notional=notional,
        estimated_initial_margin=margin,
        warnings=tuple(warnings),
    )
