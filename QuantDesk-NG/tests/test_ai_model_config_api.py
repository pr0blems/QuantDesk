from __future__ import annotations

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from pydantic import BaseModel, Field, SecretStr, ValidationError
from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

from quantdesk_v2.ai_providers import AI_PROVIDER_PRESETS
from quantdesk_v2.config import Settings
from quantdesk_v2.database import get_db
from quantdesk_v2.main import create_app
from quantdesk_v2.models import AiModelConfig, AuditLog
from quantdesk_v2.schemas import AiModelConfigCreate, AiModelConfigUpdate
from quantdesk_v2.security import CredentialCipher


class _SensitiveValidationPayload(BaseModel):
    api_secret: str = Field(min_length=16)
    password: str = Field(min_length=16)
    access_token: str = Field(min_length=16)


def _build_test_client(mysql_test_engine: Engine):
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
        bind=mysql_test_engine,
        autoflush=False,
        expire_on_commit=False,
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


def _register_and_login(client: TestClient, username: str) -> tuple[int, str]:
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


def _headers(user_id: int, access_token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {access_token}",
        "X-QuantDesk-User-ID": str(user_id),
    }


def test_provider_registry_uses_only_fixed_https_origins() -> None:
    assert set(AI_PROVIDER_PRESETS) == {
        "openai",
        "deepseek",
        "doubao",
        "qwen",
        "kimi",
        "minimax",
    }
    for code, preset in AI_PROVIDER_PRESETS.items():
        assert preset.code == code
        assert preset.base_url.startswith("https://")
        assert "://" not in preset.host
        assert preset.path.startswith("/")
        assert preset.default_model in preset.models


def test_ai_model_schemas_forbid_user_controlled_origins_and_blank_key_updates() -> None:
    try:
        AiModelConfigCreate.model_validate(
            {
                "provider_code": "deepseek",
                "display_name": "DeepSeek",
                "model_name": "deepseek-v4-flash",
                "api_key": "secret-key-value",
                "base_url": "http://127.0.0.1:3306",
            }
        )
    except ValidationError as exc:
        assert exc.errors()[0]["type"] == "extra_forbidden"
    else:
        raise AssertionError("base_url must never be accepted from a user payload")

    assert AiModelConfigUpdate(api_key="", display_name="保留原密钥").api_key is None
    try:
        AiModelConfigUpdate()
    except ValidationError:
        pass
    else:
        raise AssertionError("empty updates must not produce misleading audit entries")


def test_ai_model_openapi_never_accepts_or_returns_secrets() -> None:
    settings = Settings(
        _env_file=None,
        app_env="test",
        database_url="mysql+pymysql://test:test@127.0.0.1/quantdesk_test_unavailable",
        jwt_secret=SecretStr("test-jwt-secret-that-is-long-enough-123456"),
        credential_master_key=SecretStr(Fernet.generate_key().decode("ascii")),
        app_cookie_secure=False,
        app_allowed_hosts="testserver",
        app_allowed_origins="http://testserver",
    )
    schemas = create_app(settings).openapi()["components"]["schemas"]
    create_fields = schemas["AiModelConfigCreate"]["properties"]
    update_fields = schemas["AiModelConfigUpdate"]["properties"]
    output_fields = schemas["AiModelConfigOut"]["properties"]

    assert "base_url" not in create_fields
    assert "base_url" not in update_fields
    assert "api_key" in create_fields
    assert "api_key" not in output_fields
    assert "api_key_encrypted" not in output_fields
    assert "api_key_fingerprint" in output_fields


