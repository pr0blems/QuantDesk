"""Export a deterministic OpenAPI contract without starting application workers."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from unittest.mock import patch

from pydantic import SecretStr

from quantdesk_v2.config import Settings


def _settings() -> Settings:
    return Settings(
        _env_file=None,
        app_env="test",
        database_url="mysql+pymysql://contract:contract@127.0.0.1/quantdesk_contract",
        jwt_secret=SecretStr("contract-jwt-secret-that-is-long-enough"),
        credential_master_key=SecretStr(
            "cXFxcXFxcXFxcXFxcXFxcXFxcXFxcXFxcXFxcXFxcXE="
        ),
        app_cookie_secure=False,
        app_allowed_hosts="testserver",
        app_allowed_origins="http://testserver",
    )


def _isolated_environment(settings: Settings) -> dict[str, str]:
    """Keep the import-time ASGI app from consuming developer or CI secrets."""

    return {
        "APP_ENV": settings.app_env,
        "DATABASE_URL": settings.database_url_value,
        "DB_PASSWORD": settings.db_password.get_secret_value(),
        "DB_SSL_REQUIRED": "false",
        "DB_SSL_VERIFY_IDENTITY": "false",
        "JWT_SECRET": settings.jwt_secret.get_secret_value(),
        "CREDENTIAL_MASTER_KEY": settings.credential_master_key.get_secret_value(),
        "OPENAI_API_KEY": settings.openai_api_key.get_secret_value(),
        "FINNHUB_API_KEY": settings.finnhub_api_key.get_secret_value(),
        "FINNHUB_WEBHOOK_SECRET": settings.finnhub_webhook_secret.get_secret_value(),
        "BINANCE_LIVE_TRADING_ENABLED": "false",
        "FINNHUB_WEBSOCKET_ENABLED": "false",
    }


def _contract() -> str:
    settings = _settings()
    with patch.dict(os.environ, _isolated_environment(settings), clear=False):
        # Import lazily because quantdesk_v2.main constructs the production ASGI
        # app at module import time. The patched environment keeps that side effect
        # deterministic and prevents local credentials from entering the tool.
        from quantdesk_v2.main import create_app

        schema = create_app(settings).openapi()
    return json.dumps(schema, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--output", type=Path)
    mode.add_argument("--check", type=Path)
    args = parser.parse_args()
    contract = _contract().encode("utf-8")
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_bytes(contract)
        return 0
    expected = args.check.read_bytes()
    if expected != contract:
        parser.error(f"OpenAPI contract is stale: {args.check}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
