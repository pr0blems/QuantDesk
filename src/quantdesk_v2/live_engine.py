"""Fail-closed Binance live strategy executor.

The worker consumes the same immutable strategy snapshots as the paper engine,
but exchange balances, positions and orders remain authoritative.  Every order
is preceded by a unique local intent so a repeated strategy tick cannot emit a
second Binance order.
"""

from __future__ import annotations

import hashlib
import json
import math
import threading
import time
from decimal import Decimal
from typing import Any

from . import market_store as store
from .binance_client import BinanceAccountClientError
from .binance_service import BinanceAccountService
from .binance_trading import BinanceUsdMTradingClient
from .config import Settings
from .market_config import tradfi_symbols
from .paper_engine import _exit_levels, _strategy_signal
from .security import CredentialCipher, SecurityError

_settings: Settings | None = None
_account_service: BinanceAccountService | None = None
_trading_client: BinanceUsdMTradingClient | None = None
_started = False
_start_lock = threading.Lock()


def configure(
    settings: Settings,
    account_service: BinanceAccountService,
    trading_client: BinanceUsdMTradingClient,
) -> None:
    global _settings, _account_service, _trading_client
    _settings = settings
    _account_service = account_service
    _trading_client = trading_client


def start() -> None:
    global _started
    with _start_lock:
        if _started or _settings is None or not _settings.binance_live_trading_enabled:
            return
        _started = True
        threading.Thread(target=live_loop, daemon=True, name="binance-live").start()


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError, json.JSONDecodeError):
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _strategy_universe(config: dict[str, Any]) -> list[str]:
    """Return the server-owned paper universe, narrowed only by live preflight."""
    universe = tradfi_symbols()
    eligible = config.get("eligible_symbols")
    if not isinstance(eligible, list):
        return universe
    eligible_set = {str(value).upper() for value in eligible}
    return [symbol for symbol in universe if symbol in eligible_set]


def _strategy_position_side(position_mode: str, direction: int) -> str:
    if position_mode == "hedge":
        return "LONG" if direction > 0 else "SHORT"
    return "BOTH"


def _position_key(position: dict[str, Any]) -> tuple[str, str]:
    symbol = str(position.get("symbol") or "").upper()
    raw_side = str(position.get("position_side") or "BOTH").upper()
    position_side = raw_side if raw_side in {"BOTH", "LONG", "SHORT"} else "BOTH"
    return symbol, position_side


def _active_accounts(account_id: int | None = None) -> list[dict[str, Any]]:
    base_query = """SELECT l.*,d.id AS deployment_id,d.strategy_revision_id,
                           u.binance_api_key_encrypted,u.binance_api_secret_encrypted,
                           u.binance_key_version,u.binance_permissions
                    FROM live_trading_accounts l
                    JOIN strategy_deployments d
                      ON d.user_id=l.user_id AND d.mode='live' AND d.target_account_id=l.id
                    JOIN users u ON u.id=l.user_id AND u.is_active=1
                    WHERE l.status='active' AND d.status='running'"""
    if account_id is None:
        rows = store.query(base_query + " ORDER BY l.id")
    else:
        rows = store.query(base_query + " AND l.id=? ORDER BY l.id", (account_id,))
    accounts: list[dict[str, Any]] = []
    for row in rows:
        account = dict(row)
        account["config_json"] = _json_object(account.get("config_json"))
        account["strategy_snapshot_json"] = _json_object(
            account.get("strategy_snapshot_json")
        )
        account["deployment_mode"] = "live"
        accounts.append(account)
    return accounts


def _credentials(account: dict[str, Any]) -> tuple[str, str]:
    if _settings is None:
        raise RuntimeError("live engine is not configured")
    if int(account["credential_version"]) != int(account["binance_key_version"]):
        raise SecurityError("credential version changed")
    cipher = CredentialCipher(_settings.credential_master_key.get_secret_value())
    return (
        cipher.decrypt(str(account.get("binance_api_key_encrypted") or "")),
        cipher.decrypt(str(account.get("binance_api_secret_encrypted") or "")),
    )


