from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

from quantdesk_v2 import admin as admin_api
from quantdesk_v2 import battle, prediction_ai_optimizer
from quantdesk_v2 import news as market_news
from quantdesk_v2.config import Settings
from quantdesk_v2.database import get_db
from quantdesk_v2.main import create_app
from quantdesk_v2.models import AdminSetting, AuditLog, NewsSourceSetting, User
from quantdesk_v2.security import CredentialCipher, api_key_fingerprint
from quantdesk_v2.strategy_ai import StrategyAiError


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
        assert regular_algorithm.json()["feature_count"] == 20
        assert regular_algorithm.json()["market_feature_count"] == 8
        assert regular_algorithm.json()["kline_strategy_count"] == 12
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
        for protected_trace_path in (
            "/api/v2/monitor/prediction-algorithm/ai-history",
            "/api/v2/monitor/prediction-algorithm/ai-history/1",
            "/api/v2/monitor/prediction-algorithm/ai-trace",
        ):
            forbidden_trace = client.get(
                protected_trace_path,
                headers={"Authorization": f"Bearer {regular_token}"},
            )
            assert forbidden_trace.status_code == 403
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
        source_map = {item["name"]: item for item in sources.json()}
        assert source_map["金十数据"]["feed_type"] == "taoz_flash"
        assert source_map["东方财富全球"]["feed_type"] == "taoz_flash"
        assert source_map["Unusual Whales"]["feed_type"] == "unusual_whales"
        assert source_map["Unusual Whales"]["enabled"] is False
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
                "feed_type": "rss",
                "lang": "en",
                "enabled": True,
                "slow": True,
                "weight": 120,
                "hourly_limit": 80,
            },
        )
        assert created_source.status_code == 201
        assert created_source.json()["name"] == "TestEditorialFeed"
        assert created_source.json()["feed_type"] == "rss"
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
        page = client.get("/admin", follow_redirects=False)
        login_page = client.get("/admin/login", follow_redirects=False)
        user_page = client.get("/monitor")

    assert page.status_code == 307
    assert page.headers["location"] == "/next/admin/#overview"
    assert page.headers["cache-control"] == "no-store"
    assert login_page.status_code == 307
    assert login_page.headers["location"] == "/next/admin/#overview"
    assert login_page.headers["cache-control"] == "no-store"
    assert user_page.status_code == 200


