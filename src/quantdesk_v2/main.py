from __future__ import annotations

import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware

from . import __version__
from .admin import UNUSUAL_WHALES_MARKET_DATA_KEY, initialize_admin_runtime
from .admin import router as admin_router
from .api import router
from .binance_client import BinanceAccountClient
from .binance_service import BinanceAccountService
from .binance_trading import BinanceUsdMTradingClient
from .config import Settings, get_settings
from .database import build_engine, engine
from .finnhub import FinnhubClient, FinnhubMarketStatusService, FinnhubWebhookReceiver
from .finnhub_quotes import FINNHUB_USAGE_SETTING_KEY, FinnhubUsQuoteService
from .interfaces.api.public_news import router as public_news_router
from .macro_market import MacroMarketService, configure_default_service, us_market_session
from .models import AdminSetting
from .news import _unusual_whales_api_key
from .strategy_routes import router as strategy_router
from .unusual_whales import UnusualWhalesMarketClient
from .unusual_whales_runtime import DEFAULT_CHANNEL_FLAGS, UnusualWhalesRuntime

FRONTEND_ROUTES = (
    "/login",
    "/monitor",
    "/ai-monitor",
    "/paper",
    "/live",
    "/overview",
    "/settings",
    "/strategies",
    "/backtest",
    "/orders",
    "/risk",
    "/audit",
)

_SENSITIVE_VALIDATION_MARKERS = (
    "api_key",
    "apikey",
    "api_secret",
    "password",
    "passwd",
    "secret",
    "token",
    "authorization",
    "cookie",
    "credential",
)


def _monitor_symbols(path: Path) -> tuple[str, ...]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return ()
    return tuple(
        str(item.get("symbol") or "").strip().upper()
        for item in payload.get("symbols", [])
        if isinstance(item, dict) and str(item.get("symbol") or "").strip()
    )


def _unusual_whales_runtime_config(database_engine) -> dict[str, Any]:
    default = {
        "enabled": True,
        "rest_enabled": True,
        "websocket_enabled": True,
        "channels": dict(DEFAULT_CHANNEL_FLAGS),
        "thresholds": {},
        "retention": {},
    }
    try:
        with Session(database_engine) as db:
            setting = db.get(AdminSetting, UNUSUAL_WHALES_MARKET_DATA_KEY)
            value = setting.value_json if setting is not None else None
    except SQLAlchemyError:
        return default
    if not isinstance(value, dict):
        return default
    channels = value.get("channels")
    thresholds = value.get("thresholds")
    retention = value.get("retention")
    return {
        "enabled": bool(value.get("enabled", True)),
        "rest_enabled": bool(value.get("rest_enabled", True)),
        "websocket_enabled": bool(value.get("websocket_enabled", True)),
        "channels": {
            **DEFAULT_CHANNEL_FLAGS,
            **(channels if isinstance(channels, dict) else {}),
        },
        "thresholds": thresholds if isinstance(thresholds, dict) else {},
        "retention": retention if isinstance(retention, dict) else {},
    }


def _finnhub_runtime_config(database_engine) -> dict[str, Any]:
    default = {"enabled": True, "market_open_only": True}
    try:
        with Session(database_engine) as db:
            setting = db.get(AdminSetting, FINNHUB_USAGE_SETTING_KEY)
            value = setting.value_json if setting is not None else None
    except SQLAlchemyError:
        return default
    if not isinstance(value, dict):
        return default
    return {
        "enabled": bool(value.get("enabled", True)),
        "market_open_only": True,
    }


def _is_sensitive_validation_field(value: object) -> bool:
    if not isinstance(value, str):
        return False
    normalized = value.strip().lower().replace("-", "_").replace(" ", "_")
    return any(marker in normalized for marker in _SENSITIVE_VALIDATION_MARKERS)


def _redact_sensitive_validation_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: (
                "[REDACTED]"
                if _is_sensitive_validation_field(key)
                else _redact_sensitive_validation_value(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_redact_sensitive_validation_value(item) for item in value]
    return value


async def _safe_request_validation_error(
    _: Request, exc: RequestValidationError
) -> JSONResponse:
    errors: list[dict[str, Any]] = []
    for raw_error in exc.errors():
        error = dict(raw_error)
        location = error.get("loc")
        sensitive_location = isinstance(location, (list, tuple)) and any(
            _is_sensitive_validation_field(item) for item in location
        )
        if "input" in error:
            error["input"] = (
                "[REDACTED]"
                if sensitive_location or error.get("type") == "json_invalid"
                else _redact_sensitive_validation_value(error["input"])
            )
        if "ctx" in error:
            error["ctx"] = _redact_sensitive_validation_value(error["ctx"])
        errors.append(error)
    return JSONResponse(
        status_code=422,
        content=jsonable_encoder({"detail": errors}),
        headers={"Cache-Control": "no-store"},
    )


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, max_request_bytes: int):
        super().__init__(app)
        self.max_request_bytes = max_request_bytes

    async def dispatch(self, request: Request, call_next):
        length = request.headers.get("content-length")
        if length and length.isdigit() and int(length) > self.max_request_bytes:
            return JSONResponse({"detail": "request body too large"}, status_code=413)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'"
        )
        return response


