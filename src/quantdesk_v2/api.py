from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from threading import Lock
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session, selectinload

from . import __version__
from .backtest import BacktestRepository, BacktestUnavailable
from .binance_client import BinanceAccountClient, BinanceAccountClientError
from .binance_performance import (
    build_binance_performance,
    empty_binance_performance,
    history_status_for_month,
    month_window_ms,
)
from .database import get_db
from .dependencies import get_current_user
from .models import AuditLog, BacktestRun, BacktestTrade, PaperAccount, User, UserSession, utcnow
from .monitor import MonitorRepository, MonitorUnavailable
from .schemas import (
    BacktestRunRequest,
    BinanceAccountSummary,
    BinanceCredentialStatus,
    BinanceCredentialUpdate,
    BinancePerformanceOut,
    DashboardPerformanceOut,
    HealthOut,
    LoginRequest,
    LogoutRequest,
    MessageOut,
    MonitorWatchlistUpdate,
    PaperAccountCreateRequest,
    PaperAccountStatusUpdate,
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
from .strategy_catalog import (
    ensure_user_default_strategies,
    get_user_strategy,
    serialize_strategy_catalog,
    strategy_to_catalog_item,
)

router = APIRouter(prefix="/api/v2")
MIN_PERSISTED_QUANTITY = Decimal("0.000000000000000001")
MAX_CONCURRENT_BACKTESTS = 2
MAX_PERSISTED_TRADES = 10_000
_backtest_guard = Lock()
_active_backtest_users: set[int] = set()


def _acquire_backtest_slot(user_id: int) -> None:
    with _backtest_guard:
        if user_id in _active_backtest_users:
            raise HTTPException(
                status_code=409, detail="current user already has a backtest running"
            )
        if len(_active_backtest_users) >= MAX_CONCURRENT_BACKTESTS:
            raise HTTPException(status_code=429, detail="backtest capacity is busy; retry shortly")
        _active_backtest_users.add(user_id)


def _release_backtest_slot(user_id: int) -> None:
    with _backtest_guard:
        _active_backtest_users.discard(user_id)


def _backtest(request: Request) -> BacktestRepository:
    settings = request.app.state.settings
    try:
        return BacktestRepository(request.app.state.database_engine, settings.monitor_symbols_config)
    except BacktestUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from None


def _monitor(request: Request) -> MonitorRepository:
    settings = request.app.state.settings
    try:
        return MonitorRepository(request.app.state.database_engine, settings.monitor_symbols_config)
    except MonitorUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from None


def _client_ip(request: Request) -> str | None:
    return request.client.host[:45] if request.client else None


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
) -> None:
    db.add(
        AuditLog(
            user_id=user_id,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            ip_address=_client_ip(request),
        )
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
    timeframe_values: set[str] = set()
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
        if not normalized_periods:
            continue
        symbol_item = _json_safe(dict(raw_symbol))
        symbol_item["timeframes"] = normalized_periods
        symbols.append(symbol_item)
    limits = _json_safe(catalog.get("limits", {}))
    if not isinstance(limits, dict):
        limits = {}
    limits.update(
        {
            "max_concurrent_backtests": MAX_CONCURRENT_BACKTESTS,
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


def _backtest_error_status(message: str) -> int:
    unavailable_markers = (
        "database",
        "config is invalid",
        "query failed",
        "cannot be opened",
        "invalid timestamp",
    )
    return 503 if any(marker in message.lower() for marker in unavailable_markers) else 422


def _run_summary(run: BacktestRun) -> dict[str, Any]:
    return {
        "id": run.id,
        "run_id": run.id,
        "strategy_id": run.strategy_id,
        "strategy_name": run.strategy_name,
        "symbol": run.symbol,
        "timeframe": run.timeframe,
        "status": run.status,
        "start_at": _utc_iso(run.start_at),
        "end_at": _utc_iso(run.end_at),
        "start_date": run.start_at.date().isoformat(),
        "end_date": run.end_at.date().isoformat(),
        "initial_capital": _float_value(run.initial_capital),
        "final_equity": _float_value(run.final_equity),
        "net_profit": _float_value(run.net_profit),
        "total_return_pct": _float_value(run.total_return_pct),
        "max_drawdown_pct": _float_value(run.max_drawdown_pct),
        "sharpe_ratio": _float_value(run.sharpe_ratio),
        "win_rate_pct": _float_value(run.win_rate_pct),
        "profit_factor": _float_value(run.profit_factor),
        "trade_count": run.trade_count,
        "metrics_json": _json_safe(run.metrics_json or {}),
        "error": run.error,
        "created_at": _utc_iso(run.created_at),
        "completed_at": _utc_iso(run.completed_at) if run.completed_at else None,
    }


def _trade_response(trade: BacktestTrade) -> dict[str, Any]:
    return {
        **_json_safe(trade.metadata_json or {}),
        "id": trade.id,
        "side": trade.side,
        "entry_ts": int(trade.entry_at.replace(tzinfo=UTC).timestamp()),
        "exit_ts": int(trade.exit_at.replace(tzinfo=UTC).timestamp()),
        "entry_at": _utc_iso(trade.entry_at),
        "exit_at": _utc_iso(trade.exit_at),
        "entry_price": _float_value(trade.entry_price),
        "exit_price": _float_value(trade.exit_price),
        "quantity": _float_value(trade.quantity),
        "gross_pnl": _float_value(trade.gross_pnl),
        "fees": _float_value(trade.fees),
        "net_pnl": _float_value(trade.net_pnl),
        "return_pct": _float_value(trade.return_pct),
        "holding_bars": trade.holding_bars,
        "exit_reason": trade.exit_reason,
    }


def _run_detail(run: BacktestRun) -> dict[str, Any]:
    metadata = run.metadata_json if isinstance(run.metadata_json, dict) else {}
    stored_account = metadata.get("account") if isinstance(metadata.get("account"), dict) else {}
    account = {
        **_json_safe(stored_account),
        "initial_capital": _float_value(run.initial_capital),
        "final_equity": _float_value(run.final_equity),
        "net_profit": _float_value(run.net_profit),
    }
    metrics = _json_safe(run.metrics_json or {})
    metrics.update(
        {
            "net_profit": _float_value(run.net_profit),
            "total_return_pct": _float_value(run.total_return_pct),
            "max_drawdown_pct": _float_value(run.max_drawdown_pct),
            "sharpe_ratio": _float_value(run.sharpe_ratio),
            "win_rate_pct": _float_value(run.win_rate_pct),
            "profit_factor": _float_value(run.profit_factor),
            "trade_count": run.trade_count,
        }
    )
    returned_count = metadata.get("response_trade_count")
    if not isinstance(returned_count, int):
        returned_count = (run.data_quality_json or {}).get("trades_returned")
    if isinstance(returned_count, int) and returned_count >= 0:
        response_trades = run.trades[-returned_count:] if returned_count else []
    else:
        response_trades = run.trades
    return {
        "run": _run_summary(run),
        "result": {
            "account": account,
            "metrics": metrics,
            "equity_curve": _json_safe(run.equity_curve_json or []),
            "trades": [_trade_response(trade) for trade in response_trades],
            "data_quality": _json_safe(run.data_quality_json or {}),
        },
    }


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


@router.get("/health", response_model=HealthOut)
def health(request: Request, db: Session = Depends(get_db)) -> HealthOut:
    try:
        db.execute(text("SELECT 1"))
    except SQLAlchemyError:
        raise HTTPException(status_code=503, detail="database unavailable") from None
    settings = request.app.state.settings
    return HealthOut(
        status="ok",
        database="ok",
        version=__version__,
        database_dialect=db.bind.dialect.name if db.bind else "unknown",
        tls_required=settings.db_ssl_required,
    )


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

    settings = request.app.state.settings
    client = BinanceAccountClient(
        settings.binance_futures_base_url,
        settings.binance_portfolio_base_url,
        recv_window_ms=settings.binance_futures_recv_window_ms,
        timeout_seconds=settings.binance_futures_timeout_seconds,
    )
    try:
        snapshot = client.account(api_key, api_secret)
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
                       user_id,symbol,amt,side,entry_price,mark_price,upnl,leverage,ts
                   ) VALUES(
                       :user_id,:symbol,:amt,:side,:entry_price,:mark_price,:upnl,:leverage,:ts
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

    settings = request.app.state.settings
    client = BinanceAccountClient(
        settings.binance_futures_base_url,
        settings.binance_portfolio_base_url,
        recv_window_ms=settings.binance_futures_recv_window_ms,
        timeout_seconds=settings.binance_futures_timeout_seconds,
    )
    try:
        snapshot = client.account(api_key, api_secret)
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
        history = client.income_history(
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
    api_key = payload.api_key.get_secret_value().strip()
    api_secret = payload.api_secret.get_secret_value().strip()
    cipher = CredentialCipher(request.app.state.settings.credential_master_key.get_secret_value())
    user.binance_api_key_encrypted = cipher.encrypt(api_key)
    user.binance_api_secret_encrypted = cipher.encrypt(api_secret)
    user.binance_key_fingerprint = api_key_fingerprint(api_key)
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
    user.binance_api_key_encrypted = None
    user.binance_api_secret_encrypted = None
    user.binance_key_fingerprint = None
    user.binance_permissions = None
    user.binance_key_updated_at = None
    user.binance_key_version += 1
    _audit(db, request, "binance.credentials.delete", user.id, "user", str(user.id))
    db.commit()
    return MessageOut(message="Binance credentials removed")


@router.get("/monitor/overview")
def monitor_overview(
    request: Request,
    user: User = Depends(get_current_user),
) -> dict:
    return _monitor(request).overview(user.monitor_watchlist or [])


@router.get("/monitor/breadth")
def monitor_breadth(
    request: Request,
    _: User = Depends(get_current_user),
) -> dict:
    return _monitor(request).breadth()


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


@router.get("/monitor/news")
def monitor_news(
    request: Request,
    limit: int = Query(default=60, ge=1, le=100),
    _: User = Depends(get_current_user),
) -> list[dict]:
    return _monitor(request).news(limit)


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


@router.get("/monitor/klines")
def monitor_klines(
    request: Request,
    symbol: str,
    tf: str = Query(default="1h", pattern="^(15m|1h|4h)$"),
    limit: int = Query(default=120, ge=20, le=300),
    _: User = Depends(get_current_user),
) -> list[dict]:
    return _monitor(request).klines(symbol, tf, limit)


@router.get("/monitor/score")
def monitor_score(
    request: Request,
    symbol: str,
    _: User = Depends(get_current_user),
) -> dict:
    return _monitor(request).score_detail(symbol)


@router.get("/monitor/report")
def monitor_report(
    request: Request,
    symbol: str,
    _: User = Depends(get_current_user),
) -> dict:
    return _monitor(request).report(symbol)


def _paper_account_record(
    db: Session, user_id: int, public_id: str | None = None
) -> PaperAccount | None:
    statement = select(PaperAccount).where(
        PaperAccount.user_id == user_id,
        PaperAccount.status != "archived",
    )
    if public_id:
        statement = statement.where(PaperAccount.public_id == public_id)
    else:
        statement = statement.order_by(
            PaperAccount.status.asc(), PaperAccount.created_at.asc(), PaperAccount.id.asc()
        )
    return db.scalar(statement.limit(1))


def _paper_account_out(account: PaperAccount) -> dict[str, Any]:
    snapshot = account.strategy_snapshot_json or {}
    return {
        "id": account.public_id,
        "name": account.name,
        "status": account.status,
        "strategy_id": snapshot.get("public_id"),
        "strategy_name": snapshot.get("name"),
        "engine_key": snapshot.get("engine_key"),
        "initial_balance": float(account.initial_balance),
        "balance": float(account.balance),
        "config": account.config_json,
        "started_at": account.started_at,
        "last_tick_at": account.last_tick_at,
        "created_at": account.created_at,
        "updated_at": account.updated_at,
    }


def _paper_response(data: dict) -> dict:
    return {**data, "permissions": {"can_reset": True}}


@router.get("/paper/accounts")
def list_paper_accounts(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list[dict[str, Any]]:
    accounts = db.scalars(
        select(PaperAccount)
        .where(PaperAccount.user_id == user.id, PaperAccount.status != "archived")
        .order_by(PaperAccount.created_at, PaperAccount.id)
    ).all()
    return [_paper_account_out(account) for account in accounts]


@router.post("/paper/accounts", status_code=status.HTTP_201_CREATED)
def create_paper_account(
    payload: PaperAccountCreateRequest,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict[str, Any]:
    existing_count = db.scalar(
        select(func.count(PaperAccount.id)).where(
            PaperAccount.user_id == user.id, PaperAccount.status != "archived"
        )
    )
    if int(existing_count or 0) >= 20:
        raise HTTPException(status_code=409, detail="paper account limit reached")
    strategy = get_user_strategy(db, user.id, payload.strategy_id)
    if strategy is None or strategy.status != "active":
        raise HTTPException(status_code=404, detail="active strategy not found")
    risk = dict(strategy.risk_defaults_json or {})
    config: dict[str, Any] = {
        "leverage": risk.get("leverage", 20),
        "max_positions": risk.get("max_positions", 15),
        "position_size_pct": risk.get("position_size_pct", 10),
        "margin_cap": risk.get("margin_cap", 0.8),
        "fee_bps": risk.get("fee_bps", 5),
        "slippage_bps": risk.get("slippage_bps", 3),
        "stop_loss_pct": risk.get("stop_loss_pct", 3),
        "take_profit_pct": risk.get("take_profit_pct", 5),
        "max_holding_bars": risk.get("max_holding_bars", 12),
    }
    for key in ("leverage", "max_positions", "position_size_pct", "margin_cap"):
        value = getattr(payload, key)
        if value is not None:
            config[key] = value
    account = PaperAccount(
        user_id=user.id,
        strategy_id=strategy.id,
        name=payload.name,
        initial_balance=Decimal(str(payload.initial_balance)),
        balance=Decimal(str(payload.initial_balance)),
        config_json=config,
        strategy_snapshot_json={
            "public_id": strategy.public_id,
            "name": strategy.name,
            "engine_key": strategy.engine_key,
            "version": strategy.version,
            "parameters": strategy.parameters_json,
            "risk_defaults": strategy.risk_defaults_json,
        },
    )
    db.add(account)
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="paper account name already exists") from exc
    _audit(db, request, "paper.account.create", user.id, "paper_account", account.public_id)
    db.commit()
    db.refresh(account)
    return _paper_account_out(account)


@router.patch("/paper/accounts/{account_id}")
def update_paper_account_status(
    account_id: str,
    payload: PaperAccountStatusUpdate,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict[str, Any]:
    account = _paper_account_record(db, user.id, account_id)
    if account is None:
        raise HTTPException(status_code=404, detail="paper account not found")
    if payload.status == "archived":
        open_count = db.scalar(
            text(
                """SELECT COUNT(*) FROM paper_positions
                   WHERE paper_account_id=:account_id AND user_id=:user_id"""
            ),
            {"account_id": account.id, "user_id": user.id},
        )
        if int(open_count or 0):
            raise HTTPException(status_code=409, detail="close positions before archiving")
    account.status = payload.status
    _audit(db, request, "paper.account.status", user.id, "paper_account", account.public_id)
    db.commit()
    db.refresh(account)
    return _paper_account_out(account)


@router.get("/paper")
def paper_account(
    request: Request,
    account_id: str | None = Query(default=None, min_length=36, max_length=36),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    account = _paper_account_record(db, user.id, account_id)
    if account is None:
        raise HTTPException(status_code=404, detail="paper account not found")
    return _paper_response(_monitor(request).paper(user.id, account.id))


@router.get("/dashboard/performance", response_model=DashboardPerformanceOut)
def dashboard_performance(
    request: Request,
    response: Response,
    month: str | None = Query(default=None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    account_id: str | None = Query(default=None, min_length=36, max_length=36),
    timezone_offset_minutes: int = Query(default=0, ge=-720, le=840),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> DashboardPerformanceOut:
    response.headers["Cache-Control"] = "private, no-store"
    local_now = datetime.now(UTC) + timedelta(minutes=timezone_offset_minutes)
    selected_month = month or local_now.strftime("%Y-%m")
    selected_year = int(selected_month[:4])
    if not 2000 <= selected_year <= 2100:
        raise HTTPException(status_code=422, detail="performance month is out of range")
    account = _paper_account_record(db, user.id, account_id)
    if account is None:
        raise HTTPException(status_code=404, detail="paper account not found")
    try:
        return DashboardPerformanceOut.model_validate(
            _monitor(request).paper_performance(
                user.id, account.id, selected_month, timezone_offset_minutes
            )
        )
    except MonitorUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from None


@router.post("/paper/reset")
def reset_paper_account(
    request: Request,
    account_id: str | None = Query(default=None, min_length=36, max_length=36),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    account = _paper_account_record(db, user.id, account_id)
    if account is None:
        raise HTTPException(status_code=404, detail="paper account not found")
    data = _monitor(request).reset_paper(user.id, account.id)
    _audit(db, request, "paper.account.reset", user.id, "paper_account", account.public_id)
    db.commit()
    return _paper_response(data)


@router.get("/backtests/catalog")
def backtest_catalog(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict[str, Any]:
    strategies = ensure_user_default_strategies(db, user.id)
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
    database_strategy = (
        strategy_to_catalog_item(selected)
        if selected is not None and selected.status == "active"
        else None
    )
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
        try:
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
            user_id=user_id,
            strategy_id=payload.strategy_id,
            strategy_name=strategy["name"].strip(),
            symbol=payload.symbol,
            timeframe=payload.timeframe,
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


@router.get("/backtests")
def list_backtests(
    limit: int = Query(default=12, ge=1, le=100),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict[str, list[dict[str, Any]]]:
    runs = db.scalars(
        select(BacktestRun)
        .where(BacktestRun.user_id == user.id)
        .order_by(BacktestRun.created_at.desc(), BacktestRun.id.desc())
        .limit(limit)
    ).all()
    return {"items": [_run_summary(run) for run in runs]}


@router.get("/backtests/{run_id}")
def get_backtest(
    run_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict[str, Any]:
    run = db.scalar(
        select(BacktestRun)
        .options(selectinload(BacktestRun.trades))
        .where(BacktestRun.id == run_id, BacktestRun.user_id == user.id)
    )
    if run is None:
        raise HTTPException(status_code=404, detail="backtest run not found")
    return _run_detail(run)
