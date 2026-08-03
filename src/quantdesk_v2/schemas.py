from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator


class RegisterRequest(BaseModel):
    username: str = Field(min_length=3, max_length=64, pattern=r"^[A-Za-z0-9_.-]+$")
    email: str | None = Field(default=None, max_length=254)
    password: SecretStr = Field(min_length=12, max_length=256)

    @field_validator("email")
    @classmethod
    def normalize_email(cls, value: str | None) -> str | None:
        return value.strip().lower() if value else None


class LoginRequest(BaseModel):
    username: str = Field(min_length=3, max_length=64)
    password: SecretStr = Field(min_length=1, max_length=256)
    client_type: Literal["web", "native"] = "web"


class RefreshRequest(BaseModel):
    refresh_token: SecretStr | None = None


class LogoutRequest(BaseModel):
    refresh_token: SecretStr | None = None


class TokenPair(BaseModel):
    access_token: str
    refresh_token: str | None = None
    token_type: Literal["bearer"] = "bearer"
    expires_in: int


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    username: str
    email: str | None
    is_active: bool
    is_admin: bool
    binance_credentials_configured: bool
    binance_key_fingerprint: str | None
    binance_key_updated_at: datetime | None
    created_at: datetime


class BinanceCredentialUpdate(BaseModel):
    api_key: SecretStr = Field(min_length=16, max_length=256)
    api_secret: SecretStr = Field(min_length=16, max_length=512)
    permissions: list[Literal["READ", "TRADE"]] = Field(
        default_factory=lambda: ["READ", "TRADE"], min_length=1, max_length=2
    )


class BinanceCredentialStatus(BaseModel):
    configured: bool
    fingerprint: str | None
    updated_at: datetime | None


class MessageOut(BaseModel):
    message: str


class HealthOut(BaseModel):
    status: Literal["ok"]
    database: Literal["ok"]
    version: str
    database_dialect: str
    tls_required: bool
