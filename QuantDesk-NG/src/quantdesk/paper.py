"""Tenant-isolated multi-account paper trading engine."""

from __future__ import annotations

import json
import math
import threading
import time
import uuid
from datetime import UTC
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal, InvalidOperation
from typing import Any

from quantdesk_v2.backtest import _build_signals, _Candle
from quantdesk_v2.strategy_runtime import (
    StrategyMarketDataError,
    StrategySpecError,
    evaluate_strategy,
    validate_strategy_spec,
)

from . import exchange_sync, store
from . import indicators as ind
from .config_loader import tradfi_symbols

DEFAULT_INITIAL_BALANCE = 10_000.0
DEFAULT_LEVERAGE = 20
DEFAULT_MAX_POSITIONS = 15
DEFAULT_MARGIN_CAP = 0.80
DEFAULT_FEE_BPS = 5.0
DEFAULT_SLIPPAGE_BPS = 3.0
DEFAULT_STOP_LOSS_PCT = 3.0
DEFAULT_TAKE_PROFIT_PCT = 5.0
DEFAULT_MAX_HOLDING_BARS = 12
ATR_STOP_MULTIPLIER = 1.5
ATR_TAKE_PROFIT_MULTIPLIER = 2.5

_lock = threading.RLock()


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
        account["strategy_snapshot_json"] = _json_object(account.get("strategy_snapshot_json"))
        result.append(account)
    return result


def _full_strategy_spec(account: dict[str, Any]) -> dict[str, Any] | None:
    snapshot = account.get("strategy_snapshot_json") or {}
    if snapshot.get("strategy_kind") != "full_strategy":
        return None
    try:
        return validate_strategy_spec(snapshot.get("spec") or snapshot.get("spec_json"))
    except (StrategySpecError, KeyError, TypeError, ValueError):
        return None


def _config(account: dict[str, Any]) -> dict[str, float | int | bool | None]:
    raw = account["config_json"]
    config: dict[str, float | int | bool | None] = {
        "leverage": max(1, min(int(raw.get("leverage", DEFAULT_LEVERAGE)), 50)),
        "max_positions": max(1, min(int(raw.get("max_positions", DEFAULT_MAX_POSITIONS)), 50)),
        "margin_cap": max(0.05, min(float(raw.get("margin_cap", DEFAULT_MARGIN_CAP)), 0.95)),
        "position_size_pct": max(0.1, min(float(raw.get("position_size_pct", 10)), 100)),
        "fee_bps": max(0.0, min(float(raw.get("fee_bps", DEFAULT_FEE_BPS)), 100)),
        "slippage_bps": max(0.0, min(float(raw.get("slippage_bps", DEFAULT_SLIPPAGE_BPS)), 100)),
        "max_slippage_bps": max(
            0.0, min(float(raw.get("slippage_bps", DEFAULT_SLIPPAGE_BPS)), 100)
        ),
        "stop_loss_pct": max(
            0.01, min(float(raw.get("stop_loss_pct", DEFAULT_STOP_LOSS_PCT)), 99.9)
        ),
        "take_profit_pct": max(
            0.01, min(float(raw.get("take_profit_pct", DEFAULT_TAKE_PROFIT_PCT)), 99.9)
        ),
        "max_holding_bars": max(
            0, min(int(raw.get("max_holding_bars", DEFAULT_MAX_HOLDING_BARS)), 10_000)
        ),
        "risk_per_trade_pct": None,
        "max_margin_pct": None,
        "initial_stop_atr": ATR_STOP_MULTIPLIER,
        "take_profit_r": ATR_TAKE_PROFIT_MULTIPLIER / ATR_STOP_MULTIPLIER,
        "exit_on_regime_break": False,
    }
    spec = _full_strategy_spec(account)
    if spec is None:
        return config
    risk = spec["risk"]
    exit_config = spec["exit"]
    config["leverage"] = min(int(config["leverage"]), int(risk["max_leverage"]))
    config["max_positions"] = min(int(config["max_positions"]), int(risk["max_open_positions"]))
    config["risk_per_trade_pct"] = float(risk["risk_per_trade_pct"])
    config["max_margin_pct"] = float(risk["max_margin_pct"])
    config["position_size_pct"] = min(
        float(config["position_size_pct"]), float(risk["max_margin_pct"])
    )
    config["initial_stop_atr"] = float(exit_config["initial_stop_atr"])
    config["take_profit_r"] = float(exit_config["take_profit_r"])
    config["max_holding_bars"] = int(exit_config["max_holding_bars"])
    config["exit_on_regime_break"] = bool(exit_config["exit_on_regime_break"])
    execution = spec.get("execution") or {}
    config["max_slippage_bps"] = float(
        execution.get("max_slippage_bps", config["max_slippage_bps"])
    )
    return config


def _positions(account: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in store.query(
            "SELECT * FROM paper_positions WHERE paper_account_id=? AND user_id=? ORDER BY id",
            (account["id"], account["user_id"]),
        )
    ]


def _prices() -> dict[str, float]:
    prices = {
        row["symbol"]: float(row["price"])
        for row in store.query("SELECT symbol,price FROM ticker WHERE price IS NOT NULL")
    }
    prices.update(exchange_sync.mark_prices(fresh_only=True))
    return prices


