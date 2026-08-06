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
from decimal import Decimal, InvalidOperation
from typing import Any

from . import market_store as store
from .binance_client import BinanceAccountClientError
from .binance_service import BinanceAccountService
from .binance_trading import BinanceUsdMTradingClient
from .config import Settings
from .domain.protection import ProtectionCoverage, ProtectionPlan
from .live_risk import (
    OpenPositionRisk,
    account_loss_limits,
    atr_risk_position_size,
    closed_bar_signal_freshness,
    leverage_for_stop_distance,
    liquidation_stop_safety,
    market_data_freshness,
    policy_from_config,
    signal_freshness,
    symbol_admission,
    symbol_risk_profile,
    tighten_policy_with_strategy,
    total_open_risk,
)
from .market_config import tradfi_symbols
from .paper_engine import _exit_levels, _strategy_signal, build_entry_basis_snapshot
from .security import CredentialCipher, SecurityError
from .strategy_evaluator import (
    StrategyEvaluationError,
    resolve_legacy_strategy_timeframe,
    strategy_timeframe_seconds,
)

_settings: Settings | None = None
_account_service: BinanceAccountService | None = None
_trading_client: BinanceUsdMTradingClient | None = None
_started = False
_start_lock = threading.Lock()
_reconcile_lock = threading.Lock()
_last_reconciled_at: dict[int, float] = {}
_reconciliation_failed: set[int] = set()
_RECONCILE_INTERVAL_SECONDS = 60.0
_position_mode_cache: dict[int, tuple[str, float]] = {}
_POSITION_MODE_TTL_SECONDS = 600.0
_account_backoff: dict[int, tuple[float, int]] = {}
_MAX_ACCOUNT_BACKOFF_SECONDS = 300.0


def configure(
    settings: Settings,
    account_service: BinanceAccountService,
    trading_client: BinanceUsdMTradingClient,
) -> None:
    global _settings, _account_service, _trading_client
    _settings = settings
    _account_service = account_service
    _trading_client = trading_client
    with _reconcile_lock:
        _last_reconciled_at.clear()
        _reconciliation_failed.clear()
        _position_mode_cache.clear()
        _account_backoff.clear()


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
                           d.runtime_state_json,
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
        account["strategy_snapshot_json"] = _json_object(account.get("strategy_snapshot_json"))
        account["deployment_mode"] = "live"
        accounts.append(account)
    return accounts


def _recovery_accounts() -> list[dict[str, Any]]:
    """Return stopped accounts whose crash-safe order intents still need resolution."""
    rows = store.query(
        """SELECT l.*,d.id AS deployment_id,d.strategy_revision_id,
                  d.runtime_state_json,
                  u.binance_api_key_encrypted,u.binance_api_secret_encrypted,
                  u.binance_key_version,u.binance_permissions
           FROM live_trading_accounts l
           JOIN strategy_deployments d
             ON d.user_id=l.user_id AND d.mode='live' AND d.target_account_id=l.id
           JOIN users u ON u.id=l.user_id AND u.is_active=1
           WHERE l.status IN ('paused','error')
             AND (
                 EXISTS(
                     SELECT 1 FROM live_order_intents i
                     WHERE i.user_id=l.user_id AND i.live_account_id=l.id
                       AND (
                           i.status IN ('created','unknown')
                           OR (
                               i.action IN ('open','close')
                               AND i.status='submitted'
                           )
                       )
                 )
                 OR (
                     l.status='error'
                     AND EXISTS(
                         SELECT 1 FROM live_order_intents p
                         WHERE p.user_id=l.user_id AND p.live_account_id=l.id
                           AND p.action IN ('stop','take_profit')
                           AND p.status='submitted'
                     )
                 )
                 OR (
                     l.status='error'
                     AND EXISTS(
                         SELECT 1 FROM live_order_intents o
                         WHERE o.user_id=l.user_id AND o.live_account_id=l.id
                           AND o.action='open' AND o.status='filled'
                           AND NOT EXISTS(
                               SELECT 1 FROM live_order_intents c
                               WHERE c.user_id=o.user_id
                                 AND c.live_account_id=o.live_account_id
                                 AND c.symbol=o.symbol
                                 AND c.position_side=o.position_side
                                 AND c.action='close' AND c.status='filled'
                                 AND c.id>o.id
                           )
                     )
                 )
             )
           ORDER BY l.id"""
    )
    accounts: list[dict[str, Any]] = []
    for row in rows:
        account = dict(row)
        account["config_json"] = _json_object(account.get("config_json"))
        account["strategy_snapshot_json"] = _json_object(account.get("strategy_snapshot_json"))
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


