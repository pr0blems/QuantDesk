from __future__ import annotations

import hashlib
import json
import math
import uuid
from collections.abc import Mapping
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from threading import Lock
from typing import Any

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Query,
    Request,
    Response,
    status,
)
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from . import __version__, battle
from .ai_model_config import get_global_ai_model_config
from .ai_providers import AI_PROVIDER_PRESETS, AiProviderPreset, get_ai_provider
from .backtest import BacktestRepository, BacktestUnavailable
from .binance_client import BinanceAccountClientError
from .binance_performance import (
    build_binance_performance,
    empty_binance_performance,
    history_status_for_month,
    month_window_ms,
)
from .database import get_db
from .dependencies import get_current_user
from .interfaces.api.ai_monitor import router as ai_monitor_router
from .interfaces.api.backtest_presenters import backtest_run_detail as _run_detail
from .interfaces.api.backtests_read import router as backtests_read_router
from .interfaces.api.common import (
    add_audit_log,
    monitor_repository,
    require_expected_user,
)
from .interfaces.api.finnhub import router as finnhub_router
from .interfaces.api.health import router as health_router
from .interfaces.api.monitor_public import router as monitor_public_router
from .interfaces.api.trading_accounts import (
    _live_risk_config as _live_risk_config,
)
from .interfaces.api.trading_accounts import (
    _lock_and_revalidate_live_arm as _lock_and_revalidate_live_arm,
)
from .interfaces.api.trading_accounts import router as trading_accounts_router
from .models import (
    AdminSetting,
    AiModelConfig,
    AuditLog,
    BacktestRun,
    BacktestTrade,
    MarketOpportunity,
    StrategyRevision,
    User,
    UserOpportunityState,
    UserSession,
    UserStrategy,
    utcnow,
)
from .monitor import MonitorRepository, MonitorUnavailable
from .schemas import (
    AiModelConfigCreate,
    AiModelConfigOut,
    AiModelConfigUpdate,
    AiProviderOut,
    BacktestRunRequest,
    BinanceAccountSummary,
    BinanceCredentialStatus,
    BinanceCredentialUpdate,
    BinancePerformanceOut,
    BinanceTradingState,
    LoginRequest,
    LogoutRequest,
    MessageOut,
    MonitorWatchlistUpdate,
    OpportunityPreferenceUpdate,
    PredictionAlgorithmOptimizationRequest,
    PredictionAlgorithmUpdate,
    RefreshRequest,
    RegisterRequest,
    TokenPair,
    UserOut,
)
from .security import (
    CredentialCipher,
    SecurityError,
    api_key_fingerprint,
    create_access_token,
    hash_password,
    hash_refresh_token,
    new_refresh_token,
    password_needs_rehash,
    verify_password,
)
from .strategy_ai import StrategyAiError, _chat_http_transport
from .strategy_artifacts import add_backtest_run_manifest
from .strategy_catalog import (
    ensure_user_default_strategies,
    get_user_strategy,
    serialize_strategy_catalog,
    strategy_to_catalog_item,
)
from .strategy_lifecycle import (
    BACKTEST_ELIGIBLE_STATUSES,
    current_strategy_revision,
)

router = APIRouter(prefix="/api/v2")
MIN_PERSISTED_QUANTITY = Decimal("0.000000000000000001")
MAX_CONCURRENT_BACKTESTS = 2
MAX_CONCURRENT_BACKTESTS_PER_USER = 2
MAX_PERSISTED_TRADES = 10_000
_backtest_guard = Lock()
_active_backtest_users: dict[int, int] = {}
_active_backtest_count = 0


def _acquire_backtest_slot(user_id: int) -> None:
    global _active_backtest_count
    with _backtest_guard:
        user_count = _active_backtest_users.get(user_id, 0)
        if user_count >= MAX_CONCURRENT_BACKTESTS_PER_USER:
            raise HTTPException(
                status_code=409,
                detail="current user reached the concurrent backtest limit",
            )
        if _active_backtest_count >= MAX_CONCURRENT_BACKTESTS:
            raise HTTPException(status_code=429, detail="backtest capacity is busy; retry shortly")
        _active_backtest_users[user_id] = user_count + 1
        _active_backtest_count += 1


def _release_backtest_slot(user_id: int) -> None:
    global _active_backtest_count
    with _backtest_guard:
        user_count = _active_backtest_users.get(user_id, 0)
        if user_count <= 1:
            _active_backtest_users.pop(user_id, None)
        else:
            _active_backtest_users[user_id] = user_count - 1
        if user_count > 0:
            _active_backtest_count = max(0, _active_backtest_count - 1)


def _backtest(request: Request) -> BacktestRepository:
    settings = request.app.state.settings
    try:
        return BacktestRepository(
            request.app.state.database_engine, settings.monitor_symbols_config
        )
    except BacktestUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from None


def _monitor(request: Request) -> MonitorRepository:
    return monitor_repository(request)


def _client_ip(request: Request) -> str | None:
    return request.client.host[:45] if request.client else None


def _require_expected_user(request: Request, user: User) -> None:
    require_expected_user(request, user)


def _user_out(user: User) -> UserOut:
    return UserOut(
        id=user.id,
        username=user.username,
        email=user.email,
        is_active=user.is_active,
        is_admin=user.is_admin,
        binance_credentials_configured=user.binance_credentials_configured,
        binance_key_fingerprint=user.binance_key_fingerprint,
        binance_key_updated_at=user.binance_key_updated_at,
        created_at=user.created_at,
    )


def _audit(
    db: Session,
    request: Request,
    action: str,
    user_id: int | None,
    resource_type: str | None = None,
    resource_id: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    add_audit_log(
        db,
        request,
        action,
        user_id,
        resource_type,
        resource_id,
        metadata,
    )


def _ai_provider_out(preset: AiProviderPreset) -> AiProviderOut:
    return AiProviderOut(
        code=preset.code,
        name=preset.label,
        base_url=preset.base_url,
        default_model=preset.default_model,
        models=list(preset.models),
    )


def _ai_model_config_out(config: AiModelConfig) -> AiModelConfigOut:
    preset = get_ai_provider(config.provider_code)
    if preset is None:
        raise HTTPException(status_code=500, detail="stored AI provider is unsupported")
    return AiModelConfigOut(
        id=config.public_id,
        provider_code=config.provider_code,
        provider_name=preset.label,
        display_name=config.display_name,
        base_url=preset.base_url,
        model_name=config.model_name,
        api_key_configured=bool(config.api_key_encrypted),
        api_key_fingerprint=config.api_key_fingerprint,
        is_enabled=config.is_enabled,
        is_default=config.is_default,
        created_at=config.created_at,
        updated_at=config.updated_at,
    )


def _lock_ai_model_owner(db: Session, user_id: int) -> None:
    """Serialize default-model transitions for one tenant."""

    locked_user_id = db.scalar(select(User.id).where(User.id == user_id).with_for_update())
    if locked_user_id is None:
        raise HTTPException(status_code=404, detail="user not found")


def _has_default_ai_model(db: Session, user_id: int) -> bool:
    return (
        db.scalar(
            select(AiModelConfig.id)
            .where(
                AiModelConfig.user_id == user_id,
                AiModelConfig.is_default.is_(True),
            )
            .limit(1)
        )
        is not None
    )


def _get_owned_ai_model_config(
    db: Session, user_id: int, public_id: str, *, for_update: bool = False
) -> AiModelConfig:
    statement = select(AiModelConfig).where(
        AiModelConfig.user_id == user_id,
        AiModelConfig.public_id == public_id,
    )
    if for_update:
        statement = statement.with_for_update()
    config = db.scalar(statement)
    if config is None:
        raise HTTPException(status_code=404, detail="AI model configuration not found")
    return config


def _set_default_ai_model(db: Session, config: AiModelConfig) -> None:
    if not config.is_enabled:
        raise HTTPException(
            status_code=422,
            detail="a default AI model configuration must be enabled",
        )
    existing_defaults = db.scalars(
        select(AiModelConfig)
        .where(
            AiModelConfig.user_id == config.user_id,
            AiModelConfig.public_id != config.public_id,
            AiModelConfig.is_default.is_(True),
        )
        .with_for_update()
    ).all()
    for existing in existing_defaults:
        existing.is_default = False
    if existing_defaults:
        db.flush()
    config.is_default = True


def _promote_ai_model_default(
    db: Session, user_id: int, *, exclude_public_id: str | None = None
) -> None:
    statement = select(AiModelConfig).where(
        AiModelConfig.user_id == user_id,
        AiModelConfig.is_enabled.is_(True),
    )
    if exclude_public_id is not None:
        statement = statement.where(AiModelConfig.public_id != exclude_public_id)
    candidate = db.scalar(statement.order_by(AiModelConfig.id).limit(1).with_for_update())
    if candidate is not None:
        _set_default_ai_model(db, candidate)


def _ai_config_conflict(db: Session) -> HTTPException:
    db.rollback()
    return HTTPException(
        status_code=409,
        detail="AI model configuration conflicts with an existing configuration",
    )


def _utc_iso(value: datetime) -> str:
    aware = value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    return aware.isoformat().replace("+00:00", "Z")


def _json_safe(value: Any) -> Any:
    """Return a JSON-compatible tree without non-finite numeric values."""
    if isinstance(value, Decimal):
        return float(value) if value.is_finite() else None
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, datetime):
        return _utc_iso(value)
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if value is None or isinstance(value, (str, int, bool)):
        return value
    return str(value)


