"""MySQL storage layer for market data, signals, news, and paper trading."""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Generator, Iterable, Mapping, Sequence
from contextlib import contextmanager
from typing import Any

from sqlalchemy.engine import Connection, Engine

_lock = threading.Lock()
_engine: Engine | None = None


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
    """Use the application's shared MySQL engine for all legacy modules."""
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


def upsert_klines(symbol: str, timeframe: str, rows: Iterable[Sequence[Any]]) -> None:
    executemany(
        "REPLACE INTO klines(symbol,tf,open_time,open,high,low,close,volume) "
        "VALUES(?,?,?,?,?,?,?,?)",
        [
            (symbol, timeframe, row[0], row[1], row[2], row[3], row[4], row[5])
            for row in rows
        ],
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
