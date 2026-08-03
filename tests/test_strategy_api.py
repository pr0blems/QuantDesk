from __future__ import annotations

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

from quantdesk_v2.config import Settings
from quantdesk_v2.database import build_engine, get_db
from quantdesk_v2.main import create_app
from quantdesk_v2.models import Base, StrategyRevision, User, UserStrategy


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
        openai_api_key=SecretStr(""),
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


def register_and_login(client: TestClient, username: str) -> dict[str, str]:
    response = client.post(
        "/api/v2/auth/register",
        json={"username": username, "password": "correct horse battery staple"},
    )
    assert response.status_code == 201
    response = client.post(
        "/api/v2/auth/login",
        json={
            "username": username,
            "password": "correct horse battery staple",
            "client_type": "web",
        },
    )
    assert response.status_code == 200
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def test_strategy_endpoints_require_authentication() -> None:
    client, _ = build_test_client()
    with client:
        assert client.get("/api/v2/strategies").status_code == 401
        assert client.post("/api/v2/strategies", json={}).status_code == 401
        assert client.get("/api/v2/strategies/not-found").status_code == 401


def test_first_login_copies_all_defaults_and_list_is_tenant_isolated() -> None:
    client, session_factory = build_test_client()
    with client:
        alice_headers = register_and_login(client, "strategy-alice")
        with session_factory() as db:
            alice = db.scalar(select(User).where(User.username == "strategy-alice"))
            assert alice is not None
            assert (
                db.scalar(
                    select(func.count())
                    .select_from(UserStrategy)
                    .where(UserStrategy.user_id == alice.id)
                )
                == 19
            )

        alice = client.get("/api/v2/strategies", headers=alice_headers)
        assert alice.status_code == 200
        body = alice.json()
        assert len(body["items"]) == len(body["templates"]) == 19
        assert [item["name"] for item in body["items"]] == [
            "趋势突破",
            "MA 金叉",
            "MACD 金叉放量",
            "量价齐升",
            "低波动龙头",
            "断板反包",
            "超跌反弹",
            "布林突破",
            "均线多头",
            "连板股",
            "缩量回踩",
            "新低反转",
            "高换手拉升",
            "连板接力",
            "逼近涨停",
            "超跌反转",
            "均线回踩反弹",
            "强势高开",
            "AI 模拟盘 ATR 趋势",
        ]
        paper_strategy = next(
            item for item in body["items"] if item["source_template_key"] == "paper_multifactor_atr_v1"
        )
        assert paper_strategy["risk_defaults"]["take_profit_pct"] == 5
        assert "2.5×ATR 固定止盈" in paper_strategy["description"]
        assert all(item["public_id"] == item["id"] for item in body["items"])
        assert all(item["is_default"] is True for item in body["items"])

        bob_headers = register_and_login(client, "strategy-bob")
        bob = client.get("/api/v2/strategies", headers=bob_headers).json()
        assert {item["id"] for item in body["items"]}.isdisjoint(
            {item["id"] for item in bob["items"]}
        )
        hidden = client.get(
            f"/api/v2/strategies/{body['items'][0]['id']}",
            headers=bob_headers,
        )
        assert hidden.status_code == 404


def test_create_manual_edit_ai_preview_apply_and_archive_are_versioned() -> None:
    client, session_factory = build_test_client()
    with client:
        headers = register_and_login(client, "strategy-editor")
        created = client.post(
            "/api/v2/strategies",
            headers=headers,
            json={
                "name": "我的短均线策略",
                "description": "用于验证策略版本",
                "category": "自定义",
                "template_key": "ma_golden_cross",
            },
        )
        assert created.status_code == 201
        strategy = created.json()
        strategy_id = strategy["id"]
        assert strategy["version"] == 1
        assert strategy["engine_key"] == "ma_cross"
        assert strategy["is_default"] is False

        update_payload = {
            "version": 1,
            "name": "我的短均线策略 v2",
            "description": strategy["description"],
            "category": strategy["category"],
            "parameters": {"fast_period": 8, "slow_period": 34},
            "risk_defaults": strategy["risk_defaults"],
        }
        updated = client.put(
            f"/api/v2/strategies/{strategy_id}", headers=headers, json=update_payload
        )
        assert updated.status_code == 200
        assert updated.json()["version"] == 2
        assert updated.json()["parameters"] == {"fast_period": 8, "slow_period": 34}

        stale = client.put(
            f"/api/v2/strategies/{strategy_id}", headers=headers, json=update_payload
        )
        assert stale.status_code == 409
        assert stale.json()["detail"]["current_version"] == 2

        preview = client.post(
            f"/api/v2/strategies/{strategy_id}/ai-preview",
            headers=headers,
            json={"prompt": "把快线周期改为 10，止损改为 3"},
        )
        assert preview.status_code == 200
        proposal = preview.json()
        assert proposal["provider"] == "local_semantic"
        assert proposal["base_version"] == 2
        assert proposal["proposed"]["parameters"]["fast_period"] == 10
        assert proposal["proposed"]["risk_defaults"]["stop_loss_pct"] == 3

        applied = client.post(
            f"/api/v2/strategies/{strategy_id}/ai-apply",
            headers=headers,
            json={
                "base_version": proposal["base_version"],
                "proposed": proposal["proposed"],
            },
        )
        assert applied.status_code == 200
        assert applied.json()["version"] == 3
        assert applied.json()["parameters"]["fast_period"] == 10

        archived = client.delete(f"/api/v2/strategies/{strategy_id}", headers=headers)
        assert archived.status_code == 200
        assert archived.json()["status"] == "archived"
        listed_ids = {
            item["id"] for item in client.get("/api/v2/strategies", headers=headers).json()["items"]
        }
        assert strategy_id not in listed_ids

        with session_factory() as db:
            saved = db.scalar(select(UserStrategy).where(UserStrategy.public_id == strategy_id))
            assert saved is not None and saved.version == 4
            revisions = db.scalars(
                select(StrategyRevision)
                .where(StrategyRevision.user_strategy_id == saved.id)
                .order_by(StrategyRevision.version)
            ).all()
            assert [item.version for item in revisions] == [1, 2, 3, 4]
            assert [item.change_source for item in revisions] == [
                "manual",
                "manual",
                "ai",
                "manual",
            ]


def test_strategy_edit_rejects_parameter_expansion_and_cross_tenant_apply() -> None:
    client, _ = build_test_client()
    with client:
        alice_headers = register_and_login(client, "strategy-owner")
        bob_headers = register_and_login(client, "strategy-attacker")
        strategy = client.get("/api/v2/strategies", headers=alice_headers).json()["items"][0]

        invalid = client.put(
            f"/api/v2/strategies/{strategy['id']}",
            headers=alice_headers,
            json={
                "version": strategy["version"],
                "name": strategy["name"],
                "description": strategy["description"],
                "category": strategy["category"],
                "parameters": {**strategy["parameters"], "arbitrary_code": 1},
                "risk_defaults": strategy["risk_defaults"],
            },
        )
        assert invalid.status_code == 422

        hidden = client.post(
            f"/api/v2/strategies/{strategy['id']}/ai-apply",
            headers=bob_headers,
            json={
                "base_version": strategy["version"],
                "proposed": {
                    "name": strategy["name"],
                    "description": strategy["description"],
                    "category": strategy["category"],
                    "parameters": strategy["parameters"],
                    "risk_defaults": strategy["risk_defaults"],
                },
            },
        )
        assert hidden.status_code == 404