def create_app(settings: Settings | None = None) -> FastAPI:
    runtime_settings = settings or get_settings()
    database_engine = build_engine(runtime_settings) if settings is not None else engine

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        runtime_settings.validate_runtime()
        if settings is None:
            # The production ASGI app owns the market collectors and the
            # multi-account paper executor. Explicitly constructed apps are
            # used by tests/tools and must not start perpetual worker threads.
            from . import (
                ai_monitor,
                battle,
                live_engine,
                market_engine,
                market_store,
                underlying_quotes,
            )

            market_store.configure_engine(database_engine)
            initialize_admin_runtime(database_engine)
            uw_runtime_config = _unusual_whales_runtime_config(database_engine)
            uw_enabled = bool(uw_runtime_config.get("enabled", True))
            app.state.unusual_whales_runtime.apply_config(
                uw_runtime_config["channels"],
                websocket_enabled=uw_enabled and uw_runtime_config["websocket_enabled"],
                rest_enabled=uw_enabled and uw_runtime_config["rest_enabled"],
                thresholds=uw_runtime_config["thresholds"],
                retention=uw_runtime_config["retention"],
            )
            app.state.unusual_whales_runtime.start()
            app.state.finnhub_us_quote_service.set_enabled(
                bool(_finnhub_runtime_config(database_engine).get("enabled", True))
            )
            market_engine.start()
            if runtime_settings.ai_monitor_background_workers_enabled:
                ai_monitor.start(
                    database_engine,
                    runtime_settings.credential_master_key.get_secret_value(),
                    runtime_settings.monitor_symbols_config,
                )
            battle.start()
            underlying_quotes.start()
            live_engine.configure(
                runtime_settings,
                app.state.binance_service,
                app.state.binance_trading_client,
            )
            live_engine.start()
            app.state.finnhub_us_quote_service.start()
        try:
            yield
        finally:
            if settings is None:
                app.state.unusual_whales_runtime.stop()
                app.state.finnhub_us_quote_service.stop()
                underlying_quotes.stop()

    app = FastAPI(
        title="QuantDesk V2",
        version=__version__,
        docs_url="/api/docs" if runtime_settings.app_env != "production" else None,
        redoc_url=None,
        lifespan=lifespan,
    )
    app.state.settings = runtime_settings
    app.state.database_engine = database_engine
    app.state.binance_service = BinanceAccountService(
        BinanceAccountClient(
            runtime_settings.binance_futures_base_url,
            runtime_settings.binance_portfolio_base_url,
            recv_window_ms=runtime_settings.binance_futures_recv_window_ms,
            timeout_seconds=runtime_settings.binance_futures_timeout_seconds,
        )
    )
    app.state.binance_trading_client = BinanceUsdMTradingClient(
        runtime_settings.binance_futures_base_url,
        recv_window_ms=runtime_settings.binance_futures_recv_window_ms,
        timeout_seconds=runtime_settings.binance_futures_timeout_seconds,
    )
    finnhub_client = FinnhubClient(
        runtime_settings.finnhub_base_url,
        runtime_settings.finnhub_api_key.get_secret_value(),
        timeout_seconds=runtime_settings.finnhub_timeout_seconds,
    )
    app.state.finnhub_client = finnhub_client
    app.state.finnhub_market_status_service = FinnhubMarketStatusService(
        finnhub_client,
        cache_seconds=runtime_settings.finnhub_market_status_cache_seconds,
        stale_seconds=runtime_settings.finnhub_market_status_stale_seconds,
    )
    initial_finnhub_config = _finnhub_runtime_config(database_engine)

    def is_us_regular_market() -> bool:
        return us_market_session(datetime.now(UTC))["key"] == "regular"

    app.state.finnhub_us_quote_service = FinnhubUsQuoteService(
        finnhub_client,
        runtime_settings.monitor_symbols_config,
        poll_seconds=runtime_settings.finnhub_quote_poll_seconds,
        stale_seconds=runtime_settings.finnhub_quote_stale_seconds,
        websocket_enabled=runtime_settings.finnhub_websocket_enabled,
        engine=database_engine,
        enabled=bool(initial_finnhub_config.get("enabled", True)),
        market_open_checker=is_us_regular_market,
    )
    initial_uw_runtime_config = _unusual_whales_runtime_config(database_engine)
    initial_uw_enabled = bool(initial_uw_runtime_config.get("enabled", True))
    app.state.unusual_whales_market_client = UnusualWhalesMarketClient(
        _unusual_whales_api_key,
        timeout_seconds=runtime_settings.finnhub_timeout_seconds,
    )
    app.state.unusual_whales_runtime = UnusualWhalesRuntime(
        database_engine,
        _unusual_whales_api_key,
        _monitor_symbols(runtime_settings.monitor_symbols_config),
        channel_flags=DEFAULT_CHANNEL_FLAGS,
        websocket_enabled=(
            initial_uw_enabled
            and bool(initial_uw_runtime_config.get("websocket_enabled", True))
        ),
        rest_client=app.state.unusual_whales_market_client,
        rest_enabled=(
            initial_uw_enabled
            and bool(initial_uw_runtime_config.get("rest_enabled", True))
        ),
        market_open_checker=is_us_regular_market,
        market_open_only=True,
    )
    app.state.unusual_whales_stream = app.state.unusual_whales_runtime
    app.state.unusual_whales_stream_client = app.state.unusual_whales_runtime.stream
    app.state.unusual_whales_channel_health = (
        app.state.unusual_whales_runtime.channel_health_snapshot
    )

    def apply_unusual_whales_runtime_config(config: dict[str, Any]) -> None:
        enabled = bool(config.get("enabled", True))
        app.state.unusual_whales_runtime.apply_config(
            config.get("channels") or DEFAULT_CHANNEL_FLAGS,
            websocket_enabled=enabled and bool(config.get("websocket_enabled", True)),
            rest_enabled=enabled and bool(config.get("rest_enabled", True)),
            thresholds=(
                config.get("thresholds")
                if isinstance(config.get("thresholds"), dict)
                else {}
            ),
            retention=(
                config.get("retention")
                if isinstance(config.get("retention"), dict)
                else {}
            ),
        )
        macro_service = getattr(app.state, "macro_market_service", None)
        if macro_service is not None:
            macro_service.set_unusual_whales_enabled(enabled)

    app.state.apply_unusual_whales_runtime_config = apply_unusual_whales_runtime_config

    def apply_finnhub_runtime_config(config: dict[str, Any]) -> None:
        enabled = bool(config.get("enabled", True))
        app.state.finnhub_us_quote_service.set_enabled(enabled)
        macro_service = getattr(app.state, "macro_market_service", None)
        if macro_service is not None:
            macro_service.set_finnhub_enabled(enabled)

    app.state.apply_finnhub_runtime_config = apply_finnhub_runtime_config
    app.state.macro_market_service = MacroMarketService(
        finnhub_client,
        app.state.finnhub_us_quote_service,
        app.state.unusual_whales_market_client,
        engine=database_engine,
        cache_seconds=5,
        finnhub_enabled=bool(initial_finnhub_config.get("enabled", True)),
        unusual_whales_enabled=initial_uw_enabled,
        unusual_whales_cache_seconds=5 * 60,
        stale_seconds=runtime_settings.finnhub_market_status_stale_seconds,
    )
    configure_default_service(app.state.macro_market_service)
    app.state.finnhub_webhook_receiver = FinnhubWebhookReceiver(
        runtime_settings.finnhub_webhook_secret.get_secret_value()
    )
    app.add_exception_handler(RequestValidationError, _safe_request_validation_error)
    app.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=runtime_settings.allowed_hosts,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=runtime_settings.allowed_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
        allow_headers=[
            "Authorization",
            "Content-Type",
            "X-API-Key",
            "X-QuantDesk-User-ID",
        ],
    )
    app.add_middleware(
        SecurityHeadersMiddleware,
        max_request_bytes=runtime_settings.max_request_bytes,
    )
    app.include_router(router)
    app.include_router(admin_router)
    app.include_router(strategy_router)
    app.include_router(public_news_router)

    if runtime_settings.static_dir.is_dir():
        app.mount(
            "/assets",
            StaticFiles(directory=runtime_settings.static_dir),
            name="assets",
        )

        def index() -> FileResponse:
            return FileResponse(runtime_settings.static_dir / "index.html")

        def admin_index() -> RedirectResponse:
            target = "/next/admin/#overview"
            if runtime_settings.app_env.lower() != "production":
                target = "http://127.0.0.1:5173/next/admin/#overview"
            return RedirectResponse(url=target, status_code=308)

        app.add_api_route(
            "/",
            index,
            methods=["GET"],
            include_in_schema=False,
            name="frontend_index",
        )
        for frontend_route in FRONTEND_ROUTES:
            app.add_api_route(
                frontend_route,
                index,
                methods=["GET"],
                include_in_schema=False,
                name=f"frontend_{frontend_route.removeprefix('/')}",
            )

        for admin_route in ("/admin", "/admin/login"):
            app.add_api_route(
                admin_route,
                admin_index,
                methods=["GET"],
                include_in_schema=False,
                name=f"admin_{admin_route.removeprefix('/').replace('/', '_')}",
            )

        def legacy_credentials_redirect() -> RedirectResponse:
            return RedirectResponse(url="/settings", status_code=308)

        app.add_api_route(
            "/credentials",
            legacy_credentials_redirect,
            methods=["GET"],
            include_in_schema=False,
            name="frontend_credentials_redirect",
        )

    if runtime_settings.react_static_dir.is_dir():
        # The React application uses hash routing and is intentionally exposed
        # under a canary path while the existing frontend remains the default.
        app.mount(
            "/next",
            StaticFiles(directory=runtime_settings.react_static_dir, html=True),
            name="react_frontend",
        )

    return app


app = create_app()