def _decimal_value(value: Any, *, required: bool = False) -> Decimal | None:
    if value is None:
        if required:
            raise ValueError("backtest result is missing a required numeric value")
        return None
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError("backtest result contains an invalid numeric value") from exc
    if not result.is_finite():
        if required:
            raise ValueError("backtest result contains a non-finite numeric value")
        return None
    return result


def _float_value(value: Any) -> float | None:
    numeric = _decimal_value(value)
    return float(numeric) if numeric is not None else None


def _quantity_value(value: Any) -> Decimal:
    quantity = _decimal_value(value, required=True)
    if quantity is None or quantity < MIN_PERSISTED_QUANTITY:
        raise ValueError("backtest trade quantity is below the persistence precision")
    return quantity


def _utc_datetime(timestamp: Any) -> datetime:
    try:
        numeric = int(timestamp)
        return datetime.fromtimestamp(numeric, UTC).replace(tzinfo=None)
    except (OSError, OverflowError, TypeError, ValueError) as exc:
        raise ValueError("backtest result contains an invalid Unix timestamp") from exc


def _unix_date(timestamp: Any) -> str | None:
    try:
        numeric = float(timestamp)
        if not math.isfinite(numeric):
            return None
        return datetime.fromtimestamp(numeric, UTC).date().isoformat()
    except (OSError, OverflowError, TypeError, ValueError):
        return None


def _catalog_response(catalog: dict[str, Any]) -> dict[str, Any]:
    """Flatten available periods and expose browser-friendly ISO date bounds."""
    strategies = _json_safe(catalog.get("strategies", []))
    symbols: list[dict[str, Any]] = []
    timeframe_values: set[str] = {
        str(item).strip()
        for item in catalog.get("timeframes", [])
        if isinstance(item, str) and str(item).strip()
    }
    bounds: dict[str, dict[str, dict[str, Any]]] = {}
    raw_symbols = catalog.get("symbols", [])
    if not isinstance(raw_symbols, list):
        raw_symbols = []
    for raw_symbol in raw_symbols:
        if not isinstance(raw_symbol, Mapping):
            continue
        symbol = str(raw_symbol.get("symbol", "")).strip().upper()
        if not symbol:
            continue
        periods = raw_symbol.get("timeframes", [])
        if not isinstance(periods, list):
            periods = []
        normalized_periods = []
        for raw_period in periods:
            if not isinstance(raw_period, Mapping):
                continue
            timeframe = str(raw_period.get("timeframe", "")).strip()
            if not timeframe:
                continue
            timeframe_values.add(timeframe)
            start_date = _unix_date(raw_period.get("start_ts"))
            end_date = _unix_date(raw_period.get("end_ts"))
            period = _json_safe(dict(raw_period))
            period.update({"start_date": start_date, "end_date": end_date})
            normalized_periods.append(period)
            bounds.setdefault(symbol, {})[timeframe] = {
                "start": start_date,
                "end": end_date,
                "min_date": start_date,
                "max_date": end_date,
                "bars": period.get("bars"),
            }
        symbol_item = _json_safe(dict(raw_symbol))
        symbol_item["timeframes"] = normalized_periods
        symbol_item["available"] = bool(normalized_periods)
        symbols.append(symbol_item)
    limits = _json_safe(catalog.get("limits", {}))
    if not isinstance(limits, dict):
        limits = {}
    limits.update(
        {
            "max_concurrent_backtests": MAX_CONCURRENT_BACKTESTS,
            "max_concurrent_backtests_per_user": MAX_CONCURRENT_BACKTESTS_PER_USER,
            "max_persisted_trades": MAX_PERSISTED_TRADES,
        }
    )
    return {
        "strategies": strategies,
        "symbols": symbols,
        "timeframes": [
            {"timeframe": value, "value": value, "label": value}
            for value in sorted(timeframe_values)
        ],
        "bounds": bounds,
        "limits": limits,
    }


def _strategy_from_catalog(catalog: dict[str, Any], strategy_id: str) -> dict[str, Any]:
    strategies = catalog.get("strategies", [])
    if isinstance(strategies, list):
        for strategy in strategies:
            if isinstance(strategy, dict) and strategy.get("id") == strategy_id:
                name = strategy.get("name")
                if not isinstance(name, str) or not name.strip():
                    break
                return strategy
    raise HTTPException(status_code=422, detail="unknown backtest strategy")


def _strategy_version(strategy: dict[str, Any]) -> str:
    definition = json.dumps(
        _json_safe(strategy), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(definition.encode("utf-8")).hexdigest()[:16]


def _engine_config(payload: BacktestRunRequest, strategy: dict[str, Any]) -> dict[str, Any]:
    definitions = {
        item.get("key"): item
        for item in strategy.get("params", [])
        if isinstance(item, dict) and isinstance(item.get("key"), str)
    }
    params: dict[str, int | float] = {}
    for key, definition in definitions.items():
        value = payload.params.get(key, definition.get("default"))
        if value is None:
            raise HTTPException(status_code=503, detail="backtest strategy catalog is invalid")
        if definition.get("type") == "integer" and float(value).is_integer():
            params[key] = int(value)
        else:
            params[key] = float(value)
    for key, value in payload.params.items():
        if key not in definitions:
            params[key] = float(value)
    start_at = datetime.combine(payload.start_date, time.min, tzinfo=UTC)
    end_at = datetime.combine(
        payload.end_date + timedelta(days=1), time.min, tzinfo=UTC
    ) - timedelta(seconds=1)
    return {
        # The public strategy UUID identifies the user's configuration; the
        # deterministic engine receives only one of the allowlisted engine keys.
        "strategy_id": str(strategy.get("engine_key") or payload.strategy_id),
        "symbol": payload.symbol,
        "timeframe": payload.timeframe,
        "start_ts": int(start_at.timestamp()),
        "end_ts": int(end_at.timestamp()),
        "initial_capital": float(payload.initial_capital),
        "position_size_pct": float(payload.position_size_pct),
        "leverage": payload.leverage,
        "fee_bps": float(payload.fee_bps),
        "slippage_bps": float(payload.slippage_bps),
        "stop_loss_pct": float(payload.stop_loss_pct),
        "take_profit_pct": float(payload.take_profit_pct),
        "max_holding_bars": payload.max_holding_bars,
        "params": params,
    }


def _strategy_trigger_timeframe(strategy: dict[str, Any]) -> str | None:
    value: object = None
    if strategy.get("strategy_kind") == "source_strategy":
        validation = strategy.get("source_validation")
        if isinstance(validation, dict):
            requirements = validation.get("data_requirements")
            value = validation.get("trigger_timeframe")
            if value is None and isinstance(requirements, dict):
                value = requirements.get("trigger_timeframe")
    elif strategy.get("strategy_kind") == "full_strategy":
        spec = strategy.get("spec")
        timeframes = spec.get("timeframes") if isinstance(spec, dict) else None
        if isinstance(timeframes, dict):
            value = timeframes.get("trigger")
    return value if isinstance(value, str) and value in {"15m", "1h", "4h"} else None


def _backtest_error_status(message: str) -> int:
    if "binance" in message.lower():
        return 502
    unavailable_markers = (
        "database",
        "config is invalid",
        "query failed",
        "cannot be opened",
        "invalid timestamp",
    )
    return 503 if any(marker in message.lower() for marker in unavailable_markers) else 422


def _issue_session(
    *, db: Session, request: Request, user: User, client_type: str
) -> tuple[UserSession, str, str, int]:
    settings = request.app.state.settings
    refresh = new_refresh_token()
    session = UserSession(
        user_id=user.id,
        refresh_token_hash=hash_refresh_token(refresh),
        client_type=client_type,
        user_agent=(request.headers.get("user-agent") or "")[:512] or None,
        ip_address=_client_ip(request),
        expires_at=utcnow() + timedelta(days=settings.refresh_token_days),
    )
    db.add(session)
    db.flush()
    access, expires_in = create_access_token(
        user_id=user.id,
        session_id=session.id,
        jwt_secret=settings.jwt_secret.get_secret_value(),
        expires_minutes=settings.access_token_minutes,
    )
    return session, refresh, access, expires_in


def _set_refresh_cookie(response: Response, request: Request, token: str) -> None:
    settings = request.app.state.settings
    response.set_cookie(
        settings.refresh_cookie_name,
        token,
        max_age=settings.refresh_token_days * 86400,
        httponly=True,
        secure=settings.app_cookie_secure,
        samesite="lax",
        path="/api/v2/auth",
    )


def _refresh_from_request(request: Request, body_token: str | None) -> str | None:
    return body_token or request.cookies.get(request.app.state.settings.refresh_cookie_name)


router.include_router(health_router)


@router.post("/auth/register", response_model=UserOut, status_code=status.HTTP_201_CREATED)
def register(payload: RegisterRequest, request: Request, db: Session = Depends(get_db)) -> UserOut:
    if not request.app.state.settings.allow_public_registration:
        raise HTTPException(status_code=403, detail="public registration is disabled")
    username = payload.username.strip().lower()
    if db.scalar(select(User.id).where(User.username == username)):
        raise HTTPException(status_code=409, detail="username already exists")
    if payload.email is not None and db.scalar(select(User.id).where(User.email == payload.email)):
        raise HTTPException(status_code=409, detail="email already exists")
    user = User(
        username=username,
        email=payload.email,
        password_hash=hash_password(payload.password.get_secret_value()),
    )
    db.add(user)
    try:
        db.flush()
        _audit(db, request, "user.register", user.id, "user", str(user.id))
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="username or email already exists") from None
    db.refresh(user)
    return _user_out(user)