def test_validation_errors_redact_secrets_but_keep_normal_422_details() -> None:
    settings = Settings(
        _env_file=None,
        app_env="test",
        database_url="mysql+pymysql://test:test@127.0.0.1/quantdesk_test_unavailable",
        jwt_secret=SecretStr("test-jwt-secret-that-is-long-enough-123456"),
        credential_master_key=SecretStr(Fernet.generate_key().decode("ascii")),
        app_cookie_secure=False,
        app_allowed_hosts="testserver",
        app_allowed_origins="http://testserver",
    )
    app = create_app(settings)

    @app.post("/_test/ai-model-validation")
    def validate_ai_model_payload(payload: AiModelConfigCreate) -> dict[str, bool]:
        return {"valid": bool(payload.api_key.get_secret_value())}

    @app.post("/_test/sensitive-validation")
    def validate_sensitive_payload(payload: _SensitiveValidationPayload) -> dict[str, bool]:
        return {"valid": bool(payload.access_token)}

    client = TestClient(app)
    invalid_key = "LEAKED AI KEY WITH SPACES"
    field_error = client.post(
        "/_test/ai-model-validation",
        json={
            "provider_code": "deepseek",
            "display_name": "DeepSeek",
            "model_name": "deepseek-v4-flash",
            "api_key": invalid_key,
        },
    )
    assert field_error.status_code == 422
    assert invalid_key not in field_error.text
    assert field_error.json()["detail"][0]["input"] == "[REDACTED]"
    assert field_error.headers["cache-control"] == "no-store"

    model_secret = "valid-secret-key-value"
    model_error = client.post(
        "/_test/ai-model-validation",
        json={
            "provider_code": "deepseek",
            "display_name": "DeepSeek",
            "model_name": "deepseek-v4-flash",
            "api_key": model_secret,
            "is_enabled": False,
            "is_default": True,
        },
    )
    assert model_error.status_code == 422
    assert model_secret not in model_error.text
    assert model_error.json()["detail"][0]["input"]["api_key"] == "[REDACTED]"

    normal_error = client.post(
        "/_test/ai-model-validation",
        json={
            "provider_code": "deepseek",
            "display_name": "DeepSeek",
            "model_name": "invalid model name",
            "api_key": model_secret,
        },
    )
    normal_detail = normal_error.json()["detail"][0]
    assert normal_detail["loc"][-1] == "model_name"
    assert normal_detail["input"] == "invalid model name"
    assert normal_detail["type"] == "string_pattern_mismatch"

    malformed = client.post(
        "/_test/ai-model-validation",
        content=f'{{"api_key":"{model_secret}"',
        headers={"Content-Type": "application/json"},
    )
    assert malformed.status_code == 422
    assert model_secret not in malformed.text
    assert malformed.json()["detail"][0]["input"] == "[REDACTED]"

    sensitive_values = {
        "api_secret": "LEAK-API-SECRET",
        "password": "LEAK-PASSWORD",
        "access_token": "LEAK-TOKEN",
    }
    sensitive_error = client.post(
        "/_test/sensitive-validation",
        json=sensitive_values,
    )
    assert sensitive_error.status_code == 422
    assert all(value not in sensitive_error.text for value in sensitive_values.values())
    assert {item["input"] for item in sensitive_error.json()["detail"]} == {"[REDACTED]"}


