"""Tenant-isolated multi-account paper trading engine."""

from __future__ import annotations

import json
import math
import threading
import time
import uuid
from dataclasses import replace
from datetime import UTC
from decimal import Decimal, InvalidOperation
from typing import Any

from quantdesk_v2.strategy_evaluator import (
    DEFAULT_STRATEGY_EVALUATOR,
    StrategyCandle,
    StrategyEvaluationError,
    bollinger_bands,
    exponential_moving_average,
    optional_exponential_moving_average,
    relative_strength_index,
    resolve_legacy_strategy_timeframe,
    simple_moving_average,
    strategy_timeframe_seconds,
)
from quantdesk_v2.strategy_runtime import (
    StrategyMarketDataError,
    StrategySpecError,
    evaluate_strategy,
    validate_strategy_spec,
)
from quantdesk_v2.strategy_source_runtime import (
    StrategySourceError,
    StrategySourceExecutionError,
    evaluate_source,
    validate_source,
)

from . import indicators as ind
from . import market_store as store
from .live_risk import (
    OpenPositionRisk,
    account_loss_limits,
    atr_risk_position_size,
    closed_bar_signal_freshness,
    market_data_freshness,
    policy_from_config,
    signal_freshness,
    symbol_admission,
    symbol_risk_profile,
    tighten_policy_with_strategy,
    total_open_risk,
)
from .market_config import tradfi_symbols

DEFAULT_INITIAL_BALANCE = 10_000.0
DEFAULT_LEVERAGE = 20
DEFAULT_MAX_POSITIONS = 15
DEFAULT_MARGIN_CAP = 0.80
DEFAULT_FEE_BPS = 5.0
DEFAULT_SLIPPAGE_BPS = 3.0
DEFAULT_FUNDING_RATE_8H_BPS = 1.0
DEFAULT_STOP_LOSS_PCT = 3.0
DEFAULT_TAKE_PROFIT_PCT = 5.0
DEFAULT_MAX_HOLDING_BARS = 12
ATR_STOP_MULTIPLIER = 1.5
ATR_TAKE_PROFIT_MULTIPLIER = 2.5
FUNDING_INTERVAL_SECONDS = 8 * 60 * 60
LEGACY_PAPER_SIGNAL_MODE = "legacy_score_v1"
STRATEGY_EVENT_SIGNAL_MODE = "strategy_event_v2"
LEGACY_ENTRY_SCORE = 60.0
LEGACY_TREND_MA_PERIOD = 150
PAPER_MAX_FUTURE_TICKER_SKEW_SECONDS = 15
ENTRY_BASIS_SCHEMA_VERSION = 2

_lock = threading.RLock()


class _PriceSnapshot(dict[str, float]):
    """Latest prices plus their observation timestamps.

    It remains a normal ``dict`` for the paper UI and existing call sites, while
    the worker can fail closed when the stored ticker is stale.
    """

    def __init__(self) -> None:
        super().__init__()
        self.timestamps: dict[str, int | float] = {}


class PaperAccountUnavailable(RuntimeError):
    pass


def _json_object(value: Any, default: dict[str, Any] | None = None) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError, json.JSONDecodeError):
            return dict(default or {})
        return parsed if isinstance(parsed, dict) else dict(default or {})
    return dict(default or {})


def _strategy_snapshots(account: dict[str, Any]) -> list[dict[str, Any]]:
    snapshot = account.get("strategy_snapshot_json")
    selected = snapshot if isinstance(snapshot, dict) else {}
    bundled = selected.get("strategy_snapshots")
    if isinstance(bundled, list):
        normalized = [item for item in bundled if isinstance(item, dict)]
        if normalized:
            return normalized
    return [selected] if selected else []


def _strategy_display_name(account: dict[str, Any]) -> str:
    names = [str(item["name"]) for item in _strategy_snapshots(account) if item.get("name")]
    return " + ".join(names) or "未命名策略"


def _account(user_id: int, account_id: int) -> dict[str, Any]:
    rows = store.query(
        "SELECT * FROM paper_accounts WHERE id=? AND user_id=? AND status<>'archived'",
        (account_id, user_id),
    )
    if not rows:
        raise PaperAccountUnavailable("paper account is unavailable")
    account = dict(rows[0])
    account["config_json"] = _json_object(account.get("config_json"))
    account["strategy_snapshot_json"] = _json_object(account.get("strategy_snapshot_json"))
    return account


def _tracked_accounts(account_id: int | None = None) -> list[dict[str, Any]]:
    if account_id is None:
        rows = store.query("SELECT * FROM paper_accounts WHERE status<>'archived' ORDER BY id")
    else:
        rows = store.query(
            "SELECT * FROM paper_accounts WHERE id=? AND status<>'archived'", (account_id,)
        )
    result = []
    for row in rows:
        account = dict(row)
        account["config_json"] = _json_object(account.get("config_json"))
        account["strategy_snapshot_json"] = _json_object(
            account.get("strategy_snapshot_json")
        )
        result.append(account)
    return result


def _config(account: dict[str, Any]) -> dict[str, float | int]:
    raw = account["config_json"]
    try:
        funding_rate_8h_bps = float(
            raw.get("funding_rate_8h_bps", DEFAULT_FUNDING_RATE_8H_BPS)
        )
    except (TypeError, ValueError):
        funding_rate_8h_bps = DEFAULT_FUNDING_RATE_8H_BPS
    if not math.isfinite(funding_rate_8h_bps):
        funding_rate_8h_bps = DEFAULT_FUNDING_RATE_8H_BPS
    return {
        "leverage": max(1, min(int(raw.get("leverage", DEFAULT_LEVERAGE)), 20)),
        "max_positions": max(
            1, min(int(raw.get("max_positions", DEFAULT_MAX_POSITIONS)), 20)
        ),
        "margin_cap": max(0.05, min(float(raw.get("margin_cap", DEFAULT_MARGIN_CAP)), 0.95)),
        "position_size_pct": max(0.1, min(float(raw.get("position_size_pct", 10)), 100)),
        "fee_bps": max(0.0, min(float(raw.get("fee_bps", DEFAULT_FEE_BPS)), 100)),
        "slippage_bps": max(
            0.0, min(float(raw.get("slippage_bps", DEFAULT_SLIPPAGE_BPS)), 100)
        ),
        # Historical funding rates are not available in the local market store.
        # Keep the paper approximation explicit and configurable instead of
        # silently treating perpetual funding as zero. Positive rates charge
        # longs and credit shorts; negative rates do the reverse.
        "funding_rate_8h_bps": max(-100.0, min(funding_rate_8h_bps, 100.0)),
        "stop_loss_pct": max(
            0.01, min(float(raw.get("stop_loss_pct", DEFAULT_STOP_LOSS_PCT)), 99.9)
        ),
        "take_profit_pct": max(
            0.01, min(float(raw.get("take_profit_pct", DEFAULT_TAKE_PROFIT_PCT)), 99.9)
        ),
        "max_holding_bars": max(
            0, min(int(raw.get("max_holding_bars", DEFAULT_MAX_HOLDING_BARS)), 10_000)
        ),
    }


def _positions(account: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in store.query(
            "SELECT * FROM paper_positions WHERE paper_account_id=? AND user_id=? ORDER BY id",
            (account["id"], account["user_id"]),
        )
    ]


def _prices() -> dict[str, float]:
    snapshot = _PriceSnapshot()
    for row in store.query("SELECT symbol,price,ts FROM ticker WHERE price IS NOT NULL"):
        try:
            symbol = str(row["symbol"])
            price = float(row["price"])
        except (KeyError, TypeError, ValueError):
            continue
        if not math.isfinite(price) or price <= 0:
            continue
        snapshot[symbol] = price
        timestamp = row.get("ts")
        if timestamp is not None:
            snapshot.timestamps[symbol] = timestamp
    return snapshot


def _used_margin(positions: list[dict[str, Any]]) -> float:
    return sum(float(position["margin"]) for position in positions)


def _upnl(position: dict[str, Any], price: float) -> float:
    return (
        (price - float(position["avg_entry"]))
        * float(position["qty"])
        * int(position["side"])
    )


def _equity(
    account: dict[str, Any], prices: dict[str, float], positions: list[dict[str, Any]]
) -> tuple[float, float]:
    unrealized = sum(
        _upnl(position, prices.get(position["symbol"], float(position["avg_entry"])))
        - float(position.get("funding_acc") or 0)
        for position in positions
    )
    return float(account["balance"]) + _used_margin(positions) + unrealized, unrealized


