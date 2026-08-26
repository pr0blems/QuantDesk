"""Read-only monitor routes with no persistence-side mutations."""

from __future__ import annotations

from dataclasses import asdict
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from ...dependencies import get_current_user
from ...models import User
from ...monitor import MonitorRepository, MonitorUnavailable

router = APIRouter()


def _repository(request: Request) -> MonitorRepository:
    settings = request.app.state.settings
    try:
        return MonitorRepository(
            request.app.state.database_engine,
            settings.monitor_symbols_config,
        )
    except MonitorUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from None


@router.get("/monitor/overview")
def monitor_overview(
    request: Request,
    user: Annotated[User, Depends(get_current_user)],
    symbol: str | None = None,
) -> dict[str, Any]:
    quote_service = getattr(request.app.state, "finnhub_us_quote_service", None)
    status_service = getattr(request.app.state, "finnhub_market_status_service", None)
    quote_snapshot = quote_service.snapshot() if quote_service is not None else {}
    market_status = asdict(status_service.status()) if status_service is not None else {}
    return _repository(request).overview(
        user.monitor_watchlist or [],
        symbols=[symbol] if symbol else None,
        underlying_quotes=quote_snapshot,
        underlying_market_status=market_status,
    )


@router.get("/monitor/breadth")
def monitor_breadth(
    request: Request,
    _: Annotated[User, Depends(get_current_user)],
) -> dict[str, Any]:
    return _repository(request).breadth()


@router.get("/monitor/intelligence")
def monitor_intelligence(
    request: Request,
    _: Annotated[User, Depends(get_current_user)],
) -> dict[str, Any]:
    return _repository(request).intelligence()


@router.get("/monitor/news")
def monitor_news(
    request: Request,
    _: Annotated[User, Depends(get_current_user)],
    limit: int = Query(default=60, ge=1, le=100),
) -> list[dict[str, Any]]:
    return _repository(request).news(limit)


@router.get("/monitor/klines")
def monitor_klines(
    request: Request,
    _: Annotated[User, Depends(get_current_user)],
    symbol: str,
    tf: str = Query(default="1h", pattern="^(15m|1h|4h)$"),
    limit: int = Query(default=120, ge=20, le=300),
) -> list[dict[str, Any]]:
    return _repository(request).klines(symbol, tf, limit)


@router.get("/monitor/strategy-indicators")
def monitor_strategy_indicators(
    request: Request,
    _: Annotated[User, Depends(get_current_user)],
    symbol: str,
    tf: str = Query(default="1h", pattern="^(15m|1h|4h)$"),
) -> dict[str, Any]:
    return _repository(request).strategy_indicators(symbol, tf)


@router.get("/monitor/score")
def monitor_score(
    request: Request,
    _: Annotated[User, Depends(get_current_user)],
    symbol: str,
) -> dict[str, Any]:
    return _repository(request).score_detail(symbol)


@router.get("/monitor/report")
def monitor_report(
    request: Request,
    _: Annotated[User, Depends(get_current_user)],
    symbol: str,
) -> dict[str, Any]:
    return _repository(request).report(symbol)
