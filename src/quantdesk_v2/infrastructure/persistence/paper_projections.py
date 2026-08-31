"""MySQL projection adapter for durable simulated fills."""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from typing import Any, Protocol

from ...application.paper_reconciliation import PaperProjectionOutcome


class PaperProjectionError(RuntimeError):
    """A durable paper fill cannot be projected safely."""


class _Transaction(Protocol):
    def execute(self, sql: str, params=()) -> int: ...

    def query(self, sql: str, params=()) -> list[Mapping[str, Any]]: ...


class _Backend(Protocol):
    def query(self, sql: str, params=()) -> list[Mapping[str, Any]]: ...

    def execute(self, sql: str, params=()) -> int: ...

    def transaction(self): ...


class MySqlPaperProjectionStore:
    """Atomically project paper fill facts into compatibility read models."""

    __slots__ = ("_backend",)

    def __init__(self, backend: _Backend) -> None:
        self._backend = backend

    def pending_execution_ids(
        self, *, user_id: int, paper_account_id: int, limit: int
    ) -> tuple[int, ...]:
        rows = self._backend.query(
            """SELECT id FROM paper_order_executions
               WHERE user_id=? AND paper_account_id=?
                 AND status='FILLED' AND projection_status IN ('pending','failed')
               ORDER BY id LIMIT ?""",
            (user_id, paper_account_id, limit),
        )
        return tuple(int(row["id"]) for row in rows)

    def project_execution(
        self, *, user_id: int, paper_account_id: int, execution_id: int
    ) -> PaperProjectionOutcome:
        with self._backend.transaction() as transaction:
            rows = transaction.query(
                """SELECT * FROM paper_order_executions
                   WHERE id=? AND user_id=? AND paper_account_id=? FOR UPDATE""",
                (execution_id, user_id, paper_account_id),
            )
            if not rows:
                raise PaperProjectionError("paper execution is unavailable in tenant scope")
            execution = dict(rows[0])
            if execution.get("status") != "FILLED":
                raise PaperProjectionError("only filled paper executions can be projected")
            if execution.get("projection_status") == "applied":
                return PaperProjectionOutcome(execution_id, "already_applied")
            payload = _projection_payload(execution)
            action = str(execution.get("action") or "")
            if action != payload.get("action"):
                raise PaperProjectionError("paper execution action differs from projection")
            if action == "open":
                self._project_open(transaction, execution, payload)
            elif action == "close":
                self._project_close(transaction, execution, payload)
            else:
                raise PaperProjectionError("paper execution action is invalid")
            updated = transaction.execute(
                """UPDATE paper_order_executions
                   SET projection_status='applied',projection_error=NULL,
                       projection_attempts=projection_attempts+1,
                       projected_at=UTC_TIMESTAMP(6),updated_at=UTC_TIMESTAMP(6)
                   WHERE id=? AND user_id=? AND paper_account_id=?
                     AND projection_status IN ('pending','failed')""",
                (execution_id, user_id, paper_account_id),
            )
            if updated != 1:
                raise PaperProjectionError("paper projection completion checkpoint was lost")
        return PaperProjectionOutcome(execution_id, "applied")

    def record_failure(
        self,
        *,
        user_id: int,
        paper_account_id: int,
        execution_id: int,
        error: str,
    ) -> None:
        self._backend.execute(
            """UPDATE paper_order_executions
               SET projection_status='failed',projection_error=?,
                   projection_attempts=projection_attempts+1,
                   updated_at=UTC_TIMESTAMP(6)
               WHERE id=? AND user_id=? AND paper_account_id=?
                 AND projection_status<>'applied'""",
            (str(error)[:2_000], execution_id, user_id, paper_account_id),
        )

    def audit_account(
        self, *, user_id: int, paper_account_id: int
    ) -> tuple[int, tuple[str, ...]]:
        counts = self._backend.query(
            """SELECT COUNT(*) AS pending_count FROM paper_order_executions
               WHERE user_id=? AND paper_account_id=? AND status='FILLED'
                 AND projection_status IN ('pending','failed')""",
            (user_id, paper_account_id),
        )
        remaining = int(counts[0]["pending_count"]) if counts else 0
        drift_codes: list[str] = []
        if self._backend.query(
            """SELECT p.id FROM paper_positions p
               LEFT JOIN paper_order_executions e
                 ON e.id=p.source_execution_id AND e.user_id=p.user_id
                AND e.paper_account_id=p.paper_account_id
               WHERE p.user_id=? AND p.paper_account_id=?
                 AND p.source_execution_id IS NOT NULL
                 AND (e.id IS NULL OR e.action<>'open' OR e.projection_status<>'applied')
               LIMIT 1""",
            (user_id, paper_account_id),
        ):
            drift_codes.append("paper_position_source_drift")
        if self._backend.query(
            """SELECT t.id FROM paper_trades t
               LEFT JOIN paper_order_executions e
                 ON e.id=t.source_execution_id AND e.user_id=t.user_id
                AND e.paper_account_id=t.paper_account_id
               WHERE t.user_id=? AND t.paper_account_id=?
                 AND t.source_execution_id IS NOT NULL
                 AND (e.id IS NULL OR e.action<>'close' OR e.projection_status<>'applied')
               LIMIT 1""",
            (user_id, paper_account_id),
        ):
            drift_codes.append("paper_trade_source_drift")
        if self._backend.query(
            """SELECT e.id FROM paper_order_executions e
               LEFT JOIN paper_trades t
                 ON t.source_execution_id=e.id AND t.user_id=e.user_id
                AND t.paper_account_id=e.paper_account_id
               WHERE e.user_id=? AND e.paper_account_id=? AND e.status='FILLED'
                 AND e.action='close' AND e.projection_status='applied'
                 AND e.projection_json IS NOT NULL AND t.id IS NULL
               LIMIT 1""",
            (user_id, paper_account_id),
        ):
            drift_codes.append("paper_close_projection_missing")
        return remaining, tuple(drift_codes)

    @staticmethod
    def _project_open(
        transaction: _Transaction,
        execution: dict[str, Any],
        payload: dict[str, Any],
    ) -> None:
        execution_id = int(execution["id"])
        existing = transaction.query(
            "SELECT id FROM paper_positions WHERE source_execution_id=? LIMIT 1",
            (execution_id,),
        )
        if existing:
            return
        position = _object(payload.get("position"), "position")
        account_rows = transaction.query(
            """SELECT balance FROM paper_accounts
               WHERE id=? AND user_id=? FOR UPDATE""",
            (execution["paper_account_id"], execution["user_id"]),
        )
        if not account_rows:
            raise PaperProjectionError("paper account is unavailable")
        debit = _finite_number(payload.get("balance_debit"), "balance_debit", minimum=0)
        balance = _finite_number(account_rows[0]["balance"], "balance", minimum=0)
        if balance + 1e-8 < debit:
            raise PaperProjectionError("paper account has insufficient margin")
        collision = transaction.query(
            """SELECT id FROM paper_positions
               WHERE paper_account_id=? AND user_id=? AND symbol=? FOR UPDATE""",
            (
                execution["paper_account_id"],
                execution["user_id"],
                execution["symbol"],
            ),
        )
        if collision:
            raise PaperProjectionError("paper symbol already has an open position")
        inserted = transaction.execute(
            """INSERT INTO paper_positions(
               paper_account_id,user_id,symbol,side,qty,avg_entry,margin,leverage,stop,target,
               adds,opened_ts,last_add_ts,open_score,basis,funding_acc,liq_price,funding_ts,
               atr_entry,peak_price,tp_done,source_execution_id
               ) VALUES(?,?,?,?,?,?,?,?,?,?,0,?,?,?,?,0,?,?,?,?,0,?)""",
            (
                execution["paper_account_id"], execution["user_id"], execution["symbol"],
                _direction(position.get("side")),
                _positive(position.get("qty"), "qty"),
                _positive(position.get("avg_entry"), "avg_entry"),
                _positive(position.get("margin"), "margin"),
                _positive_integer(position.get("leverage"), "leverage"),
                _optional_number(position.get("stop"), "stop"),
                _optional_number(position.get("target"), "target"),
                _positive_integer(position.get("opened_ts"), "opened_ts"),
                _optional_integer(position.get("last_add_ts"), "last_add_ts"),
                _optional_integer(position.get("open_score"), "open_score"),
                json.dumps(_object(position.get("basis"), "basis"), ensure_ascii=False),
                _optional_number(position.get("liq_price"), "liq_price"),
                _positive_integer(position.get("funding_ts"), "funding_ts"),
                _optional_number(position.get("atr_entry"), "atr_entry"),
                _positive(position.get("peak_price"), "peak_price"),
                execution_id,
            ),
        )
        if inserted != 1:
            raise PaperProjectionError("paper position projection was not inserted")
        new_balance = max(round(balance - debit, 8), 0.0)
        updated = transaction.execute(
            """UPDATE paper_accounts SET balance=?,updated_at=CURRENT_TIMESTAMP
               WHERE id=? AND user_id=?""",
            (new_balance, execution["paper_account_id"], execution["user_id"]),
        )
        if updated != 1:
            raise PaperProjectionError("paper balance projection was not updated")

    @staticmethod
    def _project_close(
        transaction: _Transaction,
        execution: dict[str, Any],
        payload: dict[str, Any],
    ) -> None:
        execution_id = int(execution["id"])
        existing = transaction.query(
            "SELECT id FROM paper_trades WHERE source_execution_id=? LIMIT 1",
            (execution_id,),
        )
        if existing:
            return
        trade = _object(payload.get("trade"), "trade")
        position_id = _positive_integer(payload.get("position_id"), "position_id")
        ownership = transaction.query(
            """SELECT p.*,a.balance FROM paper_positions p
               JOIN paper_accounts a ON a.id=p.paper_account_id AND a.user_id=p.user_id
               WHERE p.id=? AND p.paper_account_id=? AND p.user_id=? FOR UPDATE""",
            (position_id, execution["paper_account_id"], execution["user_id"]),
        )
        if not ownership:
            raise PaperProjectionError("position to reduce is unavailable")
        current = ownership[0]
        if (
            str(current["symbol"]) != str(execution["symbol"])
            or int(current["side"]) != _direction(trade.get("side"))
            or int(current["opened_ts"]) != _positive_integer(trade.get("opened_ts"), "opened_ts")
        ):
            raise PaperProjectionError("position identity differs from close projection")
        deleted = transaction.execute(
            "DELETE FROM paper_positions WHERE id=? AND paper_account_id=? AND user_id=?",
            (position_id, execution["paper_account_id"], execution["user_id"]),
        )
        if deleted != 1:
            raise PaperProjectionError("paper position ownership changed concurrently")
        inserted = transaction.execute(
            """INSERT INTO paper_trades(
               paper_account_id,user_id,symbol,side,qty,entry_price,exit_price,margin,pnl,
               fee,funding,reason,open_score,opened_ts,closed_ts,entry_basis_json,
               source_execution_id
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                execution["paper_account_id"], execution["user_id"], execution["symbol"],
                _direction(trade.get("side")),
                _positive(trade.get("qty"), "qty"),
                _positive(trade.get("entry_price"), "entry_price"),
                _positive(trade.get("exit_price"), "exit_price"),
                _positive(trade.get("margin"), "margin"),
                _finite_number(trade.get("pnl"), "pnl"),
                _finite_number(trade.get("fee"), "fee", minimum=0),
                _finite_number(trade.get("funding"), "funding"),
                str(trade.get("reason") or "")[:2_000],
                _optional_integer(trade.get("open_score"), "open_score"),
                _positive_integer(trade.get("opened_ts"), "opened_ts"),
                _positive_integer(trade.get("closed_ts"), "closed_ts"),
                json.dumps(_object(trade.get("entry_basis"), "entry_basis"), ensure_ascii=False),
                execution_id,
            ),
        )
        if inserted != 1:
            raise PaperProjectionError("paper trade projection was not inserted")
        returned = _finite_number(payload.get("balance_credit"), "balance_credit", minimum=0)
        balance = _finite_number(current["balance"], "balance", minimum=0)
        updated = transaction.execute(
            """UPDATE paper_accounts SET balance=?,updated_at=CURRENT_TIMESTAMP
               WHERE id=? AND user_id=?""",
            (
                max(round(balance + returned, 8), 0.0),
                execution["paper_account_id"],
                execution["user_id"],
            ),
        )
        if updated != 1:
            raise PaperProjectionError("paper balance projection was not updated")


def _projection_payload(execution: dict[str, Any]) -> dict[str, Any]:
    if execution.get("projection_version") != "paper_projection_v1":
        raise PaperProjectionError("paper projection version is unsupported")
    raw = execution.get("projection_json")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise PaperProjectionError("paper projection JSON is invalid") from exc
    payload = _object(raw, "projection")
    if payload.get("schema_version") != 1:
        raise PaperProjectionError("paper projection schema is unsupported")
    return payload


def _object(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise PaperProjectionError(f"{name} must be an object")
    return value


def _finite_number(value: Any, name: str, minimum: float | None = None) -> float:
    if isinstance(value, bool):
        raise PaperProjectionError(f"{name} must be numeric")
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise PaperProjectionError(f"{name} must be numeric") from exc
    if not math.isfinite(number) or (minimum is not None and number < minimum):
        raise PaperProjectionError(f"{name} is outside its allowed range")
    return number


def _positive(value: Any, name: str) -> float:
    number = _finite_number(value, name)
    if number <= 0:
        raise PaperProjectionError(f"{name} must be positive")
    return number


def _optional_number(value: Any, name: str) -> float | None:
    return None if value is None else _finite_number(value, name)


def _positive_integer(value: Any, name: str) -> int:
    if isinstance(value, bool):
        raise PaperProjectionError(f"{name} must be an integer")
    try:
        integer = int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise PaperProjectionError(f"{name} must be an integer") from exc
    if integer <= 0:
        raise PaperProjectionError(f"{name} must be positive")
    return integer


def _optional_integer(value: Any, name: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise PaperProjectionError(f"{name} must be an integer")
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise PaperProjectionError(f"{name} must be an integer") from exc


def _direction(value: Any) -> int:
    if isinstance(value, bool):
        raise PaperProjectionError("side must be -1 or 1")
    try:
        raw = int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise PaperProjectionError("side must be -1 or 1") from exc
    direction = _positive_integer(abs(raw), "side") * (1 if raw > 0 else -1)
    if direction not in {-1, 1}:
        raise PaperProjectionError("side must be -1 or 1")
    return direction
