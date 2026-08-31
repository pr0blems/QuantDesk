from __future__ import annotations

import json
from contextlib import contextmanager
from typing import Any

import pytest

from quantdesk_v2.application.paper_reconciliation import (
    PaperExecutionReconciliationService,
    PaperProjectionOutcome,
)
from quantdesk_v2.infrastructure.persistence.paper_projections import (
    MySqlPaperProjectionStore,
    PaperProjectionError,
)


class _ServiceStore:
    def __init__(self) -> None:
        self.pending = [101, 102]
        self.failed: list[tuple[int, int, int, str]] = []
        self.projected: list[tuple[int, int, int]] = []

    def pending_execution_ids(self, *, user_id: int, paper_account_id: int, limit: int):
        assert (user_id, paper_account_id, limit) == (7, 11, 100)
        return tuple(self.pending)

    def project_execution(self, *, user_id: int, paper_account_id: int, execution_id: int):
        self.projected.append((user_id, paper_account_id, execution_id))
        if execution_id == 102:
            raise RuntimeError("broken projection")
        self.pending.remove(execution_id)
        return PaperProjectionOutcome(execution_id, "applied")

    def record_failure(
        self, *, user_id: int, paper_account_id: int, execution_id: int, error: str
    ) -> None:
        self.failed.append((user_id, paper_account_id, execution_id, error))

    def audit_account(self, *, user_id: int, paper_account_id: int):
        assert (user_id, paper_account_id) == (7, 11)
        return len(self.pending), ()


def test_reconciliation_is_tenant_scoped_and_blocks_only_failed_account() -> None:
    store = _ServiceStore()
    service = PaperExecutionReconciliationService(store)

    result = service.reconcile_account(user_id=7, paper_account_id=11)

    assert result.ready is False
    assert result.discovered == 2
    assert result.applied == 1
    assert result.failed == 1
    assert result.remaining == 1
    assert store.projected == [(7, 11, 101), (7, 11, 102)]
    assert store.failed[0][:3] == (7, 11, 102)
    assert "broken projection" in store.failed[0][3]


class _OpenBackend:
    def __init__(self) -> None:
        self.fact: dict[str, Any] = {
            "id": 101,
            "user_id": 7,
            "paper_account_id": 11,
            "symbol": "TESTUSDT",
            "action": "open",
            "status": "FILLED",
            "projection_status": "pending",
            "projection_version": "paper_projection_v1",
            "projection_json": json.dumps(
                {
                    "schema_version": 1,
                    "action": "open",
                    "balance_debit": 51,
                    "position": {
                        "side": 1,
                        "qty": 10,
                        "avg_entry": 100,
                        "margin": 50,
                        "leverage": 20,
                        "stop": 97,
                        "target": 105,
                        "opened_ts": 1_000,
                        "last_add_ts": 1_000,
                        "open_score": 80,
                        "basis": {"schema_version": 1},
                        "liq_price": 95.5,
                        "funding_ts": 1_000,
                        "atr_entry": 2,
                        "peak_price": 100,
                    },
                }
            ),
        }
        self.balance = 10_000.0
        self.position_source: int | None = None
        self.writes: list[tuple[str, tuple[Any, ...]]] = []

    @contextmanager
    def transaction(self):
        yield self

    def query(self, sql: str, params=()):
        values = tuple(params)
        if "SELECT * FROM paper_order_executions" in sql:
            if values != (101, 7, 11):
                return []
            return [dict(self.fact)]
        if "paper_positions WHERE source_execution_id" in sql:
            return [{"id": 1}] if self.position_source == values[0] else []
        if "SELECT balance FROM paper_accounts" in sql:
            return [{"balance": self.balance}]
        if "WHERE paper_account_id=? AND user_id=? AND symbol=?" in sql:
            return []
        raise AssertionError(f"unexpected query: {sql}")

    def execute(self, sql: str, params=()):
        values = tuple(params)
        self.writes.append((sql, values))
        if "INSERT INTO paper_positions" in sql:
            self.position_source = int(values[-1])
        elif "UPDATE paper_accounts SET balance" in sql:
            self.balance = float(values[0])
        elif "UPDATE paper_order_executions" in sql and "projection_status='applied'" in sql:
            self.fact["projection_status"] = "applied"
        return 1


