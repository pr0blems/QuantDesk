"""Runtime adapters for source-qualified Martingale TP4 Shadow evaluation."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ...domain.martingale_tp4 import (
    MartingaleTp4Config,
    strategy_parameters_from_catalog_parameters,
)
from ...tiger_market_data import TigerBar
from .market_gate import BinanceExecutionQuote, TigerReferenceQuote
from .replay import BoxLevel, build_box_levels

DEFAULT_SIGNAL_POINT_SIZE = Decimal("0.01")
DEFAULT_MAXIMUM_TIGER_AGE_SECONDS = 15
DEFAULT_MAXIMUM_BINANCE_AGE_SECONDS = 5
DEFAULT_MAXIMUM_CLOCK_SKEW_SECONDS = 10
DEFAULT_MAXIMUM_BASIS_BPS = Decimal("100")
DEFAULT_MAXIMUM_CYCLE_LOSS_PCT = Decimal("1")
DEFAULT_MAXIMUM_CYCLE_MARGIN_PCT = Decimal("10")
DEFAULT_MINIMUM_LIQUIDATION_BUFFER_PCT = Decimal("8")
DEFAULT_DAILY_LOSS_LIMIT_PCT = Decimal("3")


@dataclass(frozen=True, slots=True)
class ShadowBoxContext:
    level: BoxLevel | None
    box_time: int | None
    reason_code: str | None


def _decimal(value: object, *, name: str, positive: bool = True) -> Decimal:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a decimal")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a decimal") from exc
    if not result.is_finite() or (positive and result <= 0):
        raise ValueError(f"{name} must be positive and finite")
    return result


def _timestamp(value: object, *, name: str) -> datetime:
    if isinstance(value, datetime):
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    timestamp = _decimal(value, name=name)
    while timestamp >= Decimal("100000000000"):
        timestamp /= Decimal("1000")
    try:
        return datetime.fromtimestamp(float(timestamp), tz=UTC)
    except (OSError, OverflowError, ValueError) as exc:
        raise ValueError(f"{name} is outside the supported timestamp range") from exc


def build_shadow_config(
    strategy_snapshot: Mapping[str, Any],
    risk_override: Mapping[str, Any],
    *,
    underlying_symbol: str,
    contract_symbol: str,
) -> MartingaleTp4Config:
    """Restore a strict runtime config from one immutable strategy revision."""

    if strategy_snapshot.get("engine_key") != "martingale_tp4":
        raise ValueError("strategy revision is not a martingale_tp4 revision")
    raw_parameters = strategy_snapshot.get("parameters")
    if not isinstance(raw_parameters, Mapping):
        raise ValueError("martingale_tp4 revision parameters are missing")
    parameters = strategy_parameters_from_catalog_parameters(raw_parameters)

    raw_stop = _decimal(
        risk_override.get("stop_loss_pct", 0),
        name="stop_loss_pct",
        positive=False,
    )
    cycle_loss = raw_stop if raw_stop > 0 else DEFAULT_MAXIMUM_CYCLE_LOSS_PCT
    position_size = _decimal(
        risk_override.get("position_size_pct", 1),
        name="position_size_pct",
    )
    cycle_margin = min(
        Decimal("100"), max(DEFAULT_MAXIMUM_CYCLE_MARGIN_PCT, position_size)
    )
    return MartingaleTp4Config.model_validate(
        {
            "market_data": {
                "underlying_symbol": underlying_symbol,
                "contract_symbol": contract_symbol,
                "maximum_tiger_age_seconds": DEFAULT_MAXIMUM_TIGER_AGE_SECONDS,
                "maximum_binance_age_seconds": DEFAULT_MAXIMUM_BINANCE_AGE_SECONDS,
                "maximum_clock_skew_seconds": DEFAULT_MAXIMUM_CLOCK_SKEW_SECONDS,
                "maximum_basis_bps": str(DEFAULT_MAXIMUM_BASIS_BPS),
            },
            "parameters": parameters.model_dump(mode="json"),
            "live_risk": {
                "max_cycle_loss_pct": str(cycle_loss),
                "max_cycle_margin_pct": str(cycle_margin),
                "minimum_liquidation_buffer_pct": str(
                    DEFAULT_MINIMUM_LIQUIDATION_BUFFER_PCT
                ),
                "daily_loss_limit_pct": str(DEFAULT_DAILY_LOSS_LIMIT_PCT),
                "additions_enabled": True,
            },
        }
    )


def binance_tick_size(source_metadata: Mapping[str, Any]) -> Decimal:
    """Read the exact PRICE_FILTER tick size captured by universe sync."""

    filters = source_metadata.get("filters")
    if isinstance(filters, list):
        for item in filters:
            if (
                isinstance(item, Mapping)
                and str(item.get("filterType") or "").upper() == "PRICE_FILTER"
            ):
                return _decimal(item.get("tickSize"), name="Binance PRICE_FILTER.tickSize")
    precision = source_metadata.get("pricePrecision")
    if isinstance(precision, int) and not isinstance(precision, bool) and 0 <= precision <= 18:
        return Decimal("1").scaleb(-precision)
    raise ValueError("Binance execution tick size is unavailable")


def tiger_quote_from_snapshot(snapshot: Mapping[str, Any]) -> TigerReferenceQuote:
    symbol = str(snapshot.get("symbol") or "").strip().upper()
    if not symbol:
        raise ValueError("Tiger snapshot symbol is missing")
    return TigerReferenceQuote(
        symbol=symbol,
        bid=_decimal(snapshot.get("best_bid"), name="Tiger best_bid"),
        ask=_decimal(snapshot.get("best_ask"), name="Tiger best_ask"),
        observed_at=_timestamp(
            snapshot.get("source_timestamp", snapshot.get("captured_at")),
            name="Tiger source_timestamp",
        ),
    )


def binance_quote_from_snapshot(snapshot: Mapping[str, Any]) -> BinanceExecutionQuote:
    symbol = str(snapshot.get("symbol") or "").strip().upper()
    if not symbol:
        raise ValueError("Binance snapshot symbol is missing")
    bid = _decimal(snapshot.get("best_bid"), name="Binance best_bid")
    ask = _decimal(snapshot.get("best_ask"), name="Binance best_ask")
    mark_value = snapshot.get("mark_price", snapshot.get("mid_price"))
    return BinanceExecutionQuote(
        symbol=symbol,
        bid=bid,
        ask=ask,
        mark=_decimal(mark_value, name="Binance mark/mid price"),
        observed_at=_timestamp(snapshot.get("captured_at"), name="Binance captured_at"),
    )


def shadow_event_id(
    tiger_snapshot: Mapping[str, Any] | None,
    binance_snapshot: Mapping[str, Any] | None,
) -> str:
    """Build one stable upstream identity without using wall-clock time."""

    tiger_id = (
        tiger_snapshot.get("source_timestamp", tiger_snapshot.get("captured_at"))
        if tiger_snapshot is not None
        else "missing"
    )
    binance_id = (
        binance_snapshot.get("last_update_id", binance_snapshot.get("captured_at"))
        if binance_snapshot is not None
        else "missing"
    )
    return f"tiger:{tiger_id}|binance:{binance_id}"


def timeframe_milliseconds(timeframe: str) -> int:
    try:
        return {
            "1m": 60_000,
            "5m": 300_000,
            "15m": 900_000,
            "30m": 1_800_000,
            "1h": 3_600_000,
        }[timeframe]
    except KeyError as exc:
        raise ValueError("unsupported martingale timeframe") from exc


def current_shadow_box(
    signal_bars: Sequence[TigerBar],
    daily_bars: Sequence[TigerBar],
    config: MartingaleTp4Config,
    *,
    point_size: Decimal,
    current_price: Decimal,
    evaluated_at: datetime,
) -> ShadowBoxContext:
    """Rebuild the current non-lookahead box from persisted closed Tiger bars."""

    minimum_signal = config.parameters.box.length + 1
    if len(signal_bars) < minimum_signal:
        return ShadowBoxContext(None, None, "tiger_box_warmup_incomplete")
    if (
        config.parameters.box.auto_range
        and len(daily_bars) < config.parameters.box.daily_atr_period
    ):
        return ShadowBoxContext(None, None, "tiger_daily_atr_warmup_incomplete")
    duration = timeframe_milliseconds(config.parameters.box.timeframe)
    evaluated = (
        evaluated_at.replace(tzinfo=UTC)
        if evaluated_at.tzinfo is None
        else evaluated_at.astimezone(UTC)
    )
    evaluated_ms = int(evaluated.timestamp() * 1000)
    if evaluated_ms - int(signal_bars[-1].close_time) > duration * 2:
        return ShadowBoxContext(None, None, "tiger_closed_bars_stale")
    current_open = max(
        evaluated_ms,
        int(signal_bars[-1].close_time),
    )
    synthetic = TigerBar(
        symbol=config.market_data.underlying_symbol,
        timeframe=config.parameters.box.timeframe,
        trade_session=config.market_data.trade_sessions[0],
        adjustment=config.market_data.adjustment,
        open_time=current_open,
        close_time=current_open + duration,
        open=current_price,
        high=current_price,
        low=current_price,
        close=current_price,
        volume=Decimal("0"),
        amount=None,
        received_at=evaluated,
        source_version="shadow_current_tick_v1",
    )
    levels = build_box_levels(
        (*signal_bars, synthetic),
        daily_bars,
        config,
        point_size=point_size,
    )
    level = levels[-1]
    return ShadowBoxContext(
        level=level,
        box_time=level.bar_open_time if level is not None else None,
        reason_code=None if level is not None else "tiger_box_not_ready",
    )


def session_hour(evaluated_at: datetime, timezone: str) -> int:
    zone_name = "America/New_York" if timezone == "exchange" else timezone
    try:
        zone = ZoneInfo(zone_name)
    except ZoneInfoNotFoundError as exc:
        raise ValueError("strategy session timezone is invalid") from exc
    observed = (
        evaluated_at.replace(tzinfo=UTC)
        if evaluated_at.tzinfo is None
        else evaluated_at.astimezone(UTC)
    )
    return observed.astimezone(zone).hour
