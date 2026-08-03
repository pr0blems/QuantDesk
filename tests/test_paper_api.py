from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy.orm import sessionmaker

from quantdesk_v2 import api
from quantdesk_v2.config import Settings
from quantdesk_v2.database import build_engine, get_db
from quantdesk_v2.main import create_app
from quantdesk_v2.models import AuditLog, Base, User


class FakePaperRepository:
    def __init__(self) -> None:
        self.paper_calls = 0
        self.performance_calls: list[tuple[str, int]] = []
        self.reset_calls = 0

    @staticmethod
    def _data() -> dict:
        return {
            "account": {"equity": 10_000},
            "positions": [],
            "trades": [],
            "curve": [],
            "stats": {},
            "rules": {},
        }

    def paper(self) -> dict:
        self.paper_calls += 1
        return self._data()

    def reset_paper(self) -> dict:
        self.reset_calls += 1
        return self._data()

    def paper_performance(self, month: str, timezone_offset_minutes: int) -> dict:
        self.performance_calls.append((month, timezone_offset_minutes))
        return {
            "source": "system_paper",
            "scope": "shared_system",
            "currency": "USDT",
            "generated_at": "2026-08-03T00:00:00Z",
            "data_as_of": None,
            "period_start": None,
            "stale": True,
            "metrics": {
                "total_pnl": 0,
                "total_return_pct": 0,
                "realized_pnl": 0,
                "unrealized_pnl": 0,
                "win_rate": 0,
                "win_rate_basis": "decisive_trades",
                "profit_factor": None,
                "profit_factor_status": "no_trades",
                "max_drawdown": 0,
                "max_drawdown_basis": "since_reset_full_equity",
                "average_profit": 0,
                "average_win": 0,
                "trades": 0,
                "wins": 0,
                "losses": 0,
                "breakeven": 0,
                "equity_samples": 0,
            },
            "calendar": {
                "month": month,
                "timezone_offset_minutes": timezone_offset_minutes,
                "timezone_label": "UTC+08:00",
                "basis": "closed_trade_net_pnl",
                "total_pnl": 0,
                "active_days": 0,
                "days": [],
            },
        }


def build_test_client():
    settings = Settings(
        _env_file=None,
        app_env="test",
        database_url="sqlite+pysqlite:///:memory:",
        db_password=SecretStr(""),
        db_ssl_required=False,
        db_ssl_verify_identity=False,
        jwt_secret=SecretStr("test-jwt-secret-that-is-long-enough-123456"),
        credential_master_key=SecretStr(Fernet.generate_key().decode("ascii")),
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
    return TestClient(app), test_session


def test_paper_endpoints_require_auth_and_admin_reset(monkeypatch) -> None:
    client, test_session = build_test_client()
    repository = FakePaperRepository()
    monkeypatch.setattr(api, "_monitor", lambda _: repository)

    with client:
        assert client.get("/api/v2/paper").status_code == 401
        assert client.get("/api/v2/dashboard/performance").status_code == 401
        registered = client.post(
            "/api/v2/auth/register",
            json={"username": "paper-user", "password": "correct horse battery staple"},
        )
        assert registered.status_code == 201
        login = client.post(
            "/api/v2/auth/login",
            json={
                "username": "paper-user",
                "password": "correct horse battery staple",
                "client_type": "web",
            },
        )
        headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

        account = client.get("/api/v2/paper", headers=headers)
        assert account.status_code == 200
        assert account.json()["permissions"] == {"can_reset": False}

        performance = client.get(
            "/api/v2/dashboard/performance?month=2026-08&timezone_offset_minutes=480",
            headers=headers,
        )
        assert performance.status_code == 200
        assert performance.headers["cache-control"] == "private, no-store"
        assert performance.json()["calendar"]["month"] == "2026-08"
        assert repository.performance_calls == [("2026-08", 480)]

        denied = client.post("/api/v2/paper/reset", headers=headers)
        assert denied.status_code == 403
        assert repository.reset_calls == 0

        with test_session() as db:
            user = db.query(User).filter(User.username == "paper-user").one()
            user.is_admin = True
            db.commit()

        reset = client.post("/api/v2/paper/reset", headers=headers)
        assert reset.status_code == 200
        assert reset.json()["permissions"] == {"can_reset": True}
        assert repository.reset_calls == 1

        with test_session() as db:
            audit = db.query(AuditLog).filter(AuditLog.action == "paper.account.reset").one()
            assert audit.resource_type == "paper"
            assert audit.resource_id == "system"
