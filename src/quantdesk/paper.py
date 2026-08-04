"""Tenant-isolated multi-account paper trading engine."""

from __future__ import annotations

import json
import math
import threading
import time
import uuid
from datetime import UTC
from typing import Any

from quantdesk_v2.backtest import _build_signals, _Candle
from quantdesk_v2.strategy_runtime import (
    StrategyMarketDataError,
    StrategySpecError,
    evaluate_strategy,
    validate_strategy_spec,
)

from . import indicators as ind
from . import store
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
        account["strategy_snapshot_json"] = _json_object(
            account.get("strategy_snapshot_json")
        )
        result.append(account)
    return result


def _config(account: dict[str, Any]) -> dict[str, float | int]:
    raw = account["config_json"]
    return {
        "leverage": max(1, min(int(raw.get("leverage", DEFAULT_LEVERAGE)), 50)),
        "max_positions": max(
            1, min(int(raw.get("max_positions", DEFAULT_MAX_POSITIONS)), 50)
        ),
        "margin_cap": max(0.05, min(float(raw.get("margin_cap", DEFAULT_MARGIN_CAP)), 0.95)),
        "position_size_pct": max(0.1, min(float(raw.get("position_size_pct", 10)), 100)),
        "fee_bps": max(0.0, min(float(raw.get("fee_bps", DEFAULT_FEE_BPS)), 100)),
        "slippage_bps": max(
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
    return {
        row["symbol"]: float(row["price"])
        for row in store.query("SELECT symbol,price FROM ticker WHERE price IS NOT NULL")
    }


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
        market = {
            timeframe: store.get_klines(symbol, timeframe, 600)
            for timeframe in timeframes
        }
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


def _exit_levels(
    entry: float,
    side: int,
    atr: float | None,
    config: dict[str, float | int],
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
) -> bool:
    config = _config(account)
    equity, _ = _equity(account, _prices(), positions)
    available = min(
        float(account["balance"]),
        equity * float(config["margin_cap"]) - _used_margin(positions),
    )
    margin = min(equity * float(config["position_size_pct"]) / 100, available)
    if margin <= 5:
        return False
    leverage = int(config["leverage"])
    execution = _execution_price(price, side, True, float(config["slippage_bps"]))
    notional = margin * leverage
    quantity = notional / execution
    fee = notional * float(config["fee_bps"]) / 10_000
    stop, target = _exit_levels(execution, side, atr, config)
    if stop is None or target is None:
        return False
    liquidation = execution * (1 - side * (1 / leverage - 0.005))
    _set_balance(account, float(account["balance"]) - margin - fee)
    store.execute(
        """INSERT INTO paper_positions(
               paper_account_id,user_id,symbol,side,qty,avg_entry,margin,leverage,stop,target,
               adds,opened_ts,last_add_ts,open_score,basis,funding_acc,liq_price,funding_ts,
               atr_entry,peak_price,tp_done
           ) VALUES(?,?,?,?,?,?,?,?,?,?,0,?,?,?, ?,0,?,0,?,?,0)""",
        (
            account["id"], account["user_id"], symbol, side, quantity, execution, margin,
            leverage, stop, target, now, now, side * 100,
            json.dumps({"reasons": basis}, ensure_ascii=False), liquidation, atr, execution,
        ),
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
                   fee,funding,reason,open_score,opened_ts,closed_ts
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                account["id"], account["user_id"], position["symbol"], position["side"],
                quantity, position["avg_entry"], execution, margin, max(pnl, -margin), fee,
                position.get("funding_acc") or 0, reason, position.get("open_score"),
                position["opened_ts"], now,
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
            "strategy_name": account["strategy_snapshot_json"].get("name"),
            "price": execution,
            "pnl": pnl - fee,
            "reason": reason,
        },
        user_id=account["user_id"],
    )
    return True


def _tick_account(account: dict[str, Any], prices: dict[str, float], now: int) -> None:
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
            try:
                direction, _, _, _ = _strategy_signal(account, position["symbol"])
            except Exception as exc:
                print(
                    f"[paper] signal unavailable for {position['symbol']}: "
                    f"{type(exc).__name__}"
                )
            if direction == -side:
                reason = "strategy_reversal"
            elif config["max_holding_bars"] and held_bars >= config["max_holding_bars"]:
                reason = "max_holding_bars"
        if reason:
            if _close_position(account, position, price, reason, now):
                closed_symbols.add(position["symbol"])
            positions = _positions(account)

    if len(positions) < int(config["max_positions"]):
        for symbol in tradfi_symbols():
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
            store.user_state_set(account["user_id"], state_key, signal_time)
            if _open_position(
                account, symbol, direction, price, atr, basis, positions, now
            ):
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
        upnl = _upnl(position, price)
        margin = float(position["margin"])
        basis = _json_object(position.get("basis"))
        output_positions.append(
            {
                **position,
                "price": price,
                "upnl": round(upnl, 2),
                "pnl_pct": round(upnl / margin * 100 if margin else 0, 2),
                "hold_h": round((now - int(position["opened_ts"])) / 3600, 1),
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
                sum(float(trade.get("pnl") or 0) - float(trade.get("fee") or 0) for trade in trades),
                2,
            ),
            "wins": len(wins),
            "losses": len(losses),
        },
        "rules": {
            "tiers": f"绑定策略：{strategy.get('name') or strategy.get('engine_key')}",
            "exits": "1.5×ATR 止损 / 2.5×ATR 止盈 / 策略反转 / 最大持仓周期 / 强平",
            "costs": f"手续费 {config['fee_bps']} bps + 滑点 {config['slippage_bps']} bps",
            "limits": f"逐仓 {config['leverage']}x / 最多 {config['max_positions']} 仓",
        },
        "disclaimer": "模拟交易仅用于策略验证与学习，不构成投资建议。",
    }
