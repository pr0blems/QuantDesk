from __future__ import annotations

from pathlib import Path

STATIC_DIR = Path(__file__).parents[1] / "src" / "quantdesk_v2" / "static"


def _asset(name: str) -> str:
    return (STATIC_DIR / name).read_text(encoding="utf-8")


def test_api_credentials_is_a_second_level_settings_section() -> None:
    html = _asset("index.html")

    settings_panel = html.index('data-panel="settings"')
    second_level_navigation = html.index('aria-label="系统设置二级导航"')
    api_credentials = html.index('id="settings-api-credentials"')
    binance_credentials = html.index('id="binance-credential-title"')
    ai_models = html.index('id="ai-model-settings-title"')

    assert settings_panel < second_level_navigation < api_credentials
    assert api_credentials < binance_credentials < ai_models
    assert 'data-settings-section="api-credentials"' in html
    assert "API 凭证" in html[second_level_navigation:api_credentials]


def test_ai_model_form_covers_supported_providers_and_secret_handling() -> None:
    html = _asset("index.html")
    provider_strip = html.index('class="ai-provider-strip"')
    list_status = html.index('id="ai-model-list-status"')
    provider_names = ("DeepSeek", "豆包", "千问", "Kimi", "MiniMax", "OpenAI")

    for provider_name in provider_names:
        assert provider_name in html[provider_strip:list_status]

    dialog = html[html.index('id="ai-model-dialog"') :]
    assert 'name="provider_code"' in dialog
    assert 'name="display_name"' in dialog
    assert 'name="model_name"' in dialog
    assert 'name="api_key" type="password"' in dialog
    assert 'name="is_enabled" type="checkbox"' in dialog
    assert 'name="is_default" type="checkbox"' in dialog
    assert 'name="base_url"' not in dialog
    assert "策略快照与修改指令会发送给所选模型服务商" in html


def test_ai_model_frontend_uses_user_scoped_crud_contract_without_key_echo() -> None:
    script = _asset("app.js")

    assert 'api("/api/v2/me/ai-model-providers")' in script
    assert 'api("/api/v2/me/ai-model-configs")' in script
    assert 'method: configId ? "PUT" : "POST"' in script
    assert 'method: "DELETE"' in script
    assert 'if (apiKey) payload.api_key = apiKey;' in script
    assert "更换服务商时必须输入对应的新 API Key" in script

    payload = script[
        script.index("const payload = {", script.index("async function saveAiModelConfig")) :
        script.index("if (apiKey) payload.api_key", script.index("async function saveAiModelConfig"))
    ]
    assert "base_url" not in payload

    renderer = script[
        script.index("function renderAiModelConfigs") :
        script.index("function renderAiModelProviderOptions")
    ]
    assert "document.createElement" in script
    assert "textContent" in script
    assert "innerHTML" not in renderer
    assert "api_key_fingerprint" in script


def test_ai_model_settings_are_responsive() -> None:
    style = _asset("style.css")

    assert ".settings-layout { display: grid" in style
    assert ".ai-model-config-card" in style
    assert ".ai-model-dialog::backdrop" in style
    assert "@media (max-width: 980px)" in style
    assert ".settings-layout { grid-template-columns: 1fr; }" in style
    assert "@media (max-width: 780px)" in style
    assert ".ai-model-field-grid, .ai-model-switches { grid-template-columns: 1fr; }" in style
