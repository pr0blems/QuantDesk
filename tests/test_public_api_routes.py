from types import SimpleNamespace

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy.exc import SQLAlchemyError

from quantdesk_v2 import __version__
from quantdesk_v2.config import Settings
from quantdesk_v2.database import get_db
from quantdesk_v2.interfaces.api.finnhub import router as finnhub_router
from quantdesk_v2.interfaces.api.health import router as health_router
from quantdesk_v2.main import create_app


def _settings() -> Settings:
    return Settings(
        _env_file=None,
        app_env="test",
        database_url="mysql+pymysql://test:test@127.0.0.1/quantdesk_test_unavailable",
        jwt_secret=SecretStr("test-jwt-secret-that-is-long-enough-123456"),
        credential_master_key=SecretStr(Fernet.generate_key().decode("ascii")),
        app_cookie_secure=False,
        app_allowed_hosts="testserver",
        app_allowed_origins="http://testserver",
    )


class _HealthDb:
    bind = SimpleNamespace(dialect=SimpleNamespace(name="mysql"))

    def __init__(self, error: SQLAlchemyError | None = None) -> None:
        self.error = error

    def execute(self, statement: object) -> None:
        assert str(statement) == "SELECT 1"
        if self.error is not None:
            raise self.error


def test_health_route_keeps_success_and_database_failure_contracts() -> None:
    app = create_app(_settings())
    health_db = _HealthDb()
    app.dependency_overrides[get_db] = lambda: health_db

    with TestClient(app) as client:
        healthy = client.get("/api/v2/health")
        health_db.error = SQLAlchemyError("database offline")
        unavailable = client.get("/api/v2/health")

    assert healthy.status_code == 200
    assert healthy.json() == {
        "status": "ok",
        "database": "ok",
        "version": __version__,
        "database_dialect": "mysql",
        "tls_required": False,
    }
    assert unavailable.status_code == 503
    assert unavailable.json() == {"detail": "database unavailable"}


def test_public_routes_are_registered_once_with_unchanged_contracts() -> None:
    app = create_app(_settings())
    expected = {
        ("/api/v2/health", "GET"): (
            "HealthOut",
            "200",
        ),
        ("/api/v2/market/us/status", "GET"): (
            "UsMarketStatusOut",
            "200",
        ),
        ("/api/v2/market/us/quotes", "GET"): (
            "FinnhubUsQuotesOut",
            "200",
        ),
        ("/api/v2/integrations/finnhub/webhook", "GET"): (
            "FinnhubWebhookStatusOut",
            "200",
        ),
        ("/api/v2/integrations/finnhub/webhook", "POST"): (
            "FinnhubWebhookAcceptedOut",
            "202",
        ),
    }

    openapi_paths = app.openapi()["paths"]
    for (path, method), (response_model, status_code) in expected.items():
        operation = openapi_paths[path][method.lower()]
        schema = operation["responses"][status_code]["content"]["application/json"]["schema"]
        assert schema["$ref"] == f"#/components/schemas/{response_model}"

    assert {route.endpoint.__module__ for route in health_router.routes} == {
        "quantdesk_v2.interfaces.api.health"
    }
    assert {route.endpoint.__module__ for route in finnhub_router.routes} == {
        "quantdesk_v2.interfaces.api.finnhub"
    }
