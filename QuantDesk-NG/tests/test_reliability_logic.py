from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from quantdesk_v2.reliability import _worker_readiness


class _Result:
    def __init__(self, rows: list[dict[str, object]]):
        self.rows = rows

    def mappings(self) -> _Result:
        return self

    def all(self) -> list[dict[str, object]]:
        return self.rows


class _Connection:
    def __init__(self, rows: list[dict[str, object]]):
        self.rows = rows

    def __enter__(self) -> _Connection:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def execute(self, *_: object, **__: object) -> _Result:
        return _Result(self.rows)


class _Engine:
    def __init__(self, rows: list[dict[str, object]]):
        self.rows = rows

    def connect(self) -> _Connection:
        return _Connection(self.rows)


def _request(rows: list[dict[str, object]]):
    return SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                settings=SimpleNamespace(worker_heartbeat_seconds=10),
                database_engine=_Engine(rows),
            )
        )
    )


def test_worker_readiness_marks_missing_and_stale_roles_not_ready() -> None:
    now = datetime.now(UTC).replace(tzinfo=None)
    workers, all_active = _worker_readiness(
        _request(
            [
                {
                    "worker_key": "quantdesk-ng:market",
                    "heartbeat_at": now - timedelta(seconds=60),
                    "expires_at": now - timedelta(seconds=1),
                }
            ]
        )
    )

    assert all_active is False
    assert workers["market"].status == "stale"
    assert workers["news"].status == "missing"


def test_worker_readiness_accepts_fresh_leases_for_every_required_role() -> None:
    now = datetime.now(UTC).replace(tzinfo=None)
    rows = [
        {
            "worker_key": f"quantdesk-ng:{role}",
            "heartbeat_at": now,
            "expires_at": now + timedelta(seconds=30),
        }
        for role in ("market", "news", "paper", "intelligence")
    ]

    workers, all_active = _worker_readiness(_request(rows))

    assert all_active is True
    assert {item.status for item in workers.values()} == {"active"}
