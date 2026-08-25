from __future__ import annotations

import copy
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
from quantdesk_v2.schemas import StrategySourceComposition
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


def test_source_composition_builds_a_parameter_contract_for_generated_code() -> None:
    composition = StrategySourceComposition.model_validate(
        {
            "indicators": [
                {
                    "key": "ema",
                    "weight": 1.2,
                    "parameters": {"fast_period": 12, "slow_period": 36},
                },
                {
                    "key": "volume_ratio",
                    "weight": 0.8,
                    "parameters": {"period": 24, "min_ratio": 1.4},
                },
            ],
            "timeframe": "1h",
            "directions": ["long", "short"],
            "confirmation_threshold": 65,
            "signal_valid_bars": 2,
        }
    )

    context, schema, parameters = strategy_routes._source_composition_context(
        composition
    )

    assert context["timeframe"] == "1h"
    assert [item["key"] for item in context["selected_indicators"]] == [
        "ema",
        "volume_ratio",
    ]
    assert parameters["ema_fast_period"] == 12
    assert parameters["volume_ratio_min_ratio"] == 1.4
    assert context["required_parameter_keys"] == sorted(parameters)
    assert context["parameter_values"] == parameters
    assert context["parameter_schema"] == schema
    assert {item["key"] for item in schema} == set(parameters)


def test_source_validation_response_exposes_source_owned_parameter_contract() -> None:
    source = '''TIMEFRAMES = ("1h",)
TRIGGER_TIMEFRAME = "1h"
LOOKBACK_BARS = 40
DIRECTIONS = ("long",)
PARAMETERS = {
    "period": {"label": "计算周期", "type": "integer", "default": 21, "min": 2, "max": 200, "step": 1},
}
def evaluate(context, params):
    bars = context["bars"][TRIGGER_TIMEFRAME]
    return {"decision": "HOLD", "evidence": {"period": params["period"], "bars": len(bars)}}
'''

    validation = strategy_routes._source_validation_response(source)

    assert validation["parameter_keys"] == ["period"]
    assert validation["parameters"] == {"period": 21}
    assert validation["parameter_schema"][0]["label"] == "计算周期"


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


def test_full_strategy_code_can_be_validated_and_published_as_a_revision(
    mysql_test_engine: Engine,
) -> None:
    client, session_factory = build_test_client(mysql_test_engine)
    with client:
        headers = register_and_login(client, "strategy-code-owner")
        items = client.get("/api/v2/strategies", headers=headers).json()["items"]
        strategy = next(
            item
            for item in items
            if item["source_template_key"] == "trend_pullback_continuation_v1"
        )
        proposed = copy.deepcopy(strategy["spec"])
        proposed["directions"] = ["long"]
        proposed["exit"]["take_profit_r"] = 3.25
        proposed["exit"]["max_holding_bars"] = 72

        validation = client.post(
            f"/api/v2/strategies/{strategy['id']}/code/validate",
            headers=headers,
            json={"spec": proposed},
        )
        assert validation.status_code == 200
        assert validation.json()["valid"] is True
        assert validation.json()["normalized_spec"]["directions"] == ["long"]
        assert len(validation.json()["spec_hash"]) == 64

        response = client.put(
            f"/api/v2/strategies/{strategy['id']}/code",
            headers=headers,
            json={
                "version": strategy["version"],
                "name": strategy["name"],
                "description": strategy["description"],
                "category": strategy["category"],
                "spec": proposed,
            },
        )
        assert response.status_code == 200
        saved = response.json()
        assert saved["version"] == strategy["version"] + 1
        assert saved["spec"]["directions"] == ["long"]
        assert saved["spec"]["exit"]["take_profit_r"] == 3.25
        assert saved["parameters"] == saved["spec"]["parameters"]
        assert saved["spec_hash"] == validation.json()["spec_hash"]

        with session_factory() as db:
            row = db.scalar(
                select(UserStrategy).where(UserStrategy.public_id == strategy["id"])
            )
            assert row is not None
            revision = db.scalar(
                select(StrategyRevision).where(
                    StrategyRevision.user_strategy_id == row.id,
                    StrategyRevision.version == saved["version"],
                )
            )
            assert revision is not None
            assert revision.change_summary == "手工修改完整策略代码"
            assert revision.spec_hash == saved["spec_hash"]


def test_strategy_code_rejects_unknown_programs_legacy_engines_and_stale_versions(
    mysql_test_engine: Engine,
) -> None:
    client, _ = build_test_client(mysql_test_engine)
    with client:
        headers = register_and_login(client, "strategy-code-guard")
        items = client.get("/api/v2/strategies", headers=headers).json()["items"]
        full = next(item for item in items if item["strategy_kind"] == "full_strategy")
        legacy = next(item for item in items if item["strategy_kind"] == "legacy_signal")

        unsafe = copy.deepcopy(full["spec"])
        unsafe["python"] = "import os"
        invalid = client.post(
            f"/api/v2/strategies/{full['id']}/code/validate",
            headers=headers,
            json={"spec": unsafe},
        )
        assert invalid.status_code == 422

        unsupported = client.post(
            f"/api/v2/strategies/{legacy['id']}/code/validate",
            headers=headers,
            json={"spec": full["spec"]},
        )
        assert unsupported.status_code == 409

        stale = client.put(
            f"/api/v2/strategies/{full['id']}/code",
            headers=headers,
            json={
                "version": full["version"] + 99,
                "name": full["name"],
                "description": full["description"],
                "category": full["category"],
                "spec": full["spec"],
            },
        )
        assert stale.status_code == 409