def _book_quotes(now_ms: int) -> dict[str, dict[str, float]]:
    rows = store.query(
        """SELECT symbol,bid_price,ask_price,bid_qty,ask_qty,received_at
           FROM market_microstructure WHERE received_at>=?""",
        (now_ms - 15_000,),
    )
    result: dict[str, dict[str, float]] = {}
    for row in rows:
        try:
            bid = float(row["bid_price"])
            ask = float(row["ask_price"])
            if bid <= 0 or ask <= 0 or bid > ask:
                continue
            result[str(row["symbol"])] = {
                "bid": bid,
                "ask": ask,
                "bid_qty": float(row.get("bid_qty") or 0),
                "ask_qty": float(row.get("ask_qty") or 0),
                "received_at": float(row["received_at"]),
            }
        except (KeyError, TypeError, ValueError):
            continue
    return result


def _market_extremes(now_ms: int) -> dict[str, dict[str, float | int | None]]:
    """Return fresh WebSocket extrema used to detect transient protective crossings."""

    minimum_received_at = now_ms - 20_000
    rows = store.query(
        """SELECT symbol,window_low_price,window_low_event_time,
                  window_high_price,window_high_event_time
           FROM market_microstructure WHERE received_at>=?""",
        (minimum_received_at,),
    )
    result: dict[str, dict[str, float | int | None]] = {}
    for row in rows:
        try:
            result[str(row["symbol"])] = {
                "low": float(row["window_low_price"])
                if row.get("window_low_price") is not None
                else None,
                "low_time": int(row["window_low_event_time"])
                if row.get("window_low_event_time") is not None
                else None,
                "high": float(row["window_high_price"])
                if row.get("window_high_price") is not None
                else None,
                "high_time": int(row["window_high_event_time"])
                if row.get("window_high_event_time") is not None
                else None,
            }
        except (KeyError, TypeError, ValueError):
            continue
    return result


def _used_margin(positions: list[dict[str, Any]]) -> float:
    return sum(float(position["margin"]) for position in positions)


def _upnl(position: dict[str, Any], price: float) -> float:
    return (price - float(position["avg_entry"])) * float(position["qty"]) * int(position["side"])


def _equity(
    account: dict[str, Any], prices: dict[str, float], positions: list[dict[str, Any]]
) -> tuple[float, float]:
    unrealized = sum(
        _upnl(position, prices.get(position["symbol"], float(position["avg_entry"])))
        - float(position.get("funding_acc") or 0)
        for position in positions
    )
    return float(account["balance"]) + _used_margin(positions) + unrealized, unrealized


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


def _set_balance(account: dict[str, Any], balance: float) -> None:
    safe_balance = max(round(balance, 8), 0.0)
    store.execute(
        "UPDATE paper_accounts SET balance=?,updated_at=CURRENT_TIMESTAMP WHERE id=? AND user_id=?",
        (safe_balance, account["id"], account["user_id"]),
    )
    account["balance"] = safe_balance


