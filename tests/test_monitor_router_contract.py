from __future__ import annotations

from cryptography.fernet import Fernet
from pydantic import SecretStr

from quantdesk_v2.config import Settings
from quantdesk_v2.interfaces.api.monitor_public import router
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


def test_read_only_monitor_routes_are_owned_by_the_monitor_router() -> None:
    expected_paths = {
        "/monitor/overview",
        "/monitor/breadth",
        "/monitor/intelligence",
        "/monitor/news",
        "/monitor/tiger-news",
        "/monitor/klines",
        "/monitor/strategy-indicators",
        "/monitor/score",
        "/monitor/report",
    }

    assert {route.path for route in router.routes} == expected_paths
    assert {route.endpoint.__module__ for route in router.routes} == {
        "quantdesk_v2.interfaces.api.monitor_public"
    }


def test_monitor_route_contracts_remain_under_api_v2() -> None:
    paths = create_app(_settings()).openapi()["paths"]

    for path in (
        "/api/v2/monitor/overview",
        "/api/v2/monitor/breadth",
        "/api/v2/monitor/intelligence",
        "/api/v2/monitor/news",
        "/api/v2/monitor/tiger-news",
        "/api/v2/monitor/klines",
        "/api/v2/monitor/strategy-indicators",
        "/api/v2/monitor/score",
        "/api/v2/monitor/report",
    ):
        assert set(paths[path]) == {"get"}

    overview_parameters = paths["/api/v2/monitor/overview"]["get"]["parameters"]
    assert any(
        parameter["name"] == "symbol" and parameter["required"] is False
        for parameter in overview_parameters
    )
