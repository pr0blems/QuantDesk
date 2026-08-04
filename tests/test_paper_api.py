from __future__ import annotations

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

from quantdesk_v2 import api
from quantdesk_v2.config import Settings
from quantdesk_v2.database import get_db
from quantdesk_v2.main import create_app
from quantdesk_v2.models import AuditLog


class FakePaperRepository:
    def __init__(self) -> None:
        self.paper_calls: list[tuple[int, int, int]] = []
        self.performance_calls: list[tuple[int, int, str, int]] = []
        self.reset_calls: list[tuple[int, int]] = []

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

    def paper(
        self, user_id: int, account_id: int, timezone_offset_minutes: int = 0
    ) -> dict:
        self.paper_calls.append((user_id, account_id, timezone_offset_minutes))
        return self._data()

    def reset_paper(self, user_id: int, account_id: int) -> dict:
        self.reset_calls.append((user_id, account_id))
        return self._data()

    def paper_performance(
        self,
        user_id: int,
        account_id: int,
        month: str,
        timezone_offset_minutes: int,
    ) -> dict:
        self.performance_calls.append(
            (user_id, account_id, month, timezone_offset_minutes)
        )
        return {
            "source": "paper_account",
            "scope": "user_account",
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


def build_test_client(mysql_test_engine: Engine):
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
    return TestClient(app), test_session


def _register_and_login(client: TestClient, username: str) -> dict[str, str]:
    assert (
        client.post(
            "/api/v2/auth/register",
            json={"username": username, "password": "correct horse battery staple"},
        ).status_code
        == 201
    )
    login = client.post(
        "/api/v2/auth/login",
        json={
            "username": username,
            "password": "correct horse battery staple",
            "client_type": "web",
        },
    )
    assert login.status_code == 200
    return {"Authorization": f"Bearer {login.json()['access_token']}"}


def test_paper_endpoints_are_account_scoped_and_tenant_isolated(
    monkeypatch, mysql_test_engine: Engine
) -> None:
    client, test_session = build_test_client(mysql_test_engine)
    repository = FakePaperRepository()
    monkeypatch.setattr(api, "_monitor", lambda _: repository)

    with client:
        assert client.get("/api/v2/paper").status_code == 401
        assert client.get("/api/v2/dashboard/performance").status_code == 401
        alice_headers = _register_and_login(client, "paper-alice")
        strategy_id = client.get(
            "/api/v2/strategies", headers=alice_headers
        ).json()["items"][0]["id"]
        created = client.post(
            "/api/v2/paper/accounts",
            headers=alice_headers,
            json={
                "name": "Alice paper",
                "strategy_id": strategy_id,
                "initial_balance": 10_000,
            },
        )
        assert created.status_code == 201
        public_account_id = created.json()["id"]

        account = client.get(
            f"/api/v2/paper?account_id={public_account_id}&timezone_offset_minutes=480",
            headers=alice_headers,
        )
        assert account.status_code == 200
        assert account.json()["permissions"] == {"can_reset": True}
        alice_user_id, database_account_id, timezone_offset = repository.paper_calls[0]
        assert timezone_offset == 480

        performance = client.get(
            "/api/v2/dashboard/performance"
            f"?account_id={public_account_id}&month=2026-08&timezone_offset_minutes=480",
            headers=alice_headers,
        )
        assert performance.status_code == 200
        assert performance.headers["cache-control"] == "private, no-store"
        assert performance.json()["calendar"]["month"] == "2026-08"
        assert repository.performance_calls == [
            (alice_user_id, database_account_id, "2026-08", 480)
        ]

        bob_headers = _register_and_login(client, "paper-bob")
        denied = client.get(
            f"/api/v2/paper?account_id={public_account_id}", headers=bob_headers
        )
        assert denied.status_code == 404
        assert len(repository.paper_calls) == 1

        reset = client.post(
            f"/api/v2/paper/reset?account_id={public_account_id}",
            headers=alice_headers,
        )
        assert reset.status_code == 200
        assert reset.json()["permissions"] == {"can_reset": True}
        assert repository.reset_calls == [(alice_user_id, database_account_id)]

        with test_session() as db:
            audit = db.query(AuditLog).filter(AuditLog.action == "paper.account.reset").one()
            assert audit.resource_type == "paper_account"
            assert audit.resource_id == public_account_id
