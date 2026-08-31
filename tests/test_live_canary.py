from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import BigInteger, create_engine, text
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session

from quantdesk_v2.application.live_canary import LiveCanaryError
from quantdesk_v2.infrastructure.live_canary import LiveCanaryService
from quantdesk_v2.models import (
    LiveCanaryRun,
    LiveCanarySample,
    LiveOrderIntent,
    LiveTradingAccount,
    WorkerHeartbeat,
    utcnow,
)


@compiles(BigInteger, "sqlite")
def _compile_big_integer_as_sqlite_integer(
    _type: BigInteger, _compiler: object, **_: object
) -> str:
    return "INTEGER"


def _session() -> Session:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    for table in (
        LiveTradingAccount.__table__,
        LiveOrderIntent.__table__,
        WorkerHeartbeat.__table__,
        LiveCanaryRun.__table__,
        LiveCanarySample.__table__,
    ):
        table.create(engine)
    with engine.begin() as connection:
        connection.execute(
            text(
                """CREATE TABLE execution_idempotency_records (
                       id INTEGER PRIMARY KEY,
                       user_scope VARCHAR(191) NOT NULL,
                       account_scope VARCHAR(191) NOT NULL,
                       claim_status VARCHAR(32) NOT NULL,
                       lease_expires_at DATETIME NOT NULL,
                       execution_state VARCHAR(32)
                   )"""
            )
        )
    return Session(engine, expire_on_commit=False)


def _account(*, status: str = "active") -> LiveTradingAccount:
    now = utcnow()
    return LiveTradingAccount(
        id=3,
        public_id="live-account-3",
        user_id=7,
        strategy_id=11,
        name="Canary",
        status=status,
        config_json={},
        strategy_snapshot_json={},
        credential_version=1,
        armed_at=now,
        last_tick_at=now,
        created_at=now,
        updated_at=now,
    )


def _heartbeat() -> WorkerHeartbeat:
    now = utcnow()
    return WorkerHeartbeat(
        worker_type="live",
        instance_key="test:live",
        status="running",
        pid=1,
        host="test",
        release_version="test",
        started_at=now,
        last_seen_at=now,
        updated_at=now,
    )


def test_live_canary_persists_samples_and_passes_only_after_full_window() -> None:
    db = _session()
    db.add_all([_account(), _heartbeat()])
    db.commit()

    service = LiveCanaryService()
    run = service.start(
        db,
        user_id=7,
        live_account_id=3,
        window_seconds=15 * 60,
        minimum_open_fills=0,
    )
    assert run.status == "running"
    assert run.sample_count == 1
    run.due_at = utcnow() - timedelta(seconds=1)

    observation = service.sample(db, run)
    db.commit()

    assert observation.passed is True
    assert run.status == "passed"
    assert run.sample_count == 2
    assert db.query(LiveCanarySample).count() == 2


def test_live_canary_fails_permanently_on_transient_unknown_intent() -> None:
    db = _session()
    db.add_all([_account(), _heartbeat()])
    db.commit()
    service = LiveCanaryService()
    run = service.start(
        db,
        user_id=7,
        live_account_id=3,
        window_seconds=15 * 60,
        minimum_open_fills=0,
    )
    db.add(
        LiveOrderIntent(
            public_id="intent-1",
            user_id=7,
            live_account_id=3,
            deployment_id=19,
            signal_key="signal-1",
            client_order_id="client-1",
            symbol="AAPLUSDT",
            action="open",
            side="BUY",
            position_side="BOTH",
            order_type="MARKET",
            status="unknown",
            request_json={},
        )
    )
    db.flush()

    observation = service.sample(db, run)
    db.commit()

    assert observation.passed is False
    assert run.status == "failed"
    assert "unknown_order_intent" in run.failure_codes_json


def test_live_canary_allows_a_current_execution_lease() -> None:
    db = _session()
    db.add_all([_account(), _heartbeat()])
    db.execute(
        text(
            """INSERT INTO execution_idempotency_records (
                   id, user_scope, account_scope, claim_status,
                   lease_expires_at, execution_state
               ) VALUES (1, :user_scope, :account_scope, 'in_progress',
                         :lease_expires_at, NULL)"""
        ),
        {
            "user_scope": "user:7",
            "account_scope": "live-account:3",
            "lease_expires_at": utcnow() + timedelta(minutes=1),
        },
    )
    db.commit()

    run = LiveCanaryService().start(
        db,
        user_id=7,
        live_account_id=3,
        window_seconds=15 * 60,
        minimum_open_fills=0,
    )

    assert run.status == "running"
    assert "execution_claim_stale" not in run.failure_codes_json


def test_live_canary_never_enables_a_paused_account() -> None:
    db = _session()
    db.add_all([_account(status="paused"), _heartbeat()])
    db.commit()

    with pytest.raises(LiveCanaryError, match="已由用户启用"):
        LiveCanaryService().start(
            db,
            user_id=7,
            live_account_id=3,
            window_seconds=15 * 60,
        )


def test_live_canary_migration_follows_legacy_paper_cutover() -> None:
    source = (
        __import__("pathlib").Path(__file__).parents[1]
        / "migrations"
        / "versions"
        / "0077_live_canary_observations.py"
    ).read_text(encoding="utf-8")

    assert 'down_revision: str | None = "0076_disable_legacy_paper"' in source
    assert '"live_canary_runs"' in source
    assert '"live_canary_samples"' in source