def _paper_risk_policy(
    account: dict[str, Any], signal_evidence: dict[str, Any] | None = None
) -> Any:
    """Return the paper risk policy, optionally tightened by a full strategy.

    Strategy-authored risk proposals are advisory ceilings. They may reduce an
    account risk limit, but the user-selected paper leverage remains fixed.
    """

    raw = account.get("config_json")
    config = raw if isinstance(raw, dict) else {}
    policy_config = dict(config)
    # Paper exposes only ``leverage``.  A shared-risk migration persisted the
    # unrelated 10x live default as ``risk_max_leverage`` and silently overrode
    # configured 20x paper accounts.  The visible paper setting is authoritative
    # and every new paper position must use that exact leverage.
    policy_config["risk_max_leverage"] = max(
        1, min(int(config.get("leverage", DEFAULT_LEVERAGE)), 20)
    )
    policy = policy_from_config(policy_config)
    if "round_trip_cost_bps" not in config:
        configured_cost = Decimal(
            str(
                2
                * (
                    float(config.get("fee_bps", DEFAULT_FEE_BPS))
                    + float(config.get("slippage_bps", DEFAULT_SLIPPAGE_BPS))
                )
            )
        )
        policy = replace(policy, round_trip_cost_bps=max(configured_cost, Decimal(0)))

    snapshots = _strategy_snapshots(account)
    full_strategy_indexes = [
        index
        for index, snapshot in enumerate(snapshots)
        if snapshot.get("strategy_kind") in {"full_strategy", "source_strategy"}
    ]
    if not full_strategy_indexes or signal_evidence is None:
        return policy
    evidence = signal_evidence if isinstance(signal_evidence, dict) else {}
    components = evidence.get("strategy_signals")
    component_list = components if isinstance(components, list) else []
    tightened = policy
    for index in full_strategy_indexes:
        component_evidence = evidence
        if len(snapshots) > 1:
            try:
                component = component_list[index]
            except IndexError as exc:
                raise ValueError("full strategy risk proposal is unavailable") from exc
            if not isinstance(component, dict):
                raise ValueError("full strategy risk proposal is unavailable")
            candidate = component.get("evidence")
            component_evidence = candidate if isinstance(candidate, dict) else {}
        proposal = component_evidence.get("risk_proposal")
        if not isinstance(proposal, dict):
            raise ValueError("full strategy risk proposal is unavailable")
        tightened = tighten_policy_with_strategy(tightened, proposal)
    return replace(tightened, max_leverage=policy.max_leverage)


def _paper_signal_mode(account: dict[str, Any]) -> str:
    """Select the paper-only compatibility boundary for entry signals.

    Full strategies keep their versioned event evaluator.  Built-in strategies
    use the original QuantDesk paper semantics only when the account migration
    or account-creation API explicitly persists that compatibility mode.
    """

    snapshots = _strategy_snapshots(account)
    if len(snapshots) > 1:
        return STRATEGY_EVENT_SIGNAL_MODE
    snapshot = snapshots[0] if snapshots else {}
    if snapshot.get("strategy_kind") in {"full_strategy", "source_strategy"}:
        return STRATEGY_EVENT_SIGNAL_MODE
    raw = account.get("config_json") or {}
    configured = str(raw.get("signal_mode") or "").strip()
    if configured == LEGACY_PAPER_SIGNAL_MODE:
        return configured
    return STRATEGY_EVENT_SIGNAL_MODE


def _current_open_risk(
    positions: list[dict[str, Any]],
    config: dict[str, float | int],
    policy: Any,
) -> Decimal:
    risks: list[OpenPositionRisk] = []
    for position in positions:
        try:
            quantity = abs(Decimal(str(position.get("qty") or 0)))
            entry = Decimal(str(position.get("avg_entry") or 0))
            side = int(position.get("side") or 0)
        except (InvalidOperation, TypeError, ValueError, OverflowError):
            continue
        if quantity <= 0 or entry <= 0 or side not in {-1, 1}:
            continue
        try:
            stop = Decimal(str(position.get("stop") or 0))
        except (InvalidOperation, TypeError, ValueError):
            stop = Decimal(0)
        if stop <= 0 or (entry - stop) * side <= 0:
            repaired_stop, _ = _exit_levels(
                float(entry), side, position.get("atr_entry"), config
            )
            if repaired_stop is not None:
                stop = Decimal(str(repaired_stop))
        if stop <= 0 or (entry - stop) * side <= 0:
            # A legacy position without a usable stop must reserve at least its
            # full posted margin instead of disappearing from portfolio risk.
            margin = max(Decimal(str(position.get("margin") or 0)), Decimal(0))
            distance = margin / quantity if margin > 0 else entry
            stop = entry - Decimal(side) * distance
            if stop <= 0:
                stop = entry * Decimal("0.000001")
        risks.append(
            OpenPositionRisk(
                quantity=quantity,
                entry_price=entry,
                stop_price=stop,
                exit_cost_bps=policy.round_trip_cost_bps,
            )
        )
    return total_open_risk(risks)


def _price_is_fresh(
    prices: dict[str, float], symbol: str, now: int, policy: Any
) -> bool:
    timestamps = getattr(prices, "timestamps", None)
    # Plain dicts are retained as a compatibility seam for deterministic unit
    # tests and internal callers. Production snapshots always carry timestamps.
    if timestamps is None:
        return True
    timestamp = timestamps.get(symbol)
    if timestamp is None:
        return False
    try:
        return market_data_freshness(
            timestamp,
            now=now,
            max_age_seconds=policy.max_ticker_age_seconds,
            max_future_skew_seconds=PAPER_MAX_FUTURE_TICKER_SKEW_SECONDS,
        ).fresh
    except (TypeError, ValueError):
        return False


