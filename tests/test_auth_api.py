from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

from quantdesk_v2.config import Settings
from quantdesk_v2.database import get_db
from quantdesk_v2.main import create_app
from quantdesk_v2.models import User
from quantdesk_v2.security import CredentialCipher


def build_test_client(mysql_test_engine: Engine):
    master_key = Fernet.generate_key().decode("ascii")
    settings = Settings(
        _env_file=None,
        app_env="test",
        database_url=mysql_test_engine.url.render_as_string(hide_password=False),
        jwt_secret=SecretStr("test-jwt-secret-that-is-long-enough-123456"),
        credential_master_key=SecretStr(master_key),
        app_cookie_secure=False,
        app_allowed_hosts="testserver",
        app_allowed_origins="http://testserver",
    )
    test_session = sessionmaker(
        bind=mysql_test_engine, autoflush=False, expire_on_commit=False
    )
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
    return TestClient(app), test_session, master_key


def test_register_login_and_encrypt_binance_credentials(mysql_test_engine: Engine) -> None:
    client, test_session, master_key = build_test_client(mysql_test_engine)
    with client:
        registered = client.post(
            "/api/v2/auth/register",
            json={
                "username": "alice",
                "email": "alice@example.com",
                "password": "a-very-strong-password",
            },
        )
        assert registered.status_code == 201

        duplicate_username = client.post(
            "/api/v2/auth/register",
            json={"username": "alice", "password": "another-strong-password"},
        )
        assert duplicate_username.status_code == 409
        assert duplicate_username.json()["detail"] == "username already exists"

        duplicate_email = client.post(
            "/api/v2/auth/register",
            json={
                "username": "another-user",
                "email": "alice@example.com",
                "password": "another-strong-password",
            },
        )
        assert duplicate_email.status_code == 409
        assert duplicate_email.json()["detail"] == "email already exists"

        logged_in = client.post(
            "/api/v2/auth/login",
            json={"username": "alice", "password": "a-very-strong-password", "client_type": "web"},
        )
        assert logged_in.status_code == 200
        access = logged_in.json()["access_token"]
        assert logged_in.json()["refresh_token"] is None

        saved = client.put(
            "/api/v2/me/binance-credentials",
            headers={
                "Authorization": f"Bearer {access}",
                "X-QuantDesk-User-ID": str(registered.json()["id"]),
            },
            json={
                "api_key": "A" * 64,
                "api_secret": "S" * 64,
                "permissions": ["READ", "TRADE"],
            },
        )
        assert saved.status_code == 200
        assert saved.json()["configured"] is True
        assert "A" * 64 not in saved.text
        assert "S" * 64 not in saved.text

        with test_session() as db:
            user = db.query(User).filter(User.username == "alice").one()
            assert user.binance_api_key_encrypted != "A" * 64
            assert user.binance_api_secret_encrypted != "S" * 64
            cipher = CredentialCipher(master_key)
            assert cipher.decrypt(user.binance_api_key_encrypted) == "A" * 64
            assert cipher.decrypt(user.binance_api_secret_encrypted) == "S" * 64

        me = client.get("/api/v2/me", headers={"Authorization": f"Bearer {access}"})
        assert me.status_code == 200
        assert me.json()["binance_credentials_configured"] is True


def test_multiple_users_without_email_and_permission_allowlist(mysql_test_engine: Engine) -> None:
    client, _, _ = build_test_client(mysql_test_engine)
    with client:
        for username in ("without-email-one", "without-email-two"):
            response = client.post(
                "/api/v2/auth/register",
                json={"username": username, "password": "correct horse battery staple"},
            )
            assert response.status_code == 201

        login = client.post(
            "/api/v2/auth/login",
            json={
                "username": "without-email-one",
                "password": "correct horse battery staple",
                "client_type": "web",
            },
        )
        access_token = login.json()["access_token"]
        current_user = client.get(
            "/api/v2/me", headers={"Authorization": f"Bearer {access_token}"}
        ).json()
        invalid = client.put(
            "/api/v2/me/binance-credentials",
            headers={
                "Authorization": f"Bearer {access_token}",
                "X-QuantDesk-User-ID": str(current_user["id"]),
            },
            json={
                "api_key": "K" * 32,
                "api_secret": "S" * 32,
                "permissions": ["WITHDRAW"],
            },
        )
        assert invalid.status_code == 422


