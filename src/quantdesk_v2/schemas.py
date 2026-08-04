from __future__ import annotations

import math
import re
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    SecretStr,
    field_validator,
    model_validator,
)

BacktestTimeframe = Literal[
    "1m",
    "3m",
    "5m",
    "15m",
    "30m",
    "1h",
    "2h",
    "4h",
    "6h",
    "8h",
    "12h",
    "1d",
    "3d",
    "1w",
]
BacktestRunStatus = Literal["queued", "running", "completed", "failed", "cancelled"]
BacktestTradeSide = Literal["long", "short"]
AiProviderCode = Literal["openai", "deepseek", "doubao", "qwen", "kimi", "minimax"]


def _bounded_numeric_map(value: dict[str, int | float], field_name: str) -> dict[str, int | float]:
    normalized: dict[str, int | float] = {}
    for raw_key, raw_value in value.items():
        key = raw_key.strip()
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", key):
            raise ValueError(f"invalid {field_name} name: {raw_key!r}")
        if isinstance(raw_value, bool) or not isinstance(raw_value, (int, float)):
            raise ValueError(f"{field_name} {key!r} must be numeric")
        numeric = float(raw_value)
        if not math.isfinite(numeric) or abs(numeric) > 1_000_000:
            raise ValueError(f"{field_name} {key!r} must be finite and bounded")
        normalized[key] = raw_value
    return normalized


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


class AdminAlertRulesUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    score_alert_long: int = Field(default=60, ge=40, le=100)
    score_alert_short: int = Field(default=-60, ge=-100, le=-40)
    score_alert_position: int = Field(default=40, ge=20, le=100)
    spike_alert_pct_5m: float = Field(default=2.0, ge=0.1, le=20)
    watchlist_only: bool = True
    enabled_timeframes: list[Literal["15m", "1h", "4h"]] = Field(
        default_factory=lambda: ["15m", "1h", "4h"], min_length=1, max_length=3
    )

    @field_validator("enabled_timeframes")
    @classmethod
    def unique_timeframes(cls, value: list[str]) -> list[str]:
        return list(dict.fromkeys(value))


def _validate_news_source_url(value: str | None) -> str | None:
    if value is None:
        return None
    if not re.match(r"^https?://[^\s]+$", value, re.IGNORECASE):
        raise ValueError("news source URL must use http or https")
    return value


class AdminNewsSourceCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: str = Field(min_length=1, max_length=80)
    url: str = Field(min_length=10, max_length=2048)
    lang: str = Field(default="en", min_length=2, max_length=16, pattern=r"^[A-Za-z0-9-]+$")
    enabled: bool = True
    slow: bool = False
    weight: int = Field(default=100, ge=1, le=1000)
    hourly_limit: int = Field(default=600, ge=1, le=10000)

    @field_validator("url")
    @classmethod
    def valid_url(cls, value: str) -> str:
        return _validate_news_source_url(value) or value


class AdminNewsSourceUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    url: str | None = Field(default=None, min_length=10, max_length=2048)
    lang: str | None = Field(default=None, min_length=2, max_length=16, pattern=r"^[A-Za-z0-9-]+$")
    enabled: bool | None = None
    slow: bool | None = None
    weight: int | None = Field(default=None, ge=1, le=1000)
    hourly_limit: int | None = Field(default=None, ge=1, le=10000)

    @field_validator("url")
    @classmethod
    def valid_url(cls, value: str | None) -> str | None:
        return _validate_news_source_url(value)

    @model_validator(mode="after")
    def require_change(self) -> Self:
        if all(
            getattr(self, name) is None
            for name in ("url", "lang", "enabled", "slow", "weight", "hourly_limit")
        ):
            raise ValueError("at least one news source field is required")
        return self


class AdminUserUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    is_active: bool | None = None
    is_admin: bool | None = None

    @model_validator(mode="after")
    def require_change(self) -> Self:
        if self.is_active is None and self.is_admin is None:
            raise ValueError("at least one user field is required")
        return self


class AdminCleanupRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    alerts_days: int = Field(default=30, ge=1, le=3650)
    news_days: int = Field(default=90, ge=1, le=3650)
    scores_days: int = Field(default=180, ge=1, le=3650)
    confirm: bool = False


