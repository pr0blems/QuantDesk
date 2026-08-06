from __future__ import annotations

from base64 import urlsafe_b64encode

from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

from quantdesk_v2 import cli
from quantdesk_v2.config import Settings
from quantdesk_v2.database import get_db
from quantdesk_v2.main import create_app
from quantdesk_v2.runtime.leases import LeaseOwner, WorkerLease


def _settings(mysql_test_engine: Engine) -> Settings:
    return Settings(
        _env_file=None,
        app_env="test",
        database_url=mysql_test_engine.url.render_as_string(hide_password=False),
        db_ssl_required=False,
        db_ssl_verify_identity=False,
        jwt_secret=SecretStr("test-jwt-secret-that-is-long-enough-123456"),
        credential_master_key=SecretStr(urlsafe_b64encode(b"r" * 32).decode("ascii")),
        app_cookie_secure=False,
        app_allowed_hosts="testserver",
        app_allowed_origins="http://testserver",
    )


def _client(mysql_test_engine: Engine) -> TestClient:
    settings = _settings(mysql_test_engine)
    test_session = sessionmaker(bind=mysql_test_engine, autoflush=False, expire_on_commit=False)
    app = create_app(settings)
    app.state.database_engine.dispose()
    app.state.database_engine = mysql_test_engine

    def override_db():
        db = test_session()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_db
    return TestClient(app)


def _leases(engine: Engine) -> list[WorkerLease]:
    leases = [
        WorkerLease(engine, LeaseOwner.create(f"quantdesk-ng:{role}"), ttl_seconds=30)
        for role in ("market", "news", "paper", "intelligence")
    ]
    for lease in leases:
        assert lease.acquire() is True
    return leases


def test_readiness_requires_every_continuous_worker(mysql_test_engine: Engine) -> None:
    client = _client(mysql_test_engine)
    with client:
        assert client.get("/api/v2/health/live").json()["status"] == "ok"

        missing = client.get("/api/v2/health/ready")
        assert missing.status_code == 503
        assert missing.json()["database"] == "ok"
        assert set(missing.json()["workers"]) == {"market", "news", "paper", "intelligence"}
        assert all(item["status"] == "missing" for item in missing.json()["workers"].values())

        leases = _leases(mysql_test_engine)
        try:
            ready = client.get("/api/v2/health/ready")
            assert ready.status_code == 200
            assert ready.json()["status"] == "ok"
            assert all(item["status"] == "active" for item in ready.json()["workers"].values())
        finally:
            for lease in leases:
                lease.release()


def test_worker_health_rejects_missing_and_stale_leases(
    mysql_test_engine: Engine, monkeypatch
) -> None:
    test_session = sessionmaker(bind=mysql_test_engine, autoflush=False, expire_on_commit=False)
    monkeypatch.setattr(cli, "SessionLocal", test_session)
    monkeypatch.setattr(cli, "get_settings", lambda: _settings(mysql_test_engine))

    assert cli.worker_health("market") == 2

    lease = WorkerLease(
        mysql_test_engine, LeaseOwner.create("quantdesk-ng:market"), ttl_seconds=30
    )
    assert lease.acquire() is True
    try:
        assert cli.worker_health("market") == 0
        with mysql_test_engine.begin() as connection:
            connection.execute(
                text(
                    """
                    UPDATE worker_leases
                       SET heartbeat_at=DATE_SUB(UTC_TIMESTAMP(6), INTERVAL 60 SECOND),
                           expires_at=DATE_SUB(UTC_TIMESTAMP(6), INTERVAL 1 SECOND)
                     WHERE worker_key='quantdesk-ng:market'
                    """
                )
            )
        assert cli.worker_health("market") == 2
    finally:
        lease.release()
