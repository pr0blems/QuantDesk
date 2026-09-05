from __future__ import annotations

import math
import re
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal
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
MarketSession = Literal["pre-market", "regular", "post-market"]


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


class UsMarketStatusOut(BaseModel):
    configured: bool
    available: bool
    exchange: Literal["US"] = "US"
    holiday: str | None = None
    is_open: bool | None = None
    session: MarketSession | None = None
    timezone: str | None = None
    source_timestamp: int | None = None
    fetched_at: datetime | None = None
    cached: bool = False
    stale: bool = False
    error_category: str | None = None


class FinnhubWebhookStatusOut(BaseModel):
    status: Literal["ready", "not_configured"]
    configured: bool
    method: Literal["POST"] = "POST"
    received_events: int = 0
    last_received_at: datetime | None = None


class FinnhubWebhookAcceptedOut(BaseModel):
    accepted: Literal[True] = True


class FinnhubUsQuoteOut(BaseModel):
    symbol: str
    available: bool
    price: float | None = None
    change: float | None = None
    change_percent: float | None = None
    day_high: float | None = None
    day_low: float | None = None
    day_open: float | None = None
    previous_close: float | None = None
    source_timestamp: int | None = None
    fetched_at: datetime | None = None
    volume: float | None = None
    live: bool = False
    stale: bool = False
    error_category: str | None = None
    storage: Literal["database", "memory_pending"] | None = None


class FinnhubUsQuotesOut(BaseModel):
    configured: bool
    enabled: bool = True
    market_open_only: bool = True
    market_open: bool = False
    collection_active: bool = False
    source: Literal["finnhub"] = "finnhub"
    exchange: Literal["US"] = "US"
    total: int
    available: int
    stream_connected: bool
    stream_error: str | None = None
    updated_at: datetime | None = None
    persisted: int = 0
    write_errors: int = 0
    last_persisted_at: datetime | None = None
    quotes: list[FinnhubUsQuoteOut]


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
    feed_type: str = Field(default="rss", pattern=r"^(rss|taoz_flash|unusual_whales)$")
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
    feed_type: str | None = Field(default=None, pattern=r"^(rss|taoz_flash|unusual_whales)$")
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
            for name in (
                "url",
                "feed_type",
                "lang",
                "enabled",
                "slow",
                "weight",
                "hourly_limit",
            )
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


class AdminAiModelConfigUpdate(BaseModel):
    """Administrator-owned update for the platform-wide DeepSeek credential."""

    model_config = ConfigDict(extra="forbid")

    model_name: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$",
    )
    api_key: SecretStr | None = Field(default=None, max_length=2048)
    is_enabled: bool = True

    @field_validator("model_name")
    @classmethod
    def normalize_model_name(cls, value: str) -> str:
        return value.strip()

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


class AdminUnusualWhalesChannels(BaseModel):
    """Administrator-controlled Unusual Whales channel switches."""

    model_config = ConfigDict(extra="forbid")

    price: bool = True
    trading_halts: bool = True
    interval_flow: bool = True
    net_flow: bool = True
    market_tide: bool = True
    gex: bool = True
    lit_trades: bool = True
    off_lit_trades: bool = True
    flow_alerts: bool = True
    option_trades: bool = False


class AdminUnusualWhalesThresholds(BaseModel):
    """Safety limits used by the real-time opportunity admission gate."""

    model_config = ConfigDict(extra="forbid")

    quote_age_regular_ms: int = Field(default=2_000, ge=250, le=60_000)
    quote_age_extended_ms: int = Field(default=10_000, ge=1_000, le=120_000)
    spread_hard_max_bps: float = Field(default=80.0, ge=1.0, le=1_000.0)
    source_divergence_max_bps: float = Field(default=35.0, ge=1.0, le=1_000.0)
    min_data_coverage: float = Field(default=0.8, ge=0.0, le=1.0)
    event_block_before_minutes: int = Field(default=30, ge=0, le=1_440)
    event_block_after_minutes: int = Field(default=15, ge=0, le=1_440)
    halt_cooldown_minutes: int = Field(default=15, ge=0, le=1_440)