def _client_id(account_id: int, key: str) -> str:
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:24]
    return f"qd{account_id:x}-{digest}"[:36]


def _safe_response(payload: dict[str, Any]) -> dict[str, Any]:
    allowed = {
        "orderId",
        "clientOrderId",
        "symbol",
        "status",
        "type",
        "side",
        "positionSide",
        "reduceOnly",
        "closePosition",
        "avgPrice",
        "origQty",
        "executedQty",
        "updateTime",
        "algoId",
        "clientAlgoId",
        "algoType",
        "algoStatus",
        "orderType",
        "triggerPrice",
    }
    return {key: payload[key] for key in allowed if key in payload}


def _create_intent(
    account: dict[str, Any],
    *,
    signal_key: str,
    symbol: str,
    action: str,
    side: str,
    position_side: str,
    order_type: str,
    quantity: Decimal | None,
    request_json: dict[str, Any],
) -> dict[str, Any] | None:
    client_id = _client_id(int(account["id"]), signal_key)
    created = store.execute(
        """INSERT IGNORE INTO live_order_intents(
               public_id,user_id,live_account_id,deployment_id,signal_key,client_order_id,
               symbol,action,side,position_side,order_type,quantity,status,request_json,
               created_at,updated_at
           ) VALUES(UUID(),?,?,?,?,?,?,?,?,?,?,?,'created',?,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)""",
        (
            account["user_id"],
            account["id"],
            account["deployment_id"],
            signal_key,
            client_id,
            symbol,
            action,
            side,
            position_side,
            order_type,
            quantity,
            json.dumps(request_json, ensure_ascii=False),
        ),
    )
    if created != 1:
        return None
    rows = store.query(
        "SELECT * FROM live_order_intents WHERE signal_key=? AND user_id=?",
        (signal_key, account["user_id"]),
    )
    return dict(rows[0]) if rows else None


def _update_intent(
    intent_id: int,
    user_id: int,
    *,
    status: str,
    response: dict[str, Any] | None = None,
    error_code: str | None = None,
) -> None:
    order_id = None
    if response is not None:
        exchange_id = response.get("orderId", response.get("algoId"))
        if exchange_id is not None:
            order_id = str(exchange_id)
    store.execute(
        """UPDATE live_order_intents
           SET status=?,binance_order_id=COALESCE(?,binance_order_id),response_json=?,
               error_code=?,submitted_at=COALESCE(submitted_at,CURRENT_TIMESTAMP),
               updated_at=CURRENT_TIMESTAMP
           WHERE id=? AND user_id=?""",
        (
            status,
            order_id,
            json.dumps(_safe_response(response), ensure_ascii=False) if response else None,
            error_code,
            intent_id,
            user_id,
        ),
    )


def _fail_account(account: dict[str, Any], code: str) -> None:
    store.execute(
        """UPDATE live_trading_accounts
           SET status='error',last_error_code=?,updated_at=CURRENT_TIMESTAMP
           WHERE id=? AND user_id=?""",
        (code[:64], account["id"], account["user_id"]),
    )
    store.execute(
        """UPDATE strategy_deployments SET status='error',last_error_code=?,updated_at=CURRENT_TIMESTAMP
           WHERE id=? AND user_id=?""",
        (code[:64], account["deployment_id"], account["user_id"]),
    )


