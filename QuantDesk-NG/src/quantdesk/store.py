"""MySQL storage layer for market data, signals, news, and paper trading."""

from __future__ import annotations

import json
import re
import threading
import time
from collections.abc import Generator, Iterable, Mapping, Sequence
from contextlib import contextmanager
from typing import Any

from sqlalchemy.engine import Connection, Engine

_lock = threading.Lock()
_engine: Engine | None = None
_admin_cache_lock = threading.Lock()
_admin_cache: dict[str, Any] = {"expires": 0.0, "alert_rules": {}}
_dynamic_monitor_cache_lock = threading.Lock()
_dynamic_monitor_cache: dict[str, Any] = {"expires": 0.0, "symbols": ()}
_MONITOR_SYMBOL_PATTERN = re.compile(r"^[A-Z0-9]{3,32}$")
MAX_DYNAMIC_MONITOR_SYMBOLS = 250


class Transaction:
    """Small placeholder-aware wrapper around one SQLAlchemy transaction."""

    def __init__(self, connection: Connection):
        self.connection = connection

    def execute(self, sql: str, params: Sequence[Any] = ()) -> int:
        statement, values = _driver_sql(sql, params)
        return self.connection.exec_driver_sql(statement, values).rowcount

    def query(self, sql: str, params: Sequence[Any] = ()) -> list[Mapping[str, Any]]:
        statement, values = _driver_sql(sql, params)
        result = self.connection.exec_driver_sql(statement, values)
        return list(result.mappings().all())


def configure_engine(engine: Engine) -> None:
    """Use the application's shared MySQL engine for market worker modules."""
    if engine.dialect.name not in {"mysql", "mariadb"}:
        raise RuntimeError("QuantDesk storage requires MySQL")
    global _engine
    _engine = engine


def get_engine() -> Engine:
    global _engine
    if _engine is None:
        from quantdesk_v2.database import engine as application_engine

        configure_engine(application_engine)
    if _engine is None:
        raise RuntimeError("MySQL engine is not configured")
    return _engine


def _driver_sql(sql: str, params: Sequence[Any]) -> tuple[str, tuple[Any, ...]]:
    if sql.count("?") != len(params):
        raise ValueError("SQL placeholder count does not match parameter count")
    return sql.replace("?", "%s"), tuple(params)


def execute(sql: str, params: Sequence[Any] = ()) -> int:
    statement, values = _driver_sql(sql, params)
    with _lock, get_engine().begin() as connection:
        result = connection.exec_driver_sql(statement, values)
        return result.rowcount


def executemany(sql: str, seq: Iterable[Sequence[Any]]) -> int:
    rows = [tuple(row) for row in seq]
    if not rows:
        return 0
    statement, _ = _driver_sql(sql, rows[0])
    if any(len(row) != len(rows[0]) for row in rows):
        raise ValueError("bulk SQL parameter rows must have equal lengths")
    with _lock, get_engine().begin() as connection:
        result = connection.exec_driver_sql(statement, rows)
        return result.rowcount


def query(sql: str, params: Sequence[Any] = ()) -> list[Mapping[str, Any]]:
    statement, values = _driver_sql(sql, params)
    with get_engine().connect() as connection:
        result = connection.exec_driver_sql(statement, values)
        return list(result.mappings().all())


@contextmanager
def transaction() -> Generator[Transaction, None, None]:
    """Run related writes atomically on one connection."""

    with _lock, get_engine().begin() as connection:
        yield Transaction(connection)


@contextmanager
def advisory_lock(name: str, timeout_seconds: int = 0) -> Generator[bool, None, None]:
    """Hold a MySQL named lock for the lifetime of the yielded context."""

    if not name or len(name) > 64:
        raise ValueError("advisory lock name must contain 1 to 64 characters")
    connection = get_engine().connect()
    acquired = False
    try:
        value = connection.exec_driver_sql(
            "SELECT GET_LOCK(%s, %s)", (name, int(timeout_seconds))
        ).scalar_one()
        acquired = value == 1
        yield acquired
    finally:
        if acquired:
            connection.exec_driver_sql("SELECT RELEASE_LOCK(%s)", (name,))
        connection.close()