def test_unusual_whales_market_data_configuration_is_admin_only_and_secret_safe(
    mysql_test_engine: Engine,
) -> None:
    client, test_session = build_admin_client(mysql_test_engine)
    with client:
        admin_id, _ = register_and_login(client, "unusual-whales-admin")
        regular_id, regular_token = register_and_login(
            client, "unusual-whales-regular"
        )
        with test_session() as db:
            db.get(User, admin_id).is_admin = True
            db.commit()

        admin_login = client.post(
            "/api/v2/auth/login",
            json={
                "username": "unusual-whales-admin",
                "password": "correct horse battery staple",
                "client_type": "web",
            },
        )
        admin_token = admin_login.json()["access_token"]
        admin_headers = {
            "Authorization": f"Bearer {admin_token}",
            "X-QuantDesk-User-ID": str(admin_id),
        }
        regular_headers = {
            "Authorization": f"Bearer {regular_token}",
            "X-QuantDesk-User-ID": str(regular_id),
        }

        assert client.get("/api/v2/admin/market-data/unusual-whales").status_code == 401
        assert (
            client.put(
                "/api/v2/admin/market-data/unusual-whales",
                json={},
            ).status_code
            == 401
        )
        assert (
            client.get(
                "/api/v2/admin/market-data/unusual-whales",
                headers=regular_headers,
            ).status_code
            == 403
        )

        initial = client.get(
            "/api/v2/admin/market-data/unusual-whales",
            headers=admin_headers,
        )
        assert initial.status_code == 200
        assert isinstance(initial.json()["configured"], bool)
        assert "api_key" not in initial.json()["config"]
        assert "api_key_encrypted" not in initial.text
        assert initial.json()["health"]["leadership"]["is_leader"] is False
        assert initial.json()["health"]["retention"]["status"] == "pending"

        api_key = "uw-test-secret-abcdef123456"
        payload = {
            "api_key": api_key,
            "mode": "gate",
            "rest_enabled": True,
            "websocket_enabled": True,
            "channels": {
                "price": True,
                "trading_halts": True,
                "interval_flow": True,
                "net_flow": True,
                "market_tide": True,
                "gex": True,
                "lit_trades": True,
                "off_lit_trades": True,
                "flow_alerts": True,
                "option_trades": False,
            },
            "thresholds": {
                "quote_age_regular_ms": 1_500,
                "quote_age_extended_ms": 8_000,
                "spread_hard_max_bps": 65.0,
                "source_divergence_max_bps": 30.0,
                "min_data_coverage": 0.85,
                "event_block_before_minutes": 45,
                "event_block_after_minutes": 20,
                "halt_cooldown_minutes": 20,
            },
            "weights": {
                "news": 0.25,
                "technical": 0.25,
                "market_context": 0.15,
                "options_flow": 0.15,
                "gex": 0.10,
                "institutional_flow": 0.10,
            },
            "retention": {
                "raw_event_days": 21,
                "feature_snapshot_days": 120,
                "cleanup_interval_minutes": 30,
                "cleanup_batch_size": 3_000,
                "cleanup_max_batches": 12,
            },
        }
        forbidden_update = client.put(
            "/api/v2/admin/market-data/unusual-whales",
            headers=regular_headers,
            json=payload,
        )
        assert forbidden_update.status_code == 403

        saved = client.put(
            "/api/v2/admin/market-data/unusual-whales",
            headers=admin_headers,
            json=payload,
        )
        assert saved.status_code == 200
        body = saved.json()
        assert body["configured"] is True
        assert body["credential_source"] == "database"
        assert body["api_key_fingerprint"] == api_key_fingerprint(api_key)
        assert body["version"] == 1
        assert body["config"]["mode"] == "gate"
        assert body["config"]["thresholds"] == payload["thresholds"]
        assert body["config"]["weights"] == payload["weights"]
        assert body["config"]["retention"] == payload["retention"]
        assert "api_key" not in body["config"]
        assert api_key not in saved.text
        assert "api_key_encrypted" not in saved.text

        fetched = client.get(
            "/api/v2/admin/market-data/unusual-whales",
            headers=admin_headers,
        )
        assert fetched.status_code == 200
        assert fetched.json()["config"] == body["config"]
        assert fetched.json()["api_key_fingerprint"] == api_key_fingerprint(api_key)
        assert api_key not in fetched.text
        assert "api_key_encrypted" not in fetched.text

        invalid_weights = {
            **payload,
            "api_key": "",
            "weights": {
                **payload["weights"],
                "institutional_flow": 0.20,
            },
        }
        rejected = client.put(
            "/api/v2/admin/market-data/unusual-whales",
            headers=admin_headers,
            json=invalid_weights,
        )
        assert rejected.status_code == 422

        with test_session() as db:
            policy = db.get(AdminSetting, admin_api.UNUSUAL_WHALES_MARKET_DATA_KEY)
            credential = db.get(
                AdminSetting,
                market_news.UNUSUAL_WHALES_CREDENTIAL_KEY,
            )
            assert policy is not None
            assert policy.version == 1
            assert policy.value_json == body["config"]
            assert "api_key" not in policy.value_json
            assert credential is not None
            assert credential.version == 1
            assert credential.value_json["api_key_fingerprint"] == api_key_fingerprint(
                api_key
            )
            ciphertext = credential.value_json["api_key_encrypted"]
            assert api_key not in ciphertext
            cipher = CredentialCipher(
                client.app.state.settings.credential_master_key.get_secret_value()
            )
            assert cipher.decrypt(ciphertext) == api_key
            audit = (
                db.query(AuditLog)
                .filter(
                    AuditLog.action
                    == "admin.market_data.unusual_whales.update"
                )
                .one()
            )
            assert audit.metadata_json["credential_replaced"] is True
            assert api_key not in str(audit.metadata_json)


