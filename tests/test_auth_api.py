from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy.orm import sessionmaker

from quantdesk_v2.config import Settings
from quantdesk_v2.database import build_engine, get_db
from quantdesk_v2.main import create_app
from quantdesk_v2.models import Base, User
from quantdesk_v2.security import CredentialCipher


def build_test_client():
    master_key = Fernet.generate_key().decode("ascii")
    settings = Settings(
        _env_file=None,
        app_env="test",
        database_url="sqlite+pysqlite:///:memory:",
        db_password=SecretStr(""),
        db_ssl_required=False,
        db_ssl_verify_identity=False,
        jwt_secret=SecretStr("test-jwt-secret-that-is-long-enough-123456"),
        credential_master_key=SecretStr(master_key),
        app_cookie_secure=False,
        app_allowed_hosts="testserver",
        app_allowed_origins="http://testserver",
    )
    engine = build_engine(settings)
    Base.metadata.create_all(engine)
    test_session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    app = create_app(settings)

    def override_db():
        db = test_session()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_db
    return TestClient(app), test_session, master_key


def test_register_login_and_encrypt_binance_credentials() -> None:
    client, test_session, master_key = build_test_client()
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
            headers={"Authorization": f"Bearer {access}"},
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


def test_multiple_users_without_email_and_permission_allowlist() -> None:
    client, _, _ = build_test_client()
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
        invalid = client.put(
            "/api/v2/me/binance-credentials",
            headers={"Authorization": f"Bearer {access_token}"},
            json={
                "api_key": "K" * 32,
                "api_secret": "S" * 32,
                "permissions": ["WITHDRAW"],
            },
        )
        assert invalid.status_code == 422


def test_login_page_and_navigation_shell_are_served() -> None:
    client, _, _ = build_test_client()
    with client:
        page = client.get("/")
        assert page.status_code == 200
        assert 'id="login-page"' in page.text
        assert 'id="sidebar"' in page.text
        assert 'data-panel-target="credentials"' in page.text
        assert page.headers["x-frame-options"] == "DENY"

        script = client.get("/assets/app.js")
        stylesheet = client.get("/assets/style.css")
        assert script.status_code == 200
        assert stylesheet.status_code == 200