def system_state_get(key: str, default: Any = None) -> Any:
    rows = query("SELECT v FROM system_state WHERE k=?", (key,))
    return json.loads(rows[0]["v"]) if rows else default


def system_state_set(key: str, value: Any) -> None:
    execute(
        "REPLACE INTO system_state(k,v) VALUES(?,?)",
        (key, json.dumps(value, ensure_ascii=False)),
    )


def user_state_get(user_id: int, key: str, default: Any = None) -> Any:
    rows = query("SELECT v FROM user_states WHERE user_id=? AND k=?", (user_id, key))
    return json.loads(rows[0]["v"]) if rows else default


def user_state_set(user_id: int, key: str, value: Any) -> None:
    execute(
        "REPLACE INTO user_states(user_id,k,v,updated_at) VALUES(?,?,?,CURRENT_TIMESTAMP)",
        (user_id, key, json.dumps(value, ensure_ascii=False)),
    )


def admin_alert_rules() -> dict[str, Any]:
    """Return the published alert rules with a short worker-side cache."""

    now = time.monotonic()
    with _admin_cache_lock:
        if now < float(_admin_cache["expires"]):
            return dict(_admin_cache["alert_rules"])
    rules: dict[str, Any] = {}
    try:
        rows = query("SELECT value_json FROM admin_settings WHERE `key`='alert_rules'")
        if rows:
            value = rows[0]["value_json"]
            if isinstance(value, str):
                value = json.loads(value)
            if isinstance(value, dict):
                rules = value
    except Exception:
        rules = {}
    with _admin_cache_lock:
        _admin_cache["alert_rules"] = dict(rules)
        _admin_cache["expires"] = now + 10.0
    return rules


def dynamic_monitor_symbols() -> list[str]:
    """Return Binance account-derived watchlist symbols with a short worker cache.

    The static configuration remains the baseline.  This supplemental list lets
    a user's active Binance position or open order receive monitoring even when
    it is not one of the preconfigured TradFi contracts.
    """

    now = time.monotonic()
    with _dynamic_monitor_cache_lock:
        if now < float(_dynamic_monitor_cache["expires"]):
            return list(_dynamic_monitor_cache["symbols"])

    symbols: list[str] = []
    try:
        rows = query(
            "SELECT monitor_watchlist FROM users "
            "WHERE is_active=1 AND monitor_watchlist IS NOT NULL"
        )
        for row in rows:
            value = row.get("monitor_watchlist")
            if isinstance(value, str):
                try:
                    value = json.loads(value)
                except json.JSONDecodeError:
                    continue
            if not isinstance(value, list):
                continue
            for raw_symbol in value:
                if not isinstance(raw_symbol, str):
                    continue
                symbol = raw_symbol.strip().upper()
                if (
                    not _MONITOR_SYMBOL_PATTERN.fullmatch(symbol)
                    or symbol in symbols
                ):
                    continue
                symbols.append(symbol)
                if len(symbols) >= MAX_DYNAMIC_MONITOR_SYMBOLS:
                    break
            if len(symbols) >= MAX_DYNAMIC_MONITOR_SYMBOLS:
                break
    except Exception:
        symbols = []

    with _dynamic_monitor_cache_lock:
        _dynamic_monitor_cache["symbols"] = tuple(symbols)
        _dynamic_monitor_cache["expires"] = now + 10.0
    return symbols


def collector_paused(name: str) -> bool:
    try:
        rows = query(
            "SELECT value_json FROM admin_settings WHERE `key`=?", (f"collector_pause:{name}",)
        )
        if not rows:
            return False
        value = rows[0]["value_json"]
        if isinstance(value, str):
            value = json.loads(value)
        return bool(value.get("paused")) if isinstance(value, dict) else False
    except Exception:
        return False


