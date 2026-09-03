"""Deterministic Tiger-bar replay for the Martingale TP4 strategy."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ...domain.martingale_tp4 import MartingaleTp4Config
from ...domain.martingale_tp4_engine import (
    BasketLeg,
    BasketSnapshot,
    DecisionAction,
    Direction,
    EnginePolicy,
    MarketTick,
    StrategyDecision,
    evaluate_tick,
)
from ...tiger_market_data import TigerBar

REPLAY_ENGINE_VERSION = "martingale_tp4_bar_replay_v2"
BOX_ALGORITHM_VERSION = "mq4_stateful_adaptive_box_v2"
FILL_MODEL_VERSION = "tiger_ohlc_path_v1"


class ReplayDataError(ValueError):
    """Raised when a replay would produce a misleading result."""


@dataclass(frozen=True, slots=True)
class ReplayCosts:
    fee_bps: Decimal = Decimal("5")
    slippage_bps: Decimal = Decimal("2")
    synthetic_spread_points: Decimal = Decimal("1")

    def __post_init__(self) -> None:
        if not Decimal("0") <= self.fee_bps <= Decimal("1000"):
            raise ValueError("fee_bps must be between 0 and 1000")
        if not Decimal("0") <= self.slippage_bps <= Decimal("1000"):
            raise ValueError("slippage_bps must be between 0 and 1000")
        if self.synthetic_spread_points < 0:
            raise ValueError("synthetic_spread_points must not be negative")


@dataclass(frozen=True, slots=True)
class ReplayCoverage:
    signal_warmup_bars: int
    signal_evaluation_bars: int
    daily_warmup_bars: int
    intraday_gap_count: int
    first_signal_open_time: int | None
    last_signal_close_time: int | None
    first_daily_open_time: int | None
    last_daily_close_time: int | None
    status: str
    reason_codes: tuple[str, ...]

    def audit_payload(self) -> dict[str, object]:
        return _json_safe(asdict(self))


@dataclass(frozen=True, slots=True)
class BoxLevel:
    bar_open_time: int
    high: Decimal
    low: Decimal
    source_bar_count: int
    target_range: Decimal
    atr: Decimal | None


@dataclass(frozen=True, slots=True)
class ReplayFill:
    sequence: int
    bar_open_time: int
    action: str
    reason_code: str
    direction: str
    quantity: Decimal
    price: Decimal
    fee: Decimal
    gross_pnl: Decimal
    net_pnl: Decimal
    leg_indices: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class ReplayCycle:
    sequence: int
    opened_at: int
    closed_at: int
    mode: str
    leg_count: int
    realized_pnl: Decimal
    fees: Decimal
    exit_reason: str
    leverage: int
    peak_initial_margin: Decimal
    minimum_available_balance: Decimal
    available_balance_after_close: Decimal
    long_leg_count: int
    short_leg_count: int


@dataclass(frozen=True, slots=True)
class ReplayResult:
    engine_version: str
    engine_policy: str
    box_algorithm_version: str
    fill_model_version: str
    data_precision: str
    config_snapshot: dict[str, object]
    replay_costs: dict[str, object]
    dataset_sha256: str
    run_sha256: str
    signal_bar_count: int
    warmup_bar_count: int
    daily_bar_count: int
    box_ready_bar_count: int
    cycles: tuple[ReplayCycle, ...]
    fills: tuple[ReplayFill, ...]
    equity_curve: tuple[dict[str, object], ...]
    metrics: dict[str, object]
    warnings: tuple[str, ...]

    def audit_payload(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["cycles"] = [_json_safe(item) for item in self.cycles]
        payload["fills"] = [_json_safe(item) for item in self.fills]
        payload["equity_curve"] = [_json_safe(item) for item in self.equity_curve]
        payload["metrics"] = _json_safe(self.metrics)
        return payload


@dataclass(slots=True)
class _OpenLeg:
    leg_index: int
    direction: Direction
    quantity: Decimal
    entry_price: Decimal
    entry_fee: Decimal
    opened_at: int


def _json_safe(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_safe(item) for item in value]
    if hasattr(value, "__dataclass_fields__"):
        return _json_safe(asdict(value))
    return value


def _validate_bar_stream(
    bars: Sequence[TigerBar],
    *,
    symbol: str,
    timeframe: str,
    minimum_count: int,
) -> tuple[TigerBar, ...]:
    selected = tuple(sorted(bars, key=lambda item: item.open_time))
    if len(selected) < minimum_count:
        raise ReplayDataError(f"{timeframe} bar coverage is insufficient")
    times = [item.open_time for item in selected]
    if len(times) != len(set(times)):
        raise ReplayDataError(f"{timeframe} bars contain duplicate timestamps")
    if any(item.symbol != symbol or item.timeframe != timeframe for item in selected):
        raise ReplayDataError(f"{timeframe} bars contain a foreign stream")
    if any(not item.valid_ohlc for item in selected):
        raise ReplayDataError(f"{timeframe} bars contain invalid OHLC values")
    if any(item.close_time <= item.open_time for item in selected):
        raise ReplayDataError(f"{timeframe} bars contain invalid close times")
    return selected


def calculate_wilder_atr(
    daily_bars: Sequence[TigerBar], period: int
) -> tuple[tuple[int, Decimal], ...]:
    if period < 2:
        raise ValueError("ATR period must be at least 2")
    ordered = tuple(sorted(daily_bars, key=lambda item: item.open_time))
    true_ranges: list[Decimal] = []
    result: list[tuple[int, Decimal]] = []
    previous_close: Decimal | None = None
    atr: Decimal | None = None
    for bar in ordered:
        true_range = bar.high - bar.low
        if previous_close is not None:
            true_range = max(
                true_range,
                abs(bar.high - previous_close),
                abs(bar.low - previous_close),
            )
        true_ranges.append(true_range)
        if len(true_ranges) == period:
            atr = sum(true_ranges, Decimal("0")) / Decimal(period)
        elif len(true_ranges) > period and atr is not None:
            atr = (atr * Decimal(period - 1) + true_range) / Decimal(period)
        if atr is not None:
            result.append((bar.close_time, atr))
        previous_close = bar.close
    return tuple(result)


def assess_replay_coverage(
    signal_bars: Sequence[TigerBar],
    daily_bars: Sequence[TigerBar],
    *,
    evaluation_begin_time: int,
    evaluation_end_time: int,
    required_signal_warmup_bars: int,
    required_daily_warmup_bars: int,
    timezone: str,
) -> ReplayCoverage:
    if evaluation_end_time <= evaluation_begin_time:
        raise ValueError("evaluation end must be greater than begin")
    if required_signal_warmup_bars < 0 or required_daily_warmup_bars < 0:
        raise ValueError("required warmup bars must not be negative")
    signal = tuple(sorted(signal_bars, key=lambda item: item.open_time))
    daily = tuple(sorted(daily_bars, key=lambda item: item.open_time))
    signal_warmup = sum(item.close_time <= evaluation_begin_time for item in signal)
    signal_evaluation = sum(
        item.open_time >= evaluation_begin_time and item.close_time <= evaluation_end_time
        for item in signal
    )
    daily_warmup = sum(item.close_time <= evaluation_begin_time for item in daily)
    gap_count = _intraday_gap_count(signal, timezone)
    reasons: list[str] = []
    if signal_warmup < required_signal_warmup_bars:
        reasons.append("signal_warmup_incomplete")
    if signal_evaluation == 0:
        reasons.append("evaluation_period_empty")
    if daily_warmup < required_daily_warmup_bars:
        reasons.append("daily_atr_warmup_incomplete")
    if gap_count:
        reasons.append("intraday_bar_gap")
    return ReplayCoverage(
        signal_warmup_bars=signal_warmup,
        signal_evaluation_bars=signal_evaluation,
        daily_warmup_bars=daily_warmup,
        intraday_gap_count=gap_count,
        first_signal_open_time=signal[0].open_time if signal else None,
        last_signal_close_time=signal[-1].close_time if signal else None,
        first_daily_open_time=daily[0].open_time if daily else None,
        last_daily_close_time=daily[-1].close_time if daily else None,
        status="usable" if not reasons else "blocked",
        reason_codes=tuple(reasons),
    )


def _intraday_gap_count(bars: Sequence[TigerBar], timezone: str) -> int:
    if len(bars) < 2 or bars[0].timeframe == "1d":
        return 0
    zone = _timezone(timezone)
    count = 0
    for previous, current in zip(bars, bars[1:], strict=False):
        previous_date = datetime.fromtimestamp(previous.open_time / 1000, tz=UTC).astimezone(
            zone
        ).date()
        current_date = datetime.fromtimestamp(current.open_time / 1000, tz=UTC).astimezone(
            zone
        ).date()
        if previous_date != current_date:
            continue
        gap_duration = current.open_time - previous.close_time
        # Providers differ on whether close_time is inclusive or exclusive.
        # A one-millisecond boundary between closed bars is still contiguous.
        if gap_duration > 1:
            duration = previous.close_time - previous.open_time
            if duration > 0:
                count += max(1, gap_duration // duration)
    return count


def build_box_levels(
    signal_bars: Sequence[TigerBar],
    daily_bars: Sequence[TigerBar],
    config: MartingaleTp4Config,
    *,
    point_size: Decimal,
) -> tuple[BoxLevel | None, ...]:
    """Build the stateful MQ4 box boundaries without rendering chart objects.

    A box needs at least ``BoxLength`` contained bars plus an older bar that
    proves the configured range boundary.  It then remains active while each
    newly closed bar is contained, and is invalidated when containment fails.
    The signal bar itself is never used to calculate its own boundaries.

    The source EA accidentally searches the low through ``MODE_HIGH`` inside
    ``_get_range``.  This implementation uses the actual low; the original
    defect is recorded in replay warnings rather than silently reproduced.
    """

    if point_size <= 0:
        raise ValueError("point_size must be positive")
    parameters = config.parameters
    box = parameters.box
    atr_values = (
        calculate_wilder_atr(daily_bars, box.daily_atr_period) if box.auto_range else ()
    )
    levels: list[BoxLevel | None] = []
    atr_index = 0
    available_atr: Decimal | None = None
    active: BoxLevel | None = None
    for index, current in enumerate(signal_bars):
        closed_bar = signal_bars[index - 1] if index else None
        atr_cutoff = closed_bar.open_time if closed_bar is not None else current.open_time
        while atr_index < len(atr_values) and atr_values[atr_index][0] <= atr_cutoff:
            available_atr = atr_values[atr_index][1]
            atr_index += 1
        if closed_bar is not None:
            invalidated = active is not None and not (
                closed_bar.low >= active.low and closed_bar.high <= active.high
            )
            if invalidated:
                active = None
            target_range = (
                available_atr * box.daily_atr_factor
                if box.auto_range and available_atr is not None
                else box.fixed_range_points * point_size
            )
            if not box.auto_range or available_atr is not None:
                period = _first_range_break_period(
                    signal_bars,
                    end_index=index - 1,
                    minimum_period=box.length,
                    maximum_period=999,
                    target_range=target_range,
                )
                if period is not None and period > box.length:
                    window = signal_bars[index - period : index]
                    upper = max(item.high for item in window)
                    lower = min(item.low for item in window)
                    oldest = window[0]
                    # These two corrections mirror BOX_DRAW: the older bar is
                    # the range-breaking anchor, not part of the consolidation.
                    if oldest.high == upper:
                        upper = lower + target_range
                    if oldest.low == lower:
                        lower = upper - target_range
                    buffer = box.buffer_points * point_size
                    candidate = BoxLevel(
                        bar_open_time=current.open_time,
                        high=upper + buffer,
                        low=lower - buffer,
                        source_bar_count=period,
                        target_range=target_range,
                        atr=available_atr,
                    )
                    if active is None and not invalidated:
                        active = candidate
                    elif active is not None:
                        active = candidate
        levels.append(
            None
            if active is None
            else BoxLevel(
                bar_open_time=current.open_time,
                high=active.high,
                low=active.low,
                source_bar_count=active.source_bar_count,
                target_range=active.target_range,
                atr=active.atr,
            )
        )
    return tuple(levels)


def _first_range_break_period(
    bars: Sequence[TigerBar],
    *,
    end_index: int,
    minimum_period: int,
    maximum_period: int,
    target_range: Decimal,
) -> int | None:
    """Return the first lookback whose true high-low range exceeds the target."""

    available = end_index + 1
    if available <= minimum_period:
        return None
    upper_limit = min(available, maximum_period)
    for period in range(minimum_period, upper_limit + 1):
        window = bars[end_index - period + 1 : end_index + 1]
        observed_range = max(item.high for item in window) - min(item.low for item in window)
        if observed_range > target_range:
            return period
    return None


def dataset_sha256(signal_bars: Sequence[TigerBar], daily_bars: Sequence[TigerBar]) -> str:
    digest = hashlib.sha256()
    for stream_name, bars in (("signal", signal_bars), ("daily", daily_bars)):
        for bar in bars:
            digest.update(
                json.dumps(
                    [
                        stream_name,
                        bar.symbol,
                        bar.timeframe,
                        bar.trade_session,
                        bar.adjustment,
                        bar.open_time,
                        bar.close_time,
                        str(bar.open),
                        str(bar.high),
                        str(bar.low),
                        str(bar.close),
                        str(bar.volume),
                    ],
                    separators=(",", ":"),
                ).encode("utf-8")
            )
    return digest.hexdigest()


def _bar_path(bar: TigerBar) -> tuple[Decimal, ...]:
    # A deterministic OHLC path is explicit because bar data cannot prove the
    # order in which its high and low occurred.
    if bar.close >= bar.open:
        return bar.open, bar.low, bar.high, bar.close
    return bar.open, bar.high, bar.low, bar.close


def run_bar_replay(
    config: MartingaleTp4Config,
    signal_bars: Sequence[TigerBar],
    daily_bars: Sequence[TigerBar],
    *,
    initial_capital: Decimal,
    point_size: Decimal,
    costs: ReplayCosts | None = None,
    engine_policy: EnginePolicy = EnginePolicy.RESEARCH_COMPATIBILITY,
    manual_entry_direction: Literal["buy", "sell"] | None = None,
    manual_entry_time: int | None = None,
    evaluation_begin_time: int | None = None,
    evaluation_end_time: int | None = None,
    leverage: int = 1,
    maintenance_margin_rate: Decimal = Decimal("0.005"),
    liquidation_fee_rate: Decimal = Decimal("0"),
) -> ReplayResult:
    if initial_capital <= 0:
        raise ValueError("initial_capital must be positive")
    if point_size <= 0:
        raise ValueError("point_size must be positive")
    if leverage < 1 or leverage > 20:
        raise ValueError("leverage must be between 1 and 20")
    if not Decimal("0") <= maintenance_margin_rate < Decimal("0.5"):
        raise ValueError("maintenance_margin_rate must be between 0 and 0.5")
    if not Decimal("0") <= liquidation_fee_rate <= Decimal("0.1"):
        raise ValueError("liquidation_fee_rate must be between 0 and 0.1")
    costs = costs or ReplayCosts()
    symbol = config.market_data.underlying_symbol
    parameters = config.parameters
    signal = _validate_bar_stream(
        signal_bars,
        symbol=symbol,
        timeframe=parameters.box.timeframe,
        minimum_count=parameters.box.length + 2,
    )
    daily = _validate_bar_stream(
        daily_bars,
        symbol=symbol,
        timeframe="1d",
        minimum_count=parameters.box.daily_atr_period + 1 if parameters.box.auto_range else 0,
    )
    evaluation_indices = [
        index
        for index, bar in enumerate(signal)
        if (evaluation_begin_time is None or bar.open_time >= evaluation_begin_time)
        and (evaluation_end_time is None or bar.close_time <= evaluation_end_time)
    ]
    if not evaluation_indices:
        raise ReplayDataError("no signal bars fall inside the evaluation period")
    first_evaluation_index = evaluation_indices[0]
    last_evaluation_index = evaluation_indices[-1]
    if parameters.mode in {"recovery", "grid"} and (
        manual_entry_direction is None or manual_entry_time is None
    ):
        raise ReplayDataError("recovery/grid replay requires a manual first-leg direction and time")

    levels = build_box_levels(signal, daily, config, point_size=point_size)
    data_hash = dataset_sha256(signal, daily)
    run_payload = {
        "engine": REPLAY_ENGINE_VERSION,
        "engine_policy": engine_policy,
        "box": BOX_ALGORITHM_VERSION,
        "fill": FILL_MODEL_VERSION,
        "dataset": data_hash,
        "config": config.model_dump(mode="json"),
        "initial_capital": str(initial_capital),
        "point_size": str(point_size),
        "costs": _json_safe(asdict(costs)),
        "manual_entry_direction": manual_entry_direction,
        "manual_entry_time": manual_entry_time,
        "evaluation_begin_time": evaluation_begin_time,
        "evaluation_end_time": evaluation_end_time,
        "leverage": leverage,
        "maintenance_margin_rate": str(maintenance_margin_rate),
        "liquidation_fee_rate": str(liquidation_fee_rate),
    }
    run_hash = hashlib.sha256(
        json.dumps(run_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()

    balance = initial_capital
    open_legs: list[_OpenLeg] = []
    fills: list[ReplayFill] = []
    cycles: list[ReplayCycle] = []
    equity_curve: list[dict[str, object]] = []
    previous_bid: Decimal | None = None
    favorable_high: Decimal | None = None
    favorable_low: Decimal | None = None
    active_box: BoxLevel | None = None
    cycle_opened_at: int | None = None
    cycle_mode = parameters.mode
    cycle_leg_count = 0
    cycle_pnl = Decimal("0")
    cycle_fees = Decimal("0")
    cycle_peak_initial_margin = Decimal("0")
    cycle_min_available_balance = initial_capital
    cycle_long_leg_count = 0
    cycle_short_leg_count = 0
    next_leg_index = 0
    manual_used = False
    peak_equity = initial_capital
    maximum_drawdown = Decimal("0")
    fee_rate = costs.fee_bps / Decimal("10000")
    slippage_rate = costs.slippage_bps / Decimal("10000")
    rejected_order_count = 0
    liquidation_count = 0
    liquidation_fees = Decimal("0")

    def mark_price(direction: Direction, bid: Decimal, ask: Decimal) -> Decimal:
        return bid if direction == Direction.BUY else ask

    def engine_snapshot(bid: Decimal, ask: Decimal) -> BasketSnapshot:
        legs = tuple(
            BasketLeg(
                leg_index=item.leg_index,
                direction=item.direction,
                quantity=item.quantity,
                entry_price=item.entry_price,
                unrealized_pnl=(
                    (mark_price(item.direction, bid, ask) - item.entry_price)
                    * (Decimal("1") if item.direction == Direction.BUY else Decimal("-1"))
                    * item.quantity
                    - item.entry_fee
                ),
            )
            for item in open_legs
        )
        level = active_box
        return BasketSnapshot(
            legs=legs,
            box_high=level.high if level is not None else None,
            box_low=level.low if level is not None else None,
            previous_bid=previous_bid,
            favorable_high=favorable_high,
            favorable_low=favorable_low,
        )

    def open_leg(
        decision: StrategyDecision,
        *,
        bar_time: int,
        bid: Decimal,
        ask: Decimal,
    ) -> None:
        nonlocal balance, cycle_fees, cycle_leg_count, next_leg_index
        nonlocal favorable_high, favorable_low, cycle_opened_at, cycle_mode
        nonlocal rejected_order_count
        nonlocal cycle_peak_initial_margin, cycle_min_available_balance
        nonlocal cycle_long_leg_count, cycle_short_leg_count
        if decision.direction is None or decision.quantity is None:
            raise RuntimeError("entry decision is incomplete")
        base_price = ask if decision.direction == Direction.BUY else bid
        fill_price = base_price * (
            Decimal("1") + slippage_rate
            if decision.direction == Direction.BUY
            else Decimal("1") - slippage_rate
        )
        current_notional = sum(
            (item.entry_price * item.quantity for item in open_legs), Decimal("0")
        )
        projected_notional = current_notional + fill_price * decision.quantity
        if projected_notional / Decimal(leverage) > max(balance, Decimal("0")):
            rejected_order_count += 1
            return
        fee = fill_price * decision.quantity * fee_rate
        balance -= fee
        cycle_fees += fee
        if cycle_opened_at is None:
            cycle_opened_at = bar_time
            cycle_mode = decision.effective_mode
        leg = _OpenLeg(
            leg_index=next_leg_index,
            direction=decision.direction,
            quantity=decision.quantity,
            entry_price=fill_price,
            entry_fee=fee,
            opened_at=bar_time,
        )
        open_legs.append(leg)
        current_initial_margin = sum(
            (item.entry_price * item.quantity for item in open_legs), Decimal("0")
        ) / Decimal(leverage)
        cycle_peak_initial_margin = max(cycle_peak_initial_margin, current_initial_margin)
        cycle_min_available_balance = min(
            cycle_min_available_balance,
            max(Decimal("0"), balance - current_initial_margin),
        )
        if decision.direction == Direction.BUY:
            cycle_long_leg_count += 1
        else:
            cycle_short_leg_count += 1
        fills.append(
            ReplayFill(
                sequence=len(fills) + 1,
                bar_open_time=bar_time,
                action=decision.action,
                reason_code=decision.reason_code,
                direction=decision.direction,
                quantity=decision.quantity,
                price=fill_price,
                fee=fee,
                gross_pnl=Decimal("0"),
                net_pnl=-fee,
                leg_indices=(next_leg_index,),
            )
        )
        next_leg_index += 1
        cycle_leg_count += 1
        favorable_high = bid
        favorable_low = ask

    def close_selected(
        decision: StrategyDecision,
        *,
        bar_time: int,
        bid: Decimal,
        ask: Decimal,
    ) -> None:
        nonlocal balance, cycle_pnl, cycle_fees, cycle_opened_at, cycle_leg_count
        nonlocal active_box, next_leg_index, favorable_high, favorable_low
        nonlocal liquidation_fees
        nonlocal cycle_peak_initial_margin, cycle_min_available_balance
        nonlocal cycle_long_leg_count, cycle_short_leg_count
        if decision.action == DecisionAction.CLOSE_ALL:
            selected = list(open_legs)
        elif decision.action == DecisionAction.CLOSE_DIRECTION:
            selected = [item for item in open_legs if item.direction == decision.direction]
        else:
            selected = [item for item in open_legs if item.leg_index in decision.leg_indices]
        selected_indices = {item.leg_index for item in selected}
        for item in selected:
            base_price = bid if item.direction == Direction.BUY else ask
            fill_price = base_price * (
                Decimal("1") - slippage_rate
                if item.direction == Direction.BUY
                else Decimal("1") + slippage_rate
            )
            direction_sign = Decimal("1") if item.direction == Direction.BUY else Decimal("-1")
            gross = (fill_price - item.entry_price) * direction_sign * item.quantity
            exit_fee = fill_price * item.quantity * fee_rate
            liquidation_fee = (
                fill_price * item.quantity * liquidation_fee_rate
                if decision.reason_code == "liquidation"
                else Decimal("0")
            )
            charged_fee = exit_fee + liquidation_fee
            net = gross - item.entry_fee - charged_fee
            balance += gross - charged_fee
            cycle_pnl += net
            cycle_fees += charged_fee
            liquidation_fees += liquidation_fee
            fills.append(
                ReplayFill(
                    sequence=len(fills) + 1,
                    bar_open_time=bar_time,
                    action=decision.action,
                    reason_code=decision.reason_code,
                    direction=item.direction,
                    quantity=item.quantity,
                    price=fill_price,
                    fee=charged_fee,
                    gross_pnl=gross,
                    net_pnl=gross - charged_fee,
                    leg_indices=(item.leg_index,),
                )
            )
        open_legs[:] = [item for item in open_legs if item.leg_index not in selected_indices]
        if not open_legs and cycle_opened_at is not None:
            cycles.append(
                ReplayCycle(
                    sequence=len(cycles) + 1,
                    opened_at=cycle_opened_at,
                    closed_at=bar_time,
                    mode=cycle_mode,
                    leg_count=cycle_leg_count,
                    realized_pnl=cycle_pnl,
                    fees=cycle_fees,
                    exit_reason=decision.reason_code,
                    leverage=leverage,
                    peak_initial_margin=cycle_peak_initial_margin,
                    minimum_available_balance=cycle_min_available_balance,
                    available_balance_after_close=balance,
                    long_leg_count=cycle_long_leg_count,
                    short_leg_count=cycle_short_leg_count,
                )
            )
            cycle_opened_at = None
            active_box = None
            next_leg_index = 0
            favorable_high = None
            favorable_low = None
            cycle_leg_count = 0
            cycle_pnl = Decimal("0")
            cycle_fees = Decimal("0")
            cycle_peak_initial_margin = Decimal("0")
            cycle_min_available_balance = balance
            cycle_long_leg_count = 0
            cycle_short_leg_count = 0

    for bar_index, bar in enumerate(signal):
        if bar_index < first_evaluation_index:
            previous_bid = bar.close
            continue
        if bar_index > last_evaluation_index:
            break
        current_level = levels[bar_index]
        for path_index, price in enumerate(_bar_path(bar)):
            bid = price
            ask = price + costs.synthetic_spread_points * point_size
            if open_legs:
                favorable_high = bid if favorable_high is None else max(favorable_high, bid)
                favorable_low = ask if favorable_low is None else min(favorable_low, ask)
                unrealized_now = sum(
                    (
                        (mark_price(item.direction, bid, ask) - item.entry_price)
                        * (Decimal("1") if item.direction == Direction.BUY else Decimal("-1"))
                        * item.quantity
                    )
                    for item in open_legs
                )
                maintenance_margin = sum(
                    (
                        mark_price(item.direction, bid, ask)
                        * item.quantity
                        * maintenance_margin_rate
                    )
                    for item in open_legs
                )
                if balance + unrealized_now <= maintenance_margin:
                    close_selected(
                        StrategyDecision(
                            action=DecisionAction.CLOSE_ALL,
                            reason_code="liquidation",
                            effective_mode=parameters.mode,
                        ),
                        bar_time=bar.open_time,
                        bid=bid,
                        ask=ask,
                    )
                    liquidation_count += 1
                    previous_bid = bid
                    break
            if not open_legs and cycle_opened_at is None and path_index == 0:
                active_box = current_level
            manual_direction: Direction | None = None
            manual_quantity: Decimal | None = None
            if (
                not manual_used
                and manual_entry_direction is not None
                and manual_entry_time is not None
                and bar.open_time >= manual_entry_time
                and path_index == 0
            ):
                manual_direction = Direction(manual_entry_direction)
                manual_quantity = parameters.sizing.initial_lot
            decision = evaluate_tick(
                parameters,
                engine_snapshot(bid, ask),
                MarketTick(
                    bid=bid,
                    ask=ask,
                    point_size=point_size,
                    hour=_session_hour(bar.open_time, parameters.session.timezone),
                ),
                account_balance=max(balance, Decimal("0.00000001")),
                policy=engine_policy,
                manual_direction=manual_direction,
                manual_quantity=manual_quantity,
            )
            if decision.action in {DecisionAction.OPEN, DecisionAction.ADD}:
                if not open_legs and manual_direction is not None:
                    manual_used = True
                    distance = parameters.ladder.distance_points * point_size
                    active_box = BoxLevel(
                        bar_open_time=bar.open_time,
                        high=price + distance if manual_direction == Direction.SELL else price,
                        low=price - distance if manual_direction == Direction.BUY else price,
                        source_bar_count=0,
                        target_range=distance,
                        atr=None,
                    )
                open_leg(decision, bar_time=bar.open_time, bid=bid, ask=ask)
            elif decision.action in {
                DecisionAction.CLOSE_ALL,
                DecisionAction.CLOSE_DIRECTION,
                DecisionAction.CLOSE_PAIR,
            }:
                close_selected(decision, bar_time=bar.open_time, bid=bid, ask=ask)
            previous_bid = bid

        close_bid = bar.close
        close_ask = bar.close + costs.synthetic_spread_points * point_size
        unrealized = sum(
            (
                (mark_price(item.direction, close_bid, close_ask) - item.entry_price)
                * (Decimal("1") if item.direction == Direction.BUY else Decimal("-1"))
                * item.quantity
            )
            for item in open_legs
        )
        equity = balance + unrealized
        peak_equity = max(peak_equity, equity)
        drawdown = (peak_equity - equity) / peak_equity * Decimal("100") if peak_equity > 0 else 0
        maximum_drawdown = max(maximum_drawdown, Decimal(drawdown))
        equity_curve.append(
            {
                "bar_open_time": bar.open_time,
                "equity": equity,
                "drawdown_pct": drawdown,
                "open_legs": len(open_legs),
            }
        )

    if open_legs:
        final_bar = signal[last_evaluation_index]
        decision = StrategyDecision(
            action=DecisionAction.CLOSE_ALL,
            reason_code="end_of_data",
            effective_mode=parameters.mode,
        )
        close_selected(
            decision,
            bar_time=final_bar.open_time,
            bid=final_bar.close,
            ask=final_bar.close + costs.synthetic_spread_points * point_size,
        )
        forced_equity = balance
        peak_equity = max(peak_equity, forced_equity)
        forced_drawdown = (
            (peak_equity - forced_equity) / peak_equity * Decimal("100")
            if peak_equity > 0
            else Decimal("0")
        )
        maximum_drawdown = max(maximum_drawdown, forced_drawdown)
        if equity_curve:
            equity_curve[-1] = {
                "bar_open_time": final_bar.open_time,
                "equity": forced_equity,
                "drawdown_pct": forced_drawdown,
                "open_legs": 0,
            }

    wins = [item for item in cycles if item.realized_pnl > 0]
    losses = [item for item in cycles if item.realized_pnl < 0]
    total_profit = sum((item.realized_pnl for item in wins), Decimal("0"))
    total_loss = sum((item.realized_pnl for item in losses), Decimal("0"))
    total_fees = sum((item.fees for item in cycles), Decimal("0"))
    final_equity = initial_capital + sum((item.realized_pnl for item in cycles), Decimal("0"))
    metrics: dict[str, object] = {
        "initial_capital": initial_capital,
        "final_equity": final_equity,
        "net_profit": final_equity - initial_capital,
        "return_pct": (final_equity / initial_capital - 1) * Decimal("100"),
        "cycle_count": len(cycles),
        "winning_cycles": len(wins),
        "losing_cycles": len(losses),
        "win_rate_pct": Decimal(len(wins)) / Decimal(len(cycles)) * Decimal("100") if cycles else 0,
        "profit_factor": total_profit / abs(total_loss) if total_loss else None,
        "total_fees": total_fees,
        "maximum_drawdown_pct": maximum_drawdown,
        "open_fill_count": sum(item.action in {"open", "add"} for item in fills),
        "leverage": leverage,
        "maintenance_margin_rate_pct": maintenance_margin_rate * Decimal("100"),
        "liquidation_count": liquidation_count,
        "liquidation_fees": liquidation_fees,
        "rejected_order_count": rejected_order_count,
    }
    warnings = [
        "ohlc_intrabar_path_is_an_assumption",
        "tiger_reference_prices_are_not_binance_execution_prices",
        "contract_multiplier_and_funding_not_applied",
        "isolated_margin_liquidation_uses_platform_tier1_mmr",
        "legacy_mq4_get_range_mode_high_defect_corrected",
    ]
    if engine_policy == EnginePolicy.RESEARCH_COMPATIBILITY:
        warnings.append("legacy_spread_can_block_strategy_exit")
    if parameters.compatibility.section_policy == "disabled":
        warnings.append("legacy_chart_section_guard_disabled")
    return ReplayResult(
        engine_version=REPLAY_ENGINE_VERSION,
        engine_policy=engine_policy,
        box_algorithm_version=BOX_ALGORITHM_VERSION,
        fill_model_version=FILL_MODEL_VERSION,
        data_precision="closed_bar_ohlc_assumed_path",
        config_snapshot=config.model_dump(mode="json"),
        replay_costs=_json_safe(asdict(costs)),
        dataset_sha256=data_hash,
        run_sha256=run_hash,
        signal_bar_count=len(evaluation_indices),
        warmup_bar_count=first_evaluation_index,
        daily_bar_count=len(daily),
        box_ready_bar_count=sum(levels[index] is not None for index in evaluation_indices),
        cycles=tuple(cycles),
        fills=tuple(fills),
        equity_curve=tuple(equity_curve),
        metrics=metrics,
        warnings=tuple(warnings),
    )


def _session_hour(timestamp_ms: int, timezone: str) -> int:
    zone = _timezone(timezone)
    observed = datetime.fromtimestamp(timestamp_ms / 1000, tz=UTC)
    return observed.astimezone(zone).hour


def _timezone(timezone: str) -> ZoneInfo:
    zone_name = "America/New_York" if timezone == "exchange" else timezone
    try:
        return ZoneInfo(zone_name)
    except ZoneInfoNotFoundError as exc:
        raise ReplayDataError("strategy session timezone is invalid") from exc