@router.post("/auth/login", response_model=TokenPair)
def login(
    payload: LoginRequest,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
) -> TokenPair:
    username = payload.username.strip().lower()
    user = db.scalar(select(User).where(User.username == username))
    if (
        user is None
        or not user.is_active
        or not verify_password(payload.password.get_secret_value(), user.password_hash)
    ):
        raise HTTPException(status_code=401, detail="invalid username or password")
    if password_needs_rehash(user.password_hash):
        user.password_hash = hash_password(payload.password.get_secret_value())
    user.last_login_at = utcnow()
    # Idempotently copy all system templates on the user's first successful
    # login. The strategy endpoints repeat this as a repair path for old sessions.
    ensure_user_default_strategies(db, user.id)
    _, refresh, access, expires_in = _issue_session(
        db=db, request=request, user=user, client_type=payload.client_type
    )
    _audit(db, request, "auth.login", user.id, "user", str(user.id))
    db.commit()
    if payload.client_type == "web":
        _set_refresh_cookie(response, request, refresh)
    return TokenPair(
        access_token=access,
        refresh_token=refresh if payload.client_type == "native" else None,
        expires_in=expires_in,
    )


@router.post("/auth/refresh", response_model=TokenPair)
def refresh(
    request: Request,
    response: Response,
    payload: RefreshRequest | None = None,
    db: Session = Depends(get_db),
) -> TokenPair:
    supplied = _refresh_from_request(
        request,
        payload.refresh_token.get_secret_value() if payload and payload.refresh_token else None,
    )
    if not supplied:
        raise HTTPException(status_code=401, detail="refresh token is required")
    old_session = db.scalar(
        select(UserSession).where(
            UserSession.refresh_token_hash == hash_refresh_token(supplied),
            UserSession.revoked_at.is_(None),
            UserSession.expires_at > utcnow(),
        )
    )
    if old_session is None:
        raise HTTPException(status_code=401, detail="invalid refresh token")
    user = db.get(User, old_session.user_id)
    if user is None or not user.is_active:
        raise HTTPException(status_code=401, detail="invalid refresh token")
    old_session.revoked_at = utcnow()
    _, new_refresh, access, expires_in = _issue_session(
        db=db, request=request, user=user, client_type=old_session.client_type
    )
    _audit(db, request, "auth.refresh", user.id, "session", old_session.id)
    db.commit()
    if old_session.client_type == "web":
        _set_refresh_cookie(response, request, new_refresh)
    return TokenPair(
        access_token=access,
        refresh_token=new_refresh if old_session.client_type == "native" else None,
        expires_in=expires_in,
    )


@router.post("/auth/logout", response_model=MessageOut)
def logout(
    request: Request,
    response: Response,
    payload: LogoutRequest | None = None,
    db: Session = Depends(get_db),
) -> MessageOut:
    supplied = _refresh_from_request(
        request,
        payload.refresh_token.get_secret_value() if payload and payload.refresh_token else None,
    )
    if supplied:
        session = db.scalar(
            select(UserSession).where(
                UserSession.refresh_token_hash == hash_refresh_token(supplied),
                UserSession.revoked_at.is_(None),
            )
        )
        if session:
            session.revoked_at = utcnow()
            _audit(db, request, "auth.logout", session.user_id, "session", session.id)
            db.commit()
    response.delete_cookie(
        request.app.state.settings.refresh_cookie_name,
        path="/api/v2/auth",
    )
    return MessageOut(message="logged out")


@router.get("/me", response_model=UserOut)
def me(user: User = Depends(get_current_user)) -> UserOut:
    return _user_out(user)


router.include_router(finnhub_router)