class AdminUnusualWhalesWeights(BaseModel):
    """Published scoring-domain weights; the sum must remain exactly one."""

    model_config = ConfigDict(extra="forbid")

    news: float = Field(default=0.20, ge=0.0, le=1.0)
    technical: float = Field(default=0.30, ge=0.0, le=1.0)
    market_context: float = Field(default=0.10, ge=0.0, le=1.0)
    options_flow: float = Field(default=0.20, ge=0.0, le=1.0)
    gex: float = Field(default=0.10, ge=0.0, le=1.0)
    institutional_flow: float = Field(default=0.10, ge=0.0, le=1.0)

    @model_validator(mode="after")
    def validate_total(self) -> Self:
        if not math.isclose(sum(self.model_dump().values()), 1.0, abs_tol=1e-6):
            raise ValueError("scoring-domain weights must add up to 1.0")
        return self


class AiMonitorScorePolicyUpdate(BaseModel):
    """Only the six-domain scoring weights exposed by the AI monitor UI."""

    model_config = ConfigDict(extra="forbid")

    weights: AdminUnusualWhalesWeights


class AiMonitorUnusualWhalesUsageUpdate(BaseModel):
    """Toggle platform-wide Unusual Whales usage without changing its policy."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool


class AiMonitorFinnhubUsageUpdate(BaseModel):
    """Toggle platform-wide Finnhub US cash-equity quote collection."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool


class AiMonitorLiveCopyUpdate(BaseModel):
    """Enable or stop routing new AI-monitor signals to live trading."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    enabled: bool
    account_id: str | None = Field(default=None, min_length=36, max_length=36)


class AiMonitorManualFollowRequest(BaseModel):
    """Explicit confirmation for one exact AI-monitor opportunity."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    account_id: str = Field(min_length=36, max_length=36)
    opportunity_id: str = Field(min_length=36, max_length=36)
    prediction_id: str | None = Field(default=None, min_length=36, max_length=36)
    manual_attempt_id: str = Field(
        min_length=36,
        max_length=36,
        pattern=r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-5][0-9a-fA-F]{3}-[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}$",
    )
    expected_contract_symbol: str = Field(
        min_length=4,
        max_length=32,
        pattern=r"^[A-Za-z0-9]+$",
    )
    expected_direction: Literal["long", "short"]
    acknowledge_real_funds: bool = False

    @field_validator("expected_contract_symbol")
    @classmethod
    def normalize_contract_symbol(cls, value: str) -> str:
        return value.upper()


class AiMonitorLiveCopyConfigUpdate(BaseModel):
    """Risk and execution policy for the isolated AI-monitor live account."""

    model_config = ConfigDict(extra="forbid")

    account_id: str | None = Field(default=None, min_length=36, max_length=36)
    position_mode: Literal["one_way", "hedge"] = "one_way"
    leverage: int = Field(default=10, ge=1, le=20)
    max_positions: int = Field(default=10, ge=1, le=20)
    position_size_basis: Literal["account_equity", "copy_total_amount"] = "account_equity"
    copy_total_amount: float = Field(default=1_000.0, ge=1.0, le=1_000_000_000.0)
    position_size_pct: float = Field(default=2.0, ge=0.1, le=20.0)
    risk_per_trade_pct: float = Field(default=0.5, ge=0.1, le=5.0)
    max_total_risk_pct: float = Field(default=4.0, ge=0.5, le=50.0)
    margin_cap_pct: float = Field(default=20.0, ge=1.0, le=100.0)
    daily_loss_limit_pct: float = Field(default=2.0, ge=0.5, le=20.0)
    max_drawdown_pct: float = Field(default=6.0, ge=1.0, le=50.0)
    round_trip_cost_bps: float = Field(default=16.0, ge=16.0, le=500.0)
    signal_max_age_seconds: int = Field(default=300, ge=60, le=1_800)
    minimum_combined_score: float = Field(default=70.0, ge=0.0, le=100.0)
    allow_long: bool = True
    allow_short: bool = True

    @model_validator(mode="after")
    def validate_live_risk_policy(self) -> Self:
        if not self.allow_long and not self.allow_short:
            raise ValueError("at least one live direction must remain enabled")
        if self.risk_per_trade_pct > self.max_total_risk_pct:
            raise ValueError("risk_per_trade_pct cannot exceed max_total_risk_pct")
        if self.position_size_pct > self.margin_cap_pct:
            raise ValueError("position_size_pct cannot exceed margin_cap_pct")
        return self


