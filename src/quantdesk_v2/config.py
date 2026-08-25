from __future__ import annotations

import base64
from functools import lru_cache
from pathlib import Path
from urllib.parse import quote_plus, urlparse

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    app_env: str = "development"
    app_host: str = "127.0.0.1"
    app_port: int = 8200
    app_allowed_hosts: str = "127.0.0.1,localhost,testserver"
    app_allowed_origins: str = "http://127.0.0.1:8200,http://localhost:8200"
    app_cookie_secure: bool = False
    max_request_bytes: int = 1_048_576

    database_url: str | None = None
    db_host: str = "127.0.0.1"
    db_port: int = 3306
    db_name: str = "quantdesk"
    db_user: str = "quantdesk"
    db_password: SecretStr = SecretStr("")
    db_ssl_required: bool = True
    db_ssl_verify_identity: bool = True
    db_ssl_ca: str | None = None

    jwt_secret: SecretStr = SecretStr("")
    credential_master_key: SecretStr = SecretStr("")
    access_token_minutes: int = 15
    refresh_token_days: int = 30
    refresh_cookie_name: str = "quantdesk_refresh"
    allow_public_registration: bool = True

    binance_futures_base_url: str = "https://fapi.binance.com"
    binance_portfolio_base_url: str = "https://papi.binance.com"
    binance_futures_timeout_seconds: float = 10.0
    binance_futures_recv_window_ms: int = 5_000
    # Process-wide kill switch.  A user must still explicitly arm one paused
    # deployment before any signed TRADE request can be emitted.
    binance_live_trading_enabled: bool = False
    binance_live_trading_interval_seconds: int = 30

    # Strategy edits are requested server-side only. The browser never receives
    # this credential and the client uses a fixed OpenAI HTTPS origin.
    openai_api_key: SecretStr = SecretStr("")
    openai_strategy_model: str = "gpt-5.6-luna"
    openai_strategy_timeout_seconds: float = 20.0
    openai_strategy_source_timeout_seconds: float = 60.0
    deepseek_optimizer_timeout_seconds: float = 120.0
    deepseek_optimizer_max_tokens: int = 16_000

    # Finnhub is called only by the server. Never expose this token to static
    # JavaScript or accept an arbitrary upstream URL from a request.
    finnhub_api_key: SecretStr = SecretStr("")
    finnhub_webhook_secret: SecretStr = SecretStr("")
    finnhub_base_url: str = "https://finnhub.io"
    finnhub_timeout_seconds: float = 5.0
    finnhub_market_status_cache_seconds: int = 30
    finnhub_market_status_stale_seconds: int = 900
    finnhub_quote_poll_seconds: float = 2.0
    finnhub_quote_stale_seconds: int = 600
    finnhub_websocket_enabled: bool = True

    # News-source rows contain the endpoint and polling policy, never this secret.
    unusual_whales_api_key: SecretStr = SecretStr("")

    # Read-only external feed of completed AI news analyses. The secret is
    # server-side only and may be overridden with EXTERNAL_NEWS_API_KEY.
    external_news_api_key: SecretStr = SecretStr("")
    external_news_ws_poll_seconds: float = 2.0

    monitor_symbols_config: Path = Path("config/tradfi_symbols.json")
    # Development/API-only instances can share a production database without
    # competing with the deployed AI Monitor scheduler. Keep production
    # behavior enabled by default; local operators can explicitly disable only
    # this worker with AI_MONITOR_BACKGROUND_WORKERS_ENABLED=false.
    ai_monitor_background_workers_enabled: bool = True

    @property
    def database_url_value(self) -> str:
        if self.database_url:
            url = self.database_url
            scheme = urlparse(url).scheme.lower()
            if scheme not in {"mysql+pymysql", "mariadb+pymysql"}:
                raise RuntimeError("DATABASE_URL must use the MySQL PyMySQL driver")
            return url
        password = quote_plus(self.db_password.get_secret_value())
        user = quote_plus(self.db_user)
        return f"mysql+pymysql://{user}:{password}@{self.db_host}:{self.db_port}/{self.db_name}?charset=utf8mb4"

    @property
    def allowed_hosts(self) -> list[str]:
        return [item.strip() for item in self.app_allowed_hosts.split(",") if item.strip()]

    @property
    def allowed_origins(self) -> list[str]:
        return [item.strip() for item in self.app_allowed_origins.split(",") if item.strip()]

    def validate_runtime(self) -> None:
        # Resolve the URL up front so unsupported database backends fail before startup.
        _ = self.database_url_value
        if not self.database_url and not self.db_password.get_secret_value():
            raise RuntimeError("DB_PASSWORD is required")
        if len(self.jwt_secret.get_secret_value()) < 32:
            raise RuntimeError("JWT_SECRET must contain at least 32 characters")
        self._validate_fernet_key()
        if "*" in self.allowed_origins:
            raise RuntimeError("Wildcard CORS origins are forbidden")
        self._validate_binance_futures_settings()
        self._validate_openai_settings()
        self._validate_finnhub_settings()
        external_news_api_key = self.external_news_api_key.get_secret_value()
        if external_news_api_key and len(external_news_api_key) < 12:
            raise RuntimeError("EXTERNAL_NEWS_API_KEY must contain at least 12 characters")
        if not 0.5 <= self.external_news_ws_poll_seconds <= 30:
            raise RuntimeError(
                "EXTERNAL_NEWS_WS_POLL_SECONDS must be between 0.5 and 30"
            )
        if self.app_env.lower() == "production":
            if not self.db_ssl_required:
                raise RuntimeError("Production database connections must require TLS")
            if not self.db_ssl_verify_identity or not self.db_ssl_ca:
                raise RuntimeError("Production database TLS must verify identity with DB_SSL_CA")
            if not self.app_cookie_secure:
                raise RuntimeError("Production cookies must be Secure")

    def _validate_fernet_key(self) -> None:
        raw = self.credential_master_key.get_secret_value().encode("ascii", errors="ignore")
        try:
            decoded = base64.urlsafe_b64decode(raw)
        except Exception as exc:
            raise RuntimeError("CREDENTIAL_MASTER_KEY is not valid URL-safe base64") from exc
        if len(decoded) != 32:
            raise RuntimeError("CREDENTIAL_MASTER_KEY must decode to 32 bytes")

    def _validate_binance_futures_settings(self) -> None:
        self._validate_binance_origin(
            "BINANCE_FUTURES_BASE_URL",
            self.binance_futures_base_url,
            {"fapi.binance.com", "demo-fapi.binance.com"},
        )
        self._validate_binance_origin(
            "BINANCE_PORTFOLIO_BASE_URL",
            self.binance_portfolio_base_url,
            {"papi.binance.com"},
        )
        if not 1 <= self.binance_futures_timeout_seconds <= 10:
            raise RuntimeError("BINANCE_FUTURES_TIMEOUT_SECONDS must be between 1 and 10")
        if not 1_000 <= self.binance_futures_recv_window_ms <= 5_000:
            raise RuntimeError("BINANCE_FUTURES_RECV_WINDOW_MS must be between 1000 and 5000")
        if not 10 <= self.binance_live_trading_interval_seconds <= 300:
            raise RuntimeError(
                "BINANCE_LIVE_TRADING_INTERVAL_SECONDS must be between 10 and 300"
            )

    def _validate_openai_settings(self) -> None:
        if self.openai_strategy_model not in {
            "gpt-5.6-sol",
            "gpt-5.6-terra",
            "gpt-5.6-luna",
        }:
            raise RuntimeError("OPENAI_STRATEGY_MODEL must be an approved GPT-5.6 model")
        if not 2 <= self.openai_strategy_timeout_seconds <= 30:
            raise RuntimeError("OPENAI_STRATEGY_TIMEOUT_SECONDS must be between 2 and 30")
        if not 10 <= self.openai_strategy_source_timeout_seconds <= 180:
            raise RuntimeError(
                "OPENAI_STRATEGY_SOURCE_TIMEOUT_SECONDS must be between 10 and 180"
            )

    def _validate_finnhub_settings(self) -> None:
        parsed = urlparse(self.finnhub_base_url)
        try:
            port = parsed.port
        except ValueError:
            port = -1
        if (
            parsed.scheme.lower() != "https"
            or parsed.hostname != "finnhub.io"
            or parsed.username is not None
            or parsed.password is not None
            or port not in (None, 443)
            or parsed.path not in ("", "/")
            or parsed.query
            or parsed.fragment
        ):
            raise RuntimeError("FINNHUB_BASE_URL must be the official Finnhub HTTPS origin")
        if not 1 <= self.finnhub_timeout_seconds <= 10:
            raise RuntimeError("FINNHUB_TIMEOUT_SECONDS must be between 1 and 10")
        if not 5 <= self.finnhub_market_status_cache_seconds <= 300:
            raise RuntimeError(
                "FINNHUB_MARKET_STATUS_CACHE_SECONDS must be between 5 and 300"
            )
        if not self.finnhub_market_status_cache_seconds <= (
            self.finnhub_market_status_stale_seconds
        ) <= 3_600:
            raise RuntimeError(
                "FINNHUB_MARKET_STATUS_STALE_SECONDS must be between the cache TTL and 3600"
            )
        if not 1 <= self.finnhub_quote_poll_seconds <= 10:
            raise RuntimeError("FINNHUB_QUOTE_POLL_SECONDS must be between 1 and 10")
        if not 60 <= self.finnhub_quote_stale_seconds <= 3_600:
            raise RuntimeError("FINNHUB_QUOTE_STALE_SECONDS must be between 60 and 3600")
        webhook_secret = self.finnhub_webhook_secret.get_secret_value()
        if webhook_secret and not 16 <= len(webhook_secret) <= 256:
            raise RuntimeError("FINNHUB_WEBHOOK_SECRET must contain 16 to 256 characters")

    @staticmethod
    def _validate_binance_origin(name: str, value: str, allowed_hosts: set[str]) -> None:
        parsed = urlparse(value)
        try:
            port = parsed.port
        except ValueError:
            port = -1
        if (
            parsed.scheme.lower() != "https"
            or parsed.hostname not in allowed_hosts
            or parsed.username is not None
            or parsed.password is not None
            or port not in (None, 443)
            or parsed.path not in ("", "/")
            or parsed.query
            or parsed.fragment
        ):
            raise RuntimeError(f"{name} must be an approved Binance HTTPS origin")

    @property
    def static_dir(self) -> Path:
        return Path(__file__).resolve().parent / "static"

    @property
    def react_static_dir(self) -> Path:
        """Optional production build of the incrementally migrated React UI."""

        return Path(__file__).resolve().parent / "react_static"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