@router.get("/me/binance-account", response_model=BinanceAccountSummary)
def binance_account_summary(
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> BinanceAccountSummary:
    response.headers["Cache-Control"] = "private, no-store"
    checked_at = datetime.now(UTC)
    if not user.binance_credentials_configured:
        return BinanceAccountSummary(
            configured=False,
            connected=False,
            can_trade=None,
            updated_at=checked_at,
            error_category="not_configured",
        )

    encrypted_key = user.binance_api_key_encrypted
    encrypted_secret = user.binance_api_secret_encrypted
    # Authentication only reads MySQL. Release that transaction before the external call.
    db.rollback()
    cipher = CredentialCipher(request.app.state.settings.credential_master_key.get_secret_value())
    try:
        api_key = cipher.decrypt(encrypted_key or "")
        api_secret = cipher.decrypt(encrypted_secret or "")
    except SecurityError:
        return BinanceAccountSummary(
            configured=True,
            connected=False,
            can_trade=None,
            updated_at=checked_at,
            error_category="credential_error",
        )

    service = request.app.state.binance_service
    try:
        snapshot = service.account(api_key, api_secret)
    except BinanceAccountClientError as exc:
        return BinanceAccountSummary(
            configured=True,
            connected=False,
            can_trade=None,
            updated_at=checked_at,
            error_category=exc.category,
        )
    db.execute(text("DELETE FROM positions WHERE user_id=:user_id"), {"user_id": user.id})
    if snapshot.positions:
        db.execute(
            text(
                """INSERT INTO positions(
                       user_id,symbol,position_side,amt,side,entry_price,mark_price,upnl,leverage,ts
                   ) VALUES(
                       :user_id,:symbol,:position_side,:amt,:side,:entry_price,:mark_price,:upnl,:leverage,:ts
                   )"""
            ),
            [{**position, "user_id": user.id} for position in snapshot.positions],
        )
    db.commit()
    return BinanceAccountSummary(
        configured=True,
        connected=True,
        can_trade=snapshot.can_trade,
        account_type=snapshot.account_type,
        wallet_balance=float(snapshot.wallet_balance),
        available_balance=float(snapshot.available_balance),
        unrealized_pnl=float(snapshot.unrealized_pnl),
        currency=snapshot.currency,
        updated_at=snapshot.updated_at,
        positions=list(snapshot.positions),
        error_category=None,
    )


@router.get("/me/binance-orders", response_model=BinanceTradingState)
def binance_orders(
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> BinanceTradingState:
    """Return a fresh, read-only view of Binance futures positions and open orders."""

    response.headers["Cache-Control"] = "private, no-store"
    checked_at = datetime.now(UTC)
    if not user.binance_credentials_configured:
        return BinanceTradingState(
            configured=False,
            connected=False,
            updated_at=checked_at,
            error_category="not_configured",
        )

    encrypted_key = user.binance_api_key_encrypted
    encrypted_secret = user.binance_api_secret_encrypted
    db.rollback()
    cipher = CredentialCipher(request.app.state.settings.credential_master_key.get_secret_value())
    try:
        api_key = cipher.decrypt(encrypted_key or "")
        api_secret = cipher.decrypt(encrypted_secret or "")
    except SecurityError:
        return BinanceTradingState(
            configured=True,
            connected=False,
            updated_at=checked_at,
            error_category="credential_error",
        )

    service = request.app.state.binance_service
    try:
        account = service.account(api_key, api_secret)
        open_orders = service.open_orders(
            api_key,
            api_secret,
            account_type=account.account_type,
        )
    except BinanceAccountClientError as exc:
        return BinanceTradingState(
            configured=True,
            connected=False,
            updated_at=checked_at,
            error_category=exc.category,
        )

    return BinanceTradingState(
        configured=True,
        connected=True,
        account_type=account.account_type,
        updated_at=datetime.now(UTC),
        positions=list(account.positions),
        open_orders=list(open_orders),
        error_category=None,
    )


@router.get("/dashboard/binance-performance", response_model=BinancePerformanceOut)
def binance_performance(
    request: Request,
    response: Response,
    month: str | None = Query(default=None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    timezone_offset_minutes: int = Query(default=0, ge=-720, le=840),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> BinancePerformanceOut:
    response.headers["Cache-Control"] = "private, no-store"
    generated_at = datetime.now(UTC)
    local_now = generated_at + timedelta(minutes=timezone_offset_minutes)
    selected_month = month or local_now.strftime("%Y-%m")
    selected_year = int(selected_month[:4])
    if not 2000 <= selected_year <= 2100:
        raise HTTPException(status_code=422, detail="performance month is out of range")

    def empty_result(
        *,
        configured: bool,
        connected: bool,
        history_status: str,
        error_category: str | None,
        snapshot: Any = None,
    ) -> BinancePerformanceOut:
        return BinancePerformanceOut.model_validate(
            empty_binance_performance(
                month=selected_month,
                timezone_offset_minutes=timezone_offset_minutes,
                generated_at=generated_at,
                configured=configured,
                connected=connected,
                history_status=history_status,
                error_category=error_category,
                account=snapshot,
            )
        )

    if not user.binance_credentials_configured:
        return empty_result(
            configured=False,
            connected=False,
            history_status="not_configured",
            error_category="not_configured",
        )

    encrypted_key = user.binance_api_key_encrypted
    encrypted_secret = user.binance_api_secret_encrypted
    # Authentication only reads MySQL. Do not retain the transaction while calling Binance.
    db.rollback()
    cipher = CredentialCipher(request.app.state.settings.credential_master_key.get_secret_value())
    try:
        api_key = cipher.decrypt(encrypted_key or "")
        api_secret = cipher.decrypt(encrypted_secret or "")
    except SecurityError:
        return empty_result(
            configured=True,
            connected=False,
            history_status="request_failed",
            error_category="credential_error",
        )

    service = request.app.state.binance_service
    try:
        snapshot = service.account(api_key, api_secret)
    except BinanceAccountClientError as exc:
        return empty_result(
            configured=True,
            connected=False,
            history_status="request_failed",
            error_category=exc.category,
        )

    availability = history_status_for_month(selected_month, timezone_offset_minutes, generated_at)
    if availability != "available":
        # Binance only retains recent income history. Never render an unavailable
        # full month as a genuine zero, and do not spend request weight on it.
        return empty_result(
            configured=True,
            connected=True,
            history_status=availability,
            error_category=None,
            snapshot=snapshot,
        )

    start_time_ms, end_exclusive_ms = month_window_ms(selected_month, timezone_offset_minutes)
    end_time_ms = min(end_exclusive_ms - 1, int(generated_at.timestamp() * 1_000))
    try:
        history = service.income_history(
            api_key,
            api_secret,
            account_type=snapshot.account_type,
            start_time_ms=start_time_ms,
            end_time_ms=end_time_ms,
        )
    except BinanceAccountClientError as exc:
        return empty_result(
            configured=True,
            connected=True,
            history_status="request_failed",
            error_category=exc.category,
            snapshot=snapshot,
        )

    return BinancePerformanceOut.model_validate(
        build_binance_performance(
            snapshot=snapshot,
            history=history,
            month=selected_month,
            timezone_offset_minutes=timezone_offset_minutes,
            generated_at=generated_at,
        )
    )


@router.put("/me/binance-credentials", response_model=BinanceCredentialStatus)
def update_binance_credentials(
    payload: BinanceCredentialUpdate,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> BinanceCredentialStatus:
    _require_expected_user(request, user)
    api_key = payload.api_key.get_secret_value().strip()
    api_secret = payload.api_secret.get_secret_value().strip()
    request.app.state.binance_service.invalidate(api_key, api_secret)
    cipher = CredentialCipher(request.app.state.settings.credential_master_key.get_secret_value())
    user.binance_api_key_encrypted = cipher.encrypt(api_key)
    user.binance_api_secret_encrypted = cipher.encrypt(api_secret)
    user.binance_key_fingerprint = api_key_fingerprint(api_key)
    if not user.binance_physical_account_id:
        user.binance_physical_account_id = f"binance-usdm:{uuid.uuid4().hex}"
    user.binance_permissions = {"requested": sorted(set(payload.permissions))}
    user.binance_key_updated_at = utcnow()
    user.binance_key_version += 1
    _audit(db, request, "binance.credentials.update", user.id, "user", str(user.id))
    db.commit()
    db.refresh(user)
    return BinanceCredentialStatus(
        configured=True,
        fingerprint=user.binance_key_fingerprint,
        updated_at=user.binance_key_updated_at,
    )


@router.delete("/me/binance-credentials", response_model=MessageOut)
def delete_binance_credentials(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> MessageOut:
    _require_expected_user(request, user)
    user.binance_api_key_encrypted = None
    user.binance_api_secret_encrypted = None
    user.binance_key_fingerprint = None
    user.binance_permissions = None
    user.binance_key_updated_at = None
    user.binance_key_version += 1
    _audit(db, request, "binance.credentials.delete", user.id, "user", str(user.id))
    db.commit()
    return MessageOut(message="Binance credentials removed")


@router.get("/me/ai-model-providers", response_model=list[AiProviderOut])
def list_ai_model_providers(
    _: User = Depends(get_current_user),
) -> list[AiProviderOut]:
    return [_ai_provider_out(preset) for preset in AI_PROVIDER_PRESETS.values()]


@router.get("/me/ai-model-configs", response_model=list[AiModelConfigOut])
def list_ai_model_configs(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list[AiModelConfigOut]:
    configs = db.scalars(
        select(AiModelConfig)
        .where(AiModelConfig.user_id == user.id)
        .order_by(AiModelConfig.is_default.desc(), AiModelConfig.updated_at.desc())
    ).all()
    return [_ai_model_config_out(config) for config in configs]


@router.post(
    "/me/ai-model-configs",
    response_model=AiModelConfigOut,
    status_code=status.HTTP_201_CREATED,
)
def create_ai_model_config(
    payload: AiModelConfigCreate,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> AiModelConfigOut:
    _require_expected_user(request, user)
    preset = get_ai_provider(payload.provider_code)
    if preset is None:
        raise HTTPException(status_code=422, detail="unsupported AI provider")
    api_key = payload.api_key.get_secret_value()
    cipher = CredentialCipher(request.app.state.settings.credential_master_key.get_secret_value())

    try:
        _lock_ai_model_owner(db, user.id)
        has_default = _has_default_ai_model(db, user.id)
        config = AiModelConfig(
            user_id=user.id,
            provider_code=preset.code,
            display_name=payload.display_name,
            model_name=payload.model_name,
            api_key_encrypted=cipher.encrypt(api_key),
            api_key_fingerprint=api_key_fingerprint(api_key),
            api_key_version=1,
            is_enabled=payload.is_enabled,
            is_default=False,
        )
        db.add(config)
        db.flush()
        if payload.is_default or (not has_default and config.is_enabled):
            _set_default_ai_model(db, config)
        _audit(
            db,
            request,
            "ai_model_config.create",
            user.id,
            "ai_model_config",
            config.public_id,
        )
        db.commit()
    except IntegrityError:
        raise _ai_config_conflict(db) from None
    db.refresh(config)
    return _ai_model_config_out(config)


@router.put("/me/ai-model-configs/{config_id}", response_model=AiModelConfigOut)
def update_ai_model_config(
    config_id: uuid.UUID,
    payload: AiModelConfigUpdate,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> AiModelConfigOut:
    _require_expected_user(request, user)
    try:
        _lock_ai_model_owner(db, user.id)
        config = _get_owned_ai_model_config(db, user.id, str(config_id), for_update=True)
        was_default = config.is_default

        if payload.provider_code is not None:
            preset = get_ai_provider(payload.provider_code)
            if preset is None:
                raise HTTPException(status_code=422, detail="unsupported AI provider")
            if preset.code != config.provider_code:
                if payload.api_key is None:
                    raise HTTPException(
                        status_code=422,
                        detail="api_key is required when changing AI provider",
                    )
                if payload.model_name is None:
                    config.model_name = preset.default_model
            config.provider_code = preset.code
        if payload.display_name is not None:
            config.display_name = payload.display_name
        if payload.model_name is not None:
            config.model_name = payload.model_name
        if payload.api_key is not None:
            api_key = payload.api_key.get_secret_value()
            cipher = CredentialCipher(
                request.app.state.settings.credential_master_key.get_secret_value()
            )
            config.api_key_encrypted = cipher.encrypt(api_key)
            config.api_key_fingerprint = api_key_fingerprint(api_key)
            config.api_key_version += 1
        if payload.is_enabled is not None:
            config.is_enabled = payload.is_enabled

        if payload.is_default is True:
            _set_default_ai_model(db, config)
        elif payload.is_default is False or not config.is_enabled:
            config.is_default = False
            db.flush()
            if was_default:
                _promote_ai_model_default(db, user.id, exclude_public_id=config.public_id)
        elif payload.is_enabled is True and not _has_default_ai_model(db, user.id):
            _set_default_ai_model(db, config)

        _audit(
            db,
            request,
            "ai_model_config.update",
            user.id,
            "ai_model_config",
            config.public_id,
        )
        db.commit()
    except IntegrityError:
        raise _ai_config_conflict(db) from None
    db.refresh(config)
    return _ai_model_config_out(config)


@router.post(
    "/me/ai-model-configs/{config_id}/test",
    response_model=MessageOut,
)
def test_ai_model_config(
    config_id: uuid.UUID,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> MessageOut:
    """Verify a saved credential against its allowlisted model endpoint."""
    _require_expected_user(request, user)
    config = _get_owned_ai_model_config(db, user.id, str(config_id))
    preset = get_ai_provider(config.provider_code)
    if preset is None or not config.api_key_encrypted:
        raise HTTPException(status_code=422, detail="模型服务配置不完整")

    cipher = CredentialCipher(request.app.state.settings.credential_master_key.get_secret_value())
    try:
        api_key = cipher.decrypt(config.api_key_encrypted)
        body = json.dumps(
            {
                "model": config.model_name,
                "messages": [{"role": "user", "content": "Reply with OK."}],
                "max_tokens": 1,
                "stream": False,
            },
            separators=(",", ":"),
        ).encode("utf-8")
        status_code, _ = _chat_http_transport(
            preset,
            body,
            {
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            15.0,
        )
    except TimeoutError:
        raise HTTPException(status_code=504, detail="连接模型服务超时") from None
    except (StrategyAiError, OSError):
        raise HTTPException(status_code=502, detail="无法连接模型服务") from None

    if status_code in {401, 403}:
        raise HTTPException(status_code=422, detail="API Key 无效或无权访问该模型")
    if status_code in {408, 504}:
        raise HTTPException(status_code=504, detail="连接模型服务超时")
    if not 200 <= status_code < 300:
        raise HTTPException(status_code=502, detail=f"模型服务返回错误（HTTP {status_code}）")
    return MessageOut(message="API 测试成功，模型服务可正常使用")


@router.delete(
    "/me/ai-model-configs/{config_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
def delete_ai_model_config(
    config_id: uuid.UUID,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> Response:
    _require_expected_user(request, user)
    try:
        _lock_ai_model_owner(db, user.id)
        config = _get_owned_ai_model_config(db, user.id, str(config_id), for_update=True)
        was_default = config.is_default
        public_id = config.public_id
        db.delete(config)
        db.flush()
        if was_default:
            _promote_ai_model_default(db, user.id)
        _audit(
            db,
            request,
            "ai_model_config.delete",
            user.id,
            "ai_model_config",
            public_id,
        )
        db.commit()
    except IntegrityError:
        raise _ai_config_conflict(db) from None
    return Response(status_code=status.HTTP_204_NO_CONTENT)


router.include_router(monitor_public_router)
router.include_router(ai_monitor_router)
router.include_router(trading_accounts_router)


def _prediction_algorithm_out(
    setting: AdminSetting | None,
    user: User,
) -> dict[str, Any]:
    stored = setting.value_json if setting is not None else None
    return {
        "model_key": battle.MODEL_KEY,
        "model_version": battle.MODEL_VERSION,
        "feature_schema_version": battle.FEATURE_SCHEMA_VERSION,
        "feature_count": len(battle.ALGORITHM_FEATURES),
        "market_feature_count": len(battle.MARKET_ALGORITHM_FEATURES),
        "kline_strategy_count": len(battle.KLINE_STRATEGY_FEATURES),
        "config": battle.normalize_algorithm_config(stored),
        "defaults": battle.default_algorithm_config(),
        "config_version": int(setting.version if setting is not None else 0),
        "source": "custom" if setting is not None else "default",
        "updated_at": setting.updated_at if setting is not None else None,
        "editable": bool(user.is_admin),
    }


@router.get("/monitor/prediction-algorithm")
def monitor_prediction_algorithm(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict[str, Any]:
    return _prediction_algorithm_out(db.get(AdminSetting, battle.ALGORITHM_SETTING_KEY), user)


def _audit_prediction_ai_rejection(
    db: Session,
    request: Request,
    user_id: int,
    current_version: int,
    model_name: str,
    exc: Exception,
) -> None:
    """Persist a redacted failed-attempt trace without changing the algorithm."""

    trace = getattr(exc, "trace", None)
    if not isinstance(trace, dict):
        return
    metadata = {
        "status": "rejected",
        "failure_category": trace.get("failure_category"),
        "provider": "deepseek",
        "model": model_name,
        "source_version": current_version,
        "saved_version": None,
        "sample_count": trace.get("sample_count"),
        "history_start_ms": trace.get("history_start_ms"),
        "history_end_ms": trace.get("history_end_ms"),
        "optimized_horizons": [],
        "optimizer_key": trace.get("optimizer_key"),
        "response_model": trace.get("response_model"),
        "system_fingerprint": trace.get("system_fingerprint"),
        "summary": trace.get("summary"),
        "reasoning_steps": trace.get("reasoning_steps", []),
        "raw_model_output": trace.get("raw_model_output"),
        "model_attempts": trace.get("model_attempts", []),
        "normalization": trace.get("normalization"),
        "horizons": trace.get("horizons", []),
        "submitted_prompt": trace.get("submitted_prompt"),
        "usage": trace.get("usage", {}),
    }
    try:
        db.rollback()
        _audit(
            db,
            request,
            "monitor.prediction_algorithm.ai_optimize_rejected",
            user_id,
            "admin_setting",
            battle.ALGORITHM_SETTING_KEY,
            metadata,
        )
        db.commit()
    except SQLAlchemyError:
        db.rollback()


@router.post("/monitor/prediction-algorithm/optimize")
def optimize_monitor_prediction_algorithm(
    payload: PredictionAlgorithmOptimizationRequest,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict[str, Any]:
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="administrator access required")
    _require_expected_user(request, user)
    user_id = user.id
    setting = db.get(AdminSetting, battle.ALGORITHM_SETTING_KEY)
    current_version = int(setting.version if setting is not None else 0)
    if payload.expected_config_version != current_version:
        raise HTTPException(
            status_code=409,
            detail="prediction algorithm changed; reload it before optimizing",
        )
    current_config = battle.normalize_algorithm_config(
        setting.value_json if setting is not None else None
    )
    deepseek_model = get_global_ai_model_config(db, legacy_fallback_user_id=user_id)
    if deepseek_model is None:
        raise HTTPException(
            status_code=409,
            detail="请先在管理后台配置并启用全局 DeepSeek",
        )
    try:
        api_key = CredentialCipher(
            request.app.state.settings.credential_master_key.get_secret_value()
        ).decrypt(deepseek_model.api_key_encrypted)
    except SecurityError:
        db.rollback()
        raise HTTPException(
            status_code=503,
            detail="DeepSeek 模型密钥无法解密，请重新配置 API Key",
        ) from None

    model_name = deepseek_model.model_name
    history_rows = _monitor(request).prediction_algorithm_history()
    timeout_seconds = request.app.state.settings.deepseek_optimizer_timeout_seconds
    max_tokens = request.app.state.settings.deepseek_optimizer_max_tokens
    # Do not hold a SQL transaction or connection while waiting for the model.
    db.rollback()
    try:
        from .prediction_ai_optimizer import optimize_prediction_algorithm_with_deepseek
        from .prediction_optimizer import PredictionOptimizationUnavailable

        result = optimize_prediction_algorithm_with_deepseek(
            history_rows,
            current_config,
            current_config_version=current_version,
            api_key=api_key,
            model_name=model_name,
            timeout_seconds=timeout_seconds,
            max_tokens=max_tokens,
        )
    except PredictionOptimizationUnavailable as exc:
        _audit_prediction_ai_rejection(
            db, request, user_id, current_version, model_name, exc
        )
        raise HTTPException(status_code=409, detail=str(exc)) from None
    except StrategyAiError as exc:
        _audit_prediction_ai_rejection(
            db, request, user_id, current_version, model_name, exc
        )
        status_by_category = {
            "not_configured": 503,
            "timeout": 504,
            "upstream": 502,
            "invalid_output": 502,
        }
        message_by_category = {
            "not_configured": "DeepSeek 模型配置无效或无权访问该模型",
            "timeout": "DeepSeek 调优请求超时，当前算法未修改",
            "upstream": "DeepSeek 服务暂时不可用，当前算法未修改",
            "invalid_output": "DeepSeek 返回的权重不符合安全约束，当前算法未修改",
        }
        raise HTTPException(
            status_code=status_by_category[exc.category],
            detail=message_by_category[exc.category],
        ) from None

    # Recheck the version under a row lock after inference. A concurrent edit
    # invalidates this recommendation instead of silently overwriting it.
    setting = db.scalar(
        select(AdminSetting)
        .where(AdminSetting.key == battle.ALGORITHM_SETTING_KEY)
        .with_for_update()
    )
    latest_version = int(setting.version if setting is not None else 0)
    if latest_version != current_version:
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail="prediction algorithm changed while DeepSeek was optimizing; retry",
        )
    value = result["recommended_config"]
    if setting is None:
        setting = AdminSetting(
            key=battle.ALGORITHM_SETTING_KEY,
            value_json=value,
            version=1,
            updated_by=user_id,
        )
        db.add(setting)
    else:
        setting.value_json = value
        setting.version += 1
        setting.updated_by = user_id
        setting.updated_at = utcnow()
    try:
        db.flush()
        _audit(
            db,
            request,
            "monitor.prediction_algorithm.ai_optimize",
            user_id,
            "admin_setting",
            battle.ALGORITHM_SETTING_KEY,
            {
                "status": "saved",
                "provider": "deepseek",
                "model": model_name,
                "source_version": current_version,
                "saved_version": setting.version,
                "sample_count": result["sample_count"],
                "history_start_ms": result["history_start_ms"],
                "history_end_ms": result["history_end_ms"],
                "optimized_horizons": [
                    report["horizon"]
                    for report in result["horizons"]
                    if report["status"] == "optimized"
                ],
                "optimizer_key": result["optimizer_key"],
                "response_model": result["response_model"],
                "system_fingerprint": result["system_fingerprint"],
                "summary": result["summary"],
                "reasoning_steps": result["reasoning_steps"],
                "raw_model_output": result["raw_model_output"],
                "model_attempts": result["model_attempts"],
                "normalization": result["normalization"],
                "submitted_prompt": result["submitted_prompt"],
                "usage": result["usage"],
            },
        )
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail="prediction algorithm changed while DeepSeek was optimizing; retry",
        ) from None

    battle.invalidate_algorithm_config_cache()
    result["saved"] = True
    result["saved_config_version"] = int(setting.version)
    result["algorithm"] = _prediction_algorithm_out(setting, user)
    return result


def _prediction_ai_trace_database_analysis(
    request: Request,
    metadata: Mapping[str, Any],
    *,
    audit_created_at: datetime | None = None,
) -> dict[str, Any]:
    """Rebuild rich version statistics for legacy traces that predate them."""

    source_version = metadata.get("source_version")
    try:
        source_version = int(source_version)
    except (TypeError, ValueError):
        return {"available": False, "reason": "source_config_version_missing"}

    submitted_prompt = metadata.get("submitted_prompt")
    submitted_user = (
        submitted_prompt.get("user") if isinstance(submitted_prompt, Mapping) else None
    )
    try:
        submitted_payload = json.loads(submitted_user) if isinstance(submitted_user, str) else {}
    except (TypeError, ValueError, json.JSONDecodeError):
        submitted_payload = {}
    submitted_horizons = (
        submitted_payload.get("training_statistics", {})
        .get("history", {})
        .get("horizons", {})
        if isinstance(submitted_payload, Mapping)
        else {}
    )
    if isinstance(submitted_horizons, Mapping) and submitted_horizons and all(
        isinstance(item, Mapping) and isinstance(item.get("training_history_analysis"), Mapping)
        for item in submitted_horizons.values()
    ):
        return {"available": False, "reason": "submitted_prompt_already_contains_analysis"}

    cutoff_ms = metadata.get("history_end_ms")
    try:
        cutoff_ms = int(cutoff_ms) if cutoff_ms is not None else None
    except (TypeError, ValueError):
        cutoff_ms = None
    try:
        rows = _monitor(request).prediction_algorithm_history()
    except MonitorUnavailable:
        return {"available": False, "reason": "database_history_temporarily_unavailable"}
    matching_rows: list[dict[str, Any]] = []
    source_config: dict[str, Any] | None = None
    audit_created_at_ms = (
        int(audit_created_at.replace(tzinfo=UTC).timestamp() * 1_000)
        if audit_created_at is not None
        else None
    )
    for row in rows:
        raw_config = row.get("algorithm_config_json")
        try:
            config = json.loads(raw_config) if isinstance(raw_config, str) else raw_config
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if not isinstance(config, dict):
            continue
        try:
            row_version = int(config.get("config_version"))
        except (TypeError, ValueError):
            continue
        if row_version != source_version:
            continue
        try:
            predicted_at_ms = int(row.get("predicted_at_ms") or 0)
        except (TypeError, ValueError):
            continue
        if cutoff_ms is not None and predicted_at_ms > cutoff_ms:
            continue
        try:
            outcome_updated_at_ms = int(row.get("outcome_updated_at_ms") or 0)
        except (TypeError, ValueError):
            outcome_updated_at_ms = 0
        if (
            audit_created_at_ms is not None
            and outcome_updated_at_ms > audit_created_at_ms
        ):
            continue
        source_config = source_config or config
        matching_rows.append(row)
    if source_config is None or not matching_rows:
        return {"available": False, "reason": "version_history_not_found"}

    try:
        original_sample_count = int(metadata.get("sample_count") or 0)
    except (TypeError, ValueError):
        original_sample_count = 0
    if 0 < original_sample_count < len(matching_rows):
        matching_rows = sorted(
            matching_rows,
            key=lambda row: (
                int(row.get("outcome_updated_at_ms") or 0),
                int(row.get("predicted_at_ms") or 0),
            ),
        )[:original_sample_count]

    try:
        from .prediction_ai_optimizer import build_prediction_ai_dataset
        from .prediction_optimizer import PredictionOptimizationUnavailable

        dataset = build_prediction_ai_dataset(
            matching_rows,
            source_config,
            current_config_version=source_version,
        )
    except PredictionOptimizationUnavailable:
        return {
            "available": False,
            "reason": "insufficient_version_history",
            "sample_count": len(matching_rows),
        }
    return {
        "available": True,
        "provenance": "database_recomputed_for_legacy_trace",
        "source_config_version": source_version,
        "history_cutoff_ms": cutoff_ms,
        "history": dataset.model_context["history"],
    }


_PREDICTION_AI_AUDIT_ACTIONS = (
    "monitor.prediction_algorithm.ai_optimize",
    "monitor.prediction_algorithm.ai_optimize_rejected",
)


def _prediction_ai_trace_out(
    request: Request,
    audit: AuditLog,
    metadata: Mapping[str, Any],
) -> dict[str, Any]:
    database_history_analysis = _prediction_ai_trace_database_analysis(
        request,
        metadata,
        audit_created_at=audit.created_at,
    )
    return {
        "audit_id": audit.id,
        "status": metadata.get("status", "saved"),
        "failure_category": metadata.get("failure_category"),
        "optimizer_key": metadata.get("optimizer_key"),
        "provider_code": metadata.get("provider"),
        "model_name": metadata.get("model"),
        "response_model": metadata.get("response_model"),
        "system_fingerprint": metadata.get("system_fingerprint"),
        "source_config_version": metadata.get("source_version"),
        "saved_config_version": metadata.get("saved_version"),
        "sample_count": metadata.get("sample_count"),
        "history_start_ms": metadata.get("history_start_ms"),
        "history_end_ms": metadata.get("history_end_ms"),
        "optimized_horizons": metadata.get("optimized_horizons", []),
        "summary": metadata.get("summary"),
        "reasoning_steps": metadata.get("reasoning_steps", []),
        "raw_model_output": metadata.get("raw_model_output"),
        "model_attempts": metadata.get("model_attempts", []),
        "normalization": metadata.get("normalization"),
        "horizons": metadata.get("horizons", []),
        "submitted_prompt": metadata.get("submitted_prompt"),
        "database_history_analysis": database_history_analysis,
        "usage": metadata.get("usage", {}),
        "created_at": audit.created_at,
    }


@router.get("/monitor/prediction-algorithm/ai-history")
def monitor_prediction_algorithm_ai_history(
    limit: int = Query(default=50, ge=1, le=100),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict[str, Any]:
    """List saved and rejected DeepSeek optimization audit records."""

    if not user.is_admin:
        raise HTTPException(status_code=403, detail="administrator access required")

    filters = (
        AuditLog.action.in_(_PREDICTION_AI_AUDIT_ACTIONS),
        AuditLog.resource_type == "admin_setting",
        AuditLog.resource_id == battle.ALGORITHM_SETTING_KEY,
    )
    total = int(
        db.scalar(select(func.count()).select_from(AuditLog).where(*filters)) or 0
    )
    audits = db.scalars(
        select(AuditLog)
        .where(*filters)
        .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
        .limit(limit)
    ).all()
    items: list[dict[str, Any]] = []
    for audit in audits:
        metadata = audit.metadata_json
        if not isinstance(metadata, dict):
            continue
        usage = metadata.get("usage")
        usage = usage if isinstance(usage, Mapping) else {}
        optimized_horizons = metadata.get("optimized_horizons")
        items.append(
            {
                "audit_id": audit.id,
                "status": metadata.get("status", "saved"),
                "failure_category": metadata.get("failure_category"),
                "source_config_version": metadata.get("source_version"),
                "saved_config_version": metadata.get("saved_version"),
                "model_name": metadata.get("model"),
                "response_model": metadata.get("response_model"),
                "sample_count": metadata.get("sample_count"),
                "optimized_horizons": (
                    optimized_horizons if isinstance(optimized_horizons, list) else []
                ),
                "summary": metadata.get("summary"),
                "total_tokens": usage.get("total_tokens"),
                "created_at": audit.created_at,
            }
        )
    return {"items": items, "total": total, "limit": limit}


@router.get("/monitor/prediction-algorithm/ai-history/{audit_id}")
def monitor_prediction_algorithm_ai_history_detail(
    audit_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict[str, Any]:
    """Return one historical DeepSeek trace with its version-level analysis."""

    if not user.is_admin:
        raise HTTPException(status_code=403, detail="administrator access required")

    audit = db.scalar(
        select(AuditLog).where(
            AuditLog.id == audit_id,
            AuditLog.action.in_(_PREDICTION_AI_AUDIT_ACTIONS),
            AuditLog.resource_type == "admin_setting",
            AuditLog.resource_id == battle.ALGORITHM_SETTING_KEY,
        )
    )
    metadata = audit.metadata_json if audit is not None else None
    if audit is None or not isinstance(metadata, dict):
        raise HTTPException(status_code=404, detail="AI analysis history record not found")
    return _prediction_ai_trace_out(request, audit, metadata)


@router.get("/monitor/prediction-algorithm/ai-trace")
def monitor_prediction_algorithm_ai_trace(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict[str, Any]:
    """Return the auditable prompt and rationale for the current AI-saved version."""

    if not user.is_admin:
        raise HTTPException(status_code=403, detail="administrator access required")

    setting = db.get(AdminSetting, battle.ALGORITHM_SETTING_KEY)
    current_version = int(setting.version if setting is not None else 0)
    audits = db.scalars(
        select(AuditLog)
        .where(
            AuditLog.action.in_(_PREDICTION_AI_AUDIT_ACTIONS),
            AuditLog.resource_type == "admin_setting",
            AuditLog.resource_id == battle.ALGORITHM_SETTING_KEY,
        )
        .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
        .limit(20)
    ).all()
    audit = None
    metadata = None
    for candidate in audits:
        candidate_metadata = candidate.metadata_json
        if not isinstance(candidate_metadata, dict):
            continue
        is_saved_current = candidate_metadata.get("saved_version") == current_version
        is_rejected_current = (
            candidate_metadata.get("status") == "rejected"
            and candidate_metadata.get("source_version") == current_version
        )
        if is_saved_current or is_rejected_current:
            audit = candidate
            metadata = candidate_metadata
            break
    if audit is None or metadata is None:
        raise HTTPException(
            status_code=404,
            detail="current prediction algorithm version has no DeepSeek trace",
        )
    return _prediction_ai_trace_out(request, audit, metadata)


@router.put("/monitor/prediction-algorithm")
def update_monitor_prediction_algorithm(
    payload: PredictionAlgorithmUpdate,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict[str, Any]:
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="administrator access required")
    _require_expected_user(request, user)
    expected_version = request.headers.get("X-QuantDesk-Algorithm-Version", "").strip()
    value = payload.model_dump(by_alias=True)
    setting = db.get(AdminSetting, battle.ALGORITHM_SETTING_KEY)
    current_version = int(setting.version if setting is not None else 0)
    if expected_version:
        try:
            parsed_expected_version = int(expected_version)
        except ValueError:
            raise HTTPException(
                status_code=400,
                detail="expected algorithm version is invalid",
            ) from None
        if parsed_expected_version != current_version:
            raise HTTPException(
                status_code=409,
                detail="prediction algorithm changed; reload it before saving",
            )
    if setting is None:
        setting = AdminSetting(
            key=battle.ALGORITHM_SETTING_KEY,
            value_json=value,
            version=1,
            updated_by=user.id,
        )
        db.add(setting)
    else:
        setting.value_json = value
        setting.version += 1
        setting.updated_by = user.id
        setting.updated_at = utcnow()
    db.flush()
    _audit(
        db,
        request,
        "monitor.prediction_algorithm.update",
        user.id,
        "admin_setting",
        battle.ALGORITHM_SETTING_KEY,
        {"version": setting.version, "config": value},
    )
    db.commit()
    battle.invalidate_algorithm_config_cache()
    return _prediction_algorithm_out(setting, user)


@router.get("/monitor/prediction-history")
def monitor_prediction_history(
    request: Request,
    page: int = Query(default=1, ge=1),
    direction: str | None = Query(default=None, pattern="^(long|short)$"),
    horizon: int | None = None,
    hit: str | None = Query(default=None, pattern="^(hit|miss)$"),
    period: str | None = Query(default=None, pattern="^(24h|7d|30d)$"),
    start_ms: int | None = Query(default=None, ge=0),
    end_ms: int | None = Query(default=None, ge=0),
    timezone_offset_minutes: int = Query(default=0, ge=-840, le=840),
    _: User = Depends(get_current_user),
) -> dict[str, Any]:
    period_ms = {
        "24h": 24 * 60 * 60 * 1_000,
        "7d": 7 * 24 * 60 * 60 * 1_000,
        "30d": 30 * 24 * 60 * 60 * 1_000,
    }
    if (start_ms is None) != (end_ms is None):
        raise HTTPException(status_code=422, detail="start_ms and end_ms must be provided together")
    if start_ms is not None and end_ms is not None:
        if period is not None:
            raise HTTPException(
                status_code=422, detail="period cannot be combined with a time range"
            )
        if start_ms >= end_ms:
            raise HTTPException(status_code=422, detail="prediction history time range is invalid")
        if end_ms - start_ms > 7 * 24 * 60 * 60 * 1_000:
            raise HTTPException(
                status_code=422, detail="prediction history time range exceeds 7 days"
            )
        predicted_after_ms = start_ms
        predicted_before_ms = end_ms
    else:
        predicted_after_ms = (
            int(datetime.now(UTC).timestamp() * 1_000) - period_ms[period]
            if period is not None
            else None
        )
        predicted_before_ms = None
    return _monitor(request).prediction_history(
        page,
        page_size=50,
        direction=direction,
        horizon_seconds=horizon,
        hit=hit,
        predicted_after_ms=predicted_after_ms,
        predicted_before_ms=predicted_before_ms,
        timezone_offset_minutes=timezone_offset_minutes,
    )


@router.get("/monitor/prediction-history/{prediction_id}/algorithm")
def monitor_prediction_algorithm_snapshot(
    prediction_id: uuid.UUID,
    request: Request,
    _: User = Depends(get_current_user),
) -> dict[str, Any]:
    snapshot = _monitor(request).prediction_algorithm_snapshot(str(prediction_id))
    if snapshot is None:
        raise HTTPException(status_code=404, detail="prediction history record not found")
    return snapshot


def _matching_strategies(db: Session, user_id: int) -> dict[str, list[dict[str, Any]]]:
    matches: dict[str, list[dict[str, Any]]] = {"long": [], "short": [], "neutral": []}
    strategies = db.scalars(
        select(UserStrategy).where(
            UserStrategy.user_id == user_id,
            UserStrategy.status == "active",
            UserStrategy.strategy_kind.in_(("full_strategy", "source_strategy")),
            UserStrategy.lifecycle_status.in_(BACKTEST_ELIGIBLE_STATUSES),
        )
    ).all()
    for strategy in strategies:
        if strategy.strategy_kind == "source_strategy":
            validation = (
                strategy.source_validation_json
                if isinstance(strategy.source_validation_json, dict)
                else {}
            )
            directions = validation.get("directions")
        else:
            spec = strategy.spec_json if isinstance(strategy.spec_json, dict) else {}
            directions = spec.get("directions")
        if not isinstance(directions, list):
            continue
        summary = {
            "id": strategy.public_id,
            "name": strategy.name,
            "version": strategy.version,
            "risk_level": strategy.risk_level,
        }
        for direction in {"long", "short"}.intersection(directions):
            matches[direction].append(summary)
    return matches


@router.get("/monitor/opportunities")
def monitor_opportunities(
    request: Request,
    limit: int = Query(default=80, ge=1, le=200),
    symbol: str | None = None,
    direction: str | None = Query(default=None, pattern="^(long|short|neutral)$"),
    include_expired: bool = False,
    include_ignored: bool = False,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict[str, Any]:
    items = _monitor(request).opportunities(
        user.id,
        limit,
        symbol=symbol,
        direction=direction,
        include_expired=include_expired,
        include_ignored=include_ignored,
    )
    matches = _matching_strategies(db, user.id)
    for item in items:
        item["matched_strategies"] = matches.get(item["direction"], [])
    return {"items": items}


@router.post("/monitor/opportunities/{opportunity_public_id}/preference")
def update_monitor_opportunity_preference(
    opportunity_public_id: str,
    payload: OpportunityPreferenceUpdate,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict[str, Any]:
    _require_expected_user(request, user)
    opportunity = db.scalar(
        select(MarketOpportunity).where(MarketOpportunity.public_id == opportunity_public_id)
    )
    if opportunity is None:
        raise HTTPException(status_code=404, detail="market opportunity not found")
    state = db.scalar(
        select(UserOpportunityState).where(
            UserOpportunityState.user_id == user.id,
            UserOpportunityState.opportunity_id == opportunity.id,
        )
    )
    if payload.action == "clear":
        if state is not None:
            db.delete(state)
        result_state = None
        notify_enabled = False
    else:
        new_state = "watching" if payload.action == "watch" else "ignored"
        should_alert = (
            new_state == "watching"
            and payload.notify_enabled
            and (state is None or state.state != "watching" or not state.notify_enabled)
        )
        if state is None:
            state = UserOpportunityState(
                user_id=user.id,
                opportunity_id=opportunity.id,
                state=new_state,
                notify_enabled=payload.notify_enabled,
                last_viewed_at=utcnow(),
                created_at=utcnow(),
                updated_at=utcnow(),
            )
            db.add(state)
        else:
            state.state = new_state
            state.notify_enabled = payload.notify_enabled
            state.last_viewed_at = utcnow()
            state.updated_at = utcnow()
        result_state = new_state
        notify_enabled = payload.notify_enabled
        if should_alert:
            db.execute(
                text(
                    """INSERT INTO alerts(
                           user_id,ts,symbol,kind,direction,score,message,detail,`read`
                       ) VALUES(:user_id,:ts,:symbol,'opportunity',:direction,:score,
                                :message,:detail,0)"""
                ),
                {
                    "user_id": user.id,
                    "ts": int(datetime.now(UTC).timestamp()),
                    "symbol": opportunity.symbol,
                    "direction": opportunity.direction,
                    "score": float(opportunity.quality_score),
                    "message": f"已关注 {opportunity.symbol} {opportunity.direction} 市场机会",
                    "detail": json.dumps(
                        {
                            "opportunity_id": opportunity.public_id,
                            "status": opportunity.status,
                            "expires_bar_time": opportunity.expires_bar_time,
                        },
                        ensure_ascii=False,
                    ),
                },
            )
    _audit(
        db,
        request,
        f"monitor.opportunity.{payload.action}",
        user.id,
        "market_opportunity",
        opportunity.public_id,
    )
    db.commit()
    return {
        "id": opportunity.public_id,
        "user_state": result_state,
        "notify_enabled": notify_enabled,
    }


@router.get("/monitor/alerts")
def monitor_alerts(
    request: Request,
    limit: int = Query(default=80, ge=1, le=200),
    user: User = Depends(get_current_user),
) -> list[dict]:
    return _monitor(request).alerts(user.id, limit)


@router.post("/monitor/alerts/read", response_model=MessageOut)
def mark_monitor_alerts_read(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> MessageOut:
    repository = _monitor(request)
    repository.mark_alerts_read(user.id)
    user.monitor_last_read_alert_id = repository.latest_alert_id(user.id)
    _audit(db, request, "monitor.alerts.read", user.id, "monitor", "alerts")
    db.commit()
    return MessageOut(message="monitor alerts marked as read")


@router.get("/monitor/watchlist")
def monitor_watchlist(user: User = Depends(get_current_user)) -> list[str]:
    return user.monitor_watchlist or []


@router.put("/monitor/watchlist")
def update_monitor_watchlist(
    payload: MonitorWatchlistUpdate,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list[str]:
    repository = _monitor(request)
    unknown = sorted(set(payload.symbols) - repository.symbol_set)
    if unknown:
        raise HTTPException(
            status_code=422, detail=f"unknown monitor symbols: {', '.join(unknown[:5])}"
        )
    user.monitor_watchlist = payload.symbols
    _audit(db, request, "monitor.watchlist.update", user.id, "user", str(user.id))
    db.commit()
    return user.monitor_watchlist


@router.get("/backtests/catalog")
def backtest_catalog(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict[str, Any]:
    strategies = [
        strategy
        for strategy in ensure_user_default_strategies(db, user.id)
        if strategy.status == "active"
        and strategy.strategy_kind in {"full_strategy", "source_strategy"}
        and strategy.lifecycle_status in BACKTEST_ELIGIBLE_STATUSES
        and (
            isinstance(strategy.spec_json, dict)
            or (
                strategy.strategy_kind == "source_strategy"
                and isinstance(strategy.source_code, str)
                and bool(strategy.source_code.strip())
            )
        )
    ]
    db.commit()
    try:
        catalog = _backtest(request).catalog()
    except BacktestUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from None
    # Market availability comes from the read-only historical store. Strategy
    # choices come exclusively from this user's MySQL strategy center.
    catalog["strategies"] = serialize_strategy_catalog(strategies)
    return _catalog_response(catalog)


@router.post("/backtests", status_code=status.HTTP_201_CREATED)
def create_backtest(
    payload: BacktestRunRequest,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict[str, Any]:
    user_id = user.id
    ensure_user_default_strategies(db, user_id)
    selected = get_user_strategy(db, user_id, payload.strategy_id)
    selected_revision = current_strategy_revision(db, selected) if selected is not None else None
    if selected is not None and not (
        selected.status == "active"
        and selected.strategy_kind in {"full_strategy", "source_strategy"}
        and selected_revision is not None
        and selected_revision.lifecycle_status in BACKTEST_ELIGIBLE_STATUSES
        and (
            isinstance(selected.spec_json, dict)
            or (
                selected.strategy_kind == "source_strategy"
                and isinstance(selected.source_code, str)
                and bool(selected.source_code.strip())
            )
        )
    ):
        raise HTTPException(
            status_code=422,
            detail="只有已通过校验并取得回测资格的当前策略修订才能运行回测",
        )
    database_strategy = strategy_to_catalog_item(selected) if selected is not None else None
    selected_strategy_id = selected.id if database_strategy is not None else None
    selected_revision_id = selected_revision.id if selected_revision is not None else None
    if selected_strategy_id is not None and selected_revision_id is None:
        raise HTTPException(status_code=409, detail="当前策略版本缺少不可变修订记录")
    db.commit()
    # Authentication only reads from MySQL. End that transaction before the CPU-heavy
    # synchronous replay so a pooled database connection is not held for the whole run.
    db.rollback()
    repository = _backtest(request)
    _acquire_backtest_slot(user_id)
    try:
        if database_strategy is not None:
            strategy = database_strategy
        else:
            # Keep the previous built-in IDs as a backwards-compatible API path;
            # the browser catalog never exposes them after this migration.
            try:
                catalog = repository.catalog()
            except BacktestUnavailable as exc:
                raise HTTPException(status_code=503, detail=str(exc)) from None
            strategy = _strategy_from_catalog(catalog, payload.strategy_id)
        config = _engine_config(payload, strategy)
        trigger_timeframe = _strategy_trigger_timeframe(strategy)
        if trigger_timeframe is not None:
            config["timeframe"] = trigger_timeframe
        try:
            if strategy.get("strategy_kind") == "source_strategy":
                source_code = strategy.get("source_code")
                if not isinstance(source_code, str) or not source_code.strip():
                    raise HTTPException(status_code=409, detail="strategy source is unavailable")
                raw_result = repository.run_source_strategy(
                    config,
                    source_code,
                    language=str(strategy.get("source_language") or "python"),
                )
            elif strategy.get("strategy_kind") == "full_strategy":
                spec = strategy.get("spec")
                if not isinstance(spec, dict):
                    raise HTTPException(status_code=409, detail="full strategy spec is unavailable")
                raw_result = repository.run_full_strategy(config, spec)
            else:
                raw_result = repository.run(config)
        except BacktestUnavailable as exc:
            detail = str(exc)
            raise HTTPException(status_code=_backtest_error_status(detail), detail=detail) from None
    finally:
        _release_backtest_slot(user_id)
    if not isinstance(raw_result, dict):
        raise HTTPException(status_code=503, detail="backtest engine returned an invalid result")

    try:
        account = raw_result.get("account")
        metrics = raw_result.get("metrics")
        trades = raw_result.get("trades")
        persisted_trades = raw_result.get("_all_trades", trades)
        equity_curve = raw_result.get("equity_curve")
        data_quality = raw_result.get("data_quality")
        if not isinstance(account, dict) or not isinstance(metrics, dict):
            raise ValueError("backtest result is missing account or metrics")
        if not isinstance(trades, list) or not isinstance(equity_curve, list):
            raise ValueError("backtest result is missing trades or equity curve")
        if not isinstance(persisted_trades, list):
            raise ValueError("backtest result contains an invalid full trade collection")
        if len(persisted_trades) > MAX_PERSISTED_TRADES:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"backtest generated more than {MAX_PERSISTED_TRADES} trades; "
                    "shorten the range or use a slower strategy"
                ),
            )
        if not isinstance(data_quality, dict):
            raise ValueError("backtest result is missing data quality information")

        now = utcnow()
        stored_config = payload.model_dump(mode="json")
        stored_config.update(
            {
                "timeframe": config["timeframe"],
                "start_ts": config["start_ts"],
                "end_ts": config["end_ts"],
                "params": _json_safe(config["params"]),
                "engine_key": config["strategy_id"],
            }
        )
        timestamp_unit = str(data_quality.get("timestamp_unit") or "seconds")
        actual_start_ts = data_quality.get("actual_start_ts")
        actual_end_ts = data_quality.get("actual_end_ts")
        actual_range = {
            "start_ts": actual_start_ts,
            "end_ts": actual_end_ts,
            "start_at": (
                _utc_iso(_utc_datetime(actual_start_ts)) if actual_start_ts is not None else None
            ),
            "end_at": (
                _utc_iso(_utc_datetime(actual_end_ts)) if actual_end_ts is not None else None
            ),
            "timestamp_unit": timestamp_unit,
        }
        strategy_version = _strategy_version(strategy)
        run = BacktestRun(
            public_id=str(uuid.uuid4()),
            user_id=user_id,
            user_strategy_id=selected_strategy_id,
            strategy_revision_id=selected_revision_id,
            strategy_id=payload.strategy_id,
            strategy_name=strategy["name"].strip(),
            symbol=payload.symbol,
            timeframe=config["timeframe"],
            status="completed",
            start_at=_utc_datetime(config["start_ts"]),
            end_at=_utc_datetime(config["end_ts"]),
            initial_capital=_decimal_value(payload.initial_capital, required=True),
            final_equity=_decimal_value(account.get("final_equity"), required=True),
            net_profit=_decimal_value(metrics.get("net_profit", account.get("net_profit"))),
            total_return_pct=_decimal_value(metrics.get("total_return_pct")),
            max_drawdown_pct=_decimal_value(metrics.get("max_drawdown_pct")),
            sharpe_ratio=_decimal_value(metrics.get("sharpe_ratio")),
            win_rate_pct=_decimal_value(metrics.get("win_rate_pct")),
            profit_factor=_decimal_value(metrics.get("profit_factor")),
            trade_count=int(metrics.get("trade_count", len(persisted_trades))),
            config_json=stored_config,
            metrics_json=_json_safe(metrics),
            equity_curve_json=_json_safe(equity_curve),
            data_quality_json=_json_safe(data_quality),
            metadata_json={
                "account": _json_safe(account),
                "app": {"name": "quantdesk_v2", "version": __version__},
                "engine": {"name": "BacktestRepository", "version": __version__},
                "strategy": {
                    "id": payload.strategy_id,
                    "name": strategy["name"].strip(),
                    "version": strategy_version,
                    "revision": strategy.get("version"),
                    "engine_key": config["strategy_id"],
                },
                "app_version": __version__,
                "engine_version": __version__,
                "strategy_version": strategy_version,
                "actual_data_range": actual_range,
                "timestamp_unit": timestamp_unit,
                "persisted_trade_count": len(persisted_trades),
                "response_trade_count": len(trades),
            },
            created_at=now,
            completed_at=now,
        )
        db.add(run)
        db.flush()
        selected_revision = None
        if selected_strategy_id is not None and selected_revision_id is not None:
            selected_revision = db.get(StrategyRevision, selected_revision_id)
            if selected_revision is None:
                raise ValueError("selected strategy revision disappeared during backtest")
        add_backtest_run_manifest(
            db,
            run,
            selected_revision,
            data_set_id=(
                f"backtest:{payload.symbol}:{config['timeframe']}:"
                f"{config['start_ts']}:{config['end_ts']}"
            ),
            extra={
                "data_quality": _json_safe(data_quality),
                "config": stored_config,
            },
        )

        known_trade_fields = {
            "side",
            "entry_ts",
            "exit_ts",
            "entry_price",
            "exit_price",
            "quantity",
            "gross_pnl",
            "fees",
            "net_pnl",
            "return_pct",
            "holding_bars",
            "exit_reason",
        }
        for raw_trade in persisted_trades:
            if not isinstance(raw_trade, dict):
                raise ValueError("backtest result contains an invalid trade")
            trade = BacktestTrade(
                run_id=run.id,
                user_id=user_id,
                side=str(raw_trade.get("side", "")),
                entry_at=_utc_datetime(raw_trade.get("entry_ts")),
                exit_at=_utc_datetime(raw_trade.get("exit_ts")),
                entry_price=_decimal_value(raw_trade.get("entry_price"), required=True),
                exit_price=_decimal_value(raw_trade.get("exit_price"), required=True),
                quantity=_quantity_value(raw_trade.get("quantity")),
                gross_pnl=_decimal_value(raw_trade.get("gross_pnl"), required=True),
                fees=_decimal_value(raw_trade.get("fees"), required=True),
                net_pnl=_decimal_value(raw_trade.get("net_pnl"), required=True),
                return_pct=_decimal_value(raw_trade.get("return_pct"), required=True),
                holding_bars=int(raw_trade.get("holding_bars", 0)),
                exit_reason=(
                    str(raw_trade["exit_reason"])[:64] if raw_trade.get("exit_reason") else None
                ),
                metadata_json=_json_safe(
                    {
                        key: value
                        for key, value in raw_trade.items()
                        if key not in known_trade_fields
                    }
                )
                or None,
            )
            run.trades.append(trade)
        _audit(db, request, "backtest.run.create", user_id, "backtest_run", str(run.id))
        db.commit()
    except (SQLAlchemyError, TypeError, ValueError):
        db.rollback()
        raise HTTPException(status_code=503, detail="backtest result could not be saved") from None
    return _run_detail(run)


router.include_router(backtests_read_router)
