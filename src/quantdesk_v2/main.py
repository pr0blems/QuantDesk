from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware

from . import __version__
from .api import router
from .config import Settings, get_settings
from .database import build_engine, engine
from .strategy_routes import router as strategy_router

FRONTEND_ROUTES = (
    "/login",
    "/monitor",
    "/paper",
    "/overview",
    "/credentials",
    "/strategies",
    "/backtest",
    "/orders",
    "/risk",
    "/audit",
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
            from quantdesk import engine as market_engine
            from quantdesk import store as legacy_store

            legacy_store.configure_engine(database_engine)
            market_engine.start()
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
    app.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=runtime_settings.allowed_hosts,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=runtime_settings.allowed_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
        allow_headers=["Authorization", "Content-Type"],
    )
    app.add_middleware(
        SecurityHeadersMiddleware,
        max_request_bytes=runtime_settings.max_request_bytes,
    )
    app.include_router(router)
    app.include_router(strategy_router)

    if runtime_settings.static_dir.is_dir():
        app.mount(
            "/assets",
            StaticFiles(directory=runtime_settings.static_dir),
            name="assets",
        )

        def index() -> FileResponse:
            return FileResponse(runtime_settings.static_dir / "index.html")

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

    return app


app = create_app()
