from __future__ import annotations

import os

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

from quantdesk_v2.config import Settings
from quantdesk_v2.database import get_db
from quantdesk_v2.main import create_app
from quantdesk_v2.models import ProxyNode
from quantdesk_v2.proxy_management import (
    CollectorProxy,
    _test_socks5_proxy,
    parse_subscription,
    sanitize_subscription_endpoint,
)


class _Socket:
    def __init__(self, incoming: bytes):
        self.incoming = bytearray(incoming)
        self.sent = bytearray()

    def recv(self, size: int) -> bytes:
        output = bytes(self.incoming[:size])
        del self.incoming[:size]
        return output

    def sendall(self, content: bytes) -> None:
        self.sent.extend(content)


def _client(mysql_test_engine: Engine) -> tuple[TestClient, sessionmaker]:
    settings = Settings(
        _env_file=None,
        app_env="test",
        database_url=os.environ["DATABASE_URL"],
        db_password=SecretStr(""),
        db_ssl_required=False,
        db_ssl_verify_identity=False,
        jwt_secret=SecretStr("test-jwt-secret-that-is-long-enough-123456"),
        credential_master_key=SecretStr(Fernet.generate_key().decode("ascii")),
        app_cookie_secure=False,
        app_allowed_hosts="testserver",
        app_allowed_origins="http://testserver",
    )
    app = create_app(settings)
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
    return TestClient(app), test_session


def _admin_headers(client: TestClient, test_session: sessionmaker) -> dict[str, str]:
    register = client.post(
        "/api/v2/auth/register",
        json={"username": "proxyadmin", "password": "A-very-long-password-123"},
    )
    assert register.status_code == 201
    user_id = register.json()["id"]
    # Tests intentionally promote the newly created isolated account through SQL.
    from quantdesk_v2.models import User

    with test_session() as db:
        user = db.get(User, user_id)
        assert user is not None
        user.is_admin = True
        db.commit()
    login = client.post(
        "/api/v2/auth/login",
        json={"username": "proxyadmin", "password": "A-very-long-password-123"},
    )
    return {
        "Authorization": f"Bearer {login.json()['access_token']}",
        "X-QuantDesk-User-ID": str(user_id),
    }


def test_parse_clash_yaml_and_base64_without_unsupported_protocols() -> None:
    yaml_nodes, yaml_skipped = parse_subscription(
        """proxies:
  - name: hk-a
    type: socks5
    server: 127.0.0.1
    port: 1080
    username: user
    password: top-secret
  - name: ignored
    type: vmess
    server: ignored.example.com
    port: 443
"""
    )
    assert [(node.protocol, node.host, node.port) for node in yaml_nodes] == [
        ("socks5", "127.0.0.1", 1080)
    ]
    assert yaml_nodes[0].password == "top-secret"
    assert yaml_skipped == 1

    encoded_nodes, skipped = parse_subscription("c29ja3M1Oi8vMTI3LjAuMC4xOjEwODA=")
    assert [(node.protocol, node.host, node.port) for node in encoded_nodes] == [
        ("socks5", "127.0.0.1", 1080)
    ]
    assert skipped == 0


def test_socks5_connect_performs_bounded_handshake() -> None:
    # no-auth response then CONNECT success with IPv4 bind address and port
    sock = _Socket(b"\x05\x00\x05\x00\x00\x01\x00\x00\x00\x00\x00\x00")
    _test_socks5_proxy(sock, CollectorProxy("socks5", "127.0.0.1", 1080))
    assert bytes(sock.sent).startswith(b"\x05\x01\x00\x05\x01\x00\x03")
    assert b"fstream.binance.com" in bytes(sock.sent)


def test_subscription_endpoint_removes_bearer_style_query_credentials() -> None:
    assert sanitize_subscription_endpoint("https://source.example.com/list?token=top-secret") == (
        "https://source.example.com/list"
    )


def test_proxy_api_encrypts_import_credentials_and_uses_safe_runtime(
    mysql_test_engine: Engine, monkeypatch
) -> None:
    client, test_session = _client(mysql_test_engine)
    with client:
        headers = _admin_headers(client, test_session)
        imported = client.post(
            "/api/v2/proxy/subscriptions",
            headers=headers,
            json={
                "name": "private source",
                "content": "proxies:\n - name: private\n   type: http\n   server: 127.0.0.1\n   port: 8080\n   username: alice\n   password: secret-password",
                "authorization": "subscription-secret",
            },
        )
        assert imported.status_code == 201
        assert "secret-password" not in imported.text
        assert "subscription-secret" not in imported.text
        node_id = imported.json()["subscription"]["id"]

        from quantdesk_v2 import proxy_routes

        def healthy(db, node, master_key, timeout_seconds=5.0):
            del db, master_key, timeout_seconds
            node.health_status = "healthy"
            node.last_latency_ms = 12
            return node

        monkeypatch.setattr(proxy_routes, "apply_test_result", healthy)
        status = client.get("/api/v2/proxy/status", headers=headers).json()
        actual_node_id = status["nodes"][0]["id"]
        tested = client.post(f"/api/v2/proxy/nodes/{actual_node_id}/test", headers=headers)
        assert tested.status_code == 200
        runtime = client.put(
            "/api/v2/proxy/runtime",
            headers=headers,
            json={"enabled": True, "selection_mode": "auto", "active_node_id": None},
        )
        assert runtime.status_code == 200
        assert runtime.json()["fallback_state"] == "proxy_active"
        assert node_id

    with mysql_test_engine.connect() as connection:
        stored = connection.execute(select(ProxyNode.password_encrypted)).scalar_one()
    assert stored and stored != "secret-password"