def test_binance_credentials_require_matching_tab_user(mysql_test_engine: Engine) -> None:
    client, test_session, _ = build_test_client(mysql_test_engine)
    with client:
        first = client.post(
            "/api/v2/auth/register",
            json={"username": "identity-one", "password": "correct horse battery staple"},
        ).json()
        second = client.post(
            "/api/v2/auth/register",
            json={"username": "identity-two", "password": "correct horse battery staple"},
        ).json()
        login = client.post(
            "/api/v2/auth/login",
            json={
                "username": "identity-one",
                "password": "correct horse battery staple",
                "client_type": "web",
            },
        )
        access = login.json()["access_token"]
        payload = {
            "api_key": "K" * 32,
            "api_secret": "S" * 32,
            "permissions": ["READ"],
        }

        missing = client.put(
            "/api/v2/me/binance-credentials",
            headers={"Authorization": f"Bearer {access}"},
            json=payload,
        )
        mismatched = client.put(
            "/api/v2/me/binance-credentials",
            headers={
                "Authorization": f"Bearer {access}",
                "X-QuantDesk-User-ID": str(second["id"]),
            },
            json=payload,
        )

        assert missing.status_code == 428
        assert mismatched.status_code == 409
        with test_session() as db:
            assert db.get(User, first["id"]).binance_credentials_configured is False
            assert db.get(User, second["id"]).binance_credentials_configured is False

        saved = client.put(
            "/api/v2/me/binance-credentials",
            headers={
                "Authorization": f"Bearer {access}",
                "X-QuantDesk-User-ID": str(first["id"]),
            },
            json=payload,
        )
        assert saved.status_code == 200

        with test_session() as db:
            assert db.get(User, first["id"]).binance_credentials_configured is True
            assert db.get(User, second["id"]).binance_credentials_configured is False

def test_login_page_and_navigation_shell_are_served(
    mysql_test_engine: Engine,
    monkeypatch,
    tmp_path,
) -> None:
    build_dir = tmp_path / "react_static"
    assets_dir = build_dir / "assets"
    assets_dir.mkdir(parents=True)
    for asset_name in (
        "ai-monitor.css",
        "backtest.css",
        "monitor.css",
        "paper.css",
    ):
        (assets_dir / asset_name).write_text("/* frontend asset */", encoding="utf-8")
    (build_dir / "index.html").write_text(
        '<!doctype html><html lang="zh-CN"><body><div id="root"></div></body></html>',
        encoding="utf-8",
    )
    monkeypatch.setattr(
        Settings,
        "react_static_dir",
        property(lambda _: build_dir),
    )

    client, _, _ = build_test_client(mysql_test_engine)
    with client:
        page = client.get("/")
        assert page.status_code == 200
        assert 'id="root"' in page.text
        for frontend_path in (
            "/login",
            "/monitor",
            "/ai-monitor",
            "/paper",
            "/live",
            "/overview",
            "/settings",
            "/strategies",
            "/backtest",
        ):
            response = client.get(frontend_path)
            assert response.status_code == 200
            assert response.content == page.content

        for retired_path, destination in {
            "/orders": "/live",
            "/risk": "/overview",
            "/audit": "/overview",
        }.items():
            response = client.get(retired_path, follow_redirects=False)
            assert response.status_code == 308
            assert response.headers["location"] == destination

        assert client.get("/unknown-frontend-route").status_code == 404
        assert client.get("/assets/app.js").status_code == 404
        assert client.get("/assets/controller-runtime.js").status_code == 404
        for asset_path in (
            "/next/assets/ai-monitor.css",
            "/next/assets/backtest.css",
            "/next/assets/monitor.css",
            "/next/assets/paper.css",
        ):
            assert client.get(asset_path).status_code == 200

        assert page.headers["x-frame-options"] == "DENY"
