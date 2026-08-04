from sqlalchemy.engine import Engine

from quantdesk_v2.runtime.leases import LeaseOwner, WorkerLease


def test_worker_lease_excludes_duplicate_role(mysql_test_engine: Engine) -> None:
    first = WorkerLease(
        mysql_test_engine,
        LeaseOwner.create("quantdesk-ng:test-role"),
        ttl_seconds=30,
    )
    second = WorkerLease(
        mysql_test_engine,
        LeaseOwner.create("quantdesk-ng:test-role"),
        ttl_seconds=30,
    )

    assert first.acquire() is True
    assert first.heartbeat() is True
    assert second.acquire() is False

    first.release()
    assert second.acquire() is True
    second.release()


def test_worker_lease_release_is_owner_scoped(mysql_test_engine: Engine) -> None:
    first = WorkerLease(
        mysql_test_engine,
        LeaseOwner.create("quantdesk-ng:owner-scope"),
        ttl_seconds=30,
    )
    stranger = WorkerLease(
        mysql_test_engine,
        LeaseOwner.create("quantdesk-ng:owner-scope"),
        ttl_seconds=30,
    )
    assert first.acquire() is True
    stranger.release()
    assert first.heartbeat() is True
    first.release()
