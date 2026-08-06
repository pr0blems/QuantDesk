from __future__ import annotations

from base64 import urlsafe_b64encode

from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

from quantdesk_v2.config import Settings
from quantdesk_v2.database import get_db
from quantdesk_v2.main import create_app


def _client(mysql_test_engine: Engine) -> TestClient:
    settings = Settings(
        _env_file=None,
        app_env="test",
        database_url=mysql_test_engine.url.render_as_string(hide_password=False),
        db_ssl_required=False,
        db_ssl_verify_identity=False,
        jwt_secret=SecretStr("test-jwt-secret-that-is-long-enough-123456"),
        credential_master_key=SecretStr(urlsafe_b64encode(b"s" * 32).decode("ascii")),
        app_cookie_secure=False,
        app_allowed_hosts="testserver",
        app_allowed_origins="http://testserver",
    )
    app: FastAPI = create_app(settings)
    app.state.database_engine.dispose()
    app.state.database_engine = mysql_test_engine
    test_session = sessionmaker(bind=mysql_test_engine, autoflush=False, expire_on_commit=False)

    def override_db():
        db = test_session()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_db
    return TestClient(app)


def _register_and_login(client: TestClient, username: str = "saas-user") -> tuple[int, str]:
    registered = client.post(
        "/api/v2/auth/register",
        json={"username": username, "password": "a-very-strong-password"},
    )
    assert registered.status_code == 201
    login = client.post(
        "/api/v2/auth/login",
        json={"username": username, "password": "a-very-strong-password", "client_type": "native"},
    )
    assert login.status_code == 200
    return registered.json()["id"], login.json()["access_token"]


def test_v1_entitlements_default_to_free_and_payment_is_disabled(mysql_test_engine: Engine) -> None:
    client = _client(mysql_test_engine)
    with client:
        _, token = _register_and_login(client)
        plans = client.get("/api/v1/plans")
        assert plans.status_code == 200
        assert plans.json()["payment_available"] is False
        openapi = client.get("/api/v1/openapi.json")
        assert openapi.status_code == 200
        assert set(openapi.json()["paths"]) >= {"/api/v1/plans", "/api/v1/entitlements/me"}

        entitlement = client.get(
            "/api/v1/entitlements/me", headers={"Authorization": f"Bearer {token}"}
        )
        assert entitlement.status_code == 200
        payload = entitlement.json()
        assert payload["plan_code"] == "free"
        assert payload["limits"]["backtest_runs_day"] == 2
        assert payload["payment_available"] is False


def test_metrics_are_prometheus_compatible_in_test_environment(mysql_test_engine: Engine) -> None:
    client = _client(mysql_test_engine)
    with client:
        client.get("/api/v2/health/live")
        metrics = client.get("/metrics")
        assert metrics.status_code == 200
        assert metrics.headers["content-type"].startswith("text/plain; version=0.0.4")
        assert "quantdesk_build_info" in metrics.text
        assert "quantdesk_http_requests_total" in metrics.text