class AdminUnusualWhalesRetention(BaseModel):
    """Bounded retention for high-volume, reproducible market-data tiers."""

    model_config = ConfigDict(extra="forbid")

    raw_event_days: int = Field(default=14, ge=1, le=365)
    feature_snapshot_days: int = Field(default=90, ge=7, le=730)
    cleanup_interval_minutes: int = Field(default=60, ge=1, le=1_440)
    cleanup_batch_size: int = Field(default=2_000, ge=100, le=20_000)
    cleanup_max_batches: int = Field(default=10, ge=1, le=100)


class AdminUnusualWhalesConfigUpdate(BaseModel):
    """Platform-wide market-data and signal-gate configuration."""

    model_config = ConfigDict(extra="forbid")

    api_key: SecretStr | None = Field(default=None, max_length=2048)
    enabled: bool = True
    mode: Literal["record", "score", "gate"] = "record"
    rest_enabled: bool = True
    websocket_enabled: bool = True
    channels: AdminUnusualWhalesChannels = Field(default_factory=AdminUnusualWhalesChannels)
    thresholds: AdminUnusualWhalesThresholds = Field(default_factory=AdminUnusualWhalesThresholds)
    weights: AdminUnusualWhalesWeights = Field(default_factory=AdminUnusualWhalesWeights)
    retention: AdminUnusualWhalesRetention = Field(default_factory=AdminUnusualWhalesRetention)

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
    if not normalized or any(
        ord(character) < 32 or ord(character) == 127 for character in normalized
    ):
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


class AiMonitorConfigUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    news_analysis_enabled: bool = False
    news_interval_minutes: int = Field(default=15, ge=5, le=1440)
    opportunity_interval_minutes: int = Field(default=15, ge=5, le=1440)
    news_lookback_hours: int = Field(default=168, ge=1, le=168)
    timeframe: Literal["15m", "1h", "4h"] = "1h"
    prediction_max_holding_bars: int = Field(default=4, ge=1, le=24)
    indicator_keys: list[str] = Field(
        default_factory=lambda: ["moving_average_bull"], min_length=1, max_length=20
    )
    monitor_symbols: list[str] = Field(default_factory=list, max_length=250)
    minimum_news_confidence: float = Field(default=0.6, ge=0, le=1)
    minimum_news_mentions: int = Field(default=1, ge=1, le=20)
    minimum_indicator_score: float = Field(default=65, ge=0, le=100)
    minimum_combined_score: float = Field(default=75, ge=75, le=100)
    maximum_market_age_seconds: int = Field(default=120, ge=5, le=3600)
    minimum_feature_quality: float = Field(default=0.7, ge=0, le=1)
    minimum_market_flow_quality: float = Field(default=0.5, ge=0, le=1)
    minimum_calibration_samples: int = Field(default=1000, ge=30, le=5000)
    live_safety_margin_bps: float = Field(default=10, ge=0, le=500)
    news_score_weight: float = Field(default=20, ge=0, le=100)
    technical_score_weight: float = Field(default=50, ge=0, le=100)
    market_flow_score_weight: float = Field(default=30, ge=0, le=100)

    @field_validator("indicator_keys")
    @classmethod
    def normalize_indicator_keys(cls, value: list[str]) -> list[str]:
        normalized: list[str] = []
        for raw_key in value:
            key = raw_key.strip().lower()
            if not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", key):
                raise ValueError("indicator key is invalid")
            if key not in normalized:
                normalized.append(key)
        if not normalized:
            raise ValueError("at least one indicator is required")
        return normalized

    @field_validator("monitor_symbols")
    @classmethod
    def normalize_monitor_symbols(cls, value: list[str]) -> list[str]:
        normalized: list[str] = []
        for raw_symbol in value:
            symbol = raw_symbol.strip().upper()
            if not re.fullmatch(r"[A-Z0-9][A-Z0-9._:/-]{1,31}", symbol):
                raise ValueError("monitor symbol is invalid")
            if symbol not in normalized:
                normalized.append(symbol)
        return normalized

    @model_validator(mode="after")
    def validate_score_weight_total(self) -> Self:
        quantum = Decimal("0.01")
        quantized = {
            name: Decimal(str(getattr(self, name))).quantize(
                quantum,
                rounding=ROUND_HALF_UP,
            )
            for name in (
                "news_score_weight",
                "technical_score_weight",
                "market_flow_score_weight",
            )
        }
        if sum(quantized.values(), Decimal("0.00")) != Decimal("100.00"):
            raise ValueError("新闻、技术指标与资金盘口权重合计必须为 100%")
        for name, value in quantized.items():
            setattr(self, name, float(value))
        return self


class AiMonitorCostConfigUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    prediction_fee_enabled: bool = True
    prediction_fee_bps_per_side: float = Field(default=5, ge=0, le=500)
    prediction_slippage_enabled: bool = True
    prediction_slippage_bps_per_side: float = Field(default=3, ge=0, le=500)
    prediction_funding_enabled: bool = True
    prediction_funding_bps_per_8h: float = Field(default=1, ge=0, le=500)


class AiMonitorNewsAnalysisUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool


class AiMonitorNewsSystemPromptUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    system_prompt: str | None = Field(default=None, max_length=8000)

    @field_validator("system_prompt")
    @classmethod
    def normalize_system_prompt(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            return None
        if len(normalized) < 40:
            raise ValueError("system prompt must contain at least 40 characters")
        if re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", normalized):
            raise ValueError("system prompt contains unsupported control characters")
        return normalized


class AiMonitorRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_type: Literal["news", "opportunity"]


class AiMonitorReplayRequest(BaseModel):
    """Create an isolated point-in-time historical replay."""

    model_config = ConfigDict(extra="forbid")

    days: int = Field(default=365, ge=30, le=730)
    timeframe: Literal["15m", "1h", "4h"] = "1h"
    symbols: list[str] = Field(default_factory=list, max_length=150)

    @field_validator("symbols")
    @classmethod
    def normalize_symbols(cls, value: list[str]) -> list[str]:
        normalized: list[str] = []
        for raw_symbol in value:
            symbol = raw_symbol.strip().upper()
            if not re.fullmatch(r"[A-Z0-9][A-Z0-9._:/-]{1,31}", symbol):
                raise ValueError("replay symbol is invalid")
            if symbol not in normalized:
                normalized.append(symbol)
        return normalized


class AiMonitorNewsAnalyzeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    news_id: str = Field(min_length=1, max_length=255)


class PredictionAlgorithmWeights(BaseModel):
    model_config = ConfigDict(extra="forbid")

    aggressive_flow: float = Field(ge=0, le=1)
    book_imbalance: float = Field(ge=0, le=1)
    book_imbalance_5: float = Field(ge=0, le=1)
    velocity: float = Field(ge=0, le=1)
    flash_imbalance: float = Field(ge=0, le=1)
    taker_flow: float = Field(ge=0, le=1)
    price_oi_impulse: float = Field(ge=0, le=1)
    trend: float = Field(ge=0, le=1)
    kline_bollinger_breakout: float = Field(ge=0, le=1)
    kline_moving_average_pullback_bounce: float = Field(ge=0, le=1)
    kline_trend_breakout: float = Field(ge=0, le=1)
    kline_price_volume_rise: float = Field(ge=0, le=1)
    kline_new_low_reversal: float = Field(ge=0, le=1)
    kline_low_volume_pullback: float = Field(ge=0, le=1)
    kline_strong_gap_open: float = Field(ge=0, le=1)
    kline_moving_average_bull: float = Field(ge=0, le=1)
    kline_ma_golden_cross: float = Field(ge=0, le=1)
    kline_macd_golden_cross_volume: float = Field(ge=0, le=1)
    kline_oversold_bounce: float = Field(ge=0, le=1)
    kline_oversold_reversal: float = Field(ge=0, le=1)

    @model_validator(mode="after")
    def weights_must_sum_to_one(self) -> Self:
        total = sum(getattr(self, name) for name in type(self).model_fields)
        if not math.isclose(total, 1.0, abs_tol=0.001):
            raise ValueError("prediction weights must sum to 1")
        return self


class PredictionAlgorithmEnabledFeatures(BaseModel):
    model_config = ConfigDict(extra="forbid")

    aggressive_flow: bool
    book_imbalance: bool
    book_imbalance_5: bool
    velocity: bool
    flash_imbalance: bool
    taker_flow: bool
    price_oi_impulse: bool
    trend: bool
    kline_bollinger_breakout: bool
    kline_moving_average_pullback_bounce: bool
    kline_trend_breakout: bool
    kline_price_volume_rise: bool
    kline_new_low_reversal: bool
    kline_low_volume_pullback: bool
    kline_strong_gap_open: bool
    kline_moving_average_bull: bool
    kline_ma_golden_cross: bool
    kline_macd_golden_cross_volume: bool
    kline_oversold_bounce: bool
    kline_oversold_reversal: bool


class AdminNewsAiBatchCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    count: Literal[300, 500]


class PredictionAlgorithmHorizons(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    five_minutes: PredictionAlgorithmWeights = Field(alias="5m")
    fifteen_minutes: PredictionAlgorithmWeights = Field(alias="15m")
    one_hour: PredictionAlgorithmWeights = Field(alias="1h")


class PredictionAlgorithmUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    direction_threshold: float = Field(ge=0.05, le=0.5)
    min_data_quality: float = Field(ge=0.5, le=1)
    account_crowding_penalty: float = Field(ge=0, le=0.5)
    funding_crowding_penalty: float = Field(ge=0, le=0.5)
    enabled_features: PredictionAlgorithmEnabledFeatures
    weights: PredictionAlgorithmHorizons

    @model_validator(mode="after")
    def enabled_features_need_positive_weight(self) -> Self:
        enabled = self.enabled_features
        enabled_names = [name for name in type(enabled).model_fields if getattr(enabled, name)]
        if not enabled_names:
            raise ValueError("at least one prediction feature must be enabled")
        for horizon in ("five_minutes", "fifteen_minutes", "one_hour"):
            weights = getattr(self.weights, horizon)
            if not any(getattr(weights, name) > 0 for name in enabled_names):
                raise ValueError("enabled prediction features need positive weight")
        return self


class PredictionAlgorithmOptimizationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_config_version: int = Field(ge=0)


class PaperAccountCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: str = Field(min_length=1, max_length=100)
    strategy_ids: list[str] | None = Field(default=None, min_length=1, max_length=10)
    strategy_id: str | None = Field(default=None, min_length=36, max_length=36)
    initial_balance: float = Field(default=10_000, gt=0, le=1_000_000_000)
    leverage: int | None = Field(default=None, ge=1, le=20)
    max_positions: int | None = Field(default=None, ge=1, le=20)
    position_size_pct: float | None = Field(default=None, gt=0, le=100)
    margin_cap: float | None = Field(default=None, gt=0, le=0.95)

    @model_validator(mode="after")
    def normalize_strategy_selection(self) -> Self:
        selected = self.strategy_ids or ([self.strategy_id] if self.strategy_id else [])
        if not selected:
            raise ValueError("at least one paper strategy is required")
        if any(len(value) != 36 for value in selected):
            raise ValueError("paper strategy ids must be 36 characters")
        if len(set(selected)) != len(selected):
            raise ValueError("paper strategy ids must be unique")
        if self.strategy_ids is not None and self.strategy_id is not None:
            raise ValueError("provide strategy_ids or strategy_id, not both")
        self.strategy_ids = selected
        return self


class PaperAccountStatusUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    status: Literal["active", "paused", "archived"] | None = None
    name: str | None = Field(default=None, min_length=1, max_length=100)

    @model_validator(mode="after")
    def require_change(self) -> Self:
        if self.status is None and self.name is None:
            raise ValueError("at least one paper account field is required")
        return self


class PaperAccountSymbolsUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    symbols: list[str] = Field(min_length=1, max_length=20)

    @field_validator("symbols", mode="before")
    @classmethod
    def normalize_symbols(cls, value: object) -> object:
        if not isinstance(value, list):
            return value
        normalized = [str(symbol).strip().upper() for symbol in value]
        if any(not re.fullmatch(r"[A-Z0-9]{2,32}", symbol) for symbol in normalized):
            raise ValueError("paper symbols must use 2-32 uppercase letters or digits")
        if len(set(normalized)) != len(normalized):
            raise ValueError("paper symbols must be unique")
        return normalized


class PaperAccountStrategyUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    strategy_ids: list[str] | None = Field(default=None, min_length=1, max_length=10)
    strategy_id: str | None = Field(default=None, min_length=36, max_length=36)
    leverage: int = Field(ge=1, le=20)
    max_positions: int = Field(ge=1, le=20)
    position_size_pct: float = Field(gt=0, le=100)
    margin_cap: float = Field(gt=0, le=0.95)

    @model_validator(mode="after")
    def normalize_strategy_selection(self) -> Self:
        selected = self.strategy_ids or ([self.strategy_id] if self.strategy_id else [])
        if not selected:
            raise ValueError("at least one paper strategy is required")
        if any(len(value) != 36 for value in selected):
            raise ValueError("paper strategy ids must be 36 characters")
        if len(set(selected)) != len(selected):
            raise ValueError("paper strategy ids must be unique")
        if self.strategy_ids is not None and self.strategy_id is not None:
            raise ValueError("provide strategy_ids or strategy_id, not both")
        self.strategy_ids = selected
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


class StrategySourceComposition(BaseModel):
    """Indicator constraints used when AI generates executable Python source."""

    model_config = ConfigDict(extra="forbid")

    indicators: list[StrategyIndicatorSelection] = Field(min_length=2, max_length=8)
    timeframe: Literal["15m", "1h", "4h"] = "1h"
    directions: list[Literal["long", "short"]] = Field(
        default_factory=lambda: ["long", "short"], min_length=1, max_length=2
    )
    confirmation_threshold: float = Field(default=60, ge=1, le=100)
    signal_valid_bars: int = Field(default=2, ge=1, le=10)

    @model_validator(mode="after")
    def validate_composition(self) -> Self:
        keys = [item.key for item in self.indicators]
        if len(set(keys)) != len(keys):
            raise ValueError("indicator selections must be unique")
        self.directions = list(dict.fromkeys(self.directions))
        return self


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
    parameters: dict[str, int | float] | None = Field(default=None, max_length=64)
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
    parameters: dict[str, int | float] = Field(max_length=64)
    risk_defaults: dict[str, int | float] = Field(max_length=16)

    @field_validator("parameters")
    @classmethod
    def validate_parameters(cls, value: dict[str, int | float]) -> dict[str, int | float]:
        return _bounded_numeric_map(value, "strategy parameter")

    @field_validator("risk_defaults")
    @classmethod
    def validate_risk_defaults(cls, value: dict[str, int | float]) -> dict[str, int | float]:
        return _bounded_numeric_map(value, "risk default")


class StrategyPromotionRequest(BaseModel):
    """Promote only the current immutable revision by one lifecycle stage."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    expected_version: int = Field(ge=1)
    target_status: Literal["validated", "backtested", "shadow", "paper", "micro_live", "live"]
    confirmed: Literal[True]
    approval_note: str | None = Field(default=None, max_length=500)

    @model_validator(mode="after")
    def require_live_approval_note(self) -> Self:
        if (
            self.target_status in {"micro_live", "live"}
            and len((self.approval_note or "").strip()) < 10
        ):
            raise ValueError("微型实盘或正式实盘晋级必须填写至少 10 个字符的审批说明")
        return self


class StrategyPromotionReviewRequest(BaseModel):
    """Create an immutable, revision-bound promotion approval request."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    request_id: str = Field(pattern=r"^[0-9a-fA-F-]{36}$")
    expected_version: int = Field(ge=1)
    target_status: Literal["validated", "backtested", "shadow", "paper", "micro_live", "live"]
    request_note: str = Field(min_length=10, max_length=500)
    confirmed: Literal[True]


class StrategyPromotionDecisionRequest(BaseModel):
    """Approve or reject one pending promotion request with optimistic locking."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    action: Literal["approve", "reject"]
    expected_review_version: int = Field(ge=1)
    decision_note: str = Field(min_length=10, max_length=500)
    confirmed: Literal[True]


class StrategyRollbackRequest(BaseModel):
    """Copy an old immutable revision into a new draft revision."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    expected_version: int = Field(ge=1)
    target_version: int = Field(ge=1)
    reason: str = Field(min_length=10, max_length=500)
    confirmed: Literal[True]


class StrategyValidationEvidenceRequest(BaseModel):
    """Record structured, revision-bound promotion evidence."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    run_id: str = Field(pattern=r"^[0-9a-fA-F-]{36}$")
    expected_version: int = Field(ge=1)
    validation_type: Literal["oos", "stress", "shadow", "paper", "micro_live", "fault_drill"]
    status: Literal["passed", "failed"]
    report: dict[str, JsonValue] = Field(max_length=64)
    confirmed: Literal[True]


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
    parameters: dict[str, int | float] = Field(max_length=64)
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


class StrategyCodeValidateRequest(BaseModel):
    """Validate one complete, declarative strategy program without persisting it."""

    model_config = ConfigDict(extra="forbid")

    spec: dict[str, JsonValue] = Field(min_length=1, max_length=32)


class StrategyCodeAiPreviewRequest(StrategyAiPreviewRequest):
    """Edit the currently visible code buffer without persisting it."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    spec: dict[str, JsonValue] = Field(min_length=1, max_length=32)


class StrategyCodeUpdateRequest(BaseModel):
    """Publish a complete strategy DSL revision under optimistic locking."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    version: int = Field(ge=1)
    name: str = Field(min_length=1, max_length=80)
    description: str = Field(default="", max_length=600)
    category: str = Field(min_length=1, max_length=32)
    spec: dict[str, JsonValue] = Field(min_length=1, max_length=32)


class StrategySourceValidateRequest(BaseModel):
    """Validate executable strategy source without persisting it."""

    model_config = ConfigDict(extra="forbid")

    language: Literal["python"] = "python"
    source_code: str = Field(min_length=1, max_length=65_536)


class StrategySourceAiPreviewRequest(StrategyAiPreviewRequest):
    """Modify the visible source buffer and return a review-only preview."""

    language: Literal["python"] = "python"
    source_code: str = Field(min_length=1, max_length=65_536)
    composition: StrategySourceComposition | None = None


class StrategySourceUpdateRequest(BaseModel):
    """Publish one immutable executable source revision."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    version: int = Field(ge=1)
    name: str = Field(min_length=1, max_length=80)
    description: str = Field(default="", max_length=600)
    category: str = Field(min_length=1, max_length=32)
    language: Literal["python"] = "python"
    source_code: str = Field(min_length=1, max_length=65_536)
    risk_defaults: dict[str, int | float] | None = Field(default=None, max_length=16)

    @field_validator("risk_defaults")
    @classmethod
    def validate_risk_defaults(
        cls, value: dict[str, int | float] | None
    ) -> dict[str, int | float] | None:
        return _bounded_numeric_map(value, "risk default") if value is not None else None


class StrategySourceCreateRequest(BaseModel):
    """Create a user-owned Python strategy with separate editable parameters."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: str = Field(default="Python EMA 策略", min_length=1, max_length=80)
    description: str = Field(default="可编辑 Python 源码策略", max_length=600)
    category: str = Field(default="源码策略", min_length=1, max_length=32)
    language: Literal["python"] = "python"
    source_code: str | None = Field(default=None, min_length=1, max_length=65_536)
    risk_defaults: dict[str, int | float] | None = Field(default=None, max_length=16)
    composition: StrategySourceComposition | None = None

    @field_validator("risk_defaults")
    @classmethod
    def validate_risk_defaults(
        cls, value: dict[str, int | float] | None
    ) -> dict[str, int | float] | None:
        return _bounded_numeric_map(value, "risk default") if value is not None else None


def _validated_strategy_params(value: dict[str, float]) -> dict[str, float]:
    normalized: dict[str, float] = {}
    for raw_key, raw_value in value.items():
        key = raw_key.strip()
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]{0,63}", key):
            raise ValueError(f"invalid strategy parameter name: {raw_key!r}")
        if not math.isfinite(raw_value) or abs(raw_value) > 1_000_000_000:
            raise ValueError(f"strategy parameter {key!r} must be finite and bounded")
        normalized[key] = raw_value
    return normalized


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
    market_data_source: Literal["auto", "tiger", "binance"] = "binance"
    start_date: date
    end_date: date
    initial_capital: Decimal = Field(ge=1, le=Decimal("1000000000000"), max_digits=30)
    position_size_pct: Decimal = Field(ge=Decimal("0.01"), le=100, max_digits=10, decimal_places=6)
    leverage: int = Field(ge=1, le=20)
    margin_mode: Literal["isolated"] = "isolated"
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
        return _validated_strategy_params(value)

    @model_validator(mode="after")
    def validate_date_range(self) -> Self:
        if self.start_date > self.end_date:
            raise ValueError("start_date must be earlier than or equal to end_date")
        if (self.end_date - self.start_date).days > 366:
            raise ValueError("backtest date range cannot exceed 366 days")
        return self


class StrategyExecutionParameterProfile(BaseModel):
    """Backtest defaults plus execution settings shared by paper and live runs."""

    model_config = ConfigDict(extra="forbid")

    initial_capital: Decimal | None = Field(
        default=None,
        ge=1,
        le=Decimal("1000000000000"),
        max_digits=30,
    )
    position_size_pct: Decimal = Field(ge=Decimal("0.01"), le=100, max_digits=10, decimal_places=6)
    leverage: int = Field(ge=1, le=20)
    margin_mode: Literal["isolated"] = "isolated"
    fee_bps: Decimal = Field(ge=0, le=1000, max_digits=10, decimal_places=6)
    slippage_bps: Decimal = Field(ge=0, le=1000, max_digits=10, decimal_places=6)
    stop_loss_pct: Decimal = Field(ge=0, le=Decimal("99.9"), max_digits=10, decimal_places=6)
    take_profit_pct: Decimal = Field(ge=0, le=Decimal("99.9"), max_digits=10, decimal_places=6)
    max_holding_bars: int = Field(ge=0, le=50_000)


class StrategyParameterProfileSaveRequest(BaseModel):
    """Save either a strategy-wide default or a symbol-specific override."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    scope: Literal["default", "symbol"]
    symbol: str | None = Field(
        default=None,
        min_length=2,
        max_length=32,
        pattern=r"^[A-Z0-9][A-Z0-9._:/-]*$",
    )
    params: dict[str, float] = Field(default_factory=dict, max_length=64)
    execution: StrategyExecutionParameterProfile | None = None

    @field_validator("symbol", mode="before")
    @classmethod
    def normalize_symbol(cls, value: object) -> object:
        return value.strip().upper() if isinstance(value, str) else value

    @field_validator("params")
    @classmethod
    def validate_params(cls, value: dict[str, float]) -> dict[str, float]:
        return _validated_strategy_params(value)

    @model_validator(mode="after")
    def validate_scope(self) -> Self:
        if self.scope == "symbol" and not self.symbol:
            raise ValueError("symbol is required for a symbol-specific profile")
        if self.scope == "default":
            self.symbol = None
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
    public_id: str
    user_id: int
    user_strategy_id: int | None
    strategy_revision_id: int | None
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
