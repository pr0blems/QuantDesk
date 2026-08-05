"""Authoritative Binance USD-M TradFi contract and account-rule synchronization."""

from __future__ import annotations

import json
import time
from decimal import Decimal, InvalidOperation
from typing import Any

from quantdesk_v2.config import get_settings
from quantdesk_v2.security import CredentialCipher, SecurityError

from . import binance_client, store

RULE_MAX_AGE_MS = 30 * 60 * 1000
MARK_MAX_AGE_MS = 30 * 1000
SCHEDULE_MAX_AGE_MS = 2 * 60 * 60 * 1000
PRIVATE_MAX_AGE_MS = 6 * 60 * 60 * 1000
PUBLIC_REFRESH_SECONDS = 5


def _number(value: Any, default: str = "0") -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        result = Decimal(default)
    return result if result.is_finite() else Decimal(default)


def _filter(symbol: dict[str, Any], name: str) -> dict[str, Any]:
    for item in symbol.get("filters") or []:
        if isinstance(item, dict) and item.get("filterType") == name:
            return item
    return {}


def sync_exchange_info(now_ms: int | None = None) -> int:
    payload = binance_client.fetch_exchange_info()
    if not isinstance(payload, dict) or not isinstance(payload.get("symbols"), list):
        raise RuntimeError("Binance exchangeInfo response is invalid")
    received = int(now_ms or time.time() * 1000)
    rows: list[tuple[Any, ...]] = []
    for symbol in payload["symbols"]:
        if not isinstance(symbol, dict) or symbol.get("contractType") != "TRADIFI_PERPETUAL":
            continue
        lot = _filter(symbol, "LOT_SIZE")
        market_lot = _filter(symbol, "MARKET_LOT_SIZE") or lot
        price = _filter(symbol, "PRICE_FILTER")
        notional = _filter(symbol, "MIN_NOTIONAL")
        rows.append(
            (
                symbol.get("symbol"),
                symbol.get("contractType"),
                symbol.get("status"),
                symbol.get("quoteAsset"),
                symbol.get("marginAsset"),
                symbol.get("underlyingType"),
                _number(price.get("tickSize")),
                _number(lot.get("stepSize")),
                _number(lot.get("minQty")),
                _number(market_lot.get("stepSize")),
                _number(market_lot.get("minQty")),
                _number(notional.get("notional")),
                _number(symbol.get("liquidationFee")),
                _number(symbol.get("marketTakeBound")),
                _number(symbol.get("triggerProtect")),
                received,
                json.dumps(symbol, ensure_ascii=False),
            )
        )
    store.executemany(
        """INSERT INTO binance_contract_rules(
               symbol,contract_type,status,quote_asset,margin_asset,underlying_type,
               tick_size,lot_step_size,min_qty,market_step_size,market_min_qty,min_notional,
               liquidation_fee_rate,market_take_bound,trigger_protect,rule_updated_at_ms,raw_json
           ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
           ON DUPLICATE KEY UPDATE contract_type=VALUES(contract_type),status=VALUES(status),
               quote_asset=VALUES(quote_asset),margin_asset=VALUES(margin_asset),
               underlying_type=VALUES(underlying_type),tick_size=VALUES(tick_size),
               lot_step_size=VALUES(lot_step_size),min_qty=VALUES(min_qty),
               market_step_size=VALUES(market_step_size),market_min_qty=VALUES(market_min_qty),
               min_notional=VALUES(min_notional),liquidation_fee_rate=VALUES(liquidation_fee_rate),
               market_take_bound=VALUES(market_take_bound),trigger_protect=VALUES(trigger_protect),
               rule_updated_at_ms=VALUES(rule_updated_at_ms),raw_json=VALUES(raw_json),
               updated_at=CURRENT_TIMESTAMP""",
        rows,
    )
    return len(rows)