def test_ai_strategy_code_preview_is_validated_before_reaching_the_editor(
    mysql_test_engine: Engine,
) -> None:
    client, _ = build_test_client(mysql_test_engine)
    with client:
        headers = register_and_login(client, "strategy-code-ai")
        items = client.get("/api/v2/strategies", headers=headers).json()["items"]
        strategy = next(item for item in items if item["strategy_kind"] == "full_strategy")

        response = client.post(
            f"/api/v2/strategies/{strategy['id']}/code/ai-preview",
            headers=headers,
            json={
                "prompt": "只做多，take_profit_r = 3.5，max_holding_bars = 72",
                "spec": strategy["spec"],
            },
        )
        assert response.status_code == 200
        preview = response.json()
        assert preview["provider"] == "local_semantic"
        assert preview["base_version"] == strategy["version"]
        assert preview["proposed_spec"]["directions"] == ["long"]
        assert preview["proposed_spec"]["exit"]["take_profit_r"] == 3.5
        assert preview["proposed_spec"]["exit"]["max_holding_bars"] == 72
        assert preview["spec_hash"]
        assert {change["path"] for change in preview["changes"]} >= {
            "spec.directions",
            "spec.exit.take_profit_r",
            "spec.exit.max_holding_bars",
        }


def test_source_strategy_created_from_indicator_composition_keeps_tunable_parameters(
    mysql_test_engine: Engine,
) -> None:
    client, session_factory = build_test_client(mysql_test_engine)
    source = '''"""EMA 与成交量确认的可编辑策略。"""
TIMEFRAMES = ("1h",)
TRIGGER_TIMEFRAME = "1h"
LOOKBACK_BARS = 120
DIRECTIONS = ("long", "short")
VALID_FOR_BARS = 2

def evaluate(context, params):
    bars = context["bars"][TRIGGER_TIMEFRAME]
    closes = [bar["close"] for bar in bars]
    volumes = [bar["volume"] for bar in bars]
    fast = ema(closes, int(params["ema_fast_period"]))
    slow = ema(closes, int(params["ema_slow_period"]))
    volume_period = int(params["volume_ratio_period"])
    average_volume = sma(volumes[:-1], volume_period)
    volume_ratio = volumes[-1] / average_volume if average_volume > 0 else 0
    score = float(params["ema_weight"]) if fast > slow else 0
    passed = volume_ratio >= float(params["volume_ratio_min_ratio"])
    if passed:
        score = score + float(params["volume_ratio_weight"])
    threshold = float(params["confirmation_threshold"])
    risk_atr = atr(bars, int(params["risk_atr_period"]))
    evidence = {"score": score, "threshold": threshold, "atr": risk_atr, "valid": params["signal_valid_bars"]}
    if fast > slow and passed:
        return {"decision": "LONG_ENTRY", "confidence": 0.7, "evidence": evidence}
    if fast < slow and passed:
        return {"decision": "SHORT_ENTRY", "confidence": 0.7, "evidence": evidence}
    return {"decision": "HOLD", "evidence": evidence}
'''
    with client:
        headers = register_and_login(client, "strategy-source-composer")
        response = client.post(
            "/api/v2/strategies/source",
            headers=headers,
            json={
                "name": "EMA 成交量 Python 策略",
                "description": "由指标选择与自然语言生成。",
                "category": "源码策略",
                "language": "python",
                "source_code": source,
                "composition": {
                    "indicators": [
                        {
                            "key": "ema",
                            "weight": 1.2,
                            "parameters": {"fast_period": 12, "slow_period": 36},
                        },
                        {
                            "key": "volume_ratio",
                            "weight": 0.8,
                            "parameters": {"period": 24, "min_ratio": 1.4},
                        },
                    ],
                    "timeframe": "1h",
                    "directions": ["long", "short"],
                    "confirmation_threshold": 65,
                    "signal_valid_bars": 2,
                },
            },
        )

        assert response.status_code == 201
        saved = response.json()
        assert saved["strategy_kind"] == "source_strategy"
        assert saved["parameters"]["ema_fast_period"] == 12
        assert saved["parameters"]["volume_ratio_min_ratio"] == 1.4
        assert saved["parameters"]["confirmation_threshold"] == 65
        assert set(saved["source_validation"]["parameter_keys"]) == set(saved["parameters"])

        with session_factory() as db:
            row = db.scalar(
                select(UserStrategy).where(UserStrategy.public_id == saved["id"])
            )
            assert row is not None
            revision = db.scalar(
                select(StrategyRevision).where(
                    StrategyRevision.user_strategy_id == row.id,
                    StrategyRevision.version == 1,
                )
            )
            assert revision is not None
            assert revision.change_source == "ai"
