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


def test_login_page_and_navigation_shell_are_served(mysql_test_engine: Engine) -> None:
    client, _, _ = build_test_client(mysql_test_engine)
    with client:
        page = client.get("/")
        assert page.status_code == 200
        assert '<html lang="zh-CN" class="auth-booting">' in page.text
        assert 'id="auth-boot"' in page.text
        assert "正在恢复登录状态" in page.text
        assert '/assets/style.css?v=20260804-7' in page.text
        assert '/assets/app.js?v=20260804-6' in page.text
        assert page.text.index('/assets/backtest.js') < page.text.index('/assets/app.js?v=20260804-6')
        assert 'id="login-page"' in page.text
        assert 'id="sidebar"' in page.text
        assert 'href="/monitor" data-panel-target="monitor" aria-current="page"' in page.text
        assert '<a class="nav-item" href="/overview" data-panel-target="overview"' in page.text
        assert '<a class="nav-item" href="/backtest" data-panel-target="backtest"' in page.text
        assert '<button class="nav-item"' not in page.text
        assert 'data-panel-target="monitor"' in page.text
        assert 'data-panel-target="monitor" aria-current="page"' in page.text
        assert '<contract-monitor id="contract-monitor">' in page.text
        assert 'data-panel-target="paper"' in page.text
        assert '<paper-dashboard id="paper-dashboard">' in page.text
        assert 'data-panel-target="backtest"' in page.text
        assert '<backtest-workbench id="backtest-workbench">' in page.text
        assert 'data-panel-target="strategies"' in page.text
        assert '<strategy-center id="strategy-center">' in page.text
        assert "策略中心</a>" in page.text
        assert 'id="binance-account-card"' in page.text
        assert 'id="orders-refresh"' in page.text
        assert 'id="orders-positions"' in page.text
        assert 'id="orders-open-orders"' in page.text
        assert 'id="binance-wallet-balance"' in page.text
        assert 'id="db-status"' not in page.text
        assert "数据库连接" not in page.text
        assert 'id="performance-dashboard"' in page.text
        assert 'id="virtual-performance-panel"' in page.text
        assert 'id="binance-performance-panel"' in page.text
        assert 'id="performance-total-return"' in page.text
        assert 'id="returns-calendar"' in page.text
        assert 'id="binance-performance-net-income"' in page.text
        assert 'id="binance-returns-calendar"' in page.text
        assert 'id="binance-performance-configure"' in page.text
        assert 'id="binance-performance-asset"' in page.text
        assert 'id="calendar-prev"' in page.text
        assert 'id="calendar-next"' in page.text
        assert "系统模拟盘 · 共享" in page.text
        assert "Binance 实盘收益" in page.text
        assert "累计总收益（自重置）" in page.text
        assert "当月已结算净收益" in page.text
        assert "已实现记录胜率" in page.text
        assert "当月平仓净收益" in page.text
        assert "平仓净收益日历" in page.text
        assert "完成账户连接" not in page.text
        assert "系统就绪度" not in page.text

        paper_asset = client.get("/assets/paper.js")
        backtest_asset = client.get("/assets/backtest.js")
        backtest_stylesheet = client.get("/assets/backtest.css")
        monitor_asset = client.get("/assets/monitor.js")
        strategy_asset = client.get("/assets/strategies.js")
        strategy_stylesheet = client.get("/assets/strategies.css")
        assert paper_asset.status_code == 200
        assert backtest_asset.status_code == 200
        assert backtest_stylesheet.status_code == 200
        assert monitor_asset.status_code == 200
        assert strategy_asset.status_code == 200
        assert strategy_stylesheet.status_code == 200
        assert 'id="btn-paper"' not in monitor_asset.text
        assert "iframe" not in backtest_asset.text.lower()
        assert "resetSession()" in backtest_asset.text
        assert 'id="leverage"' in backtest_asset.text
        assert 'max="20"' in backtest_asset.text
        assert 'max="99.9"' in backtest_asset.text
        assert "bars_used" in backtest_asset.text
        assert "我的策略" in backtest_asset.text
        assert "新增策略" in strategy_asset.text
        assert "/ai-preview" in strategy_asset.text
        assert "/ai-apply" in strategy_asset.text
        assert "base_version" in strategy_asset.text
        assert "iframe" not in strategy_asset.text.lower()
        assert "@media (max-width: 820px)" in strategy_stylesheet.text
        assert "grid-template-rows: auto minmax(0, 1fr)" in strategy_stylesheet.text
        assert "scrollbar-gutter: stable" in strategy_stylesheet.text
        assert "max-height: calc(100vh - 122px)" not in strategy_stylesheet.text
        assert "@media (max-width: 420px)" in backtest_stylesheet.text
        assert 'href="/settings" data-panel-target="settings"' in page.text
        assert 'data-panel-target="credentials"' not in page.text
        assert 'data-panel="settings"' in page.text
        assert 'data-open-panel="settings"' in page.text
        assert 'data-open-panel="credentials"' not in page.text
        assert 'id="credential-form"' in page.text
        assert page.headers["x-frame-options"] == "DENY"
        for frontend_path in (
            "/login",
            "/monitor",
            "/paper",
            "/overview",
            "/settings",
            "/strategies",
            "/backtest",
            "/orders",
            "/risk",
            "/audit",
        ):
            assert client.get(frontend_path).status_code == 200
        assert client.get("/unknown-frontend-route").status_code == 404

        script = client.get("/assets/app.js")
        monitor_script = client.get("/assets/monitor.js")
        monitor_stylesheet = client.get("/assets/monitor.css")
        stylesheet = client.get("/assets/style.css")
        assert script.status_code == 200
        assert "apiErrorMessage" in script.text
        assert 'api("/api/v2/me/binance-account")' in script.text
        assert 'api("/api/v2/me/binance-orders")' in script.text
        assert "renderBinanceOrders" in script.text
        assert "renderBinanceAccount" in script.text
        assert "formatAccountAmount" in script.text
        assert "/api/v2/dashboard/performance?month=" in script.text
        assert "/api/v2/dashboard/binance-performance?month=" in script.text
        assert "timezone_offset_minutes" in script.text
        assert ".getTimezoneOffset()" in script.text
        assert "/api/v2/paper" not in script.text
        assert "formatPerformancePnl" in script.text
        assert "renderDashboardPerformance" in script.text
        assert "renderPerformanceCalendar" in script.text
        assert "normalizeBinanceDashboardPerformance" in script.text
        assert "renderBinanceDashboardPerformance" in script.text
        assert "normalizeBinanceAssetPerformance" in script.text
        assert "withBinancePerformanceAsset" in script.text
        assert 'status === "history_unavailable"' in script.text
        assert "setPerformanceControlsLoading" in script.text
        assert "Promise.allSettled" in script.text
        assert "if (performance.account)" in script.text
        assert 'const LOGIN_PATH = "/login"' in script.text
        assert "const panelPaths" in script.text
        assert "safeNextPath" in script.text
        assert "window.history.pushState" in script.text
        assert "window.history.replaceState" in script.text
        assert 'window.addEventListener("popstate"' in script.text
        assert "let authBootResolved = false;" in script.text
        assert "function finishAuthBoot()" in script.text
        assert "if (!authBootResolved) return;" in script.text
        assert 'user = await api("/api/v2/me")' in script.text
        assert 'api("/api/v2/health").catch(() => null)' in script.text
        assert 'api("/api/v2/me"), api("/api/v2/health")' not in script.text
        dashboard_loader = script.text.index("async function loadDashboard()")
        authenticated_reveal = script.text.index("setAuthenticated(true);", dashboard_loader)
        background_health = script.text.index('api("/api/v2/health").catch(() => null)', dashboard_loader)
        assert authenticated_reveal < background_health
        assert 'document.title = "登录 · QuantDesk"' in script.text
        assert "document.title = `${panelNames[selected]} · QuantDesk`" in script.text
        assert 'openPanel("monitor")' not in script.text
        assert "location.hash" not in script.text
        assert "text-decoration: none" in stylesheet.text
        assert "html.auth-booting #login-page" in stylesheet.text
        assert "html.auth-booting .auth-boot" in stylesheet.text
        assert ".auth-boot-spinner" in stylesheet.text
        assert ".performance-columns" in stylesheet.text
        assert ".calendar-day.today" in stylesheet.text
        assert "@media (max-width: 1080px)" in stylesheet.text
        assert "@media (max-width: 520px)" in stylesheet.text
        assert monitor_script.status_code == 200
        assert monitor_stylesheet.status_code == 200
        assert stylesheet.status_code == 200