def _place_market(
    account: dict[str, Any],
    api_key: str,
    api_secret: str,
    *,
    signal_key: str,
    symbol: str,
    action: str,
    side: str,
    position_side: str,
    quantity: Decimal,
    reduce_only: bool,
) -> dict[str, Any] | None:
    if _trading_client is None:
        raise RuntimeError("live engine is not configured")
    intent = _create_intent(
        account,
        signal_key=signal_key,
        symbol=symbol,
        action=action,
        side=side,
        position_side=position_side,
        order_type="MARKET",
        quantity=quantity,
        request_json={
            "symbol": symbol,
            "side": side,
            "position_side": position_side,
            "type": "MARKET",
            "quantity": format(quantity, "f"),
            "reduce_only": reduce_only,
        },
    )
    if intent is None:
        return None
    try:
        response = _trading_client.place_market_order(
            api_key,
            api_secret,
            symbol=symbol,
            side=side,  # type: ignore[arg-type]
            quantity=quantity,
            client_order_id=intent["client_order_id"],
            position_side=position_side,  # type: ignore[arg-type]
            reduce_only=reduce_only,
        )
    except BinanceAccountClientError as exc:
        if exc.category in {"timeout", "network", "upstream"}:
            try:
                response = _trading_client.query_order(
                    api_key,
                    api_secret,
                    symbol=symbol,
                    client_order_id=intent["client_order_id"],
                )
            except BinanceAccountClientError:
                _update_intent(
                    intent["id"], account["user_id"], status="unknown", error_code=exc.category
                )
                _fail_account(account, "order_state_unknown")
                return None
        else:
            _update_intent(
                intent["id"], account["user_id"], status="rejected", error_code=exc.category
            )
            return None
    order_status = str(response.get("status") or "").upper()
    if order_status != "FILLED":
        _update_intent(
            intent["id"],
            account["user_id"],
            status="unknown",
            response=response,
            error_code="unexpected_order_status",
        )
        _fail_account(account, "order_state_unknown")
        return None
    _update_intent(intent["id"], account["user_id"], status="filled", response=response)
    return response


def _place_protection(
    account: dict[str, Any],
    api_key: str,
    api_secret: str,
    *,
    symbol: str,
    side: str,
    position_side: str,
    quantity: Decimal | None,
    signal_time: int,
    stop: Decimal,
    target: Decimal,
) -> bool:
    if _trading_client is None:
        return False
    created: list[dict[str, Any]] = []
    for action, order_type, trigger in (
        ("stop", "STOP_MARKET", stop),
        ("take_profit", "TAKE_PROFIT_MARKET", target),
    ):
        signal_key = (
            f"live:{account['deployment_id']}:{symbol}:{position_side}:"
            f"{signal_time}:{action}"
        )
        intent = _create_intent(
            account,
            signal_key=signal_key,
            symbol=symbol,
            action=action,
            side=side,
            position_side=position_side,
            order_type=order_type,
            quantity=quantity,
            request_json={
                "symbol": symbol,
                "side": side,
                "position_side": position_side,
                "type": order_type,
                "stop_price": format(trigger, "f"),
                "quantity": format(quantity, "f") if quantity is not None else None,
                "close_position": quantity is None,
                "working_type": "MARK_PRICE",
            },
        )
        if intent is None:
            return False
        try:
            response = _trading_client.place_close_trigger(
                api_key,
                api_secret,
                symbol=symbol,
                side=side,  # type: ignore[arg-type]
                order_type=order_type,  # type: ignore[arg-type]
                stop_price=trigger,
                client_order_id=intent["client_order_id"],
                position_side=position_side,  # type: ignore[arg-type]
                quantity=quantity,
            )
        except BinanceAccountClientError as exc:
            if exc.category in {"timeout", "network", "upstream"}:
                try:
                    response = _trading_client.query_algo_order(
                        api_key,
                        api_secret,
                        client_order_id=intent["client_order_id"],
                    )
                except BinanceAccountClientError:
                    _update_intent(
                        intent["id"],
                        account["user_id"],
                        status="unknown",
                        error_code=exc.category,
                    )
                    return False
                _update_intent(
                    intent["id"], account["user_id"], status="submitted", response=response
                )
                created.append(intent)
                continue
            _update_intent(
                intent["id"], account["user_id"], status="rejected", error_code=exc.category
            )
            for previous in created:
                try:
                    _trading_client.cancel_algo_order(
                        api_key,
                        api_secret,
                        client_order_id=previous["client_order_id"],
                    )
                    _update_intent(
                        previous["id"], account["user_id"], status="canceled"
                    )
                except BinanceAccountClientError:
                    pass
            return False
        _update_intent(intent["id"], account["user_id"], status="submitted", response=response)
        created.append(intent)
    return True


