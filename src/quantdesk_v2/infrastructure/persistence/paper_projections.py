"""MySQL projection adapter for durable simulated fills."""

from __future__ import annotations

import hashlib
import json
import math
import uuid
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol

from ...application.paper_reconciliation import PaperProjectionOutcome


class PaperProjectionError(RuntimeError):
    """A durable paper fill cannot be projected safely."""


@dataclass(frozen=True, slots=True)
class PaperHistoryAudit:
    """Dry-run result for facts on both sides of the balance checkpoint."""

    user_id: int
    paper_account_id: int
    baseline_execution_id: int | None
    applied_count: int
    checkpointed_count: int
    replayable_count: int
    incomplete_after_checkpoint: tuple[int, ...]
    malformed_execution_ids: tuple[int, ...]
    missing_ledger_execution_ids: tuple[int, ...]
    balance_consistent: bool

    @property
    def rebuild_safe(self) -> bool:
        return (
            not self.incomplete_after_checkpoint
            and not self.malformed_execution_ids
            and self.balance_consistent
        )

    def snapshot(self) -> dict[str, Any]:
        return {**asdict(self), "rebuild_safe": self.rebuild_safe}


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
            self._record_position_snapshot(transaction, execution, payload)
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
    ) -> tuple[int, tuple[str, ...], tuple[str, ...]]:
        counts = self._backend.query(
            """SELECT COUNT(*) AS pending_count FROM paper_order_executions
               WHERE user_id=? AND paper_account_id=? AND status='FILLED'
                 AND projection_status IN ('pending','failed')""",
            (user_id, paper_account_id),
        )
        remaining = int(counts[0]["pending_count"]) if counts else 0
        drift_codes: list[str] = []
        warning_codes: list[str] = []
        balance_drift = self._backend.query(
            """SELECT a.balance,c.expected_balance
               FROM paper_accounts a
               LEFT JOIN paper_account_balance_checkpoints c
                 ON c.paper_account_id=a.id AND c.user_id=a.user_id
               WHERE a.id=? AND a.user_id=?
                 AND (c.paper_account_id IS NULL
                      OR ABS(a.balance-c.expected_balance)>0.00000001)
               LIMIT 1""",
            (paper_account_id, user_id),
        )
        if balance_drift:
            drift_codes.append("paper_balance_drift")
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
        if self._backend.query(
            """SELECT l.id FROM paper_account_ledger_entries l
               LEFT JOIN paper_order_executions e
                 ON e.id=l.source_execution_id AND e.user_id=l.user_id
                AND e.paper_account_id=l.paper_account_id
               WHERE l.user_id=? AND l.paper_account_id=?
                 AND (e.id IS NULL OR e.status<>'FILLED'
                      OR e.projection_status<>'applied')
               LIMIT 1""",
            (user_id, paper_account_id),
        ):
            drift_codes.append("paper_balance_ledger_source_drift")
        if self._backend.query(
            """SELECT a.id FROM paper_accounts a
               LEFT JOIN (
                   SELECT paper_account_id,user_id,MAX(ts) AS latest_ts
                   FROM paper_equity
                   WHERE paper_account_id=? AND user_id=?
                   GROUP BY paper_account_id,user_id
               ) e ON e.paper_account_id=a.id AND e.user_id=a.user_id
               WHERE a.id=? AND a.user_id=? AND a.last_tick_at IS NOT NULL
                 AND (e.latest_ts IS NULL
                      OR e.latest_ts<UNIX_TIMESTAMP(a.last_tick_at)-120)
               LIMIT 1""",
            (paper_account_id, user_id, paper_account_id, user_id),
        ):
            warning_codes.append("paper_equity_projection_stale")
        return remaining, tuple(drift_codes), tuple(warning_codes)

    def audit_applied_history(
        self, *, user_id: int, paper_account_id: int
    ) -> PaperHistoryAudit:
        """Dry-run historical facts without fabricating pre-checkpoint detail."""

        report, _ = self._history_ledger_plan(
            self._backend,
            user_id=user_id,
            paper_account_id=paper_account_id,
            lock=False,
        )
        return report

    def rebuild_missing_history_ledger(
        self, *, user_id: int, paper_account_id: int
    ) -> tuple[PaperHistoryAudit, int]:
        """Restore only derivable post-checkpoint ledger rows in one transaction."""

        with self._backend.transaction() as transaction:
            report, missing = self._history_ledger_plan(
                transaction,
                user_id=user_id,
                paper_account_id=paper_account_id,
                lock=True,
            )
            if not report.rebuild_safe:
                raise PaperProjectionError(
                    "paper history is not safe for controlled ledger reconstruction"
                )
            rebuilt = 0
            for execution_id, entry_type, amount, balance_after in missing:
                inserted = transaction.execute(
                    """INSERT INTO paper_account_ledger_entries(
                           public_id,paper_account_id,user_id,source_execution_id,
                           entry_type,amount,balance_after
                       ) VALUES(?,?,?,?,?,?,?)""",
                    (
                        str(uuid.uuid4()),
                        paper_account_id,
                        user_id,
                        execution_id,
                        entry_type,
                        amount,
                        balance_after,
                    ),
                )
                if inserted != 1:
                    raise PaperProjectionError(
                        "paper history ledger reconstruction lost its insert"
                    )
                rebuilt += 1
        return report, rebuilt

    @staticmethod
    def _history_ledger_plan(
        reader: _Backend | _Transaction,
        *,
        user_id: int,
        paper_account_id: int,
        lock: bool,
    ) -> tuple[PaperHistoryAudit, list[tuple[int, str, float, float]]]:
        checkpoint_sql = (
            """SELECT a.balance,c.baseline_balance,c.baseline_execution_id,
                      c.expected_balance,c.last_execution_id
               FROM paper_accounts a
               JOIN paper_account_balance_checkpoints c
                 ON c.paper_account_id=a.id AND c.user_id=a.user_id
               WHERE a.id=? AND a.user_id=? FOR UPDATE"""
            if lock
            else """SELECT a.balance,c.baseline_balance,c.baseline_execution_id,
                           c.expected_balance,c.last_execution_id
                    FROM paper_accounts a
                    JOIN paper_account_balance_checkpoints c
                      ON c.paper_account_id=a.id AND c.user_id=a.user_id
                    WHERE a.id=? AND a.user_id=?"""
        )
        checkpoint_rows = reader.query(
            checkpoint_sql,
            (paper_account_id, user_id),
        )
        if not checkpoint_rows:
            raise PaperProjectionError("paper balance checkpoint is unavailable")
        checkpoint = checkpoint_rows[0]
        baseline_id = (
            int(checkpoint["baseline_execution_id"])
            if checkpoint.get("baseline_execution_id") is not None
            else None
        )
        baseline_cutoff = baseline_id or 0
        facts_sql = (
            """SELECT id,action,projection_version,projection_json
               FROM paper_order_executions
               WHERE user_id=? AND paper_account_id=? AND status='FILLED'
                 AND projection_status='applied'
               ORDER BY id FOR UPDATE"""
            if lock
            else """SELECT id,action,projection_version,projection_json
                    FROM paper_order_executions
                    WHERE user_id=? AND paper_account_id=? AND status='FILLED'
                      AND projection_status='applied'
                    ORDER BY id"""
        )
        facts = reader.query(
            facts_sql,
            (user_id, paper_account_id),
        )
        ledger_sql = (
            """SELECT source_execution_id,entry_type,amount,balance_after
               FROM paper_account_ledger_entries
               WHERE user_id=? AND paper_account_id=?
                 AND source_execution_id>?
               ORDER BY source_execution_id FOR UPDATE"""
            if lock
            else """SELECT source_execution_id,entry_type,amount,balance_after
                    FROM paper_account_ledger_entries
                    WHERE user_id=? AND paper_account_id=?
                      AND source_execution_id>?
                    ORDER BY source_execution_id"""
        )
        ledger_rows = reader.query(
            ledger_sql,
            (user_id, paper_account_id, baseline_cutoff),
        )
        ledgers = {int(row["source_execution_id"]): row for row in ledger_rows}
        balance = _money(checkpoint["baseline_balance"], "baseline_balance")
        malformed: list[int] = []
        incomplete: list[int] = []
        missing: list[tuple[int, str, float, float]] = []
        checkpointed_count = 0
        replayable_count = 0
        last_post_checkpoint_id = baseline_cutoff
        for raw_fact in facts:
            fact = dict(raw_fact)
            execution_id = int(fact["id"])
            if execution_id <= baseline_cutoff:
                checkpointed_count += 1
                continue
            last_post_checkpoint_id = execution_id
            if fact.get("projection_json") is None:
                incomplete.append(execution_id)
                continue
            try:
                payload = _projection_payload(fact)
                action = str(fact.get("action") or "")
                if action == "open":
                    amount = -_money(payload.get("balance_debit"), "balance_debit")
                    entry_type = "open_debit"
                elif action == "close":
                    amount = _money(payload.get("balance_credit"), "balance_credit")
                    entry_type = "close_credit"
                else:
                    raise PaperProjectionError("paper execution action is invalid")
                balance = _round_money(balance + amount)
                if balance < 0:
                    raise PaperProjectionError("paper history balance became negative")
                ledger = ledgers.get(execution_id)
                if ledger is None:
                    missing.append(
                        (execution_id, entry_type, float(amount), float(balance))
                    )
                elif (
                    str(ledger.get("entry_type")) != entry_type
                    or abs(_money(ledger.get("amount"), "amount") - amount)
                    > Decimal("0.00000001")
                    or abs(
                        _money(ledger.get("balance_after"), "balance_after")
                        - balance
                    )
                    > Decimal("0.00000001")
                ):
                    raise PaperProjectionError("paper history ledger differs from fact")
                replayable_count += 1
            except PaperProjectionError:
                malformed.append(execution_id)
        actual = _money(checkpoint["balance"], "balance")
        expected = _money(checkpoint["expected_balance"], "expected_balance")
        checkpoint_last = int(checkpoint.get("last_execution_id") or baseline_cutoff)
        balance_consistent = (
            abs(actual - expected) <= Decimal("0.00000001")
            and abs(balance - expected) <= Decimal("0.00000001")
            and checkpoint_last == last_post_checkpoint_id
        )
        report = PaperHistoryAudit(
            user_id=user_id,
            paper_account_id=paper_account_id,
            baseline_execution_id=baseline_id,
            applied_count=len(facts),
            checkpointed_count=checkpointed_count,
            replayable_count=replayable_count,
            incomplete_after_checkpoint=tuple(incomplete),
            malformed_execution_ids=tuple(malformed),
            missing_ledger_execution_ids=tuple(item[0] for item in missing),
            balance_consistent=balance_consistent,
        )
        return report, missing

    def record_reconciliation(
        self,
        *,
        user_id: int,
        paper_account_id: int,
        pending_count: int,
        drift_codes: tuple[str, ...],
        warning_codes: tuple[str, ...],
        errors: tuple[str, ...],
        ready: bool,
    ) -> None:
        status = "healthy" if ready and not warning_codes else "warning" if ready else "blocked"
        details = json.dumps(
            {
                "drift_codes": list(drift_codes),
                "warning_codes": list(warning_codes),
                "errors": list(errors),
            },
            ensure_ascii=False,
        )
        self._backend.execute(
            """INSERT INTO paper_account_reconciliation_status(
                   paper_account_id,user_id,status,pending_count,drift_count,warning_count,
                   details_json,last_success_at,last_error_at,checked_at,updated_at
               ) VALUES(?,?,?,?,?,?,?,
                        IF(?=1,UTC_TIMESTAMP(6),NULL),
                        IF(?=1,NULL,UTC_TIMESTAMP(6)),UTC_TIMESTAMP(6),UTC_TIMESTAMP(6))
               ON DUPLICATE KEY UPDATE
                   status=VALUES(status),pending_count=VALUES(pending_count),
                   drift_count=VALUES(drift_count),warning_count=VALUES(warning_count),
                   details_json=VALUES(details_json),
                   last_success_at=IF(VALUES(status)<>'blocked',UTC_TIMESTAMP(6),last_success_at),
                   last_error_at=IF(VALUES(status)='blocked',UTC_TIMESTAMP(6),last_error_at),
                   checked_at=UTC_TIMESTAMP(6),updated_at=UTC_TIMESTAMP(6)""",
            (
                paper_account_id,
                user_id,
                status,
                pending_count,
                len(drift_codes),
                len(warning_codes),
                details,
                1 if ready else 0,
                1 if ready else 0,
            ),
        )

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
        position = _object(payload.get("position"), "position")
        debit = _finite_number(payload.get("balance_debit"), "balance_debit", minimum=0)
        if not existing:
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
        MySqlPaperProjectionStore._apply_balance_delta(
            transaction, execution, -debit, "open_debit"
        )

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
        trade = _object(payload.get("trade"), "trade")
        position_id = _positive_integer(payload.get("position_id"), "position_id")
        if not existing:
            ownership = transaction.query(
                """SELECT p.* FROM paper_positions p
                   WHERE p.id=? AND p.paper_account_id=? AND p.user_id=? FOR UPDATE""",
                (position_id, execution["paper_account_id"], execution["user_id"]),
            )
            if not ownership:
                raise PaperProjectionError("position to reduce is unavailable")
            current = ownership[0]
            if (
                str(current["symbol"]) != str(execution["symbol"])
                or int(current["side"]) != _direction(trade.get("side"))
                or int(current["opened_ts"])
                != _positive_integer(trade.get("opened_ts"), "opened_ts")
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
                    json.dumps(
                        _object(trade.get("entry_basis"), "entry_basis"),
                        ensure_ascii=False,
                    ),
                    execution_id,
                ),
            )
            if inserted != 1:
                raise PaperProjectionError("paper trade projection was not inserted")
        returned = _finite_number(payload.get("balance_credit"), "balance_credit", minimum=0)
        MySqlPaperProjectionStore._apply_balance_delta(
            transaction, execution, returned, "close_credit"
        )

    @staticmethod
    def _record_position_snapshot(
        transaction: _Transaction,
        execution: dict[str, Any],
        payload: dict[str, Any],
    ) -> None:
        action = str(execution["action"])
        source = (
            _object(payload.get("position"), "position")
            if action == "open"
            else _object(payload.get("trade"), "trade")
        )
        direction = _direction(source.get("side"))
        snapshot = {
            "schema_version": 1,
            "execution_id": int(execution["id"]),
            "execution_public_id": execution.get("public_id"),
            "action": action,
            "state": "open" if action == "open" else "closed",
            "symbol": str(execution["symbol"]),
            "position_side": str(
                execution.get("position_side")
                or ("LONG" if direction > 0 else "SHORT")
            ),
            "quantity": _positive(source.get("qty"), "qty") if action == "open" else 0,
            "average_entry_price": _positive(
                source.get("avg_entry") if action == "open" else source.get("entry_price"),
                "average_entry_price",
            ),
            "mark_price": _positive(
                source.get("avg_entry") if action == "open" else source.get("exit_price"),
                "mark_price",
            ),
        }
        encoded = json.dumps(
            snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        observed_ts = _positive_integer(
            source.get("opened_ts") if action == "open" else source.get("closed_ts"),
            "observed_ts",
        )
        transaction.execute(
            """INSERT IGNORE INTO position_snapshots(
                   public_id,user_id,deployment_id,strategy_revision_id,mode,
                   account_scope,symbol,position_side,position_state,quantity,
                   average_entry_price,mark_price,source_type,source_key,
                   snapshot_json,snapshot_hash,observed_at,created_at
               ) SELECT UUID(),?,?,deployment_row.strategy_revision_id,'paper',
                        ?,?,?,?,?,?,?,?,?,?,?,?,FROM_UNIXTIME(?),UTC_TIMESTAMP(6)
                 FROM strategy_deployments AS deployment_row
                WHERE deployment_row.id=? AND deployment_row.user_id=?""",
            (
                execution["user_id"],
                execution.get("deployment_id"),
                f"paper:{execution['paper_account_id']}",
                execution["symbol"],
                snapshot["position_side"],
                snapshot["state"],
                snapshot["quantity"],
                snapshot["average_entry_price"],
                snapshot["mark_price"],
                "paper_execution",
                str(execution.get("public_id") or execution["id"]),
                encoded,
                hashlib.sha256(encoded.encode("utf-8")).hexdigest(),
                observed_ts,
                execution.get("deployment_id"),
                execution["user_id"],
            ),
        )

    @staticmethod
    def _apply_balance_delta(
        transaction: _Transaction,
        execution: dict[str, Any],
        delta: float,
        entry_type: str,
    ) -> None:
        execution_id = int(execution["id"])
        if transaction.query(
            "SELECT id FROM paper_account_ledger_entries WHERE source_execution_id=? LIMIT 1",
            (execution_id,),
        ):
            return
        checkpoint_rows = transaction.query(
            """SELECT expected_balance FROM paper_account_balance_checkpoints
               WHERE paper_account_id=? AND user_id=? FOR UPDATE""",
            (execution["paper_account_id"], execution["user_id"]),
        )
        account_rows = transaction.query(
            """SELECT balance FROM paper_accounts
               WHERE id=? AND user_id=? FOR UPDATE""",
            (execution["paper_account_id"], execution["user_id"]),
        )
        if not checkpoint_rows or not account_rows:
            raise PaperProjectionError("paper balance checkpoint is unavailable")
        expected = _finite_number(
            checkpoint_rows[0]["expected_balance"], "expected_balance", minimum=0
        )
        actual = _finite_number(account_rows[0]["balance"], "balance", minimum=0)
        if abs(actual - expected) > 1e-8:
            raise PaperProjectionError("paper balance drift requires review")
        balance_after = round(expected + delta, 8)
        if balance_after < 0:
            raise PaperProjectionError("paper account has insufficient margin")
        if transaction.execute(
            """UPDATE paper_accounts SET balance=?,updated_at=CURRENT_TIMESTAMP
               WHERE id=? AND user_id=?""",
            (balance_after, execution["paper_account_id"], execution["user_id"]),
        ) != 1:
            raise PaperProjectionError("paper balance projection was not updated")
        if transaction.execute(
            """UPDATE paper_account_balance_checkpoints
               SET expected_balance=?,last_execution_id=?,updated_at=UTC_TIMESTAMP(6)
               WHERE paper_account_id=? AND user_id=?""",
            (
                balance_after,
                execution_id,
                execution["paper_account_id"],
                execution["user_id"],
            ),
        ) != 1:
            raise PaperProjectionError("paper balance checkpoint was not updated")
        if transaction.execute(
            """INSERT INTO paper_account_ledger_entries(
                   public_id,paper_account_id,user_id,source_execution_id,
                   entry_type,amount,balance_after
               ) VALUES(?,?,?,?,?,?,?)""",
            (
                str(uuid.uuid4()),
                execution["paper_account_id"],
                execution["user_id"],
                execution_id,
                entry_type,
                delta,
                balance_after,
            ),
        ) != 1:
            raise PaperProjectionError("paper balance ledger entry was not inserted")


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


def _money(value: Any, name: str) -> Decimal:
    if isinstance(value, bool):
        raise PaperProjectionError(f"{name} must be numeric")
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise PaperProjectionError(f"{name} must be numeric") from exc
    if not number.is_finite():
        raise PaperProjectionError(f"{name} must be finite")
    return number


def _round_money(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.00000001"))


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
