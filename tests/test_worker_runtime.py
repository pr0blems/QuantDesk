from __future__ import annotations

from quantdesk_v2 import worker_runtime


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