def _cancel_protection(
    account: dict[str, Any],
    api_key: str,
    api_secret: str,
    symbol: str,
    position_side: str,
) -> None:
    if _trading_client is None:
        return
    rows = store.query(
        """SELECT id,client_order_id FROM live_order_intents
           WHERE user_id=? AND live_account_id=? AND symbol=? AND position_side=?
             AND action IN ('stop','take_profit') AND status='submitted'""",
        (account["user_id"], account["id"], symbol, position_side),
    )
    for row in rows:
        try:
            _trading_client.cancel_algo_order(
                api_key,
                api_secret,
                client_order_id=row["client_order_id"],
            )
            _update_intent(row["id"], account["user_id"], status="canceled")
        except BinanceAccountClientError as exc:
            # -2011 means the order is already filled/cancelled and is safe to leave for sync.
            if exc.code != -2011:
                _fail_account(account, "protective_cancel_failed")


def _managed_positions(account: dict[str, Any]) -> dict[tuple[str, str], dict[str, Any]]:
    rows = store.query(
        """SELECT id,symbol,position_side,action,status,quantity FROM live_order_intents
           WHERE user_id=? AND live_account_id=?
             AND action IN ('open','close') AND status='filled'
           ORDER BY id DESC""",
        (account["user_id"], account["id"]),
    )
    latest: dict[tuple[str, str], dict[str, Any]] = {}
    for raw_row in rows:
        row = dict(raw_row)
        key = (str(row["symbol"]), str(row.get("position_side") or "BOTH"))
        if key not in latest:
            latest[key] = row
    return {key: row for key, row in latest.items() if row["action"] == "open"}


def _managed_open(
    account: dict[str, Any], symbol: str, position_side: str
) -> dict[str, Any] | None:
    return _managed_positions(account).get((symbol, position_side))


def _protection_counts(account: dict[str, Any]) -> dict[tuple[str, str], int]:
    rows = store.query(
        """SELECT symbol,position_side,COUNT(*) AS total FROM live_order_intents
           WHERE user_id=? AND live_account_id=?
             AND action IN ('stop','take_profit') AND status='submitted'
           GROUP BY symbol,position_side""",
        (account["user_id"], account["id"]),
    )
    return {
        (str(row["symbol"]), str(row.get("position_side") or "BOTH")): int(
            row["total"] or 0
        )
        for row in rows
    }


def _close_position(
    account: dict[str, Any], api_key: str, api_secret: str, position: dict[str, Any], reason: str
) -> None:
    if _trading_client is None:
        return
    symbol = str(position["symbol"])
    position_side = str(position.get("position_side") or "BOTH").upper()
    managed = _managed_open(account, symbol, position_side)
    if managed is None:
        return
    rules = _trading_client.symbol_rules(symbol)
    actual_quantity = Decimal(str(position["amt"]))
    managed_quantity = Decimal(str(managed.get("quantity") or actual_quantity))
    quantity = rules.quantity(min(actual_quantity, managed_quantity))
    _cancel_protection(account, api_key, api_secret, symbol, position_side)
    side = "SELL" if position["side"] == "long" else "BUY"
    signal_key = (
        f"live:{account['deployment_id']}:{symbol}:{position_side}:close:"
        f"{reason}:{int(time.time()) // 60}"
    )
    response = _place_market(
        account,
        api_key,
        api_secret,
        signal_key=signal_key,
        symbol=symbol,
        action="close",
        side=side,
        position_side=position_side,
        quantity=quantity,
        reduce_only=position_side == "BOTH",
    )
    if response is None:
        _fail_account(account, "position_close_failed")


