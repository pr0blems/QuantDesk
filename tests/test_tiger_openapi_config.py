from __future__ import annotations

from pathlib import Path

import pytest

from quantdesk_v2.config import Settings


def test_blank_tiger_private_key_path_is_treated_as_unconfigured() -> None:
    settings = Settings(_env_file=None, tiger_openapi_private_key_path="")

    assert settings.tiger_openapi_private_key_path is None
    settings._validate_tiger_quote_settings()


def test_tiger_openapi_credentials_must_be_configured_together(tmp_path: Path) -> None:
    private_key = tmp_path / "tiger_private_key.pem"
    private_key.write_text("test-key", encoding="utf-8")
    settings = Settings(
        _env_file=None,
        tiger_openapi_tiger_id="tiger-id",
        tiger_openapi_private_key_path=private_key,
    )

    with pytest.raises(RuntimeError, match="must be configured together"):
        settings._validate_tiger_quote_settings()


def test_complete_tiger_openapi_credentials_accept_readable_key(tmp_path: Path) -> None:
    private_key = tmp_path / "tiger_private_key.pem"
    private_key.write_text("test-key", encoding="utf-8")
    settings = Settings(
        _env_file=None,
        tiger_openapi_tiger_id="tiger-id",
        tiger_openapi_account="account-id",
        tiger_openapi_private_key_path=private_key,
    )

    settings._validate_tiger_quote_settings()
