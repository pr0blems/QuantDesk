from __future__ import annotations

from datetime import datetime

from quantdesk_v2 import worker_runtime


class _Mappings:
    def __init__(self, row) -> None:
        self.row = row

    def mappings(self):
        return self

    def one(self):
        return self.row


class _HeartbeatConnection:
    def __init__(self, rows) -> None:
        self.rows = iter(rows)

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        return None

    def execute(self, *_args, **_kwargs):
        return _Mappings(next(self.rows))


class _HeartbeatEngine:
    def __init__(self, rows) -> None:
        self.rows = rows

    def connect(self):
        return _HeartbeatConnection(self.rows)


class _ExpiredLeaseConnection:
    def __init__(self, *, fail_close: bool = False) -> None:
        self.closed = False
        self.fail_close = fail_close

    def execute(self, *_args, **_kwargs):
        raise RuntimeError("mysql connection already expired")

    def close(self) -> None:
        self.closed = True
        if self.fail_close:
            raise RuntimeError("socket already closed")


def test_release_singleton_tolerates_an_expired_mysql_connection(capsys) -> None:
    connection = _ExpiredLeaseConnection()

    worker_runtime._release_singleton(connection, "paper")

    assert connection.closed is True
    assert "lease release skipped: RuntimeError" in capsys.readouterr().err


def test_release_singleton_tolerates_an_already_closed_socket(capsys) -> None:
    connection = _ExpiredLeaseConnection(fail_close=True)

    worker_runtime._release_singleton(connection, "live")

    assert connection.closed is True
    error = capsys.readouterr().err
    assert "lease release skipped: RuntimeError" in error
    assert "lease connection close skipped: RuntimeError" in error


def test_paper_heartbeat_exposes_reconciliation_metrics() -> None:
    now = datetime(2026, 8, 31, 12, 0, 0)
    engine = _HeartbeatEngine(
        [
            {
                "account_count": 3,
                "blocked_count": 1,
                "warning_count": 1,
                "pending_count": 2,
                "drift_count": 1,
                "last_success_at": now,
            },
            {"pending_count": 1, "failed_count": 1},
        ]
    )

    status, details = worker_runtime._paper_heartbeat_details(engine)

    assert status == "running"
    assert details == {
        "paper_reconciliation": {
            "state": "blocked",
            "account_count": 3,
            "blocked_count": 1,
            "warning_count": 1,
            "pending_count": 1,
            "failed_count": 1,
            "drift_count": 1,
            "last_success_at": "2026-08-31T12:00:00",
        }
    }


def test_paper_heartbeat_stays_running_for_nonblocking_warnings() -> None:
    engine = _HeartbeatEngine(
        [
            {
                "account_count": 2,
                "blocked_count": 0,
                "warning_count": 2,
                "pending_count": 0,
                "drift_count": 0,
                "last_success_at": None,
            },
            {"pending_count": 0, "failed_count": 0},
        ]
    )

    status, details = worker_runtime._paper_heartbeat_details(engine)

    assert status == "running"
    assert details["paper_reconciliation"]["state"] == "warning"