def _open_position(
    account: dict[str, Any],
    api_key: str,
    api_secret: str,
    snapshot: Any,
    *,
    symbol: str,
    direction: int,
    price: float,
    atr: float | None,
    signal_time: int,
) -> None:
    if _trading_client is None:
        return
    config = account["config_json"]
    position_mode = str(config.get("position_mode") or "one_way")
    position_side = _strategy_position_side(position_mode, direction)
    leverage = max(1, min(int(config.get("leverage", 3)), 20))
    rules = _trading_client.symbol_rules(symbol)
    available = Decimal(str(snapshot.available_balance))
    wallet = Decimal(str(snapshot.wallet_balance))
    current_margin = sum(
        Decimal(str(position.get("notional") or 0))
        / Decimal(str(max(int(position.get("leverage") or leverage), 1)))
        for position in snapshot.positions
    )
    requested_margin = min(
        available,
        wallet * Decimal(str(config.get("position_size_pct", 2))) / Decimal(100),
        max(
            Decimal(0),
            wallet * Decimal(str(config.get("margin_cap", 0.20))) - current_margin,
        ),
    )
    if requested_margin <= 0:
        return
    raw_quantity = requested_margin * Decimal(leverage) / Decimal(str(price))
    try:
        quantity = rules.quantity(raw_quantity)
    except ValueError:
        return
    if quantity * Decimal(str(price)) < rules.min_notional:
        return
    _trading_client.change_leverage(
        api_key, api_secret, symbol=symbol, leverage=leverage
    )
    side = "BUY" if direction > 0 else "SELL"
    close_side = "SELL" if direction > 0 else "BUY"
    signal_key = f"live:{account['deployment_id']}:{symbol}:{signal_time}:open:{direction}"
    response = _place_market(
        account,
        api_key,
        api_secret,
        signal_key=signal_key,
        symbol=symbol,
        action="open",
        side=side,
        position_side=position_side,
        quantity=quantity,
        reduce_only=False,
    )
    if response is None:
        return
    try:
        entry = float(response.get("avgPrice") or price)
    except (TypeError, ValueError):
        entry = price
    stop_raw, target_raw = _exit_levels(entry, direction, atr, config)
    if stop_raw is None or target_raw is None:
        _close_position(
            account,
            api_key,
            api_secret,
            {
                "symbol": symbol,
                "amt": float(quantity),
                "side": "long" if direction > 0 else "short",
                "position_side": position_side,
            },
            "missing_protection",
        )
        _fail_account(account, "missing_protection")
        return
    stop = rules.price(Decimal(str(stop_raw)))
    target = rules.price(Decimal(str(target_raw)))
    if not _place_protection(
        account,
        api_key,
        api_secret,
        symbol=symbol,
        side=close_side,
        position_side=position_side,
        quantity=quantity if position_side != "BOTH" else None,
        signal_time=signal_time,
        stop=stop,
        target=target,
    ):
        _close_position(
            account,
            api_key,
            api_secret,
            {
                "symbol": symbol,
                "amt": float(quantity),
                "side": "long" if direction > 0 else "short",
                "position_side": position_side,
            },
            "protection_failed",
        )
        _fail_account(account, "protection_failed")
        return
    store.execute(
        """UPDATE strategy_signals SET status='executed'
           WHERE deployment_id=? AND user_id=? AND symbol=? AND signal_bar_time=?
             AND status='approved'""",
        (account["deployment_id"], account["user_id"], symbol, signal_time),
    )


