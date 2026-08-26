from __future__ import annotations

import uuid

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import func, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

from quantdesk_v2.config import Settings
from quantdesk_v2.database import get_db
from quantdesk_v2.main import create_app
from quantdesk_v2.models import AuditLog, TradingControlEvent, TradingControlLatch, User


def _client(mysql_test_engine: Engine):
    settings = Settings(
        _env_file=None,
        app_env="test",
        database_url=mysql_test_engine.url.render_as_string(hide_password=False),
        jwt_secret=SecretStr("test-jwt-secret-that-is-long-enough-123456"),
        credential_master_key=SecretStr(Fernet.generate_key().decode("ascii")),
        app_cookie_secure=False,
        app_allowed_hosts="testserver",
        app_allowed_origins="http://testserver",
        openai_api_key=SecretStr(""),
    )
    factory = sessionmaker(
        bind=mysql_test_engine,
        autoflush=False,
        expire_on_commit=False,
    )
    app = create_app(settings)
    app.state.database_engine.dispose()
    app.state.database_engine = mysql_test_engine

    def override_db():
        db = factory()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_db
    return TestClient(app), factory


def _register_login(client: TestClient, username: str) -> tuple[int, dict[str, str]]:
    registered = client.post(
        "/api/v2/auth/register",
        json={"username": username, "password": "correct horse battery staple"},
    )
    assert registered.status_code == 201
    user_id = int(registered.json()["id"])
    login = client.post(
        "/api/v2/auth/login",
        json={
            "username": username,
            "password": "correct horse battery staple",
            "client_type": "web",
        },
    )
    assert login.status_code == 200
    return user_id, {
        "Authorization": f"Bearer {login.json()['access_token']}",
        "X-QuantDesk-User-ID": str(user_id),
    }


def test_global_kill_switch_is_idempotent_versioned_and_audited(
    mysql_test_engine: Engine,
) -> None:
    client, factory = _client(mysql_test_engine)
    with client:
        user_id, headers = _register_login(client, "risk-admin")
        with factory() as db:
            user = db.get(User, user_id)
            assert user is not None
            user.is_admin = True
            db.commit()

        engage_id = str(uuid.uuid4())
        command = {
            "command_id": engage_id,
            "action": "engage",
            "scope_type": "global",
            "scope_key": "*",
            "expected_version": 0,
            "reason_code": "operator_freeze",
            "reason": "人工冻结全部新增风险以执行故障检查",
            "confirmed": True,
        }
        engaged = client.post(
            "/api/v2/risk/kill-switch",
            headers=headers,
            json=command,
        )
        assert engaged.status_code == 200, engaged.text
        assert engaged.json()["engaged"] is True
        assert engaged.json()["version"] == 1

        replayed = client.post(
            "/api/v2/risk/kill-switch",
            headers=headers,
            json=command,
        )
        assert replayed.status_code == 200
        assert replayed.json()["idempotent"] is True

        released = client.post(
            "/api/v2/risk/kill-switch",
            headers=headers,
            json={
                **command,
                "command_id": str(uuid.uuid4()),
                "action": "release",
                "expected_version": 1,
                "reason_code": "checks_completed",
                "reason": "故障检查和账户对账已经完成，人工解除冻结",
            },
        )
        assert released.status_code == 200, released.text
        assert released.json()["engaged"] is False
        assert released.json()["version"] == 2

        with factory() as db:
            latch = db.scalar(select(TradingControlLatch))
            assert latch is not None
            assert latch.engaged is False
            assert db.scalar(select(func.count(TradingControlEvent.id))) == 2
            assert (
                db.scalar(
                    select(func.count(AuditLog.id)).where(
                        AuditLog.action.like("risk.kill_switch.%")
                    )
                )
                == 2
            )


def test_non_admin_cannot_engage_global_kill_switch(mysql_test_engine: Engine) -> None:
    client, _factory = _client(mysql_test_engine)
    with client:
        _user_id, headers = _register_login(client, "risk-user")
        response = client.post(
            "/api/v2/risk/kill-switch",
            headers=headers,
            json={
                "command_id": str(uuid.uuid4()),
                "action": "engage",
                "scope_type": "global",
                "scope_key": "*",
                "expected_version": 0,
                "reason_code": "operator_freeze",
                "reason": "普通用户不应能够冻结整个交易平台",
                "confirmed": True,
            },
        )
        assert response.status_code == 403