def _candles(symbol: str) -> tuple[list[_Candle], list[dict[str, Any]]]:
    rows = store.get_klines(symbol, "4h", 600)
    candles = [
        _Candle(
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


def _strategy_signal(
    account: dict[str, Any], symbol: str
) -> tuple[int, float | None, list[str], int | None]:
    snapshot = account["strategy_snapshot_json"]
    if snapshot.get("strategy_kind") == "full_strategy":
        return _full_strategy_signal(account, snapshot, symbol)

    engine_key = str(snapshot.get("engine_key") or "multi_factor")
    parameters = _json_object(snapshot.get("parameters"))
    candles, rows = _candles(symbol)
    if len(candles) < 3:
        return 0, None, [], None
    try:
        signals = _build_signals(engine_key, candles, parameters)
    except (KeyError, TypeError, ValueError):
        return 0, None, [], None
    direction = int(signals[-1]) if signals else 0
    atr = None
    if len(rows) > 15:
        atr = ind.atr(
            [float(row["high"]) for row in rows],
            [float(row["low"]) for row in rows],
            [float(row["close"]) for row in rows],
        )
    basis = [
        f"策略：{snapshot.get('name') or engine_key}",
        f"引擎：{engine_key}",
        "周期：4h",
    ]
    return direction, atr, basis, int(rows[-1]["open_time"])


def _full_strategy_signal(
    account: dict[str, Any], snapshot: dict[str, Any], symbol: str
) -> tuple[int, float | None, list[str], int | None]:
    """Evaluate one immutable full-strategy snapshot on its declared timeframes."""

    raw_spec = snapshot.get("spec") or snapshot.get("spec_json")
    try:
        spec = validate_strategy_spec(raw_spec)
        timeframes = set(spec["timeframes"].values())
        market = {timeframe: store.get_klines(symbol, timeframe, 600) for timeframe in timeframes}
        decision = evaluate_strategy(spec, market)
    except (StrategySpecError, StrategyMarketDataError, KeyError, TypeError, ValueError) as exc:
        return 0, None, [f"完整策略不可用：{type(exc).__name__}"], None

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
    if direction and not _record_full_strategy_decision(account, symbol, spec, decision):
        return 0, atr, [*basis, "信号未执行：缺少可审计的策略部署记录"], decision.signal_time
    return direction, atr, basis, decision.signal_time


def _record_full_strategy_decision(
    account: dict[str, Any],
    symbol: str,
    spec: dict[str, Any],
    decision: Any,
) -> bool:
    deployments = store.query(
        """SELECT id,strategy_revision_id FROM strategy_deployments
           WHERE user_id=? AND mode='paper' AND target_account_id=? AND status='running'
           ORDER BY id DESC LIMIT 1""",
        (account["user_id"], account["id"]),
    )
    if not deployments or decision.signal_time is None:
        return False
    deployment = deployments[0]
    timeframe = str(spec["timeframes"]["trigger"])
    idempotency_key = (
        f"paper:{deployment['id']}:{deployment['strategy_revision_id']}:"
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


def _execution_price(price: float, side: int, opening: bool, slippage_bps: float) -> float:
    adverse = (side > 0) == opening
    adjustment = price * slippage_bps / 10_000
    return price + adjustment if adverse else price - adjustment


def _decimal(value: Any) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal("0")
    return result if result.is_finite() else Decimal("0")


def _floor_step(value: float, step: Any) -> float:
    amount = _decimal(value)
    increment = _decimal(step)
    if amount <= 0 or increment <= 0:
        return 0.0
    return float((amount / increment).to_integral_value(rounding=ROUND_FLOOR) * increment)


def _round_tick(value: float | None, tick: Any, *, upward: bool) -> float | None:
    if value is None:
        return None
    amount = _decimal(value)
    increment = _decimal(tick)
    if amount <= 0 or increment <= 0:
        return None
    rounding = ROUND_CEILING if upward else ROUND_FLOOR
    return float((amount / increment).to_integral_value(rounding=rounding) * increment)


def _bracket_for_notional(brackets: list[dict[str, Any]], notional: float) -> dict[str, Any] | None:
    for bracket in brackets:
        floor = float(bracket.get("notional_floor") or 0)
        cap = float(bracket.get("notional_cap") or math.inf)
        if floor <= notional <= cap:
            return bracket
    return brackets[-1] if brackets else None


def _isolated_liquidation_price(
    entry: float,
    quantity: float,
    margin: float,
    side: int,
    bracket: dict[str, Any],
    funding_paid: float = 0.0,
) -> float | None:
    if entry <= 0 or quantity <= 0 or margin <= 0 or side not in {-1, 1}:
        return None
    mmr = float(bracket.get("maint_margin_ratio") or 0)
    cum = float(bracket.get("cum") or 0)
    if side > 0 and mmr < 1:
        result = (quantity * entry - margin + funding_paid - cum) / (quantity * (1 - mmr))
    elif side < 0:
        result = (margin - funding_paid + quantity * entry + cum) / (quantity * (1 + mmr))
    else:
        return None
    return result if math.isfinite(result) and result > 0 else None


def _exit_levels(
    entry: float,
    side: int,
    atr: float | None,
    config: dict[str, float | int | bool | None],
) -> tuple[float | None, float | None]:
    """Return direction-safe stop and take-profit levels for one position.

    The default paper strategy is ATR based. Percentage values remain a fallback
    for symbols that do not yet have enough 4h candles to calculate a valid ATR.
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
        stop_distance = float(config["initial_stop_atr"] or ATR_STOP_MULTIPLIER) * atr_value
        take_distance = stop_distance * float(
            config["take_profit_r"] or ATR_TAKE_PROFIT_MULTIPLIER / ATR_STOP_MULTIPLIER
        )
    else:
        stop_distance = entry * float(config["stop_loss_pct"]) / 100
        take_distance = entry * float(config["take_profit_pct"]) / 100

    stop = entry - side * stop_distance if stop_distance > 0 else None
    target = entry + side * take_distance if take_distance > 0 else None
    if stop is not None and (not math.isfinite(stop) or stop <= 0 or (entry - stop) * side <= 0):
        fallback = entry * float(config["stop_loss_pct"]) / 100
        stop = entry - side * fallback if fallback > 0 else None
    if target is not None and (
        not math.isfinite(target) or target <= 0 or (target - entry) * side <= 0
    ):
        fallback = entry * float(config["take_profit_pct"]) / 100
        target = entry + side * fallback if fallback > 0 else None
    if stop is not None and (not math.isfinite(stop) or stop <= 0 or (entry - stop) * side <= 0):
        stop = None
    if target is not None and (
        not math.isfinite(target) or target <= 0 or (target - entry) * side <= 0
    ):
        target = None
    return stop, target


def _repair_missing_target(
    account: dict[str, Any],
    position: dict[str, Any],
    atr: float | None,
    config: dict[str, float | int | bool | None],
) -> None:
    """Backfill existing zero or invalid targets before evaluating exits."""

    entry = float(position["avg_entry"])
    side = int(position["side"])
    try:
        current_target = float(position.get("target") or 0)
    except (TypeError, ValueError):
        current_target = 0.0
    if math.isfinite(current_target) and current_target > 0 and (current_target - entry) * side > 0:
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
    quotes: dict[str, dict[str, float]] | None = None,
) -> bool:
    config = _config(account)
    equity, _ = _equity(account, _prices(), positions)
    exact_environment = quotes is not None
    if exact_environment:
        environment, blocked_reason = exchange_sync.execution_readiness(
            account["user_id"], symbol
        )
        if environment is None:
            store.user_state_set(
                account["user_id"],
                f"paper:{account['id']}:environment",
                {"ready": False, "reason": blocked_reason, "symbol": symbol, "checked_at": now},
            )
            return False
        rule = environment["rule"]
        commission = environment["commission"]
        brackets = environment["brackets"]
        quote = quotes.get(symbol)
        if quote is None:
            return False
        execution = float(quote["ask"] if side > 0 else quote["bid"])
        if not math.isfinite(execution) or execution <= 0:
            return False
        reference = float(rule.get("mark_price") or price)
        adverse_slippage_bps = max((execution / reference - 1) * side * 10_000, 0.0)
        if adverse_slippage_bps > float(config["max_slippage_bps"]):
            return False
    else:
        # Compatibility path for direct engine callers predating the synchronized
        # quote argument. The runtime always supplies a quote dictionary.
        execution = _execution_price(price, side, True, float(config["slippage_bps"]))
        adverse_slippage_bps = float(config["slippage_bps"])
        rule = {
            "tick_size": "0.00000001",
            "market_step_size": "0.000000000001",
            "market_min_qty": 0,
            "min_notional": 0,
            "rule_updated_at_ms": 0,
        }
        commission = {"taker_rate": float(config["fee_bps"]) / 10_000}
        brackets = [
            {
                "initial_leverage": int(config["leverage"]),
                "notional_floor": 0,
                "notional_cap": math.inf,
                "maint_margin_ratio": 0.005,
                "cum": 0,
            }
        ]
        quote = {"bid_qty": math.inf, "ask_qty": math.inf}
    available_margin = min(
        float(account["balance"]),
        equity * float(config["margin_cap"]) - _used_margin(positions),
    )
    first_bracket = brackets[0] if brackets else None
    if first_bracket is None:
        return False
    leverage = min(int(config["leverage"]), int(first_bracket["initial_leverage"]))
    stop, target = _exit_levels(execution, side, atr, config)
    tick_size = rule["tick_size"]
    stop = _round_tick(stop, tick_size, upward=side > 0)
    target = _round_tick(target, tick_size, upward=side < 0)
    if stop is None or target is None:
        return False
    fee_rate = float(commission["taker_rate"])
    max_notional = max(available_margin, 0.0) * leverage
    max_notional = min(
        max_notional,
        equity * float(config["position_size_pct"]) / 100 * leverage,
        float(account["balance"]) / (1 / leverage + fee_rate),
    )
    risk_per_trade_pct = config.get("risk_per_trade_pct")
    if risk_per_trade_pct is not None:
        stop_distance = abs(execution - stop)
        if stop_distance <= 0:
            return False
        risk_budget = equity * float(risk_per_trade_pct) / 100
        max_notional = min(max_notional, risk_budget / stop_distance * execution)
    if not math.isfinite(max_notional) or max_notional <= 0:
        return False
    bracket = _bracket_for_notional(brackets, max_notional)
    if bracket is None:
        return False
    leverage = min(leverage, int(bracket["initial_leverage"]))
    max_notional = min(max_notional, max(available_margin, 0.0) * leverage)
    quantity = _floor_step(max_notional / execution, rule["market_step_size"])
    available_book_qty = float(quote["ask_qty"] if side > 0 else quote["bid_qty"])
    if quantity <= 0 or quantity > available_book_qty:
        return False
    notional = quantity * execution
    if quantity < float(rule["market_min_qty"]) or notional < float(rule["min_notional"]):
        return False
    bracket = _bracket_for_notional(brackets, notional)
    if bracket is None or leverage > int(bracket["initial_leverage"]):
        return False
    margin = notional / leverage
    fee = notional * fee_rate
    if margin + fee > float(account["balance"]):
        return False
    liquidation = _isolated_liquidation_price(
        execution, quantity, margin, side, bracket
    )
    liquidation = _round_tick(liquidation, tick_size, upward=side > 0)
    if liquidation is None:
        return False
    _set_balance(account, float(account["balance"]) - margin - fee)
    protection_started_at_ms = int(time.time() * 1000)
    store.execute(
        """INSERT INTO paper_positions(
               paper_account_id,user_id,symbol,side,qty,avg_entry,margin,leverage,stop,target,
               adds,opened_ts,protection_started_at_ms,last_add_ts,open_score,basis,funding_acc,
               liq_price,funding_ts,atr_entry,peak_price,tp_done,execution_model,open_fee,
               fee_rate_open,rule_updated_at_ms
           ) VALUES(?,?,?,?,?,?,?,?,?,?,0,?,?,?,?,?,0,?,0,?,?,0,?,?,?,?)""",
        (
            account["id"],
            account["user_id"],
            symbol,
            side,
            quantity,
            execution,
            margin,
            leverage,
            stop,
            target,
            now,
            protection_started_at_ms,
            now,
            side * 100,
            json.dumps(
                {
                    "reasons": basis,
                    "execution": {
                        "source": "binance_book_ticker_ioc",
                        "reference": "mark_price",
                        "slippage_bps": adverse_slippage_bps,
                        "fee_source": "binance_user_commission_rate",
                    },
                },
                ensure_ascii=False,
            ),
            liquidation,
            atr,
            execution,
            "binance_synced_v2" if exact_environment else "legacy_fixed_v1",
            fee,
            fee_rate,
            rule["rule_updated_at_ms"],
        ),
    )
    if exact_environment:
        store.user_state_set(
            account["user_id"],
            f"paper:{account['id']}:environment",
            {"ready": True, "reason": None, "symbol": symbol, "checked_at": now},
        )
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
            "strategy_name": account["strategy_snapshot_json"].get("name"),
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
    exact = position.get("execution_model") in {
        "binance_synced_v2",
        "binance_transition_v2",
    }
    execution = (
        float(price)
        if exact
        else _execution_price(price, int(position["side"]), False, float(config["slippage_bps"]))
    )
    quantity = float(position["qty"])
    fee_rate = float(config["fee_bps"]) / 10_000
    if exact:
        rates = store.query(
            """SELECT taker_rate FROM binance_user_commission_rates
               WHERE user_id=? AND symbol=? ORDER BY synced_at_ms DESC LIMIT 1""",
            (account["user_id"], position["symbol"]),
        )
        fee_rate = (
            float(rates[0]["taker_rate"])
            if rates
            else float(position.get("fee_rate_open") or 0)
        )
    close_fee = quantity * execution * fee_rate
    liquidation_fee = 0.0
    if exact and reason == "liquidation":
        rule = exchange_sync.contract_rule(position["symbol"])
        liquidation_fee = quantity * execution * float(
            rule.get("liquidation_fee_rate") if rule else 0
        )
    funding = float(position.get("funding_acc") or 0)
    gross_pnl = (execution - float(position["avg_entry"])) * quantity * int(position["side"])
    pnl = gross_pnl - funding
    margin = float(position["margin"])
    returned = max(margin + pnl - close_fee - liquidation_fee, 0.0)
    open_fee = float(position.get("open_fee") or 0)
    total_fee = open_fee + close_fee + liquidation_fee
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
                   fee,funding,reason,open_score,opened_ts,closed_ts,open_fee,close_fee,
                   liquidation_fee,execution_model
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                account["id"],
                account["user_id"],
                position["symbol"],
                position["side"],
                quantity,
                position["avg_entry"],
                execution,
                margin,
                max(pnl, -margin),
                total_fee,
                funding,
                reason,
                position.get("open_score"),
                position["opened_ts"],
                now,
                open_fee,
                close_fee,
                liquidation_fee,
                position.get("execution_model") or "legacy_fixed_v1",
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
        f" @ {execution:.8g}，盈亏 {pnl - total_fee:+.2f} USDT（{reason}）",
        {
            "paper_account_id": account["public_id"],
            "paper_account_name": account["name"],
            "strategy_name": account["strategy_snapshot_json"].get("name"),
            "price": execution,
            "pnl": pnl - total_fee,
            "reason": reason,
        },
        user_id=account["user_id"],
    )
    return True


def _protective_exit(
    position: dict[str, Any],
    current_price: float,
    extreme: dict[str, float | int | None] | None,
) -> tuple[str | None, float]:
    """Resolve protective exits, including crossings that recovered before the paper tick."""

    side = int(position["side"])
    levels = (
        ("stop_loss", position.get("stop")),
        ("liquidation", position.get("liq_price")),
        ("take_profit", position.get("target")),
    )
    for reason, raw_level in levels:
        if raw_level is None:
            continue
        level = float(raw_level)
        crossed = {
            (1, "stop_loss"): current_price <= level,
            (1, "liquidation"): current_price <= level,
            (1, "take_profit"): current_price >= level,
            (-1, "stop_loss"): current_price >= level,
            (-1, "liquidation"): current_price >= level,
            (-1, "take_profit"): current_price <= level,
        }[(side, reason)]
        if crossed:
            return reason, current_price

    if not extreme:
        return None, current_price
    protection_started_at_ms = int(
        position.get("protection_started_at_ms") or int(position["opened_ts"]) * 1000
    )
    low = extreme.get("low")
    low_time = extreme.get("low_time")
    high = extreme.get("high")
    high_time = extreme.get("high_time")
    candidates: list[tuple[int, int, str, float]] = []

    def add_candidate(
        event_time: float | int | None,
        priority: int,
        reason: str,
        level: float | int | None,
        observed: float | int | None,
        crossed: bool,
    ) -> None:
        if (
            crossed
            and event_time is not None
            and int(event_time) >= protection_started_at_ms
            and level is not None
            and observed is not None
        ):
            candidates.append((int(event_time), priority, reason, float(level)))

    stop = position.get("stop")
    liquidation = position.get("liq_price")
    if side > 0:
        add_candidate(
            low_time,
            0,
            "stop_loss",
            stop,
            low,
            stop is not None and low is not None and float(low) <= float(stop),
        )
        add_candidate(
            low_time,
            1,
            "liquidation",
            liquidation,
            low,
            liquidation is not None and low is not None and float(low) <= float(liquidation),
        )
    else:
        add_candidate(
            high_time,
            0,
            "stop_loss",
            stop,
            high,
            stop is not None and high is not None and float(high) >= float(stop),
        )
        add_candidate(
            high_time,
            1,
            "liquidation",
            liquidation,
            high,
            liquidation is not None and high is not None and float(high) >= float(liquidation),
        )
    if not candidates:
        return None, current_price
    _, _, reason, trigger_price = min(candidates)
    return reason, trigger_price


def _full_strategy_regime_broken(account: dict[str, Any], symbol: str, side: int) -> bool:
    spec = _full_strategy_spec(account)
    if spec is None or not spec["exit"]["exit_on_regime_break"]:
        return False
    try:
        timeframes = set(spec["timeframes"].values())
        market = {timeframe: store.get_klines(symbol, timeframe, 600) for timeframe in timeframes}
        decision = evaluate_strategy(spec, market)
        regime = decision.evidence.get("regime")
        if not isinstance(regime, dict):
            return False
        fast = float(regime["fast_ema"])
        slow = float(regime["slow_ema"])
        slope = float(regime["fast_slope_pct"])
        plus_di = float(regime["plus_di"])
        minus_di = float(regime["minus_di"])
    except (
        StrategySpecError,
        StrategyMarketDataError,
        KeyError,
        TypeError,
        ValueError,
    ):
        return False
    if side > 0:
        return fast <= slow or slope <= 0 or plus_di <= minus_di
    return fast >= slow or slope >= 0 or minus_di <= plus_di


def _sync_funding_and_liquidation(
    account: dict[str, Any], position: dict[str, Any], mark_price: float, now: int
) -> None:
    """Apply published funding settlements and refresh isolated liquidation price."""

    last_funding_ms = max(
        int(position.get("funding_ts") or 0), int(position["opened_ts"]) * 1000
    )
    events = store.query(
        """SELECT funding_time,funding_rate,mark_price FROM binance_funding_events
           WHERE symbol=? AND funding_time>? AND funding_time<=? ORDER BY funding_time""",
        (position["symbol"], last_funding_ms, now * 1000),
    )
    funding_paid = float(position.get("funding_acc") or 0)
    for event in events:
        settlement_mark = float(event.get("mark_price") or mark_price)
        funding_paid += (
            float(position["qty"])
            * settlement_mark
            * float(event["funding_rate"])
            * int(position["side"])
        )
        last_funding_ms = int(event["funding_time"])

    brackets = store.query(
        """SELECT * FROM binance_user_leverage_brackets
           WHERE user_id=? AND symbol=? ORDER BY bracket""",
        (account["user_id"], position["symbol"]),
    )
    liquidation = position.get("liq_price")
    bracket = _bracket_for_notional(
        [dict(row) for row in brackets], float(position["qty"]) * mark_price
    )
    if bracket is not None:
        liquidation = _isolated_liquidation_price(
            float(position["avg_entry"]),
            float(position["qty"]),
            float(position["margin"]),
            int(position["side"]),
            bracket,
            funding_paid,
        )
        rule = exchange_sync.contract_rule(position["symbol"])
        if rule is not None:
            liquidation = _round_tick(
                liquidation, rule["tick_size"], upward=int(position["side"]) > 0
            )
    if events or (liquidation is not None and liquidation != position.get("liq_price")):
        store.execute(
            """UPDATE paper_positions SET funding_acc=?,funding_ts=?,liq_price=?
               WHERE id=? AND paper_account_id=? AND user_id=?""",
            (
                funding_paid,
                last_funding_ms,
                liquidation,
                position["id"],
                account["id"],
                account["user_id"],
            ),
        )
        position["funding_acc"] = funding_paid
        position["funding_ts"] = last_funding_ms
        position["liq_price"] = liquidation


def _transition_legacy_position(account: dict[str, Any], position: dict[str, Any]) -> bool:
    """Move an existing paper position to live rules without rewriting its fill."""

    if position.get("execution_model") != "legacy_fixed_v1":
        return True
    rule = exchange_sync.contract_rule(position["symbol"])
    if rule is None:
        return False
    profile, _ = exchange_sync.ensure_user_profile(account["user_id"], position["symbol"])
    if profile is None:
        return False
    historical_rate = float(_config(account)["fee_bps"]) / 10_000
    historical_open_fee = (
        float(position["qty"]) * float(position["avg_entry"]) * historical_rate
    )
    store.execute(
        """UPDATE paper_positions SET execution_model='binance_transition_v2',open_fee=?,
               fee_rate_open=?,rule_updated_at_ms=?
           WHERE id=? AND paper_account_id=? AND user_id=? AND execution_model='legacy_fixed_v1'""",
        (
            historical_open_fee,
            historical_rate,
            rule["rule_updated_at_ms"],
            position["id"],
            account["id"],
            account["user_id"],
        ),
    )
    position["execution_model"] = "binance_transition_v2"
    position["open_fee"] = historical_open_fee
    position["fee_rate_open"] = historical_rate
    position["rule_updated_at_ms"] = rule["rule_updated_at_ms"]
    store.user_state_set(
        account["user_id"],
        f"paper:{account['id']}:environment",
        {
            "ready": True,
            "reason": None,
            "symbol": position["symbol"],
            "checked_at": int(time.time()),
            "position_transition": True,
        },
    )
    return True


def _tick_account(
    account: dict[str, Any],
    prices: dict[str, float],
    now: int,
    extremes: dict[str, dict[str, float | int | None]] | None = None,
    quotes: dict[str, dict[str, float]] | None = None,
    *,
    allow_entries: bool = True,
) -> None:
    config = _config(account)
    positions = _positions(account)
    closed_symbols: set[str] = set()
    for position in list(positions):
        price = prices.get(position["symbol"])
        if not price or not math.isfinite(float(price)) or float(price) <= 0:
            continue
        side = int(position["side"])
        if side not in {-1, 1}:
            print(f"[paper] invalid side skipped: position={position.get('id')}")
            continue
        _transition_legacy_position(account, position)
        if position.get("execution_model") in {
            "binance_synced_v2",
            "binance_transition_v2",
        }:
            _sync_funding_and_liquidation(account, position, float(price), now)
        # Stored exchange-risk levels must remain effective even when indicator
        # calculation or historical market data is temporarily unavailable.
        _repair_missing_target(account, position, None, config)
        held_bars = max((now - int(position["opened_ts"])) // (4 * 3600), 0)
        reason, exit_price = _protective_exit(
            position, float(price), (extremes or {}).get(position["symbol"])
        )
        if reason is None:
            direction = 0
            try:
                direction, _, _, _ = _strategy_signal(account, position["symbol"])
            except Exception as exc:
                print(f"[paper] signal unavailable for {position['symbol']}: {type(exc).__name__}")
            if direction == -side:
                reason = "strategy_reversal"
            elif config["exit_on_regime_break"] and _full_strategy_regime_broken(
                account, position["symbol"], side
            ):
                reason = "regime_break"
            elif config["max_holding_bars"] and held_bars >= config["max_holding_bars"]:
                reason = "max_holding_bars"
        if reason:
            if _close_position(account, position, exit_price, reason, now):
                closed_symbols.add(position["symbol"])
            positions = _positions(account)

    if allow_entries and len(positions) < int(config["max_positions"]):
        entry_symbols = tradfi_symbols()
        entry_quotes = quotes
        for symbol in entry_symbols:
            if len(positions) >= int(config["max_positions"]):
                break
            if symbol in closed_symbols:
                continue
            if any(position["symbol"] == symbol for position in positions):
                continue
            price = prices.get(symbol)
            if not price:
                continue
            direction, atr, basis, signal_time = _strategy_signal(account, symbol)
            if direction not in {-1, 1} or signal_time is None:
                continue
            state_key = f"paper:{account['id']}:signal:{symbol}"
            if store.user_state_get(account["user_id"], state_key) == signal_time:
                continue
            if entry_quotes is None:
                entry_quotes = _book_quotes(now * 1000)
            if _open_position(
                account, symbol, direction, price, atr, basis, positions, now, entry_quotes
            ):
                store.user_state_set(account["user_id"], state_key, signal_time)
                positions = _positions(account)

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
            extremes = _market_extremes(now * 1000)
            for account in _tracked_accounts(account_id):
                if account["status"] == "active":
                    _tick_account(account, prices, now, extremes)
                else:
                    _tick_account(account, prices, now, extremes, allow_entries=False)


def paper_loop(stop_event=None) -> None:
    print("[paper] multi-user paper engine started")
    while stop_event is None or not stop_event.is_set():
        if store.collector_paused("paper"):
            if stop_event is not None and stop_event.wait(5):
                break
            if stop_event is None:
                time.sleep(5)
            continue
        try:
            tick()
            store.collector_report("paper", success=True)
        except Exception as exc:
            print("[paper] tick error:", exc)
            store.collector_report("paper", success=False, error=str(exc))
        if stop_event is not None:
            stop_event.wait(5)
        else:
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


def api_data(user_id: int, account_id: int, timezone_offset_minutes: int = 0) -> dict[str, Any]:
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
        upnl = _upnl(position, price)
        margin = float(position["margin"])
        stop = float(position["stop"]) if position.get("stop") is not None else None
        risk_at_stop = (
            float(position["qty"]) * abs(float(position["avg_entry"]) - stop)
            if stop is not None
            else None
        )
        risk_limit = (
            equity * float(config["risk_per_trade_pct"]) / 100
            if config["risk_per_trade_pct"] is not None
            else None
        )
        basis = _json_object(position.get("basis"))
        output_positions.append(
            {
                **position,
                "price": price,
                "upnl": round(upnl, 2),
                "pnl_pct": round(upnl / margin * 100 if margin else 0, 2),
                "hold_h": round((now - int(position["opened_ts"])) / 3600, 1),
                "risk_at_stop": round(risk_at_stop, 2) if risk_at_stop is not None else None,
                "risk_pct": round(risk_at_stop / equity * 100, 3)
                if risk_at_stop is not None and equity > 0
                else None,
                "risk_policy_compliant": (
                    int(position["leverage"]) <= int(config["leverage"])
                    and (
                        risk_limit is None
                        or risk_at_stop is None
                        or risk_at_stop <= risk_limit * 1.01
                    )
                ),
                "protection_mode": "websocket_extrema_15s",
                "reasons": basis.get("reasons", []),
            }
        )
    trades = [
        dict(row)
        for row in store.query(
            """SELECT * FROM paper_trades WHERE paper_account_id=? AND user_id=?
               ORDER BY closed_ts DESC LIMIT 100""",
            (account_id, user_id),
        )
    ]
    curve_rows = store.query(
        """SELECT ts,equity FROM paper_equity WHERE paper_account_id=? AND user_id=?
           ORDER BY ts DESC LIMIT 2880""",
        (account_id, user_id),
    )
    curve = [(row["ts"], row["equity"]) for row in reversed(curve_rows)]
    wins = [trade for trade in trades if float(trade.get("pnl") or 0) > 0]
    losses = [trade for trade in trades if float(trade.get("pnl") or 0) <= 0]
    gross_win = sum(float(trade.get("pnl") or 0) for trade in wins)
    gross_loss = abs(sum(float(trade.get("pnl") or 0) for trade in losses))
    initial = float(account["initial_balance"])
    peak = initial
    drawdown = 0.0
    for _, value in curve:
        peak = max(peak, float(value))
        if peak:
            drawdown = max(drawdown, (peak - float(value)) / peak * 100)
    strategy = account["strategy_snapshot_json"]
    environment_state = store.user_state_get(
        user_id,
        f"paper:{account_id}:environment",
        {"ready": False, "reason": "environment_not_checked"},
    )
    credential_rows = store.query(
        """SELECT binance_api_key_encrypted IS NOT NULL AND
                  binance_api_secret_encrypted IS NOT NULL AS configured
           FROM users WHERE id=?""",
        (user_id,),
    )
    credentials_configured = bool(credential_rows and credential_rows[0]["configured"])
    if isinstance(environment_state, dict):
        environment_state = {
            **environment_state,
            "credentials_configured": credentials_configured,
        }
        if not credentials_configured:
            environment_state["ready"] = False
            environment_state["reason"] = "binance_credentials_required"
    freshness_now_ms = int(time.time() * 1000)
    public_rule_rows = store.query(
        """SELECT COUNT(*) AS n FROM binance_contract_rules
           WHERE status='TRADING' AND rule_updated_at_ms>=? AND mark_updated_at_ms>=?""",
        (
            freshness_now_ms - exchange_sync.RULE_MAX_AGE_MS,
            freshness_now_ms - exchange_sync.MARK_MAX_AGE_MS,
        ),
    )
    public_rule_count = int(public_rule_rows[0]["n"]) if public_rule_rows else 0
    return {
        "paper_account": {
            "id": account["public_id"],
            "name": account["name"],
            "status": account["status"],
            "strategy_id": strategy.get("public_id"),
            "strategy_name": strategy.get("name"),
            "engine_key": strategy.get("engine_key"),
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
            "risk_per_trade_pct": config["risk_per_trade_pct"],
            "risk_budget": round(equity * float(config["risk_per_trade_pct"]) / 100, 2)
            if config["risk_per_trade_pct"] is not None
            else None,
            "protective_exit_source": "binance_websocket_15s_extrema",
            "valuation_source": "binance_mark_price",
            "execution_environment": environment_state,
            "synced_tradfi_symbols": public_rule_count,
            "legacy_risk_mismatch_count": sum(
                not item["risk_policy_compliant"] for item in output_positions
            ),
            "started_ts": int(account["started_at"].replace(tzinfo=UTC).timestamp()),
        },
        "positions": output_positions,
        "trades": trades[:50],
        "curve": curve[-1440:],
        "stats": {
            "trades": len(trades),
            "win_rate": round(len(wins) / len(trades) * 100 if trades else 0, 2),
            "profit_factor": round(gross_win / gross_loss, 2) if gross_loss else None,
            "max_drawdown": round(drawdown, 2),
            "realized": round(
                sum(
                    float(trade.get("pnl") or 0) - float(trade.get("fee") or 0) for trade in trades
                ),
                2,
            ),
            "wins": len(wins),
            "losses": len(losses),
        },
        "rules": {
            "tiers": f"绑定策略：{strategy.get('name') or strategy.get('engine_key')}",
            "exits": f"{config['initial_stop_atr']}×ATR 初始止损 / "
            f"{float(config['initial_stop_atr']) * float(config['take_profit_r']):g}×ATR 止盈 "
            f"({config['take_profit_r']}R) / 趋势失效 / 策略反转 / 最大持仓周期 / 强平",
            "costs": "Binance 账户级 maker/taker 费率 + 实时盘口 IOC 成交；不再使用固定费率或固定滑点",
            "limits": f"逐仓 {config['leverage']}x / 最多 {config['max_positions']} 仓",
        },
        "disclaimer": "模拟交易仅用于策略验证与学习，不构成投资建议。",
    }