def test_ai_model_config_crud_is_encrypted_audited_and_tenant_isolated(
    mysql_test_engine: Engine,
) -> None:
    client, test_session, master_key = _build_test_client(mysql_test_engine)
    first_key = "sk-first-user-secret-value"
    second_key = "sk-second-model-secret-value"
    reset_provider_key = "sk-openai-replacement-secret-value"
    explicit_model_key = "sk-qwen-explicit-model-secret-value"

    with client:
        first_user_id, first_access = _register_and_login(client, "ai-owner-one")
        second_user_id, second_access = _register_and_login(client, "ai-owner-two")
        first_headers = _headers(first_user_id, first_access)
        second_headers = _headers(second_user_id, second_access)

        providers = client.get(
            "/api/v2/me/ai-model-providers",
            headers={"Authorization": f"Bearer {first_access}"},
        )
        assert providers.status_code == 200
        assert {item["code"] for item in providers.json()} == set(AI_PROVIDER_PRESETS)

        rejected_origin = client.post(
            "/api/v2/me/ai-model-configs",
            headers=first_headers,
            json={
                "provider_code": "deepseek",
                "display_name": "blocked origin",
                "model_name": "deepseek-v4-flash",
                "api_key": first_key,
                "base_url": "http://169.254.169.254/latest/meta-data",
            },
        )
        assert rejected_origin.status_code == 422

        first = client.post(
            "/api/v2/me/ai-model-configs",
            headers=first_headers,
            json={
                "provider_code": "deepseek",
                "display_name": "我的 DeepSeek",
                "model_name": "deepseek-v4-flash",
                "api_key": first_key,
                "is_enabled": True,
                "is_default": False,
            },
        )
        assert first.status_code == 201
        first_body = first.json()
        assert first_body["is_default"] is True
        assert first_body["base_url"] == "https://api.deepseek.com"
        assert first_body["api_key_configured"] is True
        assert first_key not in first.text
        assert "api_key_encrypted" not in first.text

        second = client.post(
            "/api/v2/me/ai-model-configs",
            headers=first_headers,
            json={
                "provider_code": "qwen",
                "display_name": "我的千问",
                "model_name": "qwen-plus",
                "api_key": second_key,
                "is_enabled": True,
                "is_default": True,
            },
        )
        assert second.status_code == 201
        second_body = second.json()
        assert second_body["is_default"] is True
        assert second_key not in second.text

        first_after_second = client.get(
            "/api/v2/me/ai-model-configs",
            headers={"Authorization": f"Bearer {first_access}"},
        )
        assert first_after_second.status_code == 200
        assert sum(item["is_default"] for item in first_after_second.json()) == 1
        assert {item["id"] for item in first_after_second.json()} == {
            first_body["id"],
            second_body["id"],
        }

        second_user_list = client.get(
            "/api/v2/me/ai-model-configs",
            headers={"Authorization": f"Bearer {second_access}"},
        )
        assert second_user_list.status_code == 200
        assert second_user_list.json() == []
        cross_tenant_update = client.put(
            f"/api/v2/me/ai-model-configs/{first_body['id']}",
            headers=second_headers,
            json={"display_name": "stolen"},
        )
        cross_tenant_delete = client.delete(
            f"/api/v2/me/ai-model-configs/{first_body['id']}",
            headers=second_headers,
        )
        assert cross_tenant_update.status_code == 404
        assert cross_tenant_delete.status_code == 404

        disabled_first = client.post(
            "/api/v2/me/ai-model-configs",
            headers=second_headers,
            json={
                "provider_code": "kimi",
                "display_name": "停用的 Kimi",
                "model_name": "kimi-k3",
                "api_key": "sk-disabled-kimi-secret-value",
                "is_enabled": False,
                "is_default": False,
            },
        )
        assert disabled_first.status_code == 201
        assert disabled_first.json()["is_default"] is False
        enabled_after_disabled = client.post(
            "/api/v2/me/ai-model-configs",
            headers=second_headers,
            json={
                "provider_code": "minimax",
                "display_name": "启用的 MiniMax",
                "model_name": "MiniMax-M2.7",
                "api_key": "sk-enabled-minimax-secret-value",
                "is_enabled": True,
                "is_default": False,
            },
        )
        assert enabled_after_disabled.status_code == 201
        assert enabled_after_disabled.json()["is_default"] is True

        provider_without_new_key = client.put(
            f"/api/v2/me/ai-model-configs/{first_body['id']}",
            headers=first_headers,
            json={"provider_code": "qwen"},
        )
        assert provider_without_new_key.status_code == 422

        provider_with_new_key = client.put(
            f"/api/v2/me/ai-model-configs/{first_body['id']}",
            headers=first_headers,
            json={"provider_code": "openai", "api_key": reset_provider_key},
        )
        assert provider_with_new_key.status_code == 200
        assert provider_with_new_key.json()["provider_code"] == "openai"
        assert (
            provider_with_new_key.json()["model_name"]
            == AI_PROVIDER_PRESETS["openai"].default_model
        )

        provider_with_explicit_model = client.put(
            f"/api/v2/me/ai-model-configs/{first_body['id']}",
            headers=first_headers,
            json={
                "provider_code": "qwen",
                "model_name": "workspace/custom-qwen-v2",
                "api_key": explicit_model_key,
            },
        )
        assert provider_with_explicit_model.status_code == 200
        assert provider_with_explicit_model.json()["provider_code"] == "qwen"
        assert provider_with_explicit_model.json()["model_name"] == "workspace/custom-qwen-v2"

        made_default = client.put(
            f"/api/v2/me/ai-model-configs/{first_body['id']}",
            headers=first_headers,
            json={"api_key": "", "is_default": True},
        )
        assert made_default.status_code == 200
        assert made_default.json()["is_default"] is True
        assert (
            made_default.json()["api_key_fingerprint"]
            == provider_with_explicit_model.json()["api_key_fingerprint"]
        )

        disabled = client.put(
            f"/api/v2/me/ai-model-configs/{first_body['id']}",
            headers=first_headers,
            json={"is_enabled": False},
        )
        assert disabled.status_code == 200
        assert disabled.json()["is_default"] is False
        listed = client.get(
            "/api/v2/me/ai-model-configs",
            headers={"Authorization": f"Bearer {first_access}"},
        ).json()
        assert next(item for item in listed if item["id"] == second_body["id"])["is_default"]

        deleted = client.delete(
            f"/api/v2/me/ai-model-configs/{second_body['id']}",
            headers=first_headers,
        )
        assert deleted.status_code == 204
        assert deleted.content == b""

        enabled_without_default = client.put(
            f"/api/v2/me/ai-model-configs/{first_body['id']}",
            headers=first_headers,
            json={"is_enabled": True},
        )
        assert enabled_without_default.status_code == 200
        assert enabled_without_default.json()["is_default"] is True

        with test_session() as db:
            saved = db.scalar(
                select(AiModelConfig).where(AiModelConfig.public_id == first_body["id"])
            )
            assert saved is not None
            assert saved.user_id == first_user_id
            assert saved.api_key_encrypted != first_key
            assert (
                CredentialCipher(master_key).decrypt(saved.api_key_encrypted) == explicit_model_key
            )
            actions = set(
                db.scalars(
                    select(AuditLog.action).where(
                        AuditLog.user_id == first_user_id,
                        AuditLog.resource_type == "ai_model_config",
                    )
                )
            )
            assert {
                "ai_model_config.create",
                "ai_model_config.update",
                "ai_model_config.delete",
            } <= actions