def test_ai_monitor_score_policy_updates_only_six_domain_weights(
    mysql_test_engine: Engine,
) -> None:
    client, test_session = build_admin_client(mysql_test_engine)
    with client:
        admin_id, _ = register_and_login(client, "score-policy-admin")
        regular_id, regular_token = register_and_login(client, "score-policy-reader")
        original = {
            "enabled": True,
            "mode": "gate",
            "rest_enabled": False,
            "websocket_enabled": True,
            "channels": {
                "price": True,
                "trading_halts": False,
                "interval_flow": True,
                "net_flow": False,
                "market_tide": True,
                "gex": True,
                "lit_trades": False,
                "off_lit_trades": True,
                "flow_alerts": True,
                "option_trades": False,
            },
            "thresholds": {
                "quote_age_regular_ms": 1_700,
                "quote_age_extended_ms": 9_000,
                "spread_hard_max_bps": 70.0,
                "source_divergence_max_bps": 32.0,
                "min_data_coverage": 0.82,
                "event_block_before_minutes": 40,
                "event_block_after_minutes": 18,
                "halt_cooldown_minutes": 17,
            },
            "weights": {
                "news": 0.30,
                "technical": 0.25,
                "market_context": 0.15,
                "options_flow": 0.12,
                "gex": 0.08,
                "institutional_flow": 0.10,
            },
            "retention": {
                "raw_event_days": 20,
                "feature_snapshot_days": 110,
                "cleanup_interval_minutes": 45,
                "cleanup_batch_size": 2_500,
                "cleanup_max_batches": 11,
            },
        }
        with test_session() as db:
            db.get(User, admin_id).is_admin = True
            db.add(
                AdminSetting(
                    key=admin_api.UNUSUAL_WHALES_MARKET_DATA_KEY,
                    value_json=original,
                    version=7,
                    updated_by=admin_id,
                )
            )
            db.commit()

        admin_login = client.post(
            "/api/v2/auth/login",
            json={
                "username": "score-policy-admin",
                "password": "correct horse battery staple",
                "client_type": "web",
            },
        )
        admin_headers = {
            "Authorization": f"Bearer {admin_login.json()['access_token']}",
            "X-QuantDesk-User-ID": str(admin_id),
        }
        regular_headers = {
            "Authorization": f"Bearer {regular_token}",
            "X-QuantDesk-User-ID": str(regular_id),
        }
        recommended = {
            "news": 0.20,
            "technical": 0.30,
            "market_context": 0.10,
            "options_flow": 0.20,
            "gex": 0.10,
            "institutional_flow": 0.10,
        }

        readable = client.get(
            "/api/v2/ai-monitor/score-policy", headers=regular_headers
        )
        assert readable.status_code == 200
        assert readable.json()["can_edit"] is False
        assert readable.json()["weights"] == original["weights"]
        assert readable.json()["weights_version"] == "uw_weights_v7"
        assert readable.json()["weight_unit"] == "fraction"

        forbidden = client.put(
            "/api/v2/ai-monitor/score-policy",
            headers=regular_headers,
            json={"weights": recommended},
        )
        assert forbidden.status_code == 403
        invalid = client.put(
            "/api/v2/ai-monitor/score-policy",
            headers=admin_headers,
            json={"weights": {**recommended, "gex": 0.20}},
        )
        assert invalid.status_code == 422

        saved = client.put(
            "/api/v2/ai-monitor/score-policy",
            headers=admin_headers,
            json={"weights": recommended},
        )
        assert saved.status_code == 200
        body = saved.json()
        assert body["mode"] == "score"
        assert body["score_enabled"] is True
        assert body["hard_gate_enabled"] is False
        assert body["weights"] == recommended
        assert body["weights_version"] == "uw_weights_v8"
        assert body["weight_total"] == 1.0

        with test_session() as db:
            setting = db.get(
                AdminSetting, admin_api.UNUSUAL_WHALES_MARKET_DATA_KEY
            )
            assert setting is not None
            assert setting.version == 8
            assert setting.value_json["mode"] == "score"
            assert setting.value_json["weights"] == recommended
            for preserved in (
                "enabled",
                "rest_enabled",
                "websocket_enabled",
                "channels",
                "thresholds",
                "retention",
            ):
                assert setting.value_json[preserved] == original[preserved]