def _cached_position_mode(account: dict[str, Any], api_key: str, api_secret: str) -> str:
    """Check the high-weight position-mode endpoint at most once per ten minutes."""

    if _trading_client is None:
        raise RuntimeError("live engine is not configured")
    account_id = int(account["id"])
    now = time.monotonic()
    with _reconcile_lock:
        cached = _position_mode_cache.get(account_id)
    if cached is not None and now < cached[1]:
        return cached[0]
    mode = _trading_client.position_mode(api_key, api_secret)
    with _reconcile_lock:
        _position_mode_cache[account_id] = (mode, now + _POSITION_MODE_TTL_SECONDS)
    return mode


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
    strategy_signal_id: int | None = None,
    entry_basis: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    client_id = _client_id(int(account["id"]), signal_key)
    created = store.execute(
        """INSERT IGNORE INTO live_order_intents(
               public_id,user_id,live_account_id,deployment_id,signal_key,client_order_id,
               symbol,action,side,position_side,order_type,quantity,status,request_json,
               strategy_signal_id,entry_basis_json,created_at,updated_at
           ) VALUES(UUID(),?,?,?,?,?,?,?,?,?,?,?,'created',?,?,?,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)""",
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
            strategy_signal_id,
            json.dumps(entry_basis, ensure_ascii=False) if entry_basis else None,
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


def _execution_enabled(account: dict[str, Any]) -> bool:
    """Fence every exchange write against a concurrent pause/error transition."""
    rows = store.query(
        """SELECT 1
           FROM live_trading_accounts l
           JOIN strategy_deployments d
             ON d.id=? AND d.user_id=l.user_id AND d.mode='live'
                AND d.target_account_id=l.id
           WHERE l.id=? AND l.user_id=?
             AND l.status='active' AND d.status='running'
           LIMIT 1""",
        (account["deployment_id"], account["id"], account["user_id"]),
    )
    return bool(rows)


def _safety_write_enabled(account: dict[str, Any]) -> bool:
    """Allow only risk-reducing writes after pause/error, never after archival."""
    rows = store.query(
        """SELECT 1
           FROM live_trading_accounts l
           JOIN strategy_deployments d
             ON d.id=? AND d.user_id=l.user_id AND d.mode='live'
                AND d.target_account_id=l.id
           WHERE l.id=? AND l.user_id=?
             AND l.status IN ('active','paused','error')
             AND d.status IN ('running','paused','error')
           LIMIT 1""",
        (account["deployment_id"], account["id"], account["user_id"]),
    )
    return bool(rows)


def _exchange_intent_status(response: dict[str, Any]) -> str:
    """Map regular/algo Binance states into the deliberately small local state set."""
    raw = str(
        response.get("algoStatus") or response.get("strategyStatus") or response.get("status") or ""
    ).upper()
    if raw in {
        "NEW",
        "PENDING_NEW",
        "PARTIALLY_FILLED",
        "ACCEPTED",
        "WORKING",
        "TRIGGERING",
    }:
        return "submitted"
    if raw in {"FILLED", "TRIGGERED", "FINISHED"}:
        return "filled"
    if raw in {"CANCELED", "CANCELLED", "EXPIRED", "EXPIRED_IN_MATCH"}:
        return "canceled"
    if raw == "REJECTED":
        return "rejected"
    return "unknown"


def _normalized_open_order_response(order: dict[str, Any]) -> dict[str, Any]:
    """Convert BinanceAccountService's normalized open order into audit fields."""
    conditional = order.get("conditional") is True
    response: dict[str, Any] = {
        "symbol": order.get("symbol"),
        "side": order.get("side"),
        "positionSide": order.get("position_side"),
    }
    if conditional:
        response.update(
            {
                "algoId": order.get("order_id"),
                "clientAlgoId": order.get("client_order_id"),
                "algoStatus": order.get("status") or "NEW",
                "orderType": order.get("type"),
                "triggerPrice": order.get("stop_price"),
            }
        )
    else:
        response.update(
            {
                "orderId": order.get("order_id"),
                "clientOrderId": order.get("client_order_id"),
                "status": order.get("status") or "NEW",
                "type": order.get("type"),
                "avgPrice": order.get("average_price"),
            }
        )
    return response


def _reconciliation_due(account_id: int, *, force: bool = False) -> bool:
    now = time.monotonic()
    with _reconcile_lock:
        previous = _last_reconciled_at.get(account_id)
        if not force and previous is not None and now - previous < _RECONCILE_INTERVAL_SECONDS:
            if account_id in _reconciliation_failed:
                raise BinanceAccountClientError("reconciliation_backoff")
            return False
        # Reserve the interval before I/O so concurrent callers cannot duplicate
        # the two high-weight all-symbol open-order requests.
        _last_reconciled_at[account_id] = now
    return True


def _finish_reconciliation(account_id: int, *, successful: bool) -> None:
    with _reconcile_lock:
        if successful:
            _reconciliation_failed.discard(account_id)
        else:
            _reconciliation_failed.add(account_id)


def _request_reconciliation(account_id: int) -> None:
    """Make a newly unknown local intent eligible for immediate recovery."""
    with _reconcile_lock:
        _last_reconciled_at.pop(account_id, None)
        _reconciliation_failed.discard(account_id)


def _account_backoff_active(account_id: int) -> bool:
    with _reconcile_lock:
        state = _account_backoff.get(account_id)
    return state is not None and time.monotonic() < state[0]


def _record_account_backoff(account_id: int, category: str) -> None:
    if category not in {"rate_limit", "network", "timeout", "upstream"}:
        return
    now = time.monotonic()
    with _reconcile_lock:
        previous = _account_backoff.get(account_id)
        failures = (previous[1] if previous is not None else 0) + 1
        base = 60.0 if category == "rate_limit" else 30.0
        delay = min(_MAX_ACCOUNT_BACKOFF_SECONDS, base * (2 ** (failures - 1)))
        _account_backoff[account_id] = (now + delay, failures)


def _clear_account_backoff(account_id: int) -> None:
    with _reconcile_lock:
        _account_backoff.pop(account_id, None)


def _reconcile_intents(
    account: dict[str, Any], api_key: str, api_secret: str, *, force: bool = False
) -> bool:
    """Resolve local non-terminal intents against Binance's current/order APIs.

    The open-order snapshot validates all active protective orders in two requests.
    Only orders absent from that snapshot need an individual terminal-state query.
    """
    if _account_service is None or _trading_client is None:
        raise RuntimeError("live engine is not configured")
    if not _reconciliation_due(int(account["id"]), force=force):
        return False
    rows = store.query(
        """SELECT id,user_id,symbol,action,status,client_order_id
           FROM live_order_intents
           WHERE user_id=? AND live_account_id=?
             AND status IN ('created','submitted','unknown')
           ORDER BY id""",
        (account["user_id"], account["id"]),
    )
    if not rows:
        _finish_reconciliation(int(account["id"]), successful=True)
        return False
    try:
        current_orders = _account_service.open_orders(
            api_key,
            api_secret,
            account_type="UM_FUTURE",
            force_refresh=True,
        )
    except Exception:
        # Keep the account fenced during a bounded retry interval. This avoids
        # hammering the two high-weight all-symbol endpoints during an outage.
        _finish_reconciliation(int(account["id"]), successful=False)
        raise
    current_by_client_id = {
        str(order.get("client_order_id") or ""): order
        for order in current_orders
        if str(order.get("client_order_id") or "")
    }
    market_state_changed = False
    pending_market = False
    for raw_row in rows:
        row = dict(raw_row)
        client_order_id = str(row["client_order_id"])
        current = current_by_client_id.get(client_order_id)
        if current is not None:
            response = _normalized_open_order_response(current)
            _update_intent(
                int(row["id"]),
                int(row["user_id"]),
                status="submitted",
                response=response,
            )
            if row["action"] in {"open", "close"}:
                # A MARKET order should be terminal before strategy evaluation
                # resumes. Keep the account fenced instead of issuing another
                # entry/close while Binance still reports it as open.
                pending_market = True
            continue
        try:
            if row["action"] in {"stop", "take_profit"}:
                response = _trading_client.query_algo_order(
                    api_key, api_secret, client_order_id=client_order_id
                )
            else:
                response = _trading_client.query_order(
                    api_key,
                    api_secret,
                    symbol=str(row["symbol"]),
                    client_order_id=client_order_id,
                )
        except BinanceAccountClientError as exc:
            if exc.code in {-2011, -2013}:
                _update_intent(
                    int(row["id"]),
                    int(row["user_id"]),
                    status="canceled",
                    error_code="exchange_order_not_found",
                )
                if row["action"] in {"open", "close"}:
                    market_state_changed = True
                continue
            _finish_reconciliation(int(account["id"]), successful=False)
            raise
        status = _exchange_intent_status(response)
        _update_intent(
            int(row["id"]),
            int(row["user_id"]),
            status=status,
            response=response,
            error_code="unrecognized_exchange_status" if status == "unknown" else None,
        )
        if row["action"] in {"open", "close"}:
            if status == "submitted":
                pending_market = True
            else:
                market_state_changed = True
        elif row["action"] in {"stop", "take_profit"} and status == "filled":
            # A triggered protection changes/removes the exchange position.
            # The caller fetched its snapshot before reconciliation, so force
            # a fresh account read before any close/open decision is made.
            market_state_changed = True
        if status == "unknown":
            _finish_reconciliation(int(account["id"]), successful=False)
            raise BinanceAccountClientError("invalid_response")
    if pending_market:
        _finish_reconciliation(int(account["id"]), successful=False)
        raise BinanceAccountClientError("order_state_pending")
    _finish_reconciliation(int(account["id"]), successful=True)
    return market_state_changed


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
    leverage: int | None = None,
    strategy_signal_id: int | None = None,
    entry_basis: dict[str, Any] | None = None,
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
            "leverage": leverage,
        },
        strategy_signal_id=strategy_signal_id,
        entry_basis=entry_basis,
    )
    if intent is None:
        return None
    write_enabled = (
        _safety_write_enabled(account)
        if action == "close" or reduce_only
        else _execution_enabled(account)
    )
    if not write_enabled:
        _update_intent(
            intent["id"],
            account["user_id"],
            status="canceled",
            error_code="execution_stopped",
        )
        return None
    if action == "open" and leverage is not None:
        try:
            _trading_client.change_leverage(
                api_key,
                api_secret,
                symbol=symbol,
                leverage=leverage,
            )
        except BinanceAccountClientError as exc:
            # No market order has been sent yet, so this intent is terminal and
            # safe to retry with a future signal rather than reconcile as an
            # uncertain exchange fill.
            _update_intent(
                intent["id"],
                account["user_id"],
                status="rejected",
                error_code=f"leverage_{exc.category}",
            )
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
                _request_reconciliation(int(account["id"]))
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
        _request_reconciliation(int(account["id"]))
        _fail_account(account, "order_state_unknown")
        return None
    try:
        _update_intent(intent["id"], account["user_id"], status="filled", response=response)
    except Exception as exc:
        # Binance is authoritative once it confirms FILLED. The pre-created
        # intent and deterministic client id make this recoverable, while
        # returning the fill lets an open install protection immediately and
        # lets a close finish without submitting a duplicate market order.
        account["_local_audit_pending"] = True
        _request_reconciliation(int(account["id"]))
        print(f"[live] filled order audit pending: {type(exc).__name__}")
    return response


