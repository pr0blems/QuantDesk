from __future__ import annotations

from typing import Any

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import func, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

from quantdesk_v2 import strategy_routes
from quantdesk_v2.config import Settings
from quantdesk_v2.database import get_db
from quantdesk_v2.main import create_app
from quantdesk_v2.models import AiModelConfig, StrategyRevision, User, UserStrategy
from quantdesk_v2.security import CredentialCipher, api_key_fingerprint
from quantdesk_v2.strategy_ai import generate_local_strategy_preview


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
        openai_api_key=SecretStr(""),
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


def test_strategy_endpoints_require_authentication(mysql_test_engine: Engine) -> None:
    client, _ = build_test_client(mysql_test_engine)
    with client:
        assert client.get("/api/v2/strategies").status_code == 401
        assert client.post("/api/v2/strategies", json={}).status_code == 401
        assert client.get("/api/v2/strategies/not-found").status_code == 401


def test_first_login_copies_all_defaults_and_list_is_tenant_isolated(
    mysql_test_engine: Engine,
) -> None:
    client, session_factory = build_test_client(mysql_test_engine)
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
                == 20
            )

        alice = client.get("/api/v2/strategies", headers=alice_headers)
        assert alice.status_code == 200
        body = alice.json()
        assert len(body["items"]) == len(body["templates"]) == 20
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


def test_create_manual_edit_ai_preview_apply_and_archive_are_versioned(
    mysql_test_engine: Engine,
) -> None:
    client, session_factory = build_test_client(mysql_test_engine)
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


def test_ai_preview_prefers_current_users_enabled_default_model(
    mysql_test_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, session_factory = build_test_client(mysql_test_engine)
    with client:
        alice_headers = register_and_login(client, "strategy-model-alice")
        bob_headers = register_and_login(client, "strategy-model-bob")
        alice_strategy = client.get(
            "/api/v2/strategies", headers=alice_headers
        ).json()["items"][0]
        assert client.get("/api/v2/strategies", headers=bob_headers).status_code == 200

        master_key = client.app.state.settings.credential_master_key.get_secret_value()
        cipher = CredentialCipher(master_key)
        alice_key = "alice-deepseek-key-abcdefghijklmnopqrstuvwxyz"
        bob_key = "bob-qwen-key-abcdefghijklmnopqrstuvwxyz"
        with session_factory() as db:
            alice = db.scalar(select(User).where(User.username == "strategy-model-alice"))
            bob = db.scalar(select(User).where(User.username == "strategy-model-bob"))
            assert alice is not None and bob is not None
            db.add_all(
                [
                    AiModelConfig(
                        user_id=alice.id,
                        provider_code="deepseek",
                        display_name="Alice DeepSeek",
                        model_name="deepseek-v4-flash",
                        api_key_encrypted=cipher.encrypt(alice_key),
                        api_key_fingerprint=api_key_fingerprint(alice_key),
                        is_enabled=True,
                        is_default=True,
                    ),
                    AiModelConfig(
                        user_id=bob.id,
                        provider_code="qwen",
                        display_name="Bob Qwen",
                        model_name="qwen-plus",
                        api_key_encrypted=cipher.encrypt(bob_key),
                        api_key_fingerprint=api_key_fingerprint(bob_key),
                        is_enabled=True,
                        is_default=True,
                    ),
                ]
            )
            db.commit()

        captured: dict[str, Any] = {}

        def configured_preview(
            strategy_dict: dict[str, Any],
            prompt: str,
            **configuration: Any,
        ) -> dict[str, Any]:
            captured.update(configuration)
            preview = generate_local_strategy_preview(strategy_dict, prompt)
            preview["provider"] = configuration["provider_code"]
            return preview

        monkeypatch.setattr(
            strategy_routes,
            "generate_user_model_strategy_preview",
            configured_preview,
        )
        monkeypatch.setattr(
            strategy_routes,
            "generate_strategy_preview",
            lambda *_: pytest.fail("server OpenAI fallback must not be selected"),
        )

        response = client.post(
            f"/api/v2/strategies/{alice_strategy['id']}/ai-preview",
            headers=alice_headers,
            json={"prompt": "把止盈改为 8"},
        )

        assert response.status_code == 200
        assert response.json()["provider"] == "deepseek"
        assert captured["provider_code"] == "deepseek"
        assert captured["model_name"] == "deepseek-v4-flash"
        assert captured["api_key"] == alice_key
        assert bob_key not in str(captured)


def test_create_indicator_composition_persists_executable_strategy(
    mysql_test_engine: Engine,
) -> None:
    client, session_factory = build_test_client(mysql_test_engine)
    with client:
        headers = register_and_login(client, "strategy-composer")
        response = client.post(
            "/api/v2/strategies",
            headers=headers,
            json={
                "name": "EMA ADX 放量策略",
                "description": "趋势与成交量共同确认",
                "category": "指标组合",
                "timeframe": "15m",
                "directions": ["long"],
                "confirmation_threshold": 65,
                "signal_valid_bars": 3,
                "indicators": [
                    {
                        "key": "ema",
                        "weight": 2,
                        "parameters": {"fast_period": 8, "slow_period": 21},
                    },
                    {
                        "key": "adx",
                        "weight": 1,
                        "parameters": {"period": 10, "min_strength": 20},
                    },
                    {
                        "key": "volume_ratio",
                        "weight": 1,
                        "parameters": {"period": 20, "min_ratio": 1.5},
                    },
                ],
            },
        )

        assert response.status_code == 201
        strategy = response.json()
        assert strategy["strategy_kind"] == "full_strategy"
        assert strategy["engine_key"] == "strategy_dsl"
        assert strategy["spec"]["strategy_type"] == "indicator_composite"
        assert strategy["spec"]["timeframes"]["trigger"] == "15m"
        assert strategy["parameters"]["ema_fast_period"] == 8
        assert {item["key"] for item in strategy["parameter_schema"]} >= {
            "ema_fast_period",
            "adx_min_strength",
            "volume_ratio_min_ratio",
        }

        with session_factory() as db:
            owner = db.scalar(select(User).where(User.username == "strategy-composer"))
            saved = db.scalar(
                select(UserStrategy).where(UserStrategy.public_id == strategy["id"])
            )
            assert owner is not None and saved is not None
            assert saved.user_id == owner.id
            assert saved.spec_hash


def test_ai_composer_returns_reviewable_indicator_draft(
    mysql_test_engine: Engine,
) -> None:
    client, _ = build_test_client(mysql_test_engine)
    with client:
        headers = register_and_login(client, "strategy-ai-composer")
        response = client.post(
            "/api/v2/strategies/compose/ai-preview",
            headers=headers,
            json={"prompt": "使用 RSI、布林带和 ATR 做 15 分钟反转策略，只做多"},
        )

        assert response.status_code == 200
        proposal = response.json()
        assert proposal["provider"] == "local_semantic"
        assert proposal["draft"]["timeframe"] == "15m"
        assert proposal["draft"]["directions"] == ["long"]
        assert {item["key"] for item in proposal["draft"]["indicators"]} == {
            "rsi",
            "bollinger",
            "atr",
        }


def test_strategy_edit_rejects_parameter_expansion_and_cross_tenant_apply(
    mysql_test_engine: Engine,
) -> None:
    client, _ = build_test_client(mysql_test_engine)
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