def collector_report(
    name: str,
    *,
    success: bool,
    items: int = 0,
    error: str | None = None,
    details: Mapping[str, Any] | None = None,
) -> None:
    """Best-effort collector heartbeat; monitoring must never stop collection."""

    now = int(time.time())
    success_at = now if success else None
    error_at = now if error else None
    try:
        execute(
            "INSERT INTO collector_status(name,heartbeat_at,last_success_at,last_error_at,last_error,cycles,items,details_json) "
            "VALUES(?,?,?,?,?,1,?,?) ON DUPLICATE KEY UPDATE "
            "heartbeat_at=VALUES(heartbeat_at),last_success_at=COALESCE(VALUES(last_success_at),last_success_at),"
            "last_error_at=COALESCE(VALUES(last_error_at),last_error_at),"
            "last_error=CASE WHEN VALUES(last_success_at) IS NOT NULL THEN NULL ELSE VALUES(last_error) END,"
            "cycles=cycles+1,items=items+VALUES(items),details_json=VALUES(details_json)",
            (
                name,
                now,
                success_at,
                error_at,
                (error or "")[:1000] or None,
                max(0, int(items)),
                json.dumps(dict(details or {}), ensure_ascii=False),
            ),
        )
    except Exception:
        return


def admin_news_sources(default_sources: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Load enabled database-managed news sources, falling back before migration."""

    try:
        rows = query(
            "SELECT name,url,lang,slow,weight,hourly_limit,enabled FROM news_source_settings "
            "ORDER BY weight DESC,name"
        )
    except Exception:
        return default_sources
    if not rows:
        return default_sources
    return [
        {
            **{key: value for key, value in dict(row).items() if key != "enabled"},
            "_admin_managed": True,
        }
        for row in rows
        if bool(row["enabled"])
    ]


def news_source_result(
    name: str,
    *,
    success: bool,
    fetched: int = 0,
    inserted: int = 0,
    error: str | None = None,
) -> None:
    now = int(time.time())
    try:
        if success:
            execute(
                "UPDATE news_source_settings SET last_success_at=?,last_error=NULL,"
                "fetched_items=fetched_items+?,inserted_items=inserted_items+? WHERE name=?",
                (now, max(0, fetched), max(0, inserted), name),
            )
        else:
            execute(
                "UPDATE news_source_settings SET last_error_at=?,last_error=? WHERE name=?",
                (now, (error or "unknown error")[:1000], name),
            )
    except Exception:
        return


def upsert_klines(symbol: str, timeframe: str, rows: Iterable[Sequence[Any]]) -> None:
    executemany(
        "REPLACE INTO klines(symbol,tf,open_time,open,high,low,close,volume) "
        "VALUES(?,?,?,?,?,?,?,?)",
        [(symbol, timeframe, row[0], row[1], row[2], row[3], row[4], row[5]) for row in rows],
    )


def get_klines(symbol: str, timeframe: str, limit: int = 300) -> list[dict[str, Any]]:
    rows = query(
        "SELECT open_time,open,high,low,close,volume FROM klines "
        "WHERE symbol=? AND tf=? ORDER BY open_time DESC LIMIT ?",
        (symbol, timeframe, limit),
    )
    return [dict(row) for row in reversed(rows)]


def latest_closed_time(symbol: str, timeframe: str) -> int:
    rows = query(
        "SELECT MAX(open_time) AS m FROM klines WHERE symbol=? AND tf=?",
        (symbol, timeframe),
    )
    return int(rows[0]["m"]) if rows and rows[0]["m"] else 0


def add_alert(
    symbol: str,
    kind: str,
    direction: str,
    score: float | None,
    message: str,
    detail: Any = None,
    user_id: int | None = None,
) -> None:
    values = (
        int(time.time()),
        symbol,
        kind,
        direction,
        score,
        message,
        json.dumps(detail, ensure_ascii=False) if detail else None,
    )
    if user_id is not None:
        execute(
            "INSERT INTO alerts(user_id,ts,symbol,kind,direction,score,message,detail) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (user_id, *values),
        )
        return
    execute(
        "INSERT INTO alerts(user_id,ts,symbol,kind,direction,score,message,detail) "
        "SELECT id,?,?,?,?,?,?,? FROM users WHERE is_active=1",
        values,
    )