def test_deepseek_algorithm_optimization_saves_a_new_version(
    mysql_test_engine: Engine,
    monkeypatch,
) -> None:
    client, test_session = build_admin_client(mysql_test_engine)
    with client:
        admin_id, _ = register_and_login(client, "algorithm-ai-admin")
        with test_session() as db:
            db.get(User, admin_id).is_admin = True
            db.commit()
        login = client.post(
            "/api/v2/auth/login",
            json={
                "username": "algorithm-ai-admin",
                "password": "correct horse battery staple",
                "client_type": "web",
            },
        )
        headers = {
            "Authorization": f"Bearer {login.json()['access_token']}",
            "X-QuantDesk-User-ID": str(admin_id),
        }
        model = client.post(
            "/api/v2/me/ai-model-configs",
            headers=headers,
            json={
                "provider_code": "deepseek",
                "display_name": "DeepSeek 调优",
                "model_name": "deepseek-v4-flash",
                "api_key": "sk-test-deepseek-secret-value",
                "is_enabled": True,
                "is_default": True,
            },
        )
        assert model.status_code == 201

        current = client.get(
            "/api/v2/monitor/prediction-algorithm",
            headers=headers,
        ).json()
        recommended = current["config"]
        recommended["weights"]["5m"]["aggressive_flow"] += 0.01
        recommended["weights"]["5m"]["book_imbalance"] -= 0.01

        def fake_optimize(rows, current_config, **kwargs):
            assert rows == []
            assert kwargs["current_config_version"] == 0
            assert kwargs["api_key"] == "sk-test-deepseek-secret-value"
            assert kwargs["model_name"] == "deepseek-v4-flash"
            assert kwargs["timeout_seconds"] == 120.0
            assert kwargs["max_tokens"] == 16_000
            return {
                "optimizer_key": "deepseek-history-v1",
                "provider_code": "deepseek",
                "model_name": "deepseek-v4-flash",
                "response_model": "deepseek-v4-flash",
                "usage": {"total_tokens": 120},
                "system_fingerprint": "test",
                "source_config_version": 0,
                "sample_count": 30,
                "history_start_ms": 1000,
                "history_end_ms": 2000,
                "optimized_horizon_count": 1,
                "recommendation_available": True,
                "recommended_config": recommended,
                "summary": "测试调优",
                "reasoning_steps": ["读取聚合统计", "生成候选权重", "通过隐藏验证集"],
                "raw_model_output": {"summary": "测试调优", "weights": {}},
                "model_attempts": [
                    {"mode": "thinking", "finish_reason": "stop"}
                ],
                "normalization": {
                    "applied": True,
                    "method": "bounded-simplex-projection",
                    "horizons": [],
                },
                "submitted_prompt": {
                    "model": "deepseek-v4-flash",
                    "system": "test system prompt",
                    "user": '{"task":"test"}',
                    "request_options": {"response_format": {"type": "json_object"}},
                },
                "horizons": [
                    {"horizon": "5m", "status": "optimized"},
                    {"horizon": "15m", "status": "no_validated_improvement"},
                    {"horizon": "1h", "status": "insufficient_samples"},
                ],
                "guardrails": {"automatic_save": True},
            }

        monkeypatch.setattr(
            prediction_ai_optimizer,
            "optimize_prediction_algorithm_with_deepseek",
            fake_optimize,
        )
        optimized = client.post(
            "/api/v2/monitor/prediction-algorithm/optimize",
            headers=headers,
            json={"expected_config_version": 0},
        )
        assert optimized.status_code == 200
        body = optimized.json()
        assert body["saved"] is True
        assert body["source_config_version"] == 0
        assert body["saved_config_version"] == 1
        assert body["algorithm"]["config_version"] == 1
        assert body["algorithm"]["config"]["weights"]["5m"]["aggressive_flow"] == 0.2
        trace = client.get(
            "/api/v2/monitor/prediction-algorithm/ai-trace",
            headers=headers,
        )
        assert trace.status_code == 200
        assert trace.json()["saved_config_version"] == 1
        assert trace.json()["reasoning_steps"][2] == "通过隐藏验证集"
        assert trace.json()["submitted_prompt"]["user"] == '{"task":"test"}'
        assert trace.json()["normalization"]["applied"] is True
        assert trace.json()["raw_model_output"]["summary"] == "测试调优"
        assert trace.json()["database_history_analysis"] == {
            "available": False,
            "reason": "version_history_not_found",
        }
        analysis_history = client.get(
            "/api/v2/monitor/prediction-algorithm/ai-history?limit=50",
            headers=headers,
        )
        assert analysis_history.status_code == 200
        history_body = analysis_history.json()
        assert history_body["total"] == 1
        assert history_body["items"][0]["status"] == "saved"
        assert history_body["items"][0]["source_config_version"] == 0
        assert history_body["items"][0]["saved_config_version"] == 1
        assert history_body["items"][0]["sample_count"] == 30
        assert history_body["items"][0]["total_tokens"] == 120
        audit_id = history_body["items"][0]["audit_id"]
        historical_trace = client.get(
            f"/api/v2/monitor/prediction-algorithm/ai-history/{audit_id}",
            headers=headers,
        )
        assert historical_trace.status_code == 200
        assert historical_trace.json()["audit_id"] == audit_id
        assert historical_trace.json()["summary"] == "测试调优"
        missing_trace = client.get(
            "/api/v2/monitor/prediction-algorithm/ai-history/999999999",
            headers=headers,
        )
        assert missing_trace.status_code == 404

        with test_session() as db:
            setting = db.get(AdminSetting, battle.ALGORITHM_SETTING_KEY)
            assert setting.version == 1
            audit = (
                db.query(AuditLog)
                .filter(AuditLog.action == "monitor.prediction_algorithm.ai_optimize")
                .one()
            )
            assert audit.metadata_json["provider"] == "deepseek"
            assert audit.metadata_json["source_version"] == 0
            assert audit.metadata_json["saved_version"] == 1
            assert audit.metadata_json["reasoning_steps"][0] == "读取聚合统计"
            assert audit.metadata_json["submitted_prompt"]["system"] == "test system prompt"

        def fake_rejected(rows, current_config, **kwargs):
            exc = StrategyAiError("invalid_output")
            exc.trace = {
                "optimizer_key": "deepseek-history-v1",
                "failure_category": "invalid_output",
                "response_model": "deepseek-v4-flash",
                "sample_count": 30,
                "history_start_ms": 1000,
                "history_end_ms": 2000,
                "summary": "返回结构不完整",
                "reasoning_steps": [],
                "raw_model_output": {"summary": "raw"},
                "normalization": None,
                "horizons": [],
                "submitted_prompt": {
                    "model": "deepseek-v4-flash",
                    "system": "rejected system prompt",
                    "user": '{"task":"rejected"}',
                },
                "usage": {"total_tokens": 88},
            }
            raise exc

        monkeypatch.setattr(
            prediction_ai_optimizer,
            "optimize_prediction_algorithm_with_deepseek",
            fake_rejected,
        )
        rejected = client.post(
            "/api/v2/monitor/prediction-algorithm/optimize",
            headers=headers,
            json={"expected_config_version": 1},
        )
        assert rejected.status_code == 502
        rejected_trace = client.get(
            "/api/v2/monitor/prediction-algorithm/ai-trace",
            headers=headers,
        )
        assert rejected_trace.status_code == 200
        assert rejected_trace.json()["status"] == "rejected"
        assert rejected_trace.json()["saved_config_version"] is None
        assert rejected_trace.json()["source_config_version"] == 1
        assert rejected_trace.json()["raw_model_output"]["summary"] == "raw"
        with test_session() as db:
            assert db.get(AdminSetting, battle.ALGORITHM_SETTING_KEY).version == 1
            rejected_audit = (
                db.query(AuditLog)
                .filter(
                    AuditLog.action
                    == "monitor.prediction_algorithm.ai_optimize_rejected"
                )
                .one()
            )
            assert rejected_audit.metadata_json["status"] == "rejected"