def test_open_fill_projection_is_idempotent_and_updates_balance_once() -> None:
    backend = _OpenBackend()
    store = MySqlPaperProjectionStore(backend)

    first = store.project_execution(user_id=7, paper_account_id=11, execution_id=101)
    second = store.project_execution(user_id=7, paper_account_id=11, execution_id=101)

    assert first.state == "applied"
    assert second.state == "already_applied"
    assert backend.position_source == 101
    assert backend.balance == pytest.approx(9_949)
    assert sum("INSERT INTO paper_positions" in sql for sql, _ in backend.writes) == 1
    assert sum("UPDATE paper_accounts SET balance" in sql for sql, _ in backend.writes) == 1


def test_projection_rejects_cross_tenant_execution_scope() -> None:
    backend = _OpenBackend()
    store = MySqlPaperProjectionStore(backend)

    with pytest.raises(PaperProjectionError, match="tenant scope"):
        store.project_execution(user_id=8, paper_account_id=11, execution_id=101)

    assert backend.position_source is None
    assert backend.balance == 10_000


class _CloseBackend:
    def __init__(self) -> None:
        self.fact: dict[str, Any] = {
            "id": 202,
            "user_id": 7,
            "paper_account_id": 11,
            "symbol": "TESTUSDT",
            "action": "close",
            "status": "FILLED",
            "projection_status": "pending",
            "projection_version": "paper_projection_v1",
            "projection_json": {
                "schema_version": 1,
                "action": "close",
                "position_id": 31,
                "balance_credit": 54,
                "trade": {
                    "side": 1,
                    "qty": 10,
                    "entry_price": 100,
                    "exit_price": 101,
                    "margin": 50,
                    "pnl": 10,
                    "fee": 6,
                    "funding": 0,
                    "reason": "take_profit",
                    "open_score": 80,
                    "opened_ts": 1_000,
                    "closed_ts": 2_000,
                    "entry_basis": {"schema_version": 1},
                },
            },
        }
        self.position = {
            "id": 31,
            "paper_account_id": 11,
            "user_id": 7,
            "symbol": "TESTUSDT",
            "side": 1,
            "opened_ts": 1_000,
            "balance": 9_949,
        }
        self.trade_source: int | None = None
        self.balance = 9_949.0
        self.writes: list[tuple[str, tuple[Any, ...]]] = []

    @contextmanager
    def transaction(self):
        yield self

    def query(self, sql: str, params=()):
        values = tuple(params)
        if "SELECT * FROM paper_order_executions" in sql:
            return [dict(self.fact)] if values == (202, 7, 11) else []
        if "paper_trades WHERE source_execution_id" in sql:
            return [{"id": 41}] if self.trade_source == values[0] else []
        if "SELECT p.*,a.balance" in sql:
            return [dict(self.position)] if self.position is not None else []
        raise AssertionError(f"unexpected query: {sql}")

    def execute(self, sql: str, params=()):
        values = tuple(params)
        self.writes.append((sql, values))
        if "DELETE FROM paper_positions" in sql:
            self.position = None
        elif "INSERT INTO paper_trades" in sql:
            self.trade_source = int(values[-1])
        elif "UPDATE paper_accounts SET balance" in sql:
            self.balance = float(values[0])
        elif "UPDATE paper_order_executions" in sql and "projection_status='applied'" in sql:
            self.fact["projection_status"] = "applied"
        return 1


def test_close_fill_projection_removes_position_and_credits_balance_once() -> None:
    backend = _CloseBackend()
    store = MySqlPaperProjectionStore(backend)

    first = store.project_execution(user_id=7, paper_account_id=11, execution_id=202)
    second = store.project_execution(user_id=7, paper_account_id=11, execution_id=202)

    assert first.state == "applied"
    assert second.state == "already_applied"
    assert backend.position is None
    assert backend.trade_source == 202
    assert backend.balance == pytest.approx(10_003)
    assert sum("INSERT INTO paper_trades" in sql for sql, _ in backend.writes) == 1
    assert sum("UPDATE paper_accounts SET balance" in sql for sql, _ in backend.writes) == 1