def _rollback_protection_orders(
    account: dict[str, Any],
    api_key: str,
    api_secret: str,
    created: list[dict[str, Any]],
) -> None:
    """Best-effort rollback that never hides an uncertain protective order."""
    if _trading_client is None:
        return
    for previous in created:
        try:
            _trading_client.cancel_algo_order(
                api_key,
                api_secret,
                client_order_id=previous["client_order_id"],
            )
            _update_intent(previous["id"], account["user_id"], status="canceled")
        except BinanceAccountClientError as exc:
            if exc.code in {-2011, -2013}:
                _update_intent(
                    previous["id"],
                    account["user_id"],
                    status="canceled",
                    error_code="exchange_order_not_found",
                )
                continue
            _update_intent(
                previous["id"],
                account["user_id"],
                status="unknown",
                error_code=f"protective_cancel_{exc.category}"[:64],
            )
            _request_reconciliation(int(account["id"]))
            _fail_account(account, "protective_cancel_failed")


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
    try:
        plan = ProtectionPlan.create(
            symbol=symbol,
            close_side=side,
            position_side=position_side,
            quantity=quantity,
            signal_time=signal_time,
            stop=stop,
            target=target,
        )
    except (TypeError, ValueError):
        return False
    created: list[dict[str, Any]] = []
    for protection in plan.orders:
        action = protection.action.value
        order_type = protection.order_type.value
        trigger = protection.trigger_price
        signal_key = plan.signal_key(account["deployment_id"], protection.action)
        intent = _create_intent(
            account,
            signal_key=signal_key,
            symbol=plan.symbol,
            action=action,
            side=plan.close_side.value,
            position_side=plan.position_side.value,
            order_type=order_type,
            quantity=plan.quantity,
            request_json={
                "symbol": plan.symbol,
                "side": plan.close_side.value,
                "position_side": plan.position_side.value,
                "type": order_type,
                "stop_price": format(trigger, "f"),
                "quantity": (
                    format(plan.quantity, "f") if plan.quantity is not None else None
                ),
                "close_position": plan.quantity is None,
                "working_type": "MARK_PRICE",
            },
        )
        if intent is None:
            return False
        if not _safety_write_enabled(account):
            _update_intent(
                intent["id"],
                account["user_id"],
                status="canceled",
                error_code="execution_stopped",
            )
            return False
        try:
            response = _trading_client.place_close_trigger(
                api_key,
                api_secret,
                symbol=plan.symbol,
                side=plan.close_side.value,  # type: ignore[arg-type]
                order_type=order_type,  # type: ignore[arg-type]
                stop_price=trigger,
                client_order_id=intent["client_order_id"],
                position_side=plan.position_side.value,  # type: ignore[arg-type]
                quantity=plan.quantity,
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
                    _request_reconciliation(int(account["id"]))
                    return False
            else:
                _update_intent(
                    intent["id"], account["user_id"], status="rejected", error_code=exc.category
                )
                _rollback_protection_orders(account, api_key, api_secret, created)
                return False
        status = _exchange_intent_status(response)
        _update_intent(
            intent["id"],
            account["user_id"],
            status=status,
            response=response,
            error_code="unrecognized_exchange_status" if status == "unknown" else None,
        )
        if status != "submitted":
            if status == "unknown":
                _request_reconciliation(int(account["id"]))
            _rollback_protection_orders(account, api_key, api_secret, created)
            return False
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
            # The authoritative order lookup already proved this intent is no
            # longer open; do not leave a stale `submitted` row behind forever.
            if exc.code in {-2011, -2013}:
                _update_intent(
                    row["id"],
                    account["user_id"],
                    status="canceled",
                    error_code="exchange_order_not_found",
                )
            else:
                _fail_account(account, "protective_cancel_failed")


def _managed_positions(account: dict[str, Any]) -> dict[tuple[str, str], dict[str, Any]]:
    rows = store.query(
        """SELECT id,symbol,position_side,action,side,status,quantity,strategy_signal_id,
                  entry_basis_json,submitted_at,created_at
           FROM live_order_intents
           WHERE user_id=? AND live_account_id=?
             AND action IN ('open','close') AND status='filled'
           ORDER BY id DESC""",
        (account["user_id"], account["id"]),
    )
    latest: dict[tuple[str, str], dict[str, Any]] = {}
    for raw_row in rows:
        row = dict(raw_row)
        key = (
            str(row["symbol"]).upper(),
            str(row.get("position_side") or "BOTH").upper(),
        )
        if key not in latest:
            latest[key] = row
    return {key: row for key, row in latest.items() if row["action"] == "open"}


def _managed_open(
    account: dict[str, Any], symbol: str, position_side: str
) -> dict[str, Any] | None:
    return _managed_positions(account).get((symbol, position_side))


def _pending_market_intent(
    account: dict[str, Any], symbol: str, position_side: str, action: str
) -> bool:
    rows = store.query(
        """SELECT 1 FROM live_order_intents
           WHERE user_id=? AND live_account_id=? AND symbol=? AND position_side=?
             AND action=? AND order_type='MARKET'
             AND status IN ('created','submitted','unknown')
           LIMIT 1""",
        (
            account["user_id"],
            account["id"],
            symbol,
            position_side,
            action,
        ),
    )
    return bool(rows)


def _protection_counts(
    account: dict[str, Any],
    managed: dict[tuple[str, str], dict[str, Any]] | None = None,
) -> dict[tuple[str, str], int]:
    managed = managed if managed is not None else _managed_positions(account)
    rows = store.query(
        """SELECT id,symbol,position_side,action FROM live_order_intents
           WHERE user_id=? AND live_account_id=?
             AND action IN ('stop','take_profit') AND status='submitted'
           ORDER BY id""",
        (account["user_id"], account["id"]),
    )
    actions: dict[tuple[str, str], set[str]] = {}
    for row in rows:
        key = (
            str(row["symbol"]).upper(),
            str(row.get("position_side") or "BOTH").upper(),
        )
        opened = managed.get(key)
        # Stale protection from an earlier generation must never protect a
        # newly reopened position with the same symbol/side.
        if opened is None or int(row["id"]) <= int(opened["id"]):
            continue
        actions.setdefault(key, set()).add(str(row["action"]))
    return {
        key: len(ProtectionCoverage.from_actions(value).actions)
        for key, value in actions.items()
    }


def _cancel_orphan_protections(
    account: dict[str, Any],
    api_key: str,
    api_secret: str,
    managed: dict[tuple[str, str], dict[str, Any]],
    exchange_position_keys: set[tuple[str, str]] | None = None,
) -> None:
    """Remove protection only when neither local nor exchange exposure exists.

    A pre-migration/manual exchange position may not have a managed open intent.
    Its already-live protection must not be canceled merely because local
    ownership metadata is incomplete.
    """
    occupied = exchange_position_keys or set()
    rows = store.query(
        """SELECT DISTINCT symbol,position_side FROM live_order_intents
           WHERE user_id=? AND live_account_id=?
             AND action IN ('stop','take_profit') AND status='submitted'""",
        (account["user_id"], account["id"]),
    )
    for row in rows:
        key = (
            str(row["symbol"]).upper(),
            str(row.get("position_side") or "BOTH").upper(),
        )
        if key not in managed and key not in occupied:
            _cancel_protection(account, api_key, api_secret, *key)


def _failed_close_keys(
    account: dict[str, Any],
    managed: dict[tuple[str, str], dict[str, Any]],
) -> set[tuple[str, str]]:
    """Return current position generations with a terminal failed close attempt."""
    rows = store.query(
        """SELECT id,symbol,position_side FROM live_order_intents
           WHERE user_id=? AND live_account_id=? AND action='close'
             AND order_type='MARKET' AND status IN ('canceled','rejected')
           ORDER BY id DESC""",
        (account["user_id"], account["id"]),
    )
    failed: set[tuple[str, str]] = set()
    for row in rows:
        key = (
            str(row["symbol"]).upper(),
            str(row.get("position_side") or "BOTH").upper(),
        )
        opened = managed.get(key)
        if opened is not None and int(row["id"]) > int(opened["id"]):
            failed.add(key)
    return failed


def _current_stop_prices(
    account: dict[str, Any],
    managed: dict[tuple[str, str], dict[str, Any]],
) -> dict[tuple[str, str], float]:
    rows = store.query(
        """SELECT id,symbol,position_side,request_json
           FROM live_order_intents
           WHERE user_id=? AND live_account_id=?
             AND action='stop' AND status='submitted'
           ORDER BY id DESC""",
        (account["user_id"], account["id"]),
    )
    stops: dict[tuple[str, str], float] = {}
    for row in rows:
        key = (
            str(row["symbol"]).upper(),
            str(row.get("position_side") or "BOTH").upper(),
        )
        opened = managed.get(key)
        if opened is None or int(row["id"]) <= int(opened["id"]) or key in stops:
            continue
        request = _json_object(row.get("request_json"))
        try:
            stop = float(request.get("stop_price"))
        except (TypeError, ValueError):
            continue
        if math.isfinite(stop) and stop > 0:
            stops[key] = stop
    return stops


def _current_open_risk(
    snapshot: Any,
    managed: dict[tuple[str, str], dict[str, Any]],
    stop_prices: dict[tuple[str, str], float],
    *,
    exit_cost_bps: Decimal,
) -> Decimal:
    risks: list[OpenPositionRisk] = []
    for position in snapshot.positions:
        key = _position_key(position)
        opened = managed.get(key)
        stop = stop_prices.get(key)
        if opened is None or stop is None:
            continue
        try:
            quantity = Decimal(str(position["amt"]))
            entry = Decimal(str(position["entry_price"]))
        except (KeyError, TypeError, ValueError):
            continue
        if quantity <= 0 or entry <= 0:
            continue
        risks.append(
            OpenPositionRisk(
                quantity=quantity,
                entry_price=entry,
                stop_price=Decimal(str(stop)),
                exit_cost_bps=exit_cost_bps,
            )
        )
    return total_open_risk(risks)


def _current_initial_margin(
    positions: Any,
    *,
    fallback_leverage: int,
) -> Decimal:
    """Sum Binance's exact initial margin, deriving only missing values."""

    total = Decimal(0)
    for position in positions:
        raw_initial_margin = position.get("initial_margin")
        if raw_initial_margin is not None:
            try:
                initial_margin = Decimal(str(raw_initial_margin))
            except (InvalidOperation, TypeError, ValueError):
                initial_margin = None
            if (
                initial_margin is not None
                and initial_margin.is_finite()
                and initial_margin > 0
            ):
                total += initial_margin
                continue
        try:
            notional = abs(Decimal(str(position.get("notional") or 0)))
            leverage = max(int(position.get("leverage") or fallback_leverage), 1)
        except (InvalidOperation, TypeError, ValueError):
            continue
        if notional.is_finite():
            total += notional / Decimal(leverage)
    return total


def _has_unmanaged_exposure(
    positions: dict[tuple[str, str], dict[str, Any]],
    managed: dict[tuple[str, str], dict[str, Any]],
) -> bool:
    """Block new entries when external positions or manual size changes exist."""

    for key, position in positions.items():
        opened = managed.get(key)
        if opened is None:
            return True
        try:
            actual = Decimal(str(position["amt"]))
            expected = Decimal(str(opened.get("quantity") or position["amt"]))
        except (InvalidOperation, KeyError, TypeError, ValueError):
            return True
        if actual != expected:
            return True
    return False


def _is_grandfathered_open(opened: dict[str, Any]) -> bool:
    """Return whether an open predates immutable live entry capture.

    Migration 0020 deliberately left historical rows NULL, while every new
    live open persists a versioned ``availability=captured`` snapshot before
    the exchange write. Unknown/malformed snapshots are treated as historical
    so a metadata problem cannot itself cause an automatic market close.
    """

    basis = _json_object(opened.get("entry_basis_json"))
    return not (
        basis.get("schema_version") == 1
        and basis.get("availability") == "captured"
        and basis.get("mode") == "live"
    )


def _risk_review_warnings(
    positions: dict[tuple[str, str], dict[str, Any]],
    managed: dict[tuple[str, str], dict[str, Any]],
    protection_counts: dict[tuple[str, str], int] | None = None,
) -> list[dict[str, Any]]:
    """Describe exposure that must be reviewed before another live entry."""

    warnings: list[dict[str, Any]] = []
    counts = protection_counts or {}
    for key in sorted(positions):
        symbol, position_side = key
        position = positions[key]
        opened = managed.get(key)
        if opened is None:
            warnings.append(
                {
                    "code": "unmanaged_exchange_position",
                    "symbol": symbol,
                    "position_side": position_side,
                    "message": (
                        "Exchange position is not owned by this deployment; "
                        "new entries are blocked until it is manually reviewed."
                    ),
                }
            )
            continue

        if _is_grandfathered_open(opened):
            warnings.append(
                {
                    "code": "historical_position_review_required",
                    "symbol": symbol,
                    "position_side": position_side,
                    "protection_count": int(counts.get(key, 0)),
                    "message": (
                        "Historical position has no captured entry basis; its existing "
                        "protection is retained and new-rule auto-close checks are skipped. "
                        "New entries remain blocked until manual review."
                    ),
                    "grandfathered_checks": [
                        "protection_completeness",
                        "liquidation_buffer",
                        "max_holding_bars",
                    ],
                }
            )

        try:
            actual = Decimal(str(position["amt"]))
            expected = Decimal(str(opened.get("quantity") or position["amt"]))
        except (InvalidOperation, KeyError, TypeError, ValueError):
            actual = None
            expected = None
        if actual is None or expected is None or actual != expected:
            warnings.append(
                {
                    "code": "managed_quantity_mismatch",
                    "symbol": symbol,
                    "position_side": position_side,
                    "expected_quantity": str(expected) if expected is not None else None,
                    "actual_quantity": str(actual) if actual is not None else None,
                    "message": (
                        "Exchange quantity differs from the managed open quantity; "
                        "new entries are blocked until it is manually reviewed."
                    ),
                }
            )
    return warnings


def _apply_risk_review_state(
    state: dict[str, Any], warnings: list[dict[str, Any]]
) -> None:
    state["risk_review_required"] = bool(warnings)
    state["risk_review_warnings"] = warnings
    raw_reasons = state.get("entry_block_reasons")
    reasons = list(raw_reasons) if isinstance(raw_reasons, list) else []
    reasons = [reason for reason in reasons if reason != "risk_review_required"]
    if warnings:
        reasons.append("risk_review_required")
    state["entry_block_reasons"] = reasons


def _persist_risk_review(
    account: dict[str, Any], warnings: list[dict[str, Any]]
) -> None:
    state = _json_object(account.get("runtime_state_json"))
    _apply_risk_review_state(state, warnings)
    store.execute(
        """UPDATE strategy_deployments SET runtime_state_json=?,updated_at=CURRENT_TIMESTAMP
           WHERE id=? AND user_id=?""",
        (json.dumps(state, ensure_ascii=False), account["deployment_id"], account["user_id"]),
    )
    account["runtime_state_json"] = state


def _entry_loss_guard(
    account: dict[str, Any],
    snapshot: Any,
    policy: Any,
    review_warnings: list[dict[str, Any]] | None = None,
) -> bool:
    """Persist daily/high-water equity and block only new entries on breach."""

    state = _json_object(account.get("runtime_state_json"))
    equity = Decimal(str(snapshot.wallet_balance)) + Decimal(
        str(getattr(snapshot, "unrealized_pnl", 0))
    )
    if equity <= 0:
        if review_warnings is not None:
            _persist_risk_review(account, review_warnings)
        return False
    today = time.strftime("%Y-%m-%d", time.gmtime())
    if state.get("risk_day") != today:
        state["risk_day"] = today
        state["day_start_equity"] = float(equity)
    try:
        day_start = Decimal(str(state.get("day_start_equity")))
    except (InvalidOperation, TypeError, ValueError):
        day_start = equity
    try:
        high_watermark = max(Decimal(str(state.get("high_watermark_equity"))), equity)
    except (InvalidOperation, TypeError, ValueError):
        high_watermark = equity
    decision = account_loss_limits(
        current_equity=equity,
        start_of_day_equity=day_start,
        high_watermark_equity=high_watermark,
        policy=policy,
    )
    state.update(
        {
            "last_equity": float(equity),
            "high_watermark_equity": float(high_watermark),
            "daily_loss_pct": float(decision.daily_loss_pct),
            "drawdown_pct": float(decision.drawdown_pct),
            "entry_block_reasons": list(decision.reasons),
        }
    )
    if review_warnings is not None:
        _apply_risk_review_state(state, review_warnings)
    store.execute(
        """UPDATE strategy_deployments SET runtime_state_json=?,updated_at=CURRENT_TIMESTAMP
           WHERE id=? AND user_id=?""",
        (json.dumps(state, ensure_ascii=False), account["deployment_id"], account["user_id"]),
    )
    account["runtime_state_json"] = state
    return decision.allow_new_entries and not review_warnings


def _signal_is_fresh(
    account: dict[str, Any],
    signal_time: int,
    policy: Any,
    evidence: dict[str, Any] | None = None,
) -> bool:
    snapshot = account.get("strategy_snapshot_json") or {}
    if snapshot.get("strategy_kind") == "legacy_signal":
        try:
            timeframe_seconds = _execution_timeframe_seconds(account)
        except (StrategyEvaluationError, TypeError, ValueError):
            return False
        closed = closed_bar_signal_freshness(
            signal_time,
            timeframe_seconds=timeframe_seconds,
            valid_bars=max(
                1,
                math.ceil(policy.max_signal_age_seconds / timeframe_seconds),
            ),
        )
        maximum_age = signal_freshness(
            signal_time,
            max_age_seconds=policy.max_signal_age_seconds,
        )
        return closed.fresh and maximum_age.fresh

    maximum_age = signal_freshness(
        signal_time,
        max_age_seconds=policy.max_signal_age_seconds,
    )
    if not maximum_age.fresh:
        return False
    raw_valid_until = (evidence or {}).get("valid_until")
    if raw_valid_until is None:
        return False
    try:
        valid_until = float(raw_valid_until)
    except (TypeError, ValueError):
        return False
    while valid_until >= 100_000_000_000:
        valid_until /= 1000
    return math.isfinite(valid_until) and time.time() <= valid_until


def _execution_timeframe_seconds(account: dict[str, Any]) -> int:
    """Resolve the immutable trigger interval used by holding/freshness rules."""

    snapshot = account.get("strategy_snapshot_json") or {}
    if snapshot.get("strategy_kind") == "full_strategy":
        spec = snapshot.get("spec")
        timeframes = spec.get("timeframes") if isinstance(spec, dict) else None
        trigger = timeframes.get("trigger") if isinstance(timeframes, dict) else None
        if not isinstance(trigger, str):
            raise StrategyEvaluationError("full strategy trigger timeframe is unavailable")
        return strategy_timeframe_seconds(trigger)
    timeframe = resolve_legacy_strategy_timeframe(
        snapshot,
        account.get("config_json")
        if isinstance(account.get("config_json"), dict)
        else None,
    )
    return strategy_timeframe_seconds(timeframe)


def _signal_exit_levels(
    entry: float,
    direction: int,
    atr: float | None,
    config: dict[str, Any],
    evidence: dict[str, Any] | None,
) -> tuple[float | None, float | None]:
    """Use a full strategy's fixed risk proposal, otherwise the legacy ATR rule."""

    proposal = (evidence or {}).get("risk_proposal")
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
            return (
                entry - direction * stop_distance,
                entry + direction * take_distance,
            )
        return None, None
    return _exit_levels(entry, direction, atr, config)


def _opened_at_seconds(managed_open: dict[str, Any]) -> float | None:
    value = managed_open.get("submitted_at") or managed_open.get("created_at")
    if hasattr(value, "timestamp"):
        return float(value.timestamp())
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        parsed = float(value)
        while parsed >= 100_000_000_000:
            parsed /= 1000
        return parsed
    return None


def _record_reconciled_close(account: dict[str, Any], managed_open: dict[str, Any]) -> None:
    """Close the local position generation after Binance reports no position."""
    symbol = str(managed_open["symbol"]).upper()
    position_side = str(managed_open.get("position_side") or "BOTH").upper()
    open_side = str(managed_open.get("side") or "BUY").upper()
    signal_key = (
        f"live:{account['deployment_id']}:{symbol}:{position_side}:"
        f"reconciled-close:{managed_open['id']}"
    )
    intent = _create_intent(
        account,
        signal_key=signal_key,
        symbol=symbol,
        action="close",
        side="SELL" if open_side == "BUY" else "BUY",
        position_side=position_side,
        order_type="RECONCILED",
        quantity=managed_open.get("quantity"),
        request_json={
            "reason": "exchange_position_absent",
            "open_intent_id": int(managed_open["id"]),
        },
        strategy_signal_id=managed_open.get("strategy_signal_id"),
        entry_basis=_json_object(managed_open.get("entry_basis_json")),
    )
    if intent is not None:
        _update_intent(
            intent["id"],
            account["user_id"],
            status="filled",
            response={"status": "FILLED", "symbol": symbol},
        )


def _close_position(
    account: dict[str, Any], api_key: str, api_secret: str, position: dict[str, Any], reason: str
) -> bool:
    if _trading_client is None:
        return False
    symbol = str(position["symbol"])
    position_side = str(position.get("position_side") or "BOTH").upper()
    managed = _managed_open(account, symbol, position_side)
    if managed is None:
        return False
    if _pending_market_intent(account, symbol, position_side, "close"):
        return False
    rules = _trading_client.symbol_rules(symbol)
    actual_quantity = Decimal(str(position["amt"]))
    managed_quantity = Decimal(str(managed.get("quantity") or actual_quantity))
    quantity = rules.quantity(min(actual_quantity, managed_quantity))
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
        strategy_signal_id=managed.get("strategy_signal_id"),
        entry_basis=_json_object(managed.get("entry_basis_json")),
    )
    if response is None:
        _fail_account(account, "position_close_failed")
        # Keep the exchange-side stop/target alive while the market close is
        # uncertain. Removing protection first creates an avoidable naked window.
        return False
    _cancel_protection(account, api_key, api_secret, symbol, position_side)
    return True


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
    current_open_risk: Decimal = Decimal(0),
    basis: list[str] | None = None,
    signal_evidence: dict[str, Any] | None = None,
) -> None:
    if _trading_client is None:
        return
    config = account["config_json"]
    policy = policy_from_config(config)
    strategy_snapshot = account.get("strategy_snapshot_json") or {}
    if strategy_snapshot.get("strategy_kind") == "full_strategy":
        proposal = (signal_evidence or {}).get("risk_proposal")
        if not isinstance(proposal, dict):
            return
        try:
            policy = tighten_policy_with_strategy(policy, proposal)
        except ValueError:
            return
    position_mode = str(config.get("position_mode") or "one_way")
    position_side = _strategy_position_side(position_mode, direction)
    requested_leverage = max(1, min(int(config.get("leverage", 3)), 20))
    preview_stop, preview_target = _signal_exit_levels(
        price,
        direction,
        atr,
        config,
        signal_evidence,
    )
    if preview_stop is None or preview_target is None:
        return
    stop_distance = abs(Decimal(str(price)) - Decimal(str(preview_stop)))
    leverage = leverage_for_stop_distance(
        entry_price=price,
        stop_distance=stop_distance,
        requested_leverage=requested_leverage,
        policy=policy,
    )
    rules = _trading_client.symbol_rules(symbol)
    available = Decimal(str(snapshot.available_balance))
    wallet = Decimal(str(snapshot.wallet_balance))
    equity = wallet + Decimal(str(getattr(snapshot, "unrealized_pnl", 0)))
    margin_equity = max(Decimal(0), min(wallet, equity))
    current_margin = _current_initial_margin(
        snapshot.positions,
        fallback_leverage=leverage,
    )
    remaining_margin = min(
        available,
        max(
            Decimal(0),
            margin_equity * Decimal(str(config.get("margin_cap", 0.20)))
            - current_margin,
        ),
    )
    sizing = atr_risk_position_size(
        equity=equity,
        available_balance=remaining_margin,
        entry_price=price,
        stop_distance=stop_distance,
        requested_leverage=leverage,
        current_open_risk=current_open_risk,
        direction=direction,
        high_risk=symbol_risk_profile(symbol).high_risk,
        policy=policy,
    )
    if not sizing.allowed:
        return
    leverage = sizing.effective_leverage
    try:
        quantity = rules.quantity(sizing.quantity)
    except ValueError:
        return
    if quantity * Decimal(str(price)) < rules.min_notional:
        return
    requested_margin = quantity * Decimal(str(price)) / Decimal(leverage)
    side = "BUY" if direction > 0 else "SELL"
    close_side = "SELL" if direction > 0 else "BUY"
    signal_key = f"live:{account['deployment_id']}:{symbol}:{signal_time}:open:{direction}"
    entry_basis, strategy_signal_id = build_entry_basis_snapshot(
        account,
        mode="live",
        symbol=symbol,
        direction=direction,
        signal_time=signal_time,
        reasons=list(basis or []),
        evidence=signal_evidence,
        entry_price=price,
        atr=atr,
        stop=preview_stop,
        target=preview_target,
        leverage=leverage,
        margin=float(requested_margin),
    )
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
        leverage=leverage,
        strategy_signal_id=strategy_signal_id,
        entry_basis=entry_basis,
    )
    if response is None:
        return
    try:
        entry = float(response.get("avgPrice") or 0)
    except (TypeError, ValueError):
        entry = 0.0
    if (not math.isfinite(entry) or entry <= 0) and response.get("clientOrderId"):
        try:
            verified_response = _trading_client.query_order(
                api_key,
                api_secret,
                symbol=symbol,
                client_order_id=str(response["clientOrderId"]),
            )
            entry = float(verified_response.get("avgPrice") or 0)
        except (BinanceAccountClientError, TypeError, ValueError):
            entry = 0.0
    fallback_position = {
        "symbol": symbol,
        "amt": float(quantity),
        "side": "long" if direction > 0 else "short",
        "position_side": position_side,
    }
    if not math.isfinite(entry) or entry <= 0:
        # A RESULT response should contain avgPrice. If it does not, install the
        # pre-trade emergency stop/target before attempting a fail-closed exit.
        # This leaves protection in place if the exit request becomes uncertain.
        _place_protection(
            account,
            api_key,
            api_secret,
            symbol=symbol,
            side=close_side,
            position_side=position_side,
            quantity=quantity if position_side != "BOTH" else None,
            signal_time=signal_time,
            stop=rules.price(Decimal(str(preview_stop))),
            target=rules.price(Decimal(str(preview_target))),
        )
        _close_position(
            account,
            api_key,
            api_secret,
            fallback_position,
            "entry_price_unverified",
        )
        _fail_account(account, "entry_price_unverified")
        return
    stop_raw, target_raw = _signal_exit_levels(
        entry,
        direction,
        atr,
        config,
        signal_evidence,
    )
    if stop_raw is None or target_raw is None:
        _close_position(
            account,
            api_key,
            api_secret,
            fallback_position,
            "missing_protection",
        )
        _fail_account(account, "missing_protection")
        return
    stop = rules.price(Decimal(str(stop_raw)))
    target = rules.price(Decimal(str(target_raw)))
    # Protection is the first action after the fill price is known. Account
    # refreshes and audit enrichment happen only after both orders are live.
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
            fallback_position,
            "protection_failed",
        )
        _fail_account(account, "protection_failed")
        return
    entry_basis["execution"].update(
        {
            "entry_price": entry,
            "stop": stop_raw,
            "target": target_raw,
            "quantity": float(quantity),
        }
    )
    store.execute(
        """UPDATE live_order_intents SET entry_basis_json=?,updated_at=CURRENT_TIMESTAMP
           WHERE user_id=? AND live_account_id=? AND signal_key=? AND action='open'""",
        (
            json.dumps(entry_basis, ensure_ascii=False),
            account["user_id"],
            account["id"],
            signal_key,
        ),
    )
    try:
        refreshed = (
            _account_service.account(api_key, api_secret, force_refresh=True)
            if _account_service
            else None
        )
    except BinanceAccountClientError:
        _close_position(
            account,
            api_key,
            api_secret,
            fallback_position,
            "position_state_unverified",
        )
        _fail_account(account, "position_state_unverified")
        return
    refreshed_position = None
    if refreshed is not None:
        refreshed_position = next(
            (
                item
                for item in refreshed.positions
                if _position_key(item) == (symbol, position_side)
            ),
            None,
        )
    if refreshed_position is None:
        _close_position(
            account,
            api_key,
            api_secret,
            fallback_position,
            "position_state_unverified",
        )
        _fail_account(account, "position_state_unverified")
        return
    liquidation_price = refreshed_position.get("liquidation_price")
    if not liquidation_price:
        safety_ok = False
    else:
        try:
            safety_ok = liquidation_stop_safety(
                entry_price=entry,
                stop_price=float(stop),
                liquidation_price=liquidation_price,
                direction=direction,
                min_buffer_pct=policy.liquidation_buffer_pct,
            ).safe
        except (TypeError, ValueError):
            safety_ok = False
    if not safety_ok:
        _close_position(
            account,
            api_key,
            api_secret,
            refreshed_position,
            "liquidation_buffer_unsafe",
        )
        _fail_account(account, "liquidation_buffer_unsafe")
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
    policy = policy_from_config(config)
    try:
        execution_timeframe_seconds = _execution_timeframe_seconds(account)
    except (StrategyEvaluationError, TypeError, ValueError):
        _fail_account(account, "strategy_timeframe_invalid")
        return
    configured_mode = str(config.get("position_mode") or "one_way")
    current_mode = _cached_position_mode(account, api_key, api_secret)
    if current_mode != configured_mode:
        _fail_account(account, "position_mode_changed")
        return
    market_state_changed = _reconcile_intents(account, api_key, api_secret)
    if market_state_changed:
        snapshot = _account_service.account(api_key, api_secret, force_refresh=True)
    symbols = _strategy_universe(config)
    positions = {_position_key(item): item for item in snapshot.positions}
    managed = _managed_positions(account)
    _cancel_orphan_protections(account, api_key, api_secret, managed, set(positions))
    protection_counts = _protection_counts(account, managed)
    stop_prices = _current_stop_prices(account, managed)
    max_positions = max(1, min(int(config.get("max_positions", 1)), 20))
    positions_changed = False

    for key in managed:
        symbol, position_side = key
        if symbol not in symbols:
            continue
        position = positions.get(key)
        if position is None:
            _cancel_protection(account, api_key, api_secret, symbol, position_side)
            _record_reconciled_close(account, managed[key])
            continue
        side = 1 if position_side == "LONG" or position["side"] == "long" else -1
        if not _is_grandfathered_open(managed[key]):
            if protection_counts.get(key, 0) != 2:
                _close_position(account, api_key, api_secret, position, "protection_missing")
                _fail_account(account, "protection_missing")
                return
            stop_price = stop_prices.get(key)
            liquidation_price = position.get("liquidation_price")
            entry_price = position.get("entry_price")
            if not stop_price or not liquidation_price or not entry_price:
                _close_position(
                    account,
                    api_key,
                    api_secret,
                    position,
                    "position_state_unverified",
                )
                _fail_account(account, "position_state_unverified")
                return
            try:
                safety = liquidation_stop_safety(
                    entry_price=entry_price,
                    stop_price=stop_price,
                    liquidation_price=liquidation_price,
                    direction=side,
                    min_buffer_pct=policy.liquidation_buffer_pct,
                )
            except (TypeError, ValueError):
                safety = None
            if safety is None or not safety.safe:
                _close_position(
                    account,
                    api_key,
                    api_secret,
                    position,
                    "liquidation_buffer_unsafe",
                )
                _fail_account(account, "liquidation_buffer_unsafe")
                return
            max_holding_bars = max(
                0, min(int(config.get("max_holding_bars", 12)), 1_000)
            )
            opened_at = _opened_at_seconds(managed[key])
            if (
                max_holding_bars
                and opened_at is not None
                and time.time() - opened_at
                >= max_holding_bars * execution_timeframe_seconds
            ):
                positions_changed = (
                    _close_position(account, api_key, api_secret, position, "max_holding_bars")
                    or positions_changed
                )
                continue
        direction, _, _, signal_time, signal_evidence = _strategy_signal(account, symbol)
        if (
            signal_time is not None
            and _signal_is_fresh(
                account,
                signal_time,
                policy,
                signal_evidence,
            )
            and direction == -side
        ):
            positions_changed = (
                _close_position(account, api_key, api_secret, position, "strategy_reversal")
                or positions_changed
            )

    if positions_changed:
        snapshot = _account_service.account(api_key, api_secret, force_refresh=True)
    positions = {_position_key(item): item for item in snapshot.positions}
    managed = _managed_positions(account)
    _cancel_orphan_protections(account, api_key, api_secret, managed, set(positions))
    position_count = len(positions)
    protection_counts = _protection_counts(account, managed)
    review_warnings = _risk_review_warnings(positions, managed, protection_counts)
    stop_prices = _current_stop_prices(account, managed)
    open_risk = _current_open_risk(
        snapshot,
        managed,
        stop_prices,
        exit_cost_bps=policy.round_trip_cost_bps,
    )
    allow_new_entries = _entry_loss_guard(
        account, snapshot, policy, review_warnings=review_warnings
    )
    allow_new_entries = allow_new_entries and not account.get("_local_audit_pending", False)
    symbol_set = set(symbols)
    prices = {
        row["symbol"]: row
        for row in store.query("SELECT symbol,price,ts FROM ticker WHERE price IS NOT NULL")
        if row["symbol"] in symbol_set
    }
    occupied_symbols = {key[0] for key in positions}
    if allow_new_entries and position_count < max_positions:
        for symbol in symbols:
            if symbol in occupied_symbols:
                continue
            ticker = prices.get(symbol)
            if ticker is None:
                continue
            try:
                price = float(ticker["price"])
                ticker_fresh = market_data_freshness(
                    ticker["ts"],
                    max_age_seconds=policy.max_ticker_age_seconds,
                ).fresh
            except (KeyError, TypeError, ValueError):
                continue
            if not ticker_fresh or not math.isfinite(price) or price <= 0:
                continue
            admission = symbol_admission(
                symbol,
                sorted(occupied_symbols),
                policy=policy,
            )
            if not admission.allowed:
                continue
            direction, atr, basis, signal_time, signal_evidence = _strategy_signal(account, symbol)
            if (
                direction not in {-1, 1}
                or signal_time is None
                or not _signal_is_fresh(
                    account,
                    signal_time,
                    policy,
                    signal_evidence,
                )
            ):
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
                current_open_risk=open_risk,
                basis=basis,
                signal_evidence=signal_evidence,
            )
            break
    store.execute(
        """UPDATE live_trading_accounts
           SET last_tick_at=CURRENT_TIMESTAMP,last_error_code=?,updated_at=CURRENT_TIMESTAMP
           WHERE id=? AND user_id=? AND status='active'""",
        (
            (
                "risk_review_required"
                if review_warnings
                else "filled_audit_pending"
                if account.get("_local_audit_pending")
                else None
            ),
            account["id"],
            account["user_id"],
        ),
    )