class BinanceCredentialUpdate(BaseModel):
    api_key: SecretStr = Field(min_length=16, max_length=256)
    api_secret: SecretStr = Field(min_length=16, max_length=512)
    permissions: list[Literal["READ", "TRADE"]] = Field(
        default_factory=lambda: ["READ", "TRADE"], min_length=1, max_length=2
    )

    @field_validator("api_key", "api_secret")
    @classmethod
    def validate_credential_characters(cls, value: SecretStr) -> SecretStr:
        raw = value.get_secret_value().strip()
        if (
            not raw.isascii()
            or not raw.isprintable()
            or any(character.isspace() for character in raw)
        ):
            raise ValueError("Binance credentials must contain printable ASCII without spaces")
        return SecretStr(raw)


class BinanceCredentialStatus(BaseModel):
    configured: bool
    fingerprint: str | None
    updated_at: datetime | None


class AiProviderOut(BaseModel):
    code: AiProviderCode
    name: str
    base_url: str
    default_model: str
    models: list[str]


def _normalize_ai_display_name(value: str) -> str:
    normalized = value.strip()
    if not normalized or any(ord(character) < 32 or ord(character) == 127 for character in normalized):
        raise ValueError("display_name must not be blank or contain control characters")
    return normalized


def _validate_ai_api_key(value: SecretStr) -> SecretStr:
    raw = value.get_secret_value().strip()
    if len(raw) < 8:
        raise ValueError("api_key must contain at least 8 characters")
    if not raw.isascii() or not raw.isprintable() or any(character.isspace() for character in raw):
        raise ValueError("api_key must contain printable ASCII without spaces")
    return SecretStr(raw)


class AiModelConfigCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider_code: AiProviderCode
    display_name: str = Field(min_length=1, max_length=80)
    model_name: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$",
    )
    api_key: SecretStr = Field(min_length=8, max_length=2048)
    is_enabled: bool = True
    is_default: bool = False

    @field_validator("display_name")
    @classmethod
    def normalize_display_name(cls, value: str) -> str:
        return _normalize_ai_display_name(value)

    @field_validator("model_name")
    @classmethod
    def normalize_model_name(cls, value: str) -> str:
        return value.strip()

    @field_validator("api_key")
    @classmethod
    def validate_api_key(cls, value: SecretStr) -> SecretStr:
        return _validate_ai_api_key(value)

    @model_validator(mode="after")
    def default_must_be_enabled(self) -> Self:
        if self.is_default and not self.is_enabled:
            raise ValueError("a default AI model configuration must be enabled")
        return self


class AiModelConfigUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider_code: AiProviderCode | None = None
    display_name: str | None = Field(default=None, min_length=1, max_length=80)
    model_name: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$",
    )
    api_key: SecretStr | None = Field(default=None, max_length=2048)
    is_enabled: bool | None = None
    is_default: bool | None = None

    @field_validator("display_name")
    @classmethod
    def normalize_display_name(cls, value: str | None) -> str | None:
        return _normalize_ai_display_name(value) if value is not None else None

    @field_validator("model_name")
    @classmethod
    def normalize_model_name(cls, value: str | None) -> str | None:
        return value.strip() if value is not None else None

    @field_validator("api_key", mode="before")
    @classmethod
    def empty_api_key_preserves_existing(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("api_key")
    @classmethod
    def validate_api_key(cls, value: SecretStr | None) -> SecretStr | None:
        return _validate_ai_api_key(value) if value is not None else None

    @model_validator(mode="after")
    def default_must_be_enabled(self) -> Self:
        if all(
            getattr(self, field_name) is None
            for field_name in (
                "provider_code",
                "display_name",
                "model_name",
                "api_key",
                "is_enabled",
                "is_default",
            )
        ):
            raise ValueError("at least one AI model configuration field is required")
        if self.is_default and self.is_enabled is False:
            raise ValueError("a default AI model configuration must be enabled")
        return self


class AiModelConfigOut(BaseModel):
    id: str
    provider_code: AiProviderCode
    provider_name: str
    display_name: str
    base_url: str
    model_name: str
    api_key_configured: bool
    api_key_fingerprint: str
    is_enabled: bool
    is_default: bool
    created_at: datetime
    updated_at: datetime


class BinanceAccountSummary(BaseModel):
    configured: bool
    connected: bool
    can_trade: bool | None = None
    account_type: Literal["UM_FUTURE", "PORTFOLIO_MARGIN"] | None = None
    wallet_balance: float | None = None
    available_balance: float | None = None
    unrealized_pnl: float | None = None
    currency: Literal["USD"] = "USD"
    updated_at: datetime
    positions: list[dict[str, Any]] = Field(default_factory=list)
    error_category: (
        Literal[
            "not_configured",
            "credential_error",
            "authentication",
            "timestamp",
            "rate_limit",
            "timeout",
            "network",
            "upstream",
            "rejected",
            "invalid_response",
        ]
        | None
    ) = None


class BinanceTradingState(BaseModel):
    configured: bool
    connected: bool
    account_type: Literal["UM_FUTURE", "PORTFOLIO_MARGIN"] | None = None
    updated_at: datetime
    positions: list[dict[str, Any]] = Field(default_factory=list)
    open_orders: list[dict[str, Any]] = Field(default_factory=list)
    error_category: (
        Literal[
            "not_configured",
            "credential_error",
            "authentication",
            "timestamp",
            "rate_limit",
            "timeout",
            "network",
            "upstream",
            "rejected",
            "invalid_response",
        ]
        | None
    ) = None


class BinancePerformanceAccount(BaseModel):
    account_type: Literal["UM_FUTURE", "PORTFOLIO_MARGIN"]
    wallet_balance: float
    available_balance: float
    unrealized_pnl: float
    currency: Literal["USD"]
    updated_at: datetime


class BinancePerformanceDay(BaseModel):
    date: date
    net_income: float
    realized_pnl: float
    funding_fee: float
    commission: float
    realized_records: int = Field(ge=0)
    wins: int = Field(ge=0)
    losses: int = Field(ge=0)
    breakeven: int = Field(ge=0)


class BinanceAssetPerformance(BaseModel):
    asset: str = Field(min_length=1, max_length=32, pattern=r"^[A-Z0-9_.:/-]+$")
    net_income: float
    realized_pnl: float
    funding_fee: float
    commission: float
    current_unrealized_pnl: float | None
    realized_records: int = Field(ge=0)
    wins: int = Field(ge=0)
    losses: int = Field(ge=0)
    breakeven: int = Field(ge=0)
    win_rate_pct: float | None = Field(default=None, ge=0, le=100)
    profit_factor: float | None = Field(default=None, ge=0)
    profit_factor_status: Literal["available", "no_losses", "no_trades"]
    gross_profit: float = Field(ge=0)
    gross_loss_abs: float = Field(ge=0)
    days: list[BinancePerformanceDay]


class BinancePerformanceOut(BaseModel):
    source: Literal["binance_income"]
    scope: Literal["current_user"]
    configured: bool
    connected: bool
    generated_at: datetime
    month: str = Field(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")
    timezone_offset_minutes: int = Field(ge=-720, le=840)
    timezone_label: str
    history_status: Literal[
        "available",
        "history_limited",
        "history_unavailable",
        "future_month",
        "not_configured",
        "request_failed",
    ]
    history_complete: bool
    month_complete: bool
    data_as_of: datetime | None
    account: BinancePerformanceAccount | None
    income_basis: Literal["realized_pnl_plus_funding_fee_plus_commission"]
    aggregation_policy: Literal["per_asset_no_conversion"]
    included_income_types: list[Literal["REALIZED_PNL", "FUNDING_FEE", "COMMISSION"]]
    excluded_income_types: list[str]
    records_received: int = Field(ge=0)
    records_included: int = Field(ge=0)
    pages_fetched: int = Field(ge=0)
    assets: list[BinanceAssetPerformance]
    error_category: (
        Literal[
            "not_configured",
            "credential_error",
            "authentication",
            "timestamp",
            "rate_limit",
            "timeout",
            "network",
            "upstream",
            "rejected",
            "invalid_response",
        ]
        | None
    ) = None


class DashboardPerformanceMetrics(BaseModel):
    total_pnl: float
    total_return_pct: float
    realized_pnl: float
    unrealized_pnl: float
    win_rate: float
    win_rate_basis: Literal["decisive_trades"]
    profit_factor: float | None
    profit_factor_status: Literal["available", "no_losses", "no_trades"]
    max_drawdown: float
    max_drawdown_basis: Literal["since_reset_full_equity"]
    average_profit: float
    average_win: float
    trades: int
    wins: int
    losses: int
    breakeven: int
    equity_samples: int


class DashboardPerformanceDay(BaseModel):
    date: date
    pnl: float
    trades: int
    wins: int
    losses: int
    breakeven: int


class DashboardPerformanceCalendar(BaseModel):
    month: str = Field(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")
    timezone_offset_minutes: int = Field(ge=-720, le=840)
    timezone_label: str
    basis: Literal["closed_trade_net_pnl"]
    total_pnl: float
    active_days: int
    days: list[DashboardPerformanceDay]


class DashboardPerformanceOut(BaseModel):
    source: Literal["paper_account"]
    scope: Literal["user_account"]
    currency: Literal["USDT"]
    generated_at: datetime
    data_as_of: datetime | None
    period_start: datetime | None
    stale: bool
    metrics: DashboardPerformanceMetrics
    calendar: DashboardPerformanceCalendar


class MonitorWatchlistUpdate(BaseModel):
    symbols: list[str] = Field(default_factory=list, max_length=250)

    @field_validator("symbols")
    @classmethod
    def normalize_symbols(cls, value: list[str]) -> list[str]:
        return sorted({symbol.strip().upper() for symbol in value if symbol.strip()})


class OpportunityPreferenceUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["watch", "ignore", "clear"]
    notify_enabled: bool = True


class PaperAccountCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: str = Field(min_length=1, max_length=100)
    strategy_id: str = Field(min_length=36, max_length=36)
    initial_balance: float = Field(default=10_000, gt=0, le=1_000_000_000)
    leverage: int | None = Field(default=None, ge=1, le=50)
    max_positions: int | None = Field(default=None, ge=1, le=50)
    position_size_pct: float | None = Field(default=None, gt=0, le=100)
    margin_cap: float | None = Field(default=None, gt=0, le=0.95)


class PaperAccountStatusUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    status: Literal["active", "paused", "archived"] | None = None
    name: str | None = Field(default=None, min_length=1, max_length=100)

    @model_validator(mode="after")
    def require_change(self) -> Self:
        if self.status is None and self.name is None:
            raise ValueError("at least one paper account field is required")
        return self


class LiveAccountCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: str = Field(min_length=1, max_length=100)
    strategy_id: str = Field(min_length=36, max_length=36)
    leverage: int = Field(default=3, ge=1, le=20)
    max_positions: int = Field(default=1, ge=1, le=20)
    position_size_pct: float = Field(default=2, gt=0, le=10)
    margin_cap: float = Field(default=0.20, gt=0, le=0.50)
    risk_per_trade_pct: float = Field(default=0.5, gt=0, le=1)
    max_total_risk_pct: float = Field(default=4, gt=0, le=8)
    max_cluster_positions: int = Field(default=2, ge=1, le=20)
    risk_max_leverage: int = Field(default=10, ge=1, le=20)
    liquidation_buffer_pct: float = Field(default=1.5, ge=0.5, le=10)
    daily_loss_limit_pct: float = Field(default=2, gt=0, le=20)
    max_drawdown_pct: float = Field(default=6, gt=0, le=30)
    short_risk_multiplier: float = Field(default=0.5, ge=0, le=1)
    max_ticker_age_seconds: int = Field(default=120, ge=30, le=900)
    max_signal_age_seconds: int = Field(default=18_000, ge=300, le=172_800)
    block_high_risk_products: bool = True


class LiveAccountStrategyUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    strategy_id: str = Field(min_length=36, max_length=36)
    leverage: int = Field(ge=1, le=20)
    max_positions: int = Field(ge=1, le=20)
    position_size_pct: float = Field(gt=0, le=10)
    margin_cap: float = Field(gt=0, le=0.50)
    risk_per_trade_pct: float | None = Field(default=None, gt=0, le=1)
    max_total_risk_pct: float | None = Field(default=None, gt=0, le=8)
    max_cluster_positions: int | None = Field(default=None, ge=1, le=20)
    risk_max_leverage: int | None = Field(default=None, ge=1, le=20)
    liquidation_buffer_pct: float | None = Field(default=None, ge=0.5, le=10)
    daily_loss_limit_pct: float | None = Field(default=None, gt=0, le=20)
    max_drawdown_pct: float | None = Field(default=None, gt=0, le=30)
    short_risk_multiplier: float | None = Field(default=None, ge=0, le=1)
    max_ticker_age_seconds: int | None = Field(default=None, ge=30, le=900)
    max_signal_age_seconds: int | None = Field(default=None, ge=300, le=172_800)
    block_high_risk_products: bool | None = None


class LiveAccountStatusUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    status: Literal["paused", "archived"] | None = None
    name: str | None = Field(default=None, min_length=1, max_length=100)

    @model_validator(mode="after")
    def require_change(self) -> Self:
        if self.status is None and self.name is None:
            raise ValueError("at least one live account field is required")
        return self


class LiveAccountArmRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    confirmation_name: str = Field(min_length=1, max_length=100)
    acknowledge_real_funds: Literal[True]


class StrategyIndicatorSelection(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    key: str = Field(min_length=2, max_length=32, pattern=r"^[a-z][a-z0-9_]{1,31}$")
    weight: float = Field(default=1, ge=0.1, le=5)
    parameters: dict[str, int | float] = Field(default_factory=dict, max_length=8)

    @field_validator("parameters")
    @classmethod
    def validate_parameters(cls, value: dict[str, int | float]) -> dict[str, int | float]:
        return _bounded_numeric_map(value, "indicator parameter")


class StrategyCreateRequest(BaseModel):
    """Create one user-owned template copy or executable indicator composition."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: str = Field(min_length=1, max_length=80)
    description: str = Field(default="", max_length=600)
    category: str = Field(default="自定义", min_length=1, max_length=32)
    template_key: str | None = Field(
        default=None,
        min_length=1,
        max_length=64,
        pattern=r"^[a-z][a-z0-9_]{0,63}$",
    )
    parameters: dict[str, int | float] | None = Field(default=None, max_length=32)
    risk_defaults: dict[str, int | float] | None = Field(default=None, max_length=16)
    indicators: list[StrategyIndicatorSelection] | None = Field(
        default=None, min_length=2, max_length=8
    )
    timeframe: Literal["15m", "1h", "4h"] = "1h"
    directions: list[Literal["long", "short"]] = Field(
        default_factory=lambda: ["long", "short"], min_length=1, max_length=2
    )
    confirmation_threshold: float = Field(default=60, ge=1, le=100)
    signal_valid_bars: int = Field(default=2, ge=1, le=10)

    @field_validator("parameters")
    @classmethod
    def validate_parameters(
        cls, value: dict[str, int | float] | None
    ) -> dict[str, int | float] | None:
        return _bounded_numeric_map(value, "strategy parameter") if value is not None else None

    @field_validator("risk_defaults")
    @classmethod
    def validate_risk_defaults(
        cls, value: dict[str, int | float] | None
    ) -> dict[str, int | float] | None:
        return _bounded_numeric_map(value, "risk default") if value is not None else None

    @model_validator(mode="after")
    def validate_creation_mode(self) -> Self:
        if self.indicators is not None:
            keys = [item.key for item in self.indicators]
            if len(set(keys)) != len(keys):
                raise ValueError("indicator selections must be unique")
            if self.template_key is not None:
                raise ValueError("template_key and indicators cannot be used together")
        self.directions = list(dict.fromkeys(self.directions))
        return self


class StrategyUpdateRequest(BaseModel):
    """Replace editable strategy fields using optimistic concurrency control."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    version: int = Field(ge=1)
    name: str = Field(min_length=1, max_length=80)
    description: str = Field(default="", max_length=600)
    category: str = Field(min_length=1, max_length=32)
    parameters: dict[str, int | float] = Field(max_length=32)
    risk_defaults: dict[str, int | float] = Field(max_length=16)

    @field_validator("parameters")
    @classmethod
    def validate_parameters(cls, value: dict[str, int | float]) -> dict[str, int | float]:
        return _bounded_numeric_map(value, "strategy parameter")

    @field_validator("risk_defaults")
    @classmethod
    def validate_risk_defaults(cls, value: dict[str, int | float]) -> dict[str, int | float]:
        return _bounded_numeric_map(value, "risk default")


class StrategyAiPreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    prompt: str = Field(min_length=1, max_length=2_000)

    @field_validator("prompt")
    @classmethod
    def reject_control_characters(cls, value: str) -> str:
        if re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", value):
            raise ValueError("prompt contains control characters")
        return value


class StrategyAiProposed(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: str = Field(min_length=1, max_length=80)
    description: str = Field(default="", max_length=600)
    category: str = Field(min_length=1, max_length=32)
    parameters: dict[str, int | float] = Field(max_length=32)
    risk_defaults: dict[str, int | float] = Field(max_length=16)

    @field_validator("parameters")
    @classmethod
    def validate_parameters(cls, value: dict[str, int | float]) -> dict[str, int | float]:
        return _bounded_numeric_map(value, "strategy parameter")

    @field_validator("risk_defaults")
    @classmethod
    def validate_risk_defaults(cls, value: dict[str, int | float]) -> dict[str, int | float]:
        return _bounded_numeric_map(value, "risk default")


class StrategyAiApplyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    base_version: int = Field(ge=1)
    proposed: StrategyAiProposed


class BacktestRunRequest(BaseModel):
    """创建单策略、单品种回测所需的可信输入。"""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    strategy_id: str = Field(
        min_length=1,
        max_length=64,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$",
    )
    symbol: str = Field(min_length=2, max_length=32, pattern=r"^[A-Z0-9][A-Z0-9._:/-]*$")
    timeframe: BacktestTimeframe
    start_date: date
    end_date: date
    initial_capital: Decimal = Field(ge=1, le=Decimal("1000000000000"), max_digits=30)
    position_size_pct: Decimal = Field(ge=Decimal("0.01"), le=100, max_digits=10, decimal_places=6)
    leverage: int = Field(ge=1, le=20)
    fee_bps: Decimal = Field(ge=0, le=1000, max_digits=10, decimal_places=6)
    slippage_bps: Decimal = Field(ge=0, le=1000, max_digits=10, decimal_places=6)
    stop_loss_pct: Decimal = Field(ge=0, le=Decimal("99.9"), max_digits=10, decimal_places=6)
    take_profit_pct: Decimal = Field(ge=0, le=Decimal("99.9"), max_digits=10, decimal_places=6)
    max_holding_bars: int = Field(ge=0, le=50_000)
    params: dict[str, float] = Field(default_factory=dict, max_length=64)

    @field_validator("symbol", mode="before")
    @classmethod
    def normalize_symbol(cls, value: object) -> object:
        return value.strip().upper() if isinstance(value, str) else value

    @field_validator("params")
    @classmethod
    def validate_params(cls, value: dict[str, float]) -> dict[str, float]:
        normalized: dict[str, float] = {}
        for raw_key, raw_value in value.items():
            key = raw_key.strip()
            if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]{0,63}", key):
                raise ValueError(f"invalid strategy parameter name: {raw_key!r}")
            if not math.isfinite(raw_value) or abs(raw_value) > 1_000_000_000:
                raise ValueError(f"strategy parameter {key!r} must be finite and bounded")
            normalized[key] = raw_value
        return normalized

    @model_validator(mode="after")
    def validate_date_range(self) -> Self:
        if self.start_date > self.end_date:
            raise ValueError("start_date must be earlier than or equal to end_date")
        if (self.end_date - self.start_date).days > 366:
            raise ValueError("backtest date range cannot exceed 366 days")
        return self


class BacktestTradeOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    run_id: int
    user_id: int
    side: BacktestTradeSide
    entry_at: datetime
    exit_at: datetime
    entry_price: Decimal
    exit_price: Decimal
    quantity: Decimal
    gross_pnl: Decimal
    fees: Decimal
    net_pnl: Decimal
    return_pct: Decimal
    holding_bars: int
    exit_reason: str | None
    metadata_json: dict[str, JsonValue] | None


class BacktestRunSummaryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    user_id: int
    strategy_id: str
    strategy_name: str
    symbol: str
    timeframe: BacktestTimeframe
    status: BacktestRunStatus
    start_at: datetime
    end_at: datetime
    initial_capital: Decimal
    final_equity: Decimal | None
    net_profit: Decimal | None
    total_return_pct: Decimal | None
    max_drawdown_pct: Decimal | None
    sharpe_ratio: Decimal | None
    win_rate_pct: Decimal | None
    profit_factor: Decimal | None
    trade_count: int
    error: str | None
    created_at: datetime
    completed_at: datetime | None


class BacktestRunOut(BacktestRunSummaryOut):
    config_json: dict[str, JsonValue]
    metrics_json: dict[str, JsonValue] | None
    equity_curve_json: list[dict[str, JsonValue]] | None
    data_quality_json: dict[str, JsonValue] | None
    metadata_json: dict[str, JsonValue] | None
    trades: list[BacktestTradeOut] = Field(default_factory=list)


class BacktestRunListOut(BaseModel):
    items: list[BacktestRunSummaryOut]
    total: int = Field(ge=0)
    page: int = Field(ge=1)
    page_size: int = Field(ge=1, le=100)


class MessageOut(BaseModel):
    message: str


class HealthOut(BaseModel):
    status: Literal["ok"]
    database: Literal["ok"]
    version: str
    database_dialect: str
    tls_required: bool
