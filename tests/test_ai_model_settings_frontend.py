from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).parents[1]
SETTINGS_PAGE = ROOT / "web" / "src" / "pages" / "SettingsPage.tsx"
APP_STYLES = ROOT / "src" / "quantdesk_v2" / "static" / "style.css"


def test_api_credentials_and_ai_models_share_the_react_settings_page() -> None:
    page = SETTINGS_PAGE.read_text(encoding="utf-8")

    assert "API CREDENTIALS" in page
    assert "AI MODEL PROVIDERS" in page
    assert page.index("API CREDENTIALS") < page.index("AI MODEL PROVIDERS")
    assert 'name="api_key"' in page
    assert 'name="api_secret"' in page
    assert "deleteCredentials" in page


def test_ai_model_form_covers_supported_providers_and_secret_handling() -> None:
    page = SETTINGS_PAGE.read_text(encoding="utf-8")

    for provider_name in ("DeepSeek", "OpenAI"):
        assert provider_name in page
    for provider_code in ("doubao", "qwen", "kimi", "minimax"):
        assert provider_code in page
    assert 'name="provider_code"' in page
    assert 'name="display_name"' in page
    assert 'name="model_name"' in page
    assert 'name="api_key"' in page
    assert 'type="password"' in page
    assert 'name="is_enabled"' in page
    assert 'name="is_default"' in page
    assert "if (apiKey) payload.api_key = apiKey;" in page
    assert "apiKeyFingerprint" in page
    assert "base_url:" not in page


def test_ai_model_settings_are_responsive() -> None:
    style = APP_STYLES.read_text(encoding="utf-8")

    assert ".settings-layout" in style
    assert ".ai-model-config-card" in style
    assert ".ai-model-dialog::backdrop" in style
    assert "@media (max-width: 980px)" in style
    assert "@media (max-width: 780px)" in style