def _recover_account(account: dict[str, Any]) -> None:
    """Resolve uncertain writes and fail-close any recovered naked position."""
    if _account_service is None:
        raise RuntimeError("live engine is not configured")
    api_key, api_secret = _credentials(account)
    snapshot = _account_service.account(api_key, api_secret, force_refresh=True)
    if snapshot.account_type != "UM_FUTURE":
        return
    market_state_changed = _reconcile_intents(account, api_key, api_secret)
    if market_state_changed:
        snapshot = _account_service.account(api_key, api_secret, force_refresh=True)
    positions = {_position_key(item): item for item in snapshot.positions}
    managed = _managed_positions(account)
    _cancel_orphan_protections(account, api_key, api_secret, managed, set(positions))
    protection_counts = _protection_counts(account, managed)
    failed_close_keys = _failed_close_keys(account, managed)
    for key, opened in managed.items():
        symbol, position_side = key
        position = positions.get(key)
        if position is None:
            _cancel_protection(account, api_key, api_secret, symbol, position_side)
            _record_reconciled_close(account, opened)
            continue
        if key in failed_close_keys:
            _close_position(
                account,
                api_key,
                api_secret,
                position,
                "recovery_close_retry",
            )
            _fail_account(account, "recovery_close_retry")
            continue
        if _is_grandfathered_open(opened):
            continue
        if protection_counts.get(key, 0) == 2:
            continue
        # Old open intents do not persist the ATR basis needed to reconstruct
        # exactly the same protection prices. Guessing new levels is unsafe;
        # close the recovered exposure with a risk-reducing market order.
        _close_position(
            account,
            api_key,
            api_secret,
            position,
            "recovery_protection_missing",
        )
        _fail_account(account, "recovery_protection_missing")
    review_warnings = _risk_review_warnings(positions, managed, protection_counts)
    if review_warnings:
        _persist_risk_review(account, review_warnings)


def tick(account_id: int | None = None) -> None:
    if _settings is None or not _settings.binance_live_trading_enabled:
        return
    with store.advisory_lock("quantdesk-binance-live-tick", 0) as acquired:
        if not acquired:
            return
        if account_id is None:
            # Resolve crash/timeout intents even though fail-closed accounts are
            # no longer eligible to evaluate strategy signals.
            for account in _recovery_accounts():
                account_id_value = int(account["id"])
                if _account_backoff_active(account_id_value):
                    continue
                try:
                    _recover_account(account)
                    _clear_account_backoff(account_id_value)
                except SecurityError:
                    continue
                except BinanceAccountClientError as exc:
                    _record_account_backoff(account_id_value, exc.category)
                    continue
                except Exception as exc:
                    print(f"[live] recovery reconciliation failed: {type(exc).__name__}")
        for account in _active_accounts(account_id):
            account_id_value = int(account["id"])
            if _account_backoff_active(account_id_value):
                continue
            try:
                _tick_account(account)
                _clear_account_backoff(account_id_value)
            except SecurityError:
                _fail_account(account, "credential_changed")
            except BinanceAccountClientError as exc:
                _record_account_backoff(account_id_value, exc.category)
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
