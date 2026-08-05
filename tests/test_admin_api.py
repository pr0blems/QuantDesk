from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

from quantdesk_v2 import battle
from quantdesk_v2.config import Settings
from quantdesk_v2.database import get_db
from quantdesk_v2.main import create_app
from quantdesk_v2.models import AdminSetting, AuditLog, NewsSourceSetting, User


def build_admin_client(mysql_test_engine: Engine):
    settings = Settings(
        _env_file=None,
        app_env="test",
        database_url=mysql_test_engine.url.render_as_string(hide_password=False),
        jwt_secret=SecretStr("test-jwt-secret-that-is-long-enough-123456"),
        credential_master_key=SecretStr(Fernet.generate_key().decode("ascii")),
        app_cookie_secure=False,
        app_allowed_hosts="testserver",
        app_allowed_origins="http://testserver",
    )
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
    return TestClient(app), test_session


def register_and_login(client: TestClient, username: str) -> tuple[int, str]:
    registered = client.post(
        "/api/v2/auth/register",
        json={"username": username, "password": "correct horse battery staple"},
    )
    assert registered.status_code == 201
    logged_in = client.post(
        "/api/v2/auth/login",
        json={
            "username": username,
            "password": "correct horse battery staple",
            "client_type": "web",
        },
    )
    assert logged_in.status_code == 200
    return registered.json()["id"], logged_in.json()["access_token"]


