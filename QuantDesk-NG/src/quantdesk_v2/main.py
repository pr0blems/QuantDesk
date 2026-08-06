from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.utils import get_openapi
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware

from . import __version__
from .admin import router as admin_router
from .api import router
from .config import Settings, get_settings
from .database import build_engine, engine
from .metrics import MetricsMiddleware, RuntimeMetrics
from .metrics import router as metrics_router
from .optimization_governance import router as optimization_governance_router
from .proxy_routes import router as proxy_router
from .reliability import router as reliability_router
from .saas import router as saas_router
from .strategy_routes import router as strategy_router

FRONTEND_ROUTES = (
    "/login",
    "/monitor",
    "/paper",
    "/overview",
    "/settings",
    "/strategies",
    "/backtest",
    "/orders",
    "/risk",
    "/audit",
    "/proxy",
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
    # Proxy subscription payloads can contain node passwords even when their
    # field name is simply ``content``; never reflect them in validation errors.
    "content",
)


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


async def _safe_request_validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
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
        # API processes are deliberately stateless with respect to perpetual
        # collectors and trading loops. Starting workers from ASGI lifespan
        # duplicates jobs when Uvicorn uses multiple processes or reloads.
        yield

    app = FastAPI(
        title="QuantDesk V2",
        version=__version__,
        docs_url="/api/docs" if runtime_settings.app_env != "production" else None,
        redoc_url=None,
        lifespan=lifespan,
    )
    app.state.settings = runtime_settings
    app.state.database_engine = database_engine
    app.state.metrics = RuntimeMetrics()
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
        allow_headers=["Authorization", "Content-Type", "X-QuantDesk-User-ID"],
    )
    app.add_middleware(
        SecurityHeadersMiddleware,
        max_request_bytes=runtime_settings.max_request_bytes,
    )
    app.add_middleware(MetricsMiddleware)
    app.include_router(router)
    app.include_router(admin_router)
    app.include_router(reliability_router)
    app.include_router(proxy_router)
    app.include_router(optimization_governance_router)
    app.include_router(strategy_router)
    app.include_router(saas_router)
    app.include_router(metrics_router)

    def public_v1_openapi() -> JSONResponse:
        """Publish only the stable external contract, never internal workbench routes."""

        schema = get_openapi(
            title="QuantDesk External API",
            version="v1",
            description="Stable API contract for external integrations.",
            routes=saas_router.routes,
        )
        return JSONResponse(schema)

    app.add_api_route(
        "/api/v1/openapi.json",
        public_v1_openapi,
        methods=["GET"],
        include_in_schema=False,
        name="public_v1_openapi",
    )

    if runtime_settings.static_dir.is_dir():
        app.mount(
            "/assets",
            StaticFiles(directory=runtime_settings.static_dir),
            name="assets",
        )

        def index() -> FileResponse:
            return FileResponse(runtime_settings.static_dir / "index.html")

        def admin_index() -> FileResponse:
            return FileResponse(runtime_settings.static_dir / "admin.html")

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

    return app


app = create_app()
