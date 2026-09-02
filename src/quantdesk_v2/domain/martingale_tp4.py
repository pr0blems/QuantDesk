"""Validated domain configuration for the Martingale TP4 basket strategy.

The original MQ4 expert adviser exposes its parameters to the operator.  This
module preserves that property while separating strategy semantics from live
account limits.  It deliberately has no FastAPI, SQLAlchemy, Tiger, or Binance
dependency so the same configuration is used by validation, backtests, shadow
execution, and the future live basket runtime.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


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
    method: Literal["fixed_quantity", "equity_ratio"] = "fixed_quantity"
    initial_value: Decimal = Field(gt=0)
    balance_unit: Decimal = Field(default=Decimal("10000"), gt=0)
    multiplier: Decimal = Field(default=Decimal("2"), gt=0, le=100)
    max_leg_quantity: Decimal = Field(gt=0)
    max_legs: int = Field(default=16, ge=1, le=100)


class LadderConfig(_StrictModel):
    distance_method: Literal["tick_multiple", "bps", "atr_multiple"] = "tick_multiple"
    distance_value: Decimal = Field(gt=0)
    grid_drift_leg: int | None = Field(default=None, ge=1, le=100)


class TakeProfitTier(_StrictModel):
    min_legs: int = Field(ge=1, le=100)
    value: Decimal = Field(gt=0)


class TakeProfitConfig(_StrictModel):
    method: Literal["tick_multiple", "bps", "atr_multiple", "amount"] = "tick_multiple"
    tiers: tuple[TakeProfitTier, ...] = Field(min_length=1, max_length=16)


class StopConfig(_StrictModel):
    basket_loss_amount: Decimal = Field(default=Decimal("0"), ge=0)
    catastrophe_stop_pct: Decimal | None = Field(default=None, gt=0, le=100)


class TrailingConfig(_StrictModel):
    enabled: bool = False
    start: Decimal = Field(default=Decimal("600"), gt=0)
    distance: Decimal = Field(default=Decimal("100"), gt=0)

    @model_validator(mode="after")
    def validate_reachable_distance(self) -> TrailingConfig:
        if self.enabled and self.distance >= self.start:
            raise ValueError("trailing distance must be lower than trailing start")
        return self


class OverlapConfig(_StrictModel):
    enabled: bool = True
    min_legs: int = Field(default=7, ge=2, le=100)
    coverage_percent: Decimal = Field(default=Decimal("111"), gt=0, le=1000)


class SessionConfig(_StrictModel):
    timezone: str = Field(default="UTC", min_length=1, max_length=64)
    start_hour: int = Field(default=1, ge=0, le=23)
    end_hour: int = Field(default=23, ge=0, le=23)

    @model_validator(mode="after")
    def reject_empty_window(self) -> SessionConfig:
        if self.start_hour == self.end_hour:
            raise ValueError("session start_hour and end_hour must differ")
        return self


class BoxConfig(_StrictModel):
    timeframe: Literal["1m", "5m", "15m", "30m", "1h"] = "15m"
    minimum_bars: int = Field(default=22, ge=2, le=999)
    range_method: Literal["daily_atr", "fixed"] = "daily_atr"
    daily_atr_period: int = Field(default=30, ge=2, le=365)
    daily_atr_factor: Decimal = Field(default=Decimal("0.2"), gt=0, le=10)
    fixed_range_value: Decimal | None = Field(default=None, gt=0)
    buffer_method: Literal["tick_multiple", "bps"] = "tick_multiple"
    buffer_value: Decimal = Field(default=Decimal("5"), ge=0)

    @model_validator(mode="after")
    def require_fixed_range(self) -> BoxConfig:
        if self.range_method == "fixed" and self.fixed_range_value is None:
            raise ValueError("fixed_range_value is required for a fixed box range")
        return self


class StrategyParameters(_StrictModel):
    mode: Literal["auto", "recovery", "grid"] = "grid"
    new_cycle: bool = True
    sizing: SizingConfig
    ladder: LadderConfig
    take_profit: TakeProfitConfig
    stop: StopConfig = StopConfig()
    trailing: TrailingConfig = TrailingConfig()
    overlap: OverlapConfig = OverlapConfig()
    session: SessionConfig = SessionConfig()
    box: BoxConfig = BoxConfig()

    @model_validator(mode="after")
    def validate_cross_parameter_semantics(self) -> StrategyParameters:
        max_legs = self.sizing.max_legs
        tiers = self.take_profit.tiers
        thresholds = [tier.min_legs for tier in tiers]
        if thresholds != sorted(set(thresholds)):
            raise ValueError("take-profit tier min_legs must be strictly increasing")
        if thresholds[-1] > max_legs:
            raise ValueError("take-profit tiers must not exceed max_legs")
        if self.overlap.enabled and self.overlap.min_legs > max_legs:
            raise ValueError("overlap min_legs must not exceed max_legs")
        drift = self.ladder.grid_drift_leg
        if drift is not None and drift > max_legs:
            raise ValueError("grid_drift_leg must not exceed max_legs")
        return self


class LiveRiskConfig(_StrictModel):
    max_cycle_loss_pct: Decimal = Field(gt=0, le=100)
    max_cycle_margin_pct: Decimal = Field(gt=0, le=100)
    max_cycle_notional: Decimal | None = Field(default=None, gt=0)
    minimum_liquidation_buffer_pct: Decimal = Field(gt=0, le=100)
    daily_loss_limit_pct: Decimal = Field(gt=0, le=100)
    additions_enabled: bool = True


class MartingaleTp4Config(_StrictModel):
    schema_version: Literal[1] = 1
    strategy_kind: Literal["basket_strategy"] = "basket_strategy"
    engine_key: Literal["martingale_tp4"] = "martingale_tp4"
    market: Literal["BINANCE_TRADIFI_PERPETUAL"] = "BINANCE_TRADIFI_PERPETUAL"
    market_data: MarketDataConfig
    parameters: StrategyParameters
    live_risk: LiveRiskConfig


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

    sizing = config.parameters.sizing
    initial = sizing.initial_value
    if sizing.method == "equity_ratio":
        initial = account_equity / sizing.balance_unit * sizing.initial_value

    quantities: list[Decimal] = []
    for index in range(sizing.max_legs):
        quantity = initial * (sizing.multiplier**index)
        quantities.append(min(quantity, sizing.max_leg_quantity))

    cumulative = sum(quantities, Decimal("0"))
    notional = cumulative * reference_price
    margin = notional / leverage
    warnings: list[str] = []
    if sizing.multiplier > 1:
        warnings.append("exponential_position_growth")
    if config.parameters.stop.basket_loss_amount == 0:
        warnings.append("basket_currency_stop_disabled")
    if config.parameters.stop.catastrophe_stop_pct is None:
        warnings.append("catastrophe_stop_disabled")
    if margin / account_equity * Decimal("100") > config.live_risk.max_cycle_margin_pct:
        warnings.append("configured_cycle_margin_limit_exceeded")
    if (
        config.live_risk.max_cycle_notional is not None
        and notional > config.live_risk.max_cycle_notional
    ):
        warnings.append("configured_cycle_notional_limit_exceeded")

    return RiskPreview(
        leg_quantities=tuple(quantities),
        cumulative_quantity=cumulative,
        gross_notional=notional,
        estimated_initial_margin=margin,
        warnings=tuple(warnings),
    )