def test_admin_authorization_rules_and_news_source_management(
    mysql_test_engine: Engine,
) -> None:
    client, test_session = build_admin_client(mysql_test_engine)
    with client:
        admin_id, _ = register_and_login(client, "platform-admin")
        regular_id, regular_token = register_and_login(client, "regular-user")
        with test_session() as db:
            db.get(User, admin_id).is_admin = True
            db.commit()
        admin_login = client.post(
            "/api/v2/auth/login",
            json={
                "username": "platform-admin",
                "password": "correct horse battery staple",
                "client_type": "web",
            },
        )
        admin_token = admin_login.json()["access_token"]
        admin_headers = {
            "Authorization": f"Bearer {admin_token}",
            "X-QuantDesk-User-ID": str(admin_id),
        }

        regular_algorithm = client.get(
            "/api/v2/monitor/prediction-algorithm",
            headers={"Authorization": f"Bearer {regular_token}"},
        )
        assert regular_algorithm.status_code == 200
        assert regular_algorithm.json()["editable"] is False
        algorithm_config = regular_algorithm.json()["defaults"]
        algorithm_config["direction_threshold"] = 0.22
        forbidden_algorithm_update = client.put(
            "/api/v2/monitor/prediction-algorithm",
            headers={
                "Authorization": f"Bearer {regular_token}",
                "X-QuantDesk-User-ID": str(regular_id),
            },
            json=algorithm_config,
        )
        assert forbidden_algorithm_update.status_code == 403
        saved_algorithm = client.put(
            "/api/v2/monitor/prediction-algorithm",
            headers=admin_headers,
            json=algorithm_config,
        )
        assert saved_algorithm.status_code == 200
        assert saved_algorithm.json()["editable"] is True
        assert saved_algorithm.json()["config_version"] == 1
        assert saved_algorithm.json()["config"]["direction_threshold"] == 0.22

        forbidden = client.get(
            "/api/v2/admin/overview",
            headers={"Authorization": f"Bearer {regular_token}"},
        )
        assert forbidden.status_code == 403

        overview = client.get("/api/v2/admin/overview", headers=admin_headers)
        assert overview.status_code == 200
        assert overview.json()["users"] == {"total": 2, "active": 2}
        assert set(overview.json()["last_24h"]) == {
            "alerts",
            "alert_kinds",
            "alert_directions",
            "news",
            "news_sentiment",
        }
        assert {
            item["name"]
            for item in client.get("/api/v2/admin/collectors", headers=admin_headers).json()
        } >= {
            "price",
            "ticker",
            "kline",
            "news",
            "social",
            "paper",
        }

        rules_payload = {
            "score_alert_long": 65,
            "score_alert_short": -65,
            "score_alert_position": 45,
            "spike_alert_pct_5m": 2.5,
            "watchlist_only": True,
            "enabled_timeframes": ["15m", "1h"],
        }
        missing_identity = client.put(
            "/api/v2/admin/alert-rules",
            headers={"Authorization": f"Bearer {admin_token}"},
            json=rules_payload,
        )
        assert missing_identity.status_code == 428
        saved = client.put("/api/v2/admin/alert-rules", headers=admin_headers, json=rules_payload)
        assert saved.status_code == 200
        assert saved.json()["version"] == 1
        assert saved.json()["rules"] == rules_payload

        sources = client.get("/api/v2/admin/news-sources", headers=admin_headers)
        assert sources.status_code == 200
        assert len(sources.json()) >= 20
        source_name = sources.json()[0]["name"]
        disabled = client.patch(
            f"/api/v2/admin/news-sources/{source_name}",
            headers=admin_headers,
            json={"enabled": False, "lang": "zh-CN"},
        )
        assert disabled.status_code == 200
        assert disabled.json()["lang"] == "zh-CN"

        created_source = client.post(
            "/api/v2/admin/news-sources",
            headers=admin_headers,
            json={
                "name": "TestEditorialFeed",
                "url": "https://example.com/markets.xml",
                "lang": "en",
                "enabled": True,
                "slow": True,
                "weight": 120,
                "hourly_limit": 80,
            },
        )
        assert created_source.status_code == 201
        assert created_source.json()["name"] == "TestEditorialFeed"
        duplicate_source = client.post(
            "/api/v2/admin/news-sources",
            headers=admin_headers,
            json={"name": "TestEditorialFeed", "url": "https://example.com/other.xml"},
        )
        assert duplicate_source.status_code == 409
        deleted_source = client.delete(
            "/api/v2/admin/news-sources/TestEditorialFeed", headers=admin_headers
        )
        assert deleted_source.status_code == 200

        alert_events = client.get("/api/v2/admin/alerts", headers=admin_headers)
        assert alert_events.status_code == 200
        assert set(alert_events.json()) == {"total", "limit", "offset", "items"}

        intelligence = client.get("/api/v2/admin/news", headers=admin_headers)
        assert intelligence.status_code == 200
        assert set(intelligence.json()) == {"total", "limit", "offset", "items"}

        symbols = client.get("/api/v2/admin/symbols?query=NVDA", headers=admin_headers)
        assert symbols.status_code == 200
        assert set(symbols.json()) == {"total", "healthy", "items"}

        users = client.get("/api/v2/admin/users?status=active", headers=admin_headers)
        assert users.status_code == 200
        assert users.json()["total"] == 2
        assert len(users.json()["items"]) == 2

        user_update = client.patch(
            f"/api/v2/admin/users/{regular_id}",
            headers=admin_headers,
            json={"is_active": False},
        )
        assert user_update.status_code == 200
        assert user_update.json()["is_active"] is False

        cleanup = client.post(
            "/api/v2/admin/maintenance/cleanup-preview",
            headers=admin_headers,
            json={"alerts_days": 30, "news_days": 90, "scores_days": 180},
        )
        assert cleanup.status_code == 200
        assert set(cleanup.json()["delete_counts"]) == {"alerts", "news", "scores"}

        with test_session() as db:
            assert db.get(AdminSetting, "alert_rules").version == 1
            assert db.get(AdminSetting, battle.ALGORITHM_SETTING_KEY).version == 1
            assert db.get(NewsSourceSetting, source_name).enabled is False
            assert db.get(NewsSourceSetting, source_name).lang == "zh-CN"
            assert db.get(NewsSourceSetting, "TestEditorialFeed") is None
            assert db.get(User, regular_id).is_active is False
            assert db.query(AuditLog).filter(AuditLog.action.like("admin.%")).count() >= 3


def test_admin_frontend_assets_and_route(mysql_test_engine: Engine) -> None:
    client, _ = build_admin_client(mysql_test_engine)
    with client:
        page = client.get("/admin")
        login_page = client.get("/admin/login")
        user_page = client.get("/monitor")
        script = client.get("/assets/admin.js")
        stylesheet = client.get("/assets/admin.css")

    assert page.status_code == 200
    assert login_page.text == page.text
    assert 'id="admin-login"' in page.text
    assert 'id="admin-shell"' in page.text
    assert 'data-view="collectors"' in page.text
    assert 'data-view="audit"' in page.text
    assert "<contract-monitor" not in page.text
    assert 'data-panel-target="admin"' not in user_page.text
    assert "/assets/admin.js" not in user_page.text
    assert script.status_code == 200
    assert 'fetch("/api/v2/auth/refresh"' in script.text
    assert "if (!user.is_admin)" in script.text
    assert 'const VIEWS = {' in script.text
    assert stylesheet.status_code == 200
    assert ".admin-shell" in stylesheet.text
    assert ".login-screen" in stylesheet.text