def sync_mark_prices(now_ms: int | None = None) -> int:
    payload = binance_client.fetch_mark_prices()
    rows = payload if isinstance(payload, list) else [payload]
    received = int(now_ms or time.time() * 1000)
    values = []
    for item in rows:
        if not isinstance(item, dict) or not item.get("symbol"):
            continue
        values.append(
            (
                _number(item.get("markPrice")),
                _number(item.get("indexPrice")),
                _number(item.get("lastFundingRate")),
                int(item.get("nextFundingTime") or 0),
                int(item.get("time") or received),
                item["symbol"],
            )
        )
    store.executemany(
        """UPDATE binance_contract_rules SET mark_price=?,index_price=?,last_funding_rate=?,
               next_funding_time=?,mark_updated_at_ms=?,updated_at=CURRENT_TIMESTAMP
           WHERE symbol=?""",
        values,
    )
    return len(values)


def sync_funding_info() -> int:
    payload = binance_client.fetch_funding_info()
    rows = payload if isinstance(payload, list) else []
    values = []
    for item in rows:
        if not isinstance(item, dict) or not item.get("symbol"):
            continue
        values.append(
            (
                int(item.get("fundingIntervalHours") or 0) or None,
                _number(item.get("adjustedFundingRateCap")),
                _number(item.get("adjustedFundingRateFloor")),
                item["symbol"],
            )
        )
    store.executemany(
        """UPDATE binance_contract_rules SET funding_interval_hours=?,funding_rate_cap=?,
               funding_rate_floor=?,updated_at=CURRENT_TIMESTAMP WHERE symbol=?""",
        values,
    )
    return len(values)


def sync_funding_events(now_ms: int | None = None) -> int:
    received = int(now_ms or time.time() * 1000)
    payload = binance_client.fetch_funding_rates(received - 24 * 60 * 60 * 1000)
    rows = payload if isinstance(payload, list) else []
    values = []
    for item in rows:
        if not isinstance(item, dict) or not item.get("symbol") or not item.get("fundingTime"):
            continue
        values.append(
            (
                item["symbol"],
                int(item["fundingTime"]),
                _number(item.get("fundingRate")),
                _number(item.get("markPrice")) if item.get("markPrice") is not None else None,
                received,
            )
        )
    store.executemany(
        """INSERT INTO binance_funding_events(symbol,funding_time,funding_rate,mark_price,received_at_ms)
           VALUES(?,?,?,?,?) ON DUPLICATE KEY UPDATE funding_rate=VALUES(funding_rate),
               mark_price=VALUES(mark_price),received_at_ms=VALUES(received_at_ms)""",
        values,
    )
    return len(values)


def sync_trading_schedule(now_ms: int | None = None) -> None:
    payload = binance_client.fetch_trading_schedule()
    if not isinstance(payload, dict) or not isinstance(payload.get("marketSchedules"), dict):
        raise RuntimeError("Binance tradingSchedule response is invalid")
    store.system_state_set(
        "binance:trading_schedule",
        {"fetched_at_ms": int(now_ms or time.time() * 1000), "payload": payload},
    )


def contract_rule(symbol: str) -> dict[str, Any] | None:
    rows = store.query("SELECT * FROM binance_contract_rules WHERE symbol=?", (symbol.upper(),))
    return dict(rows[0]) if rows else None


def mark_prices(*, fresh_only: bool = True) -> dict[str, float]:
    minimum = int(time.time() * 1000) - MARK_MAX_AGE_MS if fresh_only else 0
    rows = store.query(
        "SELECT symbol,mark_price FROM binance_contract_rules WHERE mark_updated_at_ms>=? AND mark_price>0",
        (minimum,),
    )
    return {str(row["symbol"]): float(row["mark_price"]) for row in rows}


