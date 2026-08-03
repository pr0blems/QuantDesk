from __future__ import annotations

import base64
from functools import lru_cache
from pathlib import Path
from urllib.parse import quote_plus

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

    @property
    def database_url_value(self) -> str:
        if self.database_url:
            return self.database_url
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
        if not self.database_url and not self.db_password.get_secret_value():
            raise RuntimeError("DB_PASSWORD is required")
        if len(self.jwt_secret.get_secret_value()) < 32:
            raise RuntimeError("JWT_SECRET must contain at least 32 characters")
        self._validate_fernet_key()
        if "*" in self.allowed_origins:
            raise RuntimeError("Wildcard CORS origins are forbidden")
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

    @property
    def static_dir(self) -> Path:
        return Path(__file__).resolve().parent / "static"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