def _tick_account(account: dict[str, Any]) -> None:
    if _account_service is None or _trading_client is None:
        raise RuntimeError("live engine is not configured")
    api_key, api_secret = _credentials(account)
    snapshot = _account_service.account(api_key, api_secret, force_refresh=True)
    if snapshot.account_type != "UM_FUTURE":
        _fail_account(account, "portfolio_margin_unsupported")
        return
    config = account["config_json"]
    configured_mode = str(config.get("position_mode") or "one_way")
    current_mode = _trading_client.position_mode(api_key, api_secret)
    if current_mode != configured_mode:
        _fail_account(account, "position_mode_changed")
        return
    symbols = _strategy_universe(config)
    positions = {_position_key(item): item for item in snapshot.positions}
    managed = _managed_positions(account)
    protection_counts = _protection_counts(account)
    max_positions = max(1, min(int(config.get("max_positions", 1)), 20))

    for key in managed:
        symbol, position_side = key
        if symbol not in symbols:
            continue
        position = positions.get(key)
        if position is None:
            _cancel_protection(account, api_key, api_secret, symbol, position_side)
            continue
        if protection_counts.get(key, 0) != 2:
            _close_position(account, api_key, api_secret, position, "protection_missing")
            _fail_account(account, "protection_missing")
            return
        direction, _, _, signal_time = _strategy_signal(account, symbol)
        side = 1 if position_side == "LONG" or position["side"] == "long" else -1
        if signal_time is not None and direction == -side:
            _close_position(account, api_key, api_secret, position, "strategy_reversal")

    snapshot = _account_service.account(api_key, api_secret, force_refresh=True)
    positions = {_position_key(item): item for item in snapshot.positions}
    managed = _managed_positions(account)
    managed_position_count = sum(1 for key in positions if key in managed)
    if managed_position_count >= max_positions:
        return
    symbol_set = set(symbols)
    prices = {
        row["symbol"]: float(row["price"])
        for row in store.query("SELECT symbol,price FROM ticker WHERE price IS NOT NULL")
        if row["symbol"] in symbol_set
    }
    occupied_symbols = {key[0] for key in positions}
    for symbol in symbols:
        if managed_position_count >= max_positions or symbol in occupied_symbols:
            continue
        price = prices.get(symbol)
        if price is None or not math.isfinite(price) or price <= 0:
            continue
        direction, atr, _, signal_time = _strategy_signal(account, symbol)
        if direction not in {-1, 1} or signal_time is None:
            continue
        _open_position(
            account,
            api_key,
            api_secret,
            snapshot,
            symbol=symbol,
            direction=direction,
            price=price,
            atr=atr,
            signal_time=signal_time,
        )
        break
    store.execute(
        """UPDATE live_trading_accounts
           SET last_tick_at=CURRENT_TIMESTAMP,last_error_code=NULL,updated_at=CURRENT_TIMESTAMP
           WHERE id=? AND user_id=? AND status='active'""",
        (account["id"], account["user_id"]),
    )


def tick(account_id: int | None = None) -> None:
    if _settings is None or not _settings.binance_live_trading_enabled:
        return
    with store.advisory_lock("quantdesk-binance-live-tick", 0) as acquired:
        if not acquired:
            return
        for account in _active_accounts(account_id):
            try:
                _tick_account(account)
            except SecurityError:
                _fail_account(account, "credential_changed")
            except BinanceAccountClientError as exc:
                store.execute(
                    """UPDATE live_trading_accounts
                       SET last_error_code=?,updated_at=CURRENT_TIMESTAMP
                       WHERE id=? AND user_id=?""",
                    (exc.category[:64], account["id"], account["user_id"]),
                )
            except Exception as exc:
                print(f"[live] account tick failed: {type(exc).__name__}")
                _fail_account(account, "internal_error")


def live_loop() -> None:
    settings = _settings
    if settings is None:
        raise RuntimeError("live engine is not configured")
    print("[live] Binance live executor started")
    while True:
        try:
            tick()
        except Exception as exc:
            print(f"[live] tick failed: {type(exc).__name__}")
        time.sleep(settings.binance_live_trading_interval_seconds)