def trading_session_open(rule: dict[str, Any], now_ms: int | None = None) -> bool:
    state = store.system_state_get("binance:trading_schedule", {})
    fetched = int(state.get("fetched_at_ms") or 0) if isinstance(state, dict) else 0
    current = int(now_ms or time.time() * 1000)
    if current - fetched > SCHEDULE_MAX_AGE_MS:
        return False
    payload = state.get("payload") if isinstance(state, dict) else None
    schedules = payload.get("marketSchedules") if isinstance(payload, dict) else None
    market_type = str(rule.get("underlying_type") or "").upper()
    if isinstance(schedules, dict) and market_type not in schedules:
        if "HK" in market_type:
            market_type = "HK_EQUITY"
        elif "KR" in market_type or "KOREA" in market_type:
            market_type = "KR_EQUITY"
        elif "COMMOD" in market_type:
            market_type = "COMMODITY"
        else:
            market_type = "EQUITY"
    schedule = schedules.get(market_type) if isinstance(schedules, dict) else None
    sessions = schedule.get("sessions") if isinstance(schedule, dict) else None
    if not isinstance(sessions, list):
        return False
    for session in sessions:
        if not isinstance(session, dict):
            continue
        if int(session.get("startTime") or 0) <= current < int(session.get("endTime") or 0):
            return session.get("type") != "NO_TRADING"
    return False


def _credentials(user_id: int) -> tuple[str, str, int] | None:
    rows = store.query(
        """SELECT binance_api_key_encrypted,binance_api_secret_encrypted,binance_key_version
           FROM users WHERE id=? AND is_active=1""",
        (user_id,),
    )
    if not rows or not rows[0].get("binance_api_key_encrypted") or not rows[0].get("binance_api_secret_encrypted"):
        return None
    try:
        cipher = CredentialCipher(get_settings().credential_master_key.get_secret_value())
        return (
            cipher.decrypt(str(rows[0]["binance_api_key_encrypted"])),
            cipher.decrypt(str(rows[0]["binance_api_secret_encrypted"])),
            int(rows[0]["binance_key_version"]),
        )
    except (SecurityError, TypeError, ValueError):
        return None