def _epoch_seconds(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(parsed):
        return None
    while abs(parsed) >= 100_000_000_000:
        parsed /= 1000
    return parsed


def _snapshot_signal_is_fresh(
    snapshot: dict[str, Any],
    config: dict[str, Any] | None,
    signal_time: Any,
    evidence: dict[str, Any],
    now: int,
    policy: Any,
) -> bool:
    try:
        if snapshot.get("strategy_kind") in {"full_strategy", "source_strategy"}:
            decision = signal_freshness(
                signal_time,
                now=now,
                max_age_seconds=policy.max_signal_age_seconds,
            )
            if not decision.fresh:
                return False
            valid_until = _epoch_seconds(evidence.get("valid_until"))
            return valid_until is not None and now <= valid_until
        timeframe = resolve_legacy_strategy_timeframe(
            snapshot,
            config,
        )
        timeframe_seconds = strategy_timeframe_seconds(timeframe)
        closed = closed_bar_signal_freshness(
            signal_time,
            timeframe_seconds=timeframe_seconds,
            now=now,
            valid_bars=max(
                1,
                math.ceil(policy.max_signal_age_seconds / timeframe_seconds),
            ),
        )
        maximum_age = signal_freshness(
            signal_time,
            now=now,
            max_age_seconds=policy.max_signal_age_seconds,
        )
        return closed.fresh and maximum_age.fresh
    except (TypeError, ValueError):
        return False


def _signal_is_fresh(
    account: dict[str, Any],
    signal_time: int,
    signal_evidence: dict[str, Any] | None,
    now: int,
    policy: Any,
) -> bool:
    snapshots = _strategy_snapshots(account)
    if not snapshots:
        return False
    config = (
        account.get("config_json")
        if isinstance(account.get("config_json"), dict)
        else None
    )
    evidence = signal_evidence if isinstance(signal_evidence, dict) else {}
    components = evidence.get("strategy_signals")
    if len(snapshots) > 1:
        if not isinstance(components, list) or len(components) != len(snapshots):
            return False
        for snapshot, component in zip(snapshots, components, strict=True):
            if not isinstance(component, dict) or component.get("direction") not in {-1, 1}:
                return False
            component_evidence = component.get("evidence")
            if not _snapshot_signal_is_fresh(
                snapshot,
                config,
                component.get("signal_time"),
                component_evidence if isinstance(component_evidence, dict) else {},
                now,
                policy,
            ):
                return False
        return True
    return _snapshot_signal_is_fresh(
        snapshots[0], config, signal_time, evidence, now, policy
    )


def _paper_signal_is_fresh(
    account: dict[str, Any],
    signal_time: int,
    signal_evidence: dict[str, Any] | None,
    now: int,
    policy: Any,
) -> bool:
    """Validate paper signals without weakening live-trading freshness.

    The original score is a state attached to the latest closed 4h bar, not a
    one-shot crossing.  The legacy engine deliberately keeps that latest state
    until a newer closed score replaces it; ticker freshness and portfolio risk
    are still enforced independently before any order can be created.
    """

    if _paper_signal_mode(account) != LEGACY_PAPER_SIGNAL_MODE:
        return _signal_is_fresh(account, signal_time, signal_evidence, now, policy)
    try:
        opened = _epoch_seconds(signal_time)
        return opened is not None and now >= opened + 4 * 60 * 60
    except (TypeError, ValueError):
        return False


def _accrue_estimated_funding(
    account: dict[str, Any],
    position: dict[str, Any],
    price: float,
    now: int,
    config: dict[str, float | int],
) -> float:
    """Accrue a fixed-rate paper estimate on UTC eight-hour boundaries.

    The local store has no historical Binance funding-rate series. The estimate
    uses the configured rate and latest local price for every elapsed boundary.
    ``funding_ts`` is a durable checkpoint, so a restart catches up missed
    boundaries without charging one twice.
    """

    try:
        opened_ts = max(int(position.get("opened_ts") or 0), 0)
        stored_funding_ts = int(position.get("funding_ts") or 0)
        previous_ts = max(stored_funding_ts, opened_ts)
        quantity = abs(float(position.get("qty") or 0))
        side = int(position.get("side") or 0)
        funding_price = float(price)
        rate_bps = float(config["funding_rate_8h_bps"])
    except (KeyError, TypeError, ValueError, OverflowError):
        return 0.0
    if (
        side not in {-1, 1}
        or quantity <= 0
        or not math.isfinite(quantity)
        or funding_price <= 0
        or not math.isfinite(funding_price)
        or not math.isfinite(rate_bps)
    ):
        return 0.0

    first_boundary = (
        previous_ts // FUNDING_INTERVAL_SECONDS + 1
    ) * FUNDING_INTERVAL_SECONDS
    if first_boundary > int(now):
        return 0.0
    periods = (int(now) - first_boundary) // FUNDING_INTERVAL_SECONDS + 1
    last_boundary = first_boundary + (periods - 1) * FUNDING_INTERVAL_SECONDS
    delta = quantity * funding_price * side * rate_bps / 10_000 * periods
    if not math.isfinite(delta):
        return 0.0
    updated = store.execute(
        """UPDATE paper_positions
           SET funding_acc=funding_acc+?,funding_ts=?
           WHERE id=? AND paper_account_id=? AND user_id=? AND funding_ts=?""",
        (
            delta,
            last_boundary,
            position["id"],
            account["id"],
            account["user_id"],
            stored_funding_ts,
        ),
    )
    if updated != 1:
        return 0.0
    position["funding_acc"] = float(position.get("funding_acc") or 0) + delta
    position["funding_ts"] = last_boundary
    return delta


def _local_day_start_ts(now: int, timezone_offset_minutes: int) -> int:
    offset_seconds = int(timezone_offset_minutes) * 60
    local_now = int(now) + offset_seconds
    return local_now - local_now % 86_400 - offset_seconds


def _today_pnl(
    account: dict[str, Any],
    current_equity: float,
    now: int,
    timezone_offset_minutes: int,
) -> float:
    """Return the account equity change since the user's local day began."""

    day_start_ts = _local_day_start_ts(now, timezone_offset_minutes)
    rows = store.query(
        """SELECT equity FROM paper_equity
           WHERE paper_account_id=? AND user_id=? AND ts<?
           ORDER BY ts DESC LIMIT 1""",
        (account["id"], account["user_id"], day_start_ts),
    )
    baseline = float(account["initial_balance"])
    if rows:
        try:
            candidate = float(rows[0]["equity"])
        except (KeyError, TypeError, ValueError):
            candidate = math.nan
        if math.isfinite(candidate):
            baseline = candidate
    return current_equity - baseline


def _entry_loss_guard(
    account: dict[str, Any], current_equity: float, now: int, policy: Any
) -> bool:
    """Block new positions after the same daily/drawdown limits as live trading."""

    if not math.isfinite(current_equity) or current_equity <= 0:
        return False
    day_start_ts = _local_day_start_ts(now, 0)
    day_rows = store.query(
        """SELECT equity FROM paper_equity
           WHERE paper_account_id=? AND user_id=? AND ts<?
           ORDER BY ts DESC LIMIT 1""",
        (account["id"], account["user_id"], day_start_ts),
    )
    baseline = float(account["initial_balance"])
    if day_rows:
        try:
            candidate = float(day_rows[0]["equity"])
        except (KeyError, TypeError, ValueError):
            candidate = math.nan
        if math.isfinite(candidate) and candidate > 0:
            baseline = candidate
    high_rows = store.query(
        """SELECT MAX(equity) AS high_watermark FROM paper_equity
           WHERE paper_account_id=? AND user_id=?""",
        (account["id"], account["user_id"]),
    )
    high_watermark = max(float(account["initial_balance"]), current_equity)
    if high_rows:
        try:
            candidate = float(high_rows[0]["high_watermark"])
        except (KeyError, TypeError, ValueError):
            candidate = math.nan
        if math.isfinite(candidate) and candidate > 0:
            high_watermark = max(high_watermark, candidate)
    try:
        return account_loss_limits(
            current_equity=current_equity,
            start_of_day_equity=baseline,
            high_watermark_equity=high_watermark,
            policy=policy,
        ).allow_new_entries
    except (TypeError, ValueError):
        return False


def _set_balance(account: dict[str, Any], balance: float) -> None:
    safe_balance = max(round(balance, 8), 0.0)
    store.execute(
        "UPDATE paper_accounts SET balance=?,updated_at=CURRENT_TIMESTAMP WHERE id=? AND user_id=?",
        (safe_balance, account["id"], account["user_id"]),
    )
    account["balance"] = safe_balance


def _candles(
    symbol: str, timeframe: str
) -> tuple[list[StrategyCandle], list[dict[str, Any]]]:
    rows = store.get_klines(symbol, timeframe, 600)
    candles = [
        StrategyCandle(
            ts=int(row["open_time"]),
            open=float(row["open"]),
            high=float(row["high"]),
            low=float(row["low"]),
            close=float(row["close"]),
            volume=float(row["volume"]),
        )
        for row in rows
    ]
    return candles, rows


def _legacy_score_signal(
    account: dict[str, Any], symbol: str, price: float | None = None
) -> tuple[int, float | None, list[str], int | None, dict[str, Any]]:
    """Reproduce the original paper account's persistent 4h score signal.

    The compatibility path intentionally reads the shared score table and uses
    the historical ``abs(score) >= 60`` plus MA150 trend filter.  Position
    sizing and all portfolio guards still run through the current risk engine.
    """

    score_rows = store.query(
        """SELECT score,detail,open_time FROM scores
           WHERE symbol=? AND tf='4h' ORDER BY open_time DESC LIMIT 1""",
        (symbol,),
    )
    if not score_rows:
        return 0, None, [], None, {"signal_mode": LEGACY_PAPER_SIGNAL_MODE}
    row = score_rows[0]
    try:
        score = float(row["score"])
        signal_time = int(row["open_time"])
    except (KeyError, TypeError, ValueError, OverflowError):
        return 0, None, [], None, {"signal_mode": LEGACY_PAPER_SIGNAL_MODE}
    if not math.isfinite(score):
        return 0, None, [], None, {"signal_mode": LEGACY_PAPER_SIGNAL_MODE}

    candles, rows = _candles(symbol, "4h")
    if len(candles) < LEGACY_TREND_MA_PERIOD:
        return 0, None, [], signal_time, {
            "signal_mode": LEGACY_PAPER_SIGNAL_MODE,
            "score": score,
            "threshold": LEGACY_ENTRY_SCORE,
            "reason_codes": ["INSUFFICIENT_MA150_HISTORY"],
        }
    closes = [item.close for item in candles]
    ma150 = sum(closes[-LEGACY_TREND_MA_PERIOD:]) / LEGACY_TREND_MA_PERIOD
    observed_price = float(price) if price is not None else closes[-1]
    atr = None
    if len(rows) > 15:
        atr = ind.atr(
            [float(item["high"]) for item in rows],
            [float(item["low"]) for item in rows],
            [float(item["close"]) for item in rows],
        )

    threshold_passed = abs(score) >= LEGACY_ENTRY_SCORE
    side = 1 if score > 0 else -1 if score < 0 else 0
    trend_passed = side != 0 and (
        (side > 0 and observed_price >= ma150)
        or (side < 0 and observed_price <= ma150)
    )
    direction = side if threshold_passed and trend_passed else 0
    reason_codes = []
    if threshold_passed:
        reason_codes.append("LEGACY_SCORE_THRESHOLD")
    if trend_passed:
        reason_codes.append("MA150_TREND_FILTER")
    evidence = {
        "signal_mode": LEGACY_PAPER_SIGNAL_MODE,
        "engine_key": LEGACY_PAPER_SIGNAL_MODE,
        "score": score,
        "threshold": LEGACY_ENTRY_SCORE,
        "reason_codes": reason_codes,
        "indicators": {
            "price": observed_price,
            "ma150": ma150,
            "atr": atr,
        },
    }
    basis = [
        f"策略：{account.get('strategy_snapshot_json', {}).get('name') or '老版评分策略'}",
        f"4h 评分：{score:+g} / 阈值：±{LEGACY_ENTRY_SCORE:g}",
        f"MA150：{ma150:.8g}",
    ]
    return direction, atr, basis, signal_time, evidence


def _engine_signal_evidence(
    engine_key: str,
    candles: list[StrategyCandle],
    parameters: dict[str, Any],
) -> dict[str, Any]:
    """Explain the exact final-bar values used by the built-in signal engines."""

    closes = [item.close for item in candles]
    if not closes:
        return {}
    index = len(closes) - 1
    evidence: dict[str, Any] = {
        "engine_key": engine_key,
        "market": {
            "open": candles[index].open,
            "high": candles[index].high,
            "low": candles[index].low,
            "close": candles[index].close,
            "volume": candles[index].volume,
        },
        "components": [],
        "reason_codes": [],
    }

    if engine_key == "ma_cross":
        fast = simple_moving_average(closes, int(parameters["fast_period"]))
        slow = simple_moving_average(closes, int(parameters["slow_period"]))
        evidence["indicators"] = {"fast_ma": fast[index], "slow_ma": slow[index]}
        evidence["reason_codes"] = ["MA_CROSS"]
        return evidence

    if engine_key == "macd_momentum":
        fast = exponential_moving_average(closes, int(parameters["fast_period"]))
        slow = exponential_moving_average(closes, int(parameters["slow_period"]))
        macd = [
            left - right if left is not None and right is not None else None
            for left, right in zip(fast, slow, strict=True)
        ]
        signal_line = optional_exponential_moving_average(
            macd, int(parameters["signal_period"])
        )
        histogram = [
            value - signal if value is not None and signal is not None else None
            for value, signal in zip(macd, signal_line, strict=True)
        ]
        evidence["indicators"] = {
            "macd": macd[index],
            "signal": signal_line[index],
            "histogram": histogram[index],
        }
        evidence["reason_codes"] = ["MACD_ZERO_CROSS"]
        return evidence

    if engine_key == "rsi_reversal":
        values = relative_strength_index(closes, int(parameters["period"]))
        evidence["indicators"] = {
            "rsi": values[index],
            "oversold": float(parameters["oversold"]),
            "overbought": float(parameters["overbought"]),
        }
        evidence["reason_codes"] = ["RSI_REENTRY"]
        return evidence

    if engine_key == "bollinger_reversion":
        middle, lower, upper = bollinger_bands(
            closes, int(parameters["period"]), float(parameters["stddev"])
        )
        evidence["indicators"] = {
            "middle": middle[index],
            "lower": lower[index],
            "upper": upper[index],
        }
        evidence["reason_codes"] = ["BOLLINGER_REENTRY"]
        return evidence

    fast = simple_moving_average(closes, int(parameters["fast_period"]))
    slow = simple_moving_average(closes, int(parameters["slow_period"]))
    ema_fast = exponential_moving_average(closes, 12)
    ema_slow = exponential_moving_average(closes, 26)
    rsi = relative_strength_index(closes, int(parameters["rsi_period"]))
    _, lower, upper = bollinger_bands(closes, 20, 2)
    components: list[dict[str, Any]] = []
    score = 0

    ma_value = 1 if fast[index] > slow[index] else -1
    score += ma_value
    components.append(
        {"code": "MA_BULLISH" if ma_value > 0 else "MA_BEARISH", "value": ma_value}
    )
    macd_value = 1 if ema_fast[index] > ema_slow[index] else -1
    score += macd_value
    components.append(
        {
            "code": "MACD_BULLISH" if macd_value > 0 else "MACD_BEARISH",
            "value": macd_value,
        }
    )
    rsi_value = 0
    if rsi[index] is not None and rsi[index] <= 35:
        rsi_value = 1
    elif rsi[index] is not None and rsi[index] >= 65:
        rsi_value = -1
    score += rsi_value
    components.append(
        {
            "code": "RSI_OVERSOLD" if rsi_value > 0 else "RSI_OVERBOUGHT" if rsi_value < 0 else "RSI_NEUTRAL",
            "value": rsi_value,
        }
    )
    band_value = 0
    if lower[index] is not None and closes[index] < lower[index]:
        band_value = 1
    elif upper[index] is not None and closes[index] > upper[index]:
        band_value = -1
    score += band_value
    components.append(
        {
            "code": "BELOW_LOWER_BAND" if band_value > 0 else "ABOVE_UPPER_BAND" if band_value < 0 else "INSIDE_BANDS",
            "value": band_value,
        }
    )
    evidence.update(
        {
            "score": score,
            "threshold": float(parameters["threshold"]),
            "components": components,
            "reason_codes": [item["code"] for item in components],
            "indicators": {
                "fast_ma": fast[index],
                "slow_ma": slow[index],
                "ema12": ema_fast[index],
                "ema26": ema_slow[index],
                "rsi": rsi[index],
                "bollinger_lower": lower[index],
                "bollinger_upper": upper[index],
            },
        }
    )
    return evidence


def _strategy_signal(
    account: dict[str, Any], symbol: str, snapshot: dict[str, Any] | None = None
) -> tuple[int, float | None, list[str], int | None, dict[str, Any]]:
    selected = snapshot or (_strategy_snapshots(account)[0] if _strategy_snapshots(account) else {})
    if selected.get("strategy_kind") == "source_strategy":
        return _source_strategy_signal(account, selected, symbol)
    if selected.get("strategy_kind") == "full_strategy":
        return _full_strategy_signal(account, selected, symbol)

    engine_key = str(selected.get("engine_key") or "multi_factor")
    parameters = _json_object(selected.get("parameters"))
    try:
        timeframe = resolve_legacy_strategy_timeframe(
            selected,
            account.get("config_json")
            if isinstance(account.get("config_json"), dict)
            else None,
        )
    except StrategyEvaluationError:
        return 0, None, [], None, {}
    candles, rows = _candles(symbol, timeframe)
    if len(candles) < 3:
        return 0, None, [], None, {}
    try:
        signals = DEFAULT_STRATEGY_EVALUATOR.evaluate(engine_key, candles, parameters)
    except (KeyError, TypeError, ValueError):
        return 0, None, [], None, {}
    direction = int(signals[-1]) if signals else 0
    atr = None
    if len(rows) > 15:
        atr = ind.atr(
            [float(row["high"]) for row in rows],
            [float(row["low"]) for row in rows],
            [float(row["close"]) for row in rows],
        )
    basis = [
        f"策略：{selected.get('name') or engine_key}",
        f"引擎：{engine_key}",
        f"周期：{timeframe}",
    ]
    try:
        evidence = _engine_signal_evidence(engine_key, candles, parameters)
    except (KeyError, TypeError, ValueError):
        evidence = {"engine_key": engine_key}
    reason_codes = evidence.get("reason_codes")
    if isinstance(reason_codes, list) and reason_codes:
        basis.append(f"信号：{' / '.join(str(item) for item in reason_codes)}")
    if evidence.get("score") is not None:
        basis.append(
            f"实际评分：{evidence['score']:+g} / 阈值：{evidence.get('threshold', '--')}"
        )
    return direction, atr, basis, int(rows[-1]["open_time"]), evidence


def _paper_strategy_signal(
    account: dict[str, Any], symbol: str, price: float | None = None
) -> tuple[int, float | None, list[str], int | None, dict[str, Any]]:
    if _paper_signal_mode(account) == LEGACY_PAPER_SIGNAL_MODE:
        return _legacy_score_signal(account, symbol, price)
    snapshots = _strategy_snapshots(account)
    if len(snapshots) <= 1:
        return _strategy_signal(account, symbol, snapshots[0] if snapshots else None)
    return _combined_strategy_signal(account, snapshots, symbol)


def _signal_consumption_value(
    signal_time: int, signal_evidence: dict[str, Any] | None
) -> int | str:
    evidence = signal_evidence if isinstance(signal_evidence, dict) else {}
    combination_key = evidence.get("combination_key")
    return str(combination_key) if combination_key else signal_time


def _combined_strategy_signal(
    account: dict[str, Any],
    snapshots: list[dict[str, Any]],
    symbol: str,
) -> tuple[int, float | None, list[str], int | None, dict[str, Any]]:
    """Require every selected strategy to emit the same non-zero direction."""

    results = [
        _strategy_signal(account, symbol, snapshot)
        for snapshot in snapshots
    ]
    directions = [int(result[0]) for result in results]
    signal_times = [result[3] for result in results]
    all_same_direction = (
        bool(directions)
        and directions[0] in {-1, 1}
        and all(direction == directions[0] for direction in directions)
        and all(signal_time is not None for signal_time in signal_times)
    )
    direction = directions[0] if all_same_direction else 0
    matched_count = (
        len(directions)
        if all_same_direction
        else max(directions.count(1), directions.count(-1))
    )
    components: list[dict[str, Any]] = []
    combined_basis = [
        f"组合条件：{matched_count}/{len(snapshots)} 策略同向满足（全部满足才开仓）"
    ]
    for snapshot, result in zip(snapshots, results, strict=True):
        component_direction, component_atr, basis, signal_time, evidence = result
        combined_basis.extend(basis)
        components.append(
            {
                "strategy_id": snapshot.get("public_id"),
                "strategy_name": snapshot.get("name"),
                "strategy_kind": snapshot.get("strategy_kind"),
                "engine_key": snapshot.get("engine_key"),
                "direction": int(component_direction),
                "atr": component_atr,
                "signal_time": signal_time,
                "evidence": evidence,
            }
        )
    primary_evidence = results[0][4] if results and isinstance(results[0][4], dict) else {}
    aggregate_time = max(
        (int(value) for value in signal_times if value is not None),
        default=None,
    )
    combination_key = "|".join(
        f"{snapshot.get('public_id') or index}:{signal_time}:{component_direction}"
        for index, (snapshot, signal_time, component_direction) in enumerate(
            zip(snapshots, signal_times, directions, strict=True)
        )
    )
    evidence = {
        "combination_mode": "all",
        "required_count": len(snapshots),
        "matched_count": matched_count,
        "combination_key": combination_key,
        "strategy_signals": components,
        "risk_proposal": primary_evidence.get("risk_proposal"),
        "score": primary_evidence.get("score"),
    }
    atr = next((result[1] for result in results if result[1] is not None), None)
    return direction, atr, combined_basis, aggregate_time, evidence


def _full_strategy_signal(
    account: dict[str, Any], snapshot: dict[str, Any], symbol: str
) -> tuple[int, float | None, list[str], int | None, dict[str, Any]]:
    """Evaluate one immutable full-strategy snapshot on its declared timeframes."""

    raw_spec = snapshot.get("spec") or snapshot.get("spec_json")
    try:
        spec = validate_strategy_spec(raw_spec)
        timeframes = set(spec["timeframes"].values())
        market = {
            timeframe: store.get_klines(symbol, timeframe, 600)
            for timeframe in timeframes
        }
        decision = evaluate_strategy(spec, market)
    except (StrategySpecError, StrategyMarketDataError, KeyError, TypeError, ValueError) as exc:
        return 0, None, [f"完整策略不可用：{type(exc).__name__}"], None, {}

    direction = {
        "LONG_ENTRY": 1,
        "SHORT_ENTRY": -1,
    }.get(decision.decision, 0)
    setup = decision.evidence.get("setup")
    atr = None
    if isinstance(setup, dict):
        try:
            candidate = float(setup.get("atr"))
        except (TypeError, ValueError):
            candidate = math.nan
        if math.isfinite(candidate) and candidate > 0:
            atr = candidate
    basis = [
        f"策略：{snapshot.get('name') or spec['strategy_type']}",
        "类型：完整策略",
        f"周期：{spec['timeframes']['regime']}/{spec['timeframes']['setup']}/{spec['timeframes']['trigger']}",
        f"决策：{decision.decision}",
    ]
    if decision.reason_codes:
        basis.append(f"依据：{' / '.join(decision.reason_codes)}")
    if decision.confidence is not None:
        basis.append(f"置信度：{decision.confidence:.2%}")
    if direction and not _record_full_strategy_decision(
        account, symbol, spec, decision, snapshot
    ):
        return 0, atr, [*basis, "信号未执行：缺少可审计的策略部署记录"], decision.signal_time, {}
    evidence = {
        "decision": decision.decision,
        "confidence": decision.confidence,
        "valid_until": decision.valid_until,
        "reason_codes": list(decision.reason_codes),
        "evidence": decision.evidence,
        "risk_proposal": decision.risk_proposal,
    }
    return direction, atr, basis, decision.signal_time, evidence


def _source_strategy_signal(
    account: dict[str, Any], snapshot: dict[str, Any], symbol: str
) -> tuple[int, float | None, list[str], int | None, dict[str, Any]]:
    """Evaluate one immutable Python source revision in the isolated worker."""

    source_code = snapshot.get("source_code")
    language = str(snapshot.get("source_language") or "python")
    parameters = _json_object(snapshot.get("parameters"))
    try:
        if not isinstance(source_code, str) or not source_code.strip():
            raise StrategySourceError("策略源码不可用")
        metadata = validate_source(source_code, language)
        market = {
            timeframe: [
                {
                    "open_time": int(row["open_time"]),
                    "open": float(row["open"]),
                    "high": float(row["high"]),
                    "low": float(row["low"]),
                    "close": float(row["close"]),
                    "volume": float(row["volume"]),
                }
                for row in store.get_klines(symbol, timeframe, metadata.lookback_bars)
            ]
            for timeframe in metadata.timeframes
        }
        decision = evaluate_source(
            source_code,
            {
                "symbol": symbol,
                "decision_time": int(time.time()),
                "bars": market,
            },
            parameters,
            language=language,
        )
    except (
        StrategySourceError,
        StrategySourceExecutionError,
        KeyError,
        TypeError,
        ValueError,
    ) as exc:
        return 0, None, [f"源码策略不可用：{type(exc).__name__}"], None, {}

    direction = {"LONG_ENTRY": 1, "SHORT_ENTRY": -1}.get(decision.decision, 0)
    atr = None
    for candidate_source in (decision.evidence, decision.risk_proposal):
        try:
            candidate = float(candidate_source.get("atr"))
        except (AttributeError, TypeError, ValueError):
            candidate = math.nan
        if math.isfinite(candidate) and candidate > 0:
            atr = candidate
            break
    basis = [
        f"策略：{snapshot.get('name') or 'Python 源码策略'}",
        f"类型：源码策略 / {language}",
        f"周期：{'/'.join(metadata.timeframes)}（触发 {metadata.trigger_timeframe}）",
        f"决策：{decision.decision}",
    ]
    if decision.reason_codes:
        basis.append(f"依据：{' / '.join(decision.reason_codes)}")
    if decision.confidence is not None:
        basis.append(f"置信度：{decision.confidence:.2%}")
    audit_spec = {"timeframes": {"trigger": metadata.trigger_timeframe}}
    if direction and not _record_full_strategy_decision(
        account, symbol, audit_spec, decision, snapshot
    ):
        return 0, atr, [*basis, "信号未执行：缺少可审计的策略部署记录"], decision.signal_time, {}
    evidence = {
        "source": "python_source_strategy_v1",
        "source_hash": metadata.source_hash,
        "runtime_version": metadata.runtime_version,
        "decision": decision.decision,
        "confidence": decision.confidence,
        "valid_until": decision.valid_until,
        "reason_codes": list(decision.reason_codes),
        "evidence": decision.evidence,
        "risk_proposal": decision.risk_proposal,
    }
    return direction, atr, basis, decision.signal_time, evidence


def _record_full_strategy_decision(
    account: dict[str, Any],
    symbol: str,
    spec: dict[str, Any],
    decision: Any,
    snapshot: dict[str, Any] | None = None,
) -> bool:
    deployment_mode = str(account.get("deployment_mode") or "paper")
    if deployment_mode not in {"paper", "live"}:
        return False
    strategy_public_id = (snapshot or {}).get("public_id")
    if strategy_public_id:
        deployments = store.query(
            """SELECT d.id,d.strategy_revision_id FROM strategy_deployments d
               JOIN user_strategies s ON s.id=d.strategy_id AND s.user_id=d.user_id
               WHERE d.user_id=? AND d.mode=? AND d.target_account_id=?
                 AND d.status='running' AND s.public_id=?
               ORDER BY d.id DESC LIMIT 1""",
            (account["user_id"], deployment_mode, account["id"], strategy_public_id),
        )
    else:
        deployments = store.query(
            """SELECT id,strategy_revision_id FROM strategy_deployments
               WHERE user_id=? AND mode=? AND target_account_id=? AND status='running'
               ORDER BY id DESC LIMIT 1""",
            (account["user_id"], deployment_mode, account["id"]),
        )
    if not deployments or decision.signal_time is None:
        return False
    deployment = deployments[0]
    timeframe = str(spec["timeframes"]["trigger"])
    idempotency_key = (
        f"{deployment_mode}:{deployment['id']}:{deployment['strategy_revision_id']}:"
        f"{symbol}:{timeframe}:{decision.signal_time}:{decision.decision}"
    )
    opportunity_direction = {
        "LONG_ENTRY": "long",
        "SHORT_ENTRY": "short",
    }.get(decision.decision)
    opportunity_id = None
    if opportunity_direction:
        opportunities = store.query(
            """SELECT id FROM market_opportunities
               WHERE symbol=? AND direction=?
                 AND status IN ('detected','watching','confirmed')
                 AND detected_bar_time<=? AND expires_bar_time>=?
               ORDER BY quality_score DESC,detected_bar_time DESC,id DESC LIMIT 1""",
            (
                symbol,
                opportunity_direction,
                decision.signal_time,
                decision.signal_time,
            ),
        )
        if opportunities:
            opportunity_id = opportunities[0]["id"]
    valid_until = decision.valid_until
    if valid_until is not None and int(valid_until) >= 100_000_000_000:
        valid_until = int(valid_until) // 1_000
    try:
        store.execute(
            """INSERT IGNORE INTO strategy_signals(
                   public_id,user_id,deployment_id,strategy_revision_id,opportunity_id,
                   symbol,timeframe,signal_bar_time,decision,confidence,status,valid_until,
                   reason_codes_json,evidence_json,risk_decision_json,idempotency_key,created_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?,'approved',FROM_UNIXTIME(?),?,?,?,?,CURRENT_TIMESTAMP)""",
            (
                str(uuid.uuid4()),
                account["user_id"],
                deployment["id"],
                deployment["strategy_revision_id"],
                opportunity_id,
                symbol,
                timeframe,
                decision.signal_time,
                decision.decision,
                decision.confidence,
                valid_until,
                json.dumps(list(decision.reason_codes), ensure_ascii=False),
                json.dumps(decision.evidence, ensure_ascii=False),
                json.dumps(decision.risk_proposal, ensure_ascii=False),
                idempotency_key,
            ),
        )
        store.execute(
            """UPDATE strategy_deployments
               SET last_evaluated_bar_time=?,last_error_code=NULL,updated_at=CURRENT_TIMESTAMP
               WHERE id=? AND user_id=?""",
            (decision.signal_time, deployment["id"], account["user_id"]),
        )
    except Exception as exc:
        print(f"[paper] full strategy signal persistence failed: {type(exc).__name__}")
        return False
    return True


def build_entry_basis_snapshot(
    account: dict[str, Any],
    *,
    mode: str,
    symbol: str,
    direction: int,
    signal_time: int | None,
    reasons: list[str],
    evidence: dict[str, Any] | None,
    entry_price: float,
    atr: float | None,
    stop: float | None,
    target: float | None,
    leverage: int,
    margin: float | None,
) -> tuple[dict[str, Any], int | None]:
    """Build one immutable, self-contained entry audit snapshot."""

    strategies = _strategy_snapshots(account)
    strategy = dict(strategies[0]) if strategies else {}
    signal_evidence = dict(evidence or {})
    strategy_signal_id = None
    strategy_revision_id = account.get("strategy_revision_id")
    deployment_id = account.get("deployment_id")
    if deployment_id is not None and signal_time is not None:
        try:
            rows = store.query(
                """SELECT id,strategy_revision_id,decision,confidence,reason_codes_json,
                          evidence_json,risk_decision_json
                   FROM strategy_signals
                   WHERE user_id=? AND deployment_id=? AND symbol=? AND signal_bar_time=?
                     AND decision IN ('LONG_ENTRY','SHORT_ENTRY')
                   ORDER BY id DESC LIMIT 1""",
                (account["user_id"], deployment_id, symbol, signal_time),
            )
        except Exception as exc:
            print(f"[{mode}] strategy signal lookup failed: {type(exc).__name__}")
            rows = []
        if rows:
            signal = dict(rows[0])
            strategy_signal_id = int(signal["id"])
            strategy_revision_id = signal.get("strategy_revision_id")
            persisted_evidence = _json_object(signal.get("evidence_json"))
            if persisted_evidence:
                signal_evidence = persisted_evidence
            persisted_reasons = signal.get("reason_codes_json")
            if isinstance(persisted_reasons, str):
                try:
                    persisted_reasons = json.loads(persisted_reasons)
                except (TypeError, ValueError, json.JSONDecodeError):
                    persisted_reasons = None
            if isinstance(persisted_reasons, list) and persisted_reasons:
                reasons = [*reasons, f"信号代码：{' / '.join(map(str, persisted_reasons))}"]
            signal_evidence = {
                **signal_evidence,
                "decision": signal.get("decision"),
                "confidence": (
                    float(signal["confidence"])
                    if signal.get("confidence") is not None
                    else signal_evidence.get("confidence")
                ),
                "risk_decision": _json_object(signal.get("risk_decision_json")),
            }

    score = signal_evidence.get("score")
    snapshot = {
        "schema_version": ENTRY_BASIS_SCHEMA_VERSION,
        "availability": "captured",
        "mode": mode,
        "captured_at": int(time.time()),
        "symbol": symbol,
        "direction": "long" if direction > 0 else "short",
        "reasons": list(dict.fromkeys(str(item) for item in reasons if item)),
        "strategy": {
            "public_id": strategy.get("public_id"),
            "name": strategy.get("name"),
            "kind": strategy.get("strategy_kind"),
            "engine_key": strategy.get("engine_key"),
            "version": strategy.get("version"),
            "spec_schema_version": strategy.get("spec_schema_version"),
            "spec_hash": strategy.get("spec_hash"),
            "parameters": strategy.get("parameters"),
        },
        "strategies": [
            {
                "public_id": item.get("public_id"),
                "name": item.get("name"),
                "kind": item.get("strategy_kind"),
                "engine_key": item.get("engine_key"),
                "version": item.get("version"),
                "spec_schema_version": item.get("spec_schema_version"),
                "spec_hash": item.get("spec_hash"),
                "parameters": item.get("parameters"),
            }
            for item in strategies
        ],
        "combination_mode": "all",
        "signal": {
            "strategy_signal_id": strategy_signal_id,
            "deployment_id": deployment_id,
            "strategy_revision_id": strategy_revision_id,
            "bar_time": signal_time,
            "score": score,
            "evidence": signal_evidence,
        },
        "execution": {
            "entry_price": entry_price,
            "atr": atr,
            "stop": stop,
            "target": target,
            "leverage": leverage,
            "margin": margin,
        },
    }
    return snapshot, strategy_signal_id


def _execution_price(price: float, side: int, opening: bool, slippage_bps: float) -> float:
    adverse = (side > 0) == opening
    adjustment = price * slippage_bps / 10_000
    return price + adjustment if adverse else price - adjustment


def _exit_levels(
    entry: float,
    side: int,
    atr: float | None,
    config: dict[str, float | int],
) -> tuple[float | None, float | None]:
    """Return direction-safe stop and take-profit levels for one position.

    The default paper strategy is ATR based. Percentage values remain a fallback
    for symbols that do not yet have enough configured-timeframe candles for ATR.
    """

    entry = float(entry)
    if side not in {-1, 1} or not math.isfinite(entry) or entry <= 0:
        return None, None
    try:
        atr_value = float(atr) if atr is not None else 0.0
    except (TypeError, ValueError):
        atr_value = 0.0
    use_atr = math.isfinite(atr_value) and atr_value > 0
    if use_atr:
        stop_distance = ATR_STOP_MULTIPLIER * atr_value
        take_distance = ATR_TAKE_PROFIT_MULTIPLIER * atr_value
    else:
        stop_distance = entry * float(config["stop_loss_pct"]) / 100
        take_distance = entry * float(config["take_profit_pct"]) / 100

    stop = entry - side * stop_distance if stop_distance > 0 else None
    target = entry + side * take_distance if take_distance > 0 else None
    if stop is not None and (
        not math.isfinite(stop) or stop <= 0 or (entry - stop) * side <= 0
    ):
        fallback = entry * float(config["stop_loss_pct"]) / 100
        stop = entry - side * fallback if fallback > 0 else None
    if target is not None and (
        not math.isfinite(target) or target <= 0 or (target - entry) * side <= 0
    ):
        fallback = entry * float(config["take_profit_pct"]) / 100
        target = entry + side * fallback if fallback > 0 else None
    if stop is not None and (
        not math.isfinite(stop) or stop <= 0 or (entry - stop) * side <= 0
    ):
        stop = None
    if target is not None and (
        not math.isfinite(target) or target <= 0 or (target - entry) * side <= 0
    ):
        target = None
    return stop, target


def _signal_exit_levels(
    entry: float,
    side: int,
    atr: float | None,
    config: dict[str, float | int],
    signal_evidence: dict[str, Any] | None,
) -> tuple[float | None, float | None]:
    """Use the full strategy's immutable distances, else the legacy ATR rule."""

    proposal = (signal_evidence or {}).get("risk_proposal")
    if isinstance(proposal, dict):
        try:
            stop_distance = float(proposal["stop_distance"])
            take_distance = float(proposal["take_profit_distance"])
        except (KeyError, TypeError, ValueError):
            return None, None
        if (
            math.isfinite(stop_distance)
            and math.isfinite(take_distance)
            and stop_distance > 0
            and take_distance > 0
        ):
            stop = entry - side * stop_distance
            target = entry + side * take_distance
            if stop > 0 and target > 0:
                return stop, target
        return None, None
    return _exit_levels(entry, side, atr, config)


def _repair_missing_target(
    account: dict[str, Any],
    position: dict[str, Any],
    atr: float | None,
    config: dict[str, float | int],
) -> None:
    """Backfill existing zero or invalid targets before evaluating exits."""

    entry = float(position["avg_entry"])
    side = int(position["side"])
    try:
        current_target = float(position.get("target") or 0)
    except (TypeError, ValueError):
        current_target = 0.0
    if (
        math.isfinite(current_target)
        and current_target > 0
        and (current_target - entry) * side > 0
    ):
        return
    _, target = _exit_levels(entry, side, position.get("atr_entry") or atr, config)
    if target is None:
        return
    store.execute(
        """UPDATE paper_positions SET target=?
           WHERE id=? AND paper_account_id=? AND user_id=?""",
        (target, position["id"], account["id"], account["user_id"]),
    )
    position["target"] = target


def _open_position(
    account: dict[str, Any],
    symbol: str,
    side: int,
    price: float,
    atr: float | None,
    basis: list[str],
    positions: list[dict[str, Any]],
    now: int,
    signal_time: int | None = None,
    signal_evidence: dict[str, Any] | None = None,
) -> bool:
    config = _config(account)
    try:
        policy = _paper_risk_policy(account, signal_evidence)
    except ValueError:
        return False
    equity, _ = _equity(account, _prices(), positions)
    available = min(
        float(account["balance"]),
        equity * float(config["margin_cap"]) - _used_margin(positions),
    )
    if not math.isfinite(equity) or equity <= 0 or available <= 0:
        return False
    execution = _execution_price(price, side, True, float(config["slippage_bps"]))
    stop, target = _signal_exit_levels(execution, side, atr, config, signal_evidence)
    if stop is None or target is None:
        return False
    stop_distance = abs(Decimal(str(execution)) - Decimal(str(stop)))
    leverage = int(config["leverage"])
    entry_fee_rate = float(config["fee_bps"]) / 10_000
    available_for_margin = available / (1 + leverage * entry_fee_rate)
    sizing = atr_risk_position_size(
        equity=equity,
        available_balance=available_for_margin,
        entry_price=execution,
        stop_distance=stop_distance,
        requested_leverage=leverage,
        current_open_risk=_current_open_risk(positions, config, policy),
        direction=side,
        high_risk=symbol_risk_profile(symbol).high_risk,
        policy=policy,
    )
    if not sizing.allowed:
        return False
    quantity = float(sizing.quantity)
    notional = quantity * execution
    margin = notional / sizing.effective_leverage
    leverage = sizing.effective_leverage
    fee = notional * entry_fee_rate
    if (
        not math.isfinite(quantity)
        or quantity <= 0
        or not math.isfinite(margin)
        or margin <= 0
        or margin + fee > available + 1e-8
    ):
        return False
    liquidation = execution * (1 - side * (1 / leverage - 0.005))
    entry_basis, _ = build_entry_basis_snapshot(
        account,
        mode="paper",
        symbol=symbol,
        direction=side,
        signal_time=signal_time,
        reasons=basis,
        evidence=signal_evidence,
        entry_price=execution,
        atr=atr,
        stop=stop,
        target=target,
        leverage=leverage,
        margin=margin,
    )
    entry_basis["execution"].update(
        {
            "entry_fee": fee,
            "fee_bps": float(config["fee_bps"]),
            "slippage_bps": float(config["slippage_bps"]),
            "estimated_loss_at_stop": float(sizing.estimated_loss_at_stop),
            "risk_budget": float(sizing.risk_budget),
        }
    )
    score = entry_basis["signal"].get("score")
    stored_score = int(score) if isinstance(score, (int, float)) else None
    debit = margin + fee
    with store.transaction() as transaction:
        balance_rows = transaction.query(
            """SELECT balance FROM paper_accounts
               WHERE id=? AND user_id=? FOR UPDATE""",
            (account["id"], account["user_id"]),
        )
        if not balance_rows:
            return False
        try:
            locked_balance = float(balance_rows[0]["balance"])
        except (KeyError, TypeError, ValueError):
            return False
        if (
            not math.isfinite(locked_balance)
            or locked_balance < 0
            or locked_balance + 1e-8 < debit
        ):
            return False
        new_balance = max(round(locked_balance - debit, 8), 0.0)
        transaction.execute(
            """UPDATE paper_accounts SET balance=?,updated_at=CURRENT_TIMESTAMP
               WHERE id=? AND user_id=?""",
            (new_balance, account["id"], account["user_id"]),
        )
        transaction.execute(
            """INSERT INTO paper_positions(
               paper_account_id,user_id,symbol,side,qty,avg_entry,margin,leverage,stop,target,
                   adds,opened_ts,last_add_ts,open_score,basis,funding_acc,liq_price,funding_ts,
                   atr_entry,peak_price,tp_done
               ) VALUES(?,?,?,?,?,?,?,?,?,?,0,?,?,?, ?,0,?,?,?,?,0)""",
            (
                account["id"], account["user_id"], symbol, side, quantity, execution,
                margin, leverage, stop, target, now, now, stored_score,
                json.dumps(entry_basis, ensure_ascii=False), liquidation, now, atr,
                execution,
            ),
        )
    account["balance"] = new_balance
    direction = "long" if side > 0 else "short"
    store.add_alert(
        symbol,
        "paper_open",
        direction,
        None,
        f"模拟盘【{account['name']}】{symbol} {direction} 开仓 @ {execution:.8g}",
        {
            "paper_account_id": account["public_id"],
            "paper_account_name": account["name"],
            "strategy_name": _strategy_display_name(account),
            "entry_basis": entry_basis,
            "price": execution,
            "quantity": quantity,
        },
        user_id=account["user_id"],
    )
    return True


def _close_position(
    account: dict[str, Any], position: dict[str, Any], price: float, reason: str, now: int
) -> bool:
    config = _config(account)
    execution = _execution_price(
        price, int(position["side"]), False, float(config["slippage_bps"])
    )
    quantity = float(position["qty"])
    fee = quantity * execution * float(config["fee_bps"]) / 10_000
    pnl = (
        (execution - float(position["avg_entry"])) * quantity * int(position["side"])
        - float(position.get("funding_acc") or 0)
    )
    margin = float(position["margin"])
    returned = max(margin + pnl - fee, 0.0)
    with store.transaction() as transaction:
        ownership = transaction.query(
            """SELECT a.balance FROM paper_positions p
               JOIN paper_accounts a ON a.id=p.paper_account_id AND a.user_id=p.user_id
               WHERE p.id=? AND p.paper_account_id=? AND p.user_id=? FOR UPDATE""",
            (position["id"], account["id"], account["user_id"]),
        )
        if not ownership:
            return False
        deleted = transaction.execute(
            "DELETE FROM paper_positions WHERE id=? AND paper_account_id=? AND user_id=?",
            (position["id"], account["id"], account["user_id"]),
        )
        if deleted != 1:
            return False
        new_balance = max(round(float(ownership[0]["balance"]) + returned, 8), 0.0)
        transaction.execute(
            """UPDATE paper_accounts SET balance=?,updated_at=CURRENT_TIMESTAMP
               WHERE id=? AND user_id=?""",
            (new_balance, account["id"], account["user_id"]),
        )
        transaction.execute(
            """INSERT INTO paper_trades(
                   paper_account_id,user_id,symbol,side,qty,entry_price,exit_price,margin,pnl,
                   fee,funding,reason,open_score,opened_ts,closed_ts,entry_basis_json
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                account["id"], account["user_id"], position["symbol"], position["side"],
                quantity, position["avg_entry"], execution, margin, max(pnl, -margin), fee,
                position.get("funding_acc") or 0, reason, position.get("open_score"),
                position["opened_ts"], now, position.get("basis"),
            ),
        )
    account["balance"] = new_balance
    direction = "long" if int(position["side"]) > 0 else "short"
    store.add_alert(
        position["symbol"],
        "paper_close",
        direction,
        None,
        f"模拟盘【{account['name']}】{position['symbol']} {direction} 平仓"
        f" @ {execution:.8g}，盈亏 {pnl - fee:+.2f} USDT（{reason}）",
        {
            "paper_account_id": account["public_id"],
            "paper_account_name": account["name"],
            "strategy_name": _strategy_display_name(account),
            "price": execution,
            "pnl": pnl - fee,
            "reason": reason,
        },
        user_id=account["user_id"],
    )
    return True


def _tick_account(account: dict[str, Any], prices: dict[str, float], now: int) -> None:
    config = _config(account)
    policy = _paper_risk_policy(account)
    positions = _positions(account)
    closed_symbols: set[str] = set()
    for position in list(positions):
        price = prices.get(position["symbol"])
        funding_price = (
            float(price)
            if price is not None and math.isfinite(float(price)) and float(price) > 0
            else float(position["avg_entry"])
        )
        _accrue_estimated_funding(account, position, funding_price, now, config)
        if (
            not price
            or not math.isfinite(float(price))
            or float(price) <= 0
            or not _price_is_fresh(prices, position["symbol"], now, policy)
        ):
            continue
        side = int(position["side"])
        if side not in {-1, 1}:
            print(f"[paper] invalid side skipped: position={position.get('id')}")
            continue
        # Stored exchange-risk levels must remain effective even when indicator
        # calculation or historical market data is temporarily unavailable.
        _repair_missing_target(account, position, None, config)
        held_bars = max((now - int(position["opened_ts"])) // (4 * 3600), 0)
        reason = None
        if position.get("liq_price") and (
            (side > 0 and price <= float(position["liq_price"]))
            or (side < 0 and price >= float(position["liq_price"]))
        ):
            reason = "liquidation"
        elif position.get("stop") and (
            (side > 0 and price <= float(position["stop"]))
            or (side < 0 and price >= float(position["stop"]))
        ):
            reason = "stop_loss"
        elif position.get("target") and (
            (side > 0 and price >= float(position["target"]))
            or (side < 0 and price <= float(position["target"]))
        ):
            reason = "take_profit"
        else:
            direction = 0
            signal_time = None
            signal_evidence: dict[str, Any] = {}
            try:
                direction, _, _, signal_time, signal_evidence = _paper_strategy_signal(
                    account, position["symbol"], price
                )
            except Exception as exc:
                print(
                    f"[paper] signal unavailable for {position['symbol']}: "
                    f"{type(exc).__name__}"
                )
            if (
                signal_time is not None
                and _paper_signal_is_fresh(
                    account, signal_time, signal_evidence, now, policy
                )
                and direction == -side
            ):
                reason = "strategy_reversal"
            elif config["max_holding_bars"] and held_bars >= config["max_holding_bars"]:
                reason = "max_holding_bars"
        if reason:
            if _close_position(account, position, price, reason, now):
                closed_symbols.add(position["symbol"])
            positions = _positions(account)

    universe = tradfi_symbols()
    equity, _ = _equity(account, prices, positions)
    occupied_symbols = {str(position["symbol"]).upper() for position in positions}
    candidates = [
        symbol
        for symbol in universe
        if symbol not in closed_symbols and symbol not in occupied_symbols
    ]
    allow_new_entries = bool(candidates) and _entry_loss_guard(
        account, equity, now, policy
    )
    if allow_new_entries and len(positions) < int(config["max_positions"]):
        for symbol in candidates:
            if len(positions) >= int(config["max_positions"]):
                break
            if symbol in closed_symbols:
                continue
            if symbol in occupied_symbols:
                continue
            price = prices.get(symbol)
            if (
                not price
                or not _price_is_fresh(prices, symbol, now, policy)
                or not symbol_admission(
                    symbol, sorted(occupied_symbols), policy=policy
                ).allowed
            ):
                continue
            direction, atr, basis, signal_time, signal_evidence = _paper_strategy_signal(
                account, symbol, price
            )
            if (
                direction not in {-1, 1}
                or signal_time is None
                or not _paper_signal_is_fresh(
                    account, signal_time, signal_evidence, now, policy
                )
            ):
                continue
            signal_mode = _paper_signal_mode(account)
            state_key = f"paper:{account['id']}:signal:{symbol}"
            consumption_value = _signal_consumption_value(signal_time, signal_evidence)
            if (
                signal_mode == STRATEGY_EVENT_SIGNAL_MODE
                and store.user_state_get(account["user_id"], state_key)
                == consumption_value
            ):
                continue
            if _open_position(
                account,
                symbol,
                direction,
                price,
                atr,
                basis,
                positions,
                now,
                signal_time,
                signal_evidence,
            ):
                if signal_mode == STRATEGY_EVENT_SIGNAL_MODE:
                    store.user_state_set(
                        account["user_id"], state_key, consumption_value
                    )
                positions = _positions(account)
                occupied_symbols = {
                    str(position["symbol"]).upper() for position in positions
                }

    _record_equity(account, prices, positions, now)


def _record_equity(
    account: dict[str, Any], prices: dict[str, float], positions: list[dict[str, Any]], now: int
) -> None:
    equity, _ = _equity(account, prices, positions)
    minute = now - now % 60
    store.execute(
        "REPLACE INTO paper_equity(paper_account_id,user_id,ts,equity,balance) VALUES(?,?,?,?,?)",
        (account["id"], account["user_id"], minute, round(equity, 2), account["balance"]),
    )
    store.execute(
        "UPDATE paper_accounts SET last_tick_at=CURRENT_TIMESTAMP WHERE id=? AND user_id=?",
        (account["id"], account["user_id"]),
    )


def tick(account_id: int | None = None) -> None:
    with store.advisory_lock("quantdesk-paper-tick", 0) as acquired:
        if not acquired:
            return
        prices = _prices()
        if not prices:
            return
        with _lock:
            now = int(time.time())
            for account in _tracked_accounts(account_id):
                if account["status"] == "active":
                    _tick_account(account, prices, now)
                else:
                    _record_equity(account, prices, _positions(account), now)


def paper_loop() -> None:
    print("[paper] multi-user paper engine started")
    while True:
        if store.collector_paused("paper"):
            time.sleep(5)
            continue
        try:
            tick()
            store.collector_report("paper", success=True)
        except Exception as exc:
            print("[paper] tick error:", exc)
            store.collector_report("paper", success=False, error=str(exc))
        time.sleep(5)


def reset(user_id: int, account_id: int) -> None:
    with _lock:
        _account(user_id, account_id)
        store.execute(
            "DELETE FROM paper_positions WHERE paper_account_id=? AND user_id=?",
            (account_id, user_id),
        )
        store.execute(
            "DELETE FROM paper_trades WHERE paper_account_id=? AND user_id=?",
            (account_id, user_id),
        )
        store.execute(
            "DELETE FROM paper_equity WHERE paper_account_id=? AND user_id=?",
            (account_id, user_id),
        )
        store.execute(
            "DELETE FROM user_states WHERE user_id=? AND k LIKE ?",
            (user_id, f"paper:{account_id}:%"),
        )
        store.execute(
            """UPDATE paper_accounts SET balance=initial_balance,started_at=CURRENT_TIMESTAMP,
                      last_tick_at=NULL,updated_at=CURRENT_TIMESTAMP
               WHERE id=? AND user_id=?""",
            (account_id, user_id),
        )


def api_data(
    user_id: int, account_id: int, timezone_offset_minutes: int = 0
) -> dict[str, Any]:
    account = _account(user_id, account_id)
    prices = _prices()
    positions = _positions(account)
    equity, unrealized = _equity(account, prices, positions)
    config = _config(account)
    now = int(time.time())
    today_pnl = _today_pnl(account, equity, now, timezone_offset_minutes)
    output_positions = []
    for position in positions:
        price = prices.get(position["symbol"], float(position["avg_entry"]))
        price_pnl = _upnl(position, price)
        funding = float(position.get("funding_acc") or 0)
        upnl = price_pnl - funding
        margin = float(position["margin"])
        basis = _json_object(position.get("basis"))
        output_positions.append(
            {
                **position,
                "price": price,
                "upnl": round(upnl, 2),
                "price_pnl": round(price_pnl, 2),
                "funding": round(funding, 8),
                "pnl_pct": round(upnl / margin * 100 if margin else 0, 2),
                "hold_h": round((now - int(position["opened_ts"])) / 3600, 1),
                "reasons": basis.get("reasons", []),
            }
        )
    trades = []
    for row in store.query(
            """SELECT * FROM paper_trades WHERE paper_account_id=? AND user_id=?
               ORDER BY closed_ts DESC LIMIT 100""",
            (account_id, user_id),
        ):
        trade = dict(row)
        entry_basis = _json_object(trade.get("entry_basis_json"))
        if not entry_basis:
            entry_basis = {
                "schema_version": 1,
                "availability": "legacy_missing",
                "reasons": ["历史成交未保存开仓依据，无法可靠复原"],
            }
        try:
            quantity = abs(float(trade.get("qty") or 0))
            entry_price = float(trade.get("entry_price") or 0)
            exit_price = float(trade.get("exit_price") or 0)
            side = int(trade.get("side") or 0)
            funding = float(trade.get("funding") or 0)
            margin = max(float(trade.get("margin") or 0), 0.0)
            price_pnl = (exit_price - entry_price) * quantity * side
            pnl_before_fees = max(price_pnl - funding, -margin)
        except (TypeError, ValueError, OverflowError):
            price_pnl = float(trade.get("pnl") or 0)
            pnl_before_fees = price_pnl
        execution = entry_basis.get("execution")
        captured_entry_fee = execution.get("entry_fee") if isinstance(execution, dict) else None
        try:
            entry_fee = float(captured_entry_fee)
        except (TypeError, ValueError, OverflowError):
            entry_fee = (
                abs(float(trade.get("qty") or 0) * float(trade.get("entry_price") or 0))
                * float(config["fee_bps"])
                / 10_000
            )
        exit_fee = max(float(trade.get("fee") or 0), 0.0)
        total_fee = max(entry_fee, 0.0) + exit_fee
        trade["entry_basis"] = entry_basis
        trade.update(
            {
                "price_pnl": price_pnl,
                "pnl": pnl_before_fees,
                "entry_fee": max(entry_fee, 0.0),
                "exit_fee": exit_fee,
                "fee": total_fee,
                "net_pnl": pnl_before_fees - total_fee,
            }
        )
        trades.append(trade)
    trade_count_rows = store.query(
        """SELECT COUNT(*) AS total FROM paper_trades
           WHERE paper_account_id=? AND user_id=?""",
        (account_id, user_id),
    )
    try:
        closed_trade_count = int(trade_count_rows[0]["total"])
    except (IndexError, KeyError, TypeError, ValueError, OverflowError):
        closed_trade_count = len(trades)
    curve_rows = store.query(
        """SELECT ts,equity FROM paper_equity WHERE paper_account_id=? AND user_id=?
           ORDER BY ts DESC LIMIT 2880""",
        (account_id, user_id),
    )
    curve = [(row["ts"], row["equity"]) for row in reversed(curve_rows)]
    wins = [trade for trade in trades if float(trade.get("net_pnl") or 0) > 0]
    losses = [trade for trade in trades if float(trade.get("net_pnl") or 0) <= 0]
    gross_win = sum(float(trade.get("net_pnl") or 0) for trade in wins)
    gross_loss = abs(sum(float(trade.get("net_pnl") or 0) for trade in losses))
    initial = float(account["initial_balance"])
    peak = initial
    drawdown = 0.0
    for _, value in curve:
        peak = max(peak, float(value))
        if peak:
            drawdown = max(drawdown, (peak - float(value)) / peak * 100)
    strategies = _strategy_snapshots(account)
    strategy = strategies[0] if strategies else {}
    strategy_ids = [str(item["public_id"]) for item in strategies if item.get("public_id")]
    strategy_names = [str(item["name"]) for item in strategies if item.get("name")]
    signal_mode = _paper_signal_mode(account)
    entry_count = closed_trade_count + sum(
        1 + max(int(position.get("adds") or 0), 0) for position in positions
    )
    entry_count += sum(
        len(trade.get("entry_basis", {}).get("additions", []))
        for trade in trades
        if isinstance(trade.get("entry_basis"), dict)
        and isinstance(trade["entry_basis"].get("additions"), list)
    )
    return {
        "paper_account": {
            "id": account["public_id"],
            "name": account["name"],
            "status": account["status"],
            "strategy_id": strategy.get("public_id"),
            "strategy_name": " + ".join(strategy_names) or strategy.get("name"),
            "strategy_ids": strategy_ids,
            "strategy_names": strategy_names,
            "strategies": [
                {
                    "id": item.get("public_id"),
                    "name": item.get("name"),
                    "engine_key": item.get("engine_key"),
                    "strategy_kind": item.get("strategy_kind"),
                    "version": item.get("version"),
                }
                for item in strategies
            ],
            "combination_mode": "all",
            "engine_key": strategy.get("engine_key"),
            "signal_mode": signal_mode,
        },
        "account": {
            "start": initial,
            "balance": round(float(account["balance"]), 2),
            "equity": round(equity, 2),
            "used_margin": round(_used_margin(positions), 2),
            "margin_usage": round(_used_margin(positions) / equity * 100 if equity else 0, 2),
            "upnl": round(unrealized, 2),
            "today_pnl": round(today_pnl, 2),
            "ret_pct": round((equity - initial) / initial * 100, 2),
            "leverage": config["leverage"],
            "max_positions": config["max_positions"],
            "started_ts": int(account["started_at"].replace(tzinfo=UTC).timestamp()),
        },
        "positions": output_positions,
        "trades": trades[:50],
        "curve": curve[-1440:],
        "stats": {
            "entries": entry_count,
            "trades": len(trades),
            "closed_total": closed_trade_count,
            "win_rate": round(len(wins) / len(trades) * 100 if trades else 0, 2),
            "profit_factor": round(gross_win / gross_loss, 2) if gross_loss else None,
            "max_drawdown": round(drawdown, 2),
            "realized": round(
                sum(float(trade.get("net_pnl") or 0) for trade in trades),
                2,
            ),
            "wins": len(wins),
            "losses": len(losses),
        },
        "rules": {
            "tiers": (
                "老版兼容：4h 评分达到 ±60，并通过 MA150 趋势过滤"
                if signal_mode == LEGACY_PAPER_SIGNAL_MODE
                else (
                    f"组合策略（全部满足才开仓）：{' + '.join(strategy_names)}"
                    if len(strategies) > 1
                    else f"绑定策略：{strategy.get('name') or strategy.get('engine_key')}"
                )
            ),
            "exits": "1.5×ATR 止损 / 2.5×ATR 止盈 / 策略反转 / 最大持仓周期 / 强平",
            "costs": (
                f"手续费 {config['fee_bps']} bps + 滑点 {config['slippage_bps']} bps"
                f" + 资金费率估算 {config['funding_rate_8h_bps']} bps/8h"
            ),
            "limits": f"逐仓 {config['leverage']}x / 最多 {config['max_positions']} 仓",
        },
        "disclaimer": "模拟交易仅用于策略验证与学习，不构成投资建议。",
    }