def _sync_brackets(user_id: int, key: str, secret: str, version: int, now_ms: int) -> None:
    payload = binance_client.fetch_leverage_brackets(key, secret)
    rows: list[tuple[Any, ...]] = []
    for item in payload:
        symbol = item.get("symbol")
        for bracket in item.get("brackets") or []:
            if not symbol or not isinstance(bracket, dict):
                continue
            rows.append(
                (
                    user_id,
                    symbol,
                    int(bracket.get("bracket") or 0),
                    int(bracket.get("initialLeverage") or 0),
                    _number(bracket.get("notionalFloor")),
                    _number(bracket.get("notionalCap")),
                    _number(bracket.get("maintMarginRatio")),
                    _number(bracket.get("cum")),
                    version,
                    now_ms,
                )
            )
    if rows:
        store.execute("DELETE FROM binance_user_leverage_brackets WHERE user_id=?", (user_id,))
        store.executemany(
            """INSERT INTO binance_user_leverage_brackets(
                   user_id,symbol,bracket,initial_leverage,notional_floor,notional_cap,
                   maint_margin_ratio,cum,credential_version,synced_at_ms
               ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
            rows,
        )


def ensure_user_profile(user_id: int, symbol: str) -> tuple[dict[str, Any] | None, str | None]:
    """Return exact user fee and bracket data, synchronizing it on cache miss."""

    now_ms = int(time.time() * 1000)
    symbol = symbol.upper()
    credentials = _credentials(user_id)
    if credentials is None:
        return None, "binance_credentials_required"
    key, secret, version = credentials
    commission_rows = store.query(
        """SELECT * FROM binance_user_commission_rates
           WHERE user_id=? AND symbol=? AND credential_version=? AND synced_at_ms>=?""",
        (user_id, symbol, version, now_ms - PRIVATE_MAX_AGE_MS),
    )
    bracket_rows = store.query(
        """SELECT * FROM binance_user_leverage_brackets
           WHERE user_id=? AND symbol=? AND credential_version=? AND synced_at_ms>=?
           ORDER BY bracket""",
        (user_id, symbol, version, now_ms - PRIVATE_MAX_AGE_MS),
    )
    try:
        if not bracket_rows:
            _sync_brackets(user_id, key, secret, version, now_ms)
        if not commission_rows:
            item = binance_client.fetch_commission_rate(symbol, key, secret)
            store.execute(
                """INSERT INTO binance_user_commission_rates(
                       user_id,symbol,maker_rate,taker_rate,rpi_rate,credential_version,synced_at_ms
                   ) VALUES(?,?,?,?,?,?,?) ON DUPLICATE KEY UPDATE maker_rate=VALUES(maker_rate),
                       taker_rate=VALUES(taker_rate),rpi_rate=VALUES(rpi_rate),
                       credential_version=VALUES(credential_version),synced_at_ms=VALUES(synced_at_ms)""",
                (
                    user_id,
                    symbol,
                    _number(item.get("makerCommissionRate")),
                    _number(item.get("takerCommissionRate")),
                    _number(item.get("rpiCommissionRate")) if item.get("rpiCommissionRate") is not None else None,
                    version,
                    now_ms,
                ),
            )
    except Exception as exc:
        print(f"[exchange-sync] private profile unavailable: {type(exc).__name__}")
        return None, "binance_private_sync_failed"
    commission_rows = store.query(
        "SELECT * FROM binance_user_commission_rates WHERE user_id=? AND symbol=?",
        (user_id, symbol),
    )
    bracket_rows = store.query(
        """SELECT * FROM binance_user_leverage_brackets WHERE user_id=? AND symbol=?
           ORDER BY bracket""",
        (user_id, symbol),
    )
    if not commission_rows or not bracket_rows:
        return None, "binance_profile_incomplete"
    return {"commission": dict(commission_rows[0]), "brackets": [dict(row) for row in bracket_rows]}, None


def execution_readiness(user_id: int, symbol: str) -> tuple[dict[str, Any] | None, str | None]:
    now_ms = int(time.time() * 1000)
    rule = contract_rule(symbol)
    if rule is None or now_ms - int(rule.get("rule_updated_at_ms") or 0) > RULE_MAX_AGE_MS:
        return None, "binance_contract_rules_stale"
    if rule.get("status") != "TRADING":
        return None, "binance_symbol_not_trading"
    if now_ms - int(rule.get("mark_updated_at_ms") or 0) > MARK_MAX_AGE_MS:
        return None, "binance_mark_price_stale"
    if not trading_session_open(rule, now_ms):
        return None, "binance_session_closed_or_stale"
    profile, reason = ensure_user_profile(user_id, symbol)
    if profile is None:
        return None, reason
    return {"rule": rule, **profile}, None


def public_sync_loop(stop_event=None) -> None:
    print("[exchange-sync] Binance environment synchronization started")
    last_rules = last_funding = last_schedule = 0.0
    while stop_event is None or not stop_event.is_set():
        started = time.monotonic()
        try:
            if started - last_rules >= 15 * 60:
                sync_exchange_info()
                sync_funding_info()
                last_rules = started
            if started - last_schedule >= 15 * 60:
                sync_trading_schedule()
                last_schedule = started
            sync_mark_prices()
            if started - last_funding >= 60:
                sync_funding_events()
                last_funding = started
            store.collector_report("binance_environment", success=True)
        except Exception as exc:
            print(f"[exchange-sync] public sync error: {type(exc).__name__}: {exc}")
            store.collector_report("binance_environment", success=False, error=str(exc))
        elapsed = time.monotonic() - started
        wait = max(PUBLIC_REFRESH_SECONDS - elapsed, 0.25)
        if stop_event is not None:
            if stop_event.wait(wait):
                break
        else:
            time.sleep(wait)
