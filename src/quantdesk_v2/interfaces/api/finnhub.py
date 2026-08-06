from __future__ import annotations

import json
from dataclasses import asdict

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request, Response, status

from ...schemas import (
    FinnhubUsQuotesOut,
    FinnhubWebhookAcceptedOut,
    FinnhubWebhookStatusOut,
    UsMarketStatusOut,
)

router = APIRouter()


@router.get("/market/us/status", response_model=UsMarketStatusOut)
def us_market_status(
    request: Request,
    response: Response,
) -> UsMarketStatusOut:
    """Return Finnhub's official US trading-session state through our server."""

    response.headers["Cache-Control"] = "public, max-age=5, stale-if-error=60"
    result = request.app.state.finnhub_market_status_service.status()
    return UsMarketStatusOut(**asdict(result))


@router.get("/market/us/quotes", response_model=FinnhubUsQuotesOut)
def us_market_quotes(request: Request, response: Response) -> FinnhubUsQuotesOut:
    """Return the independent Finnhub US equity cache, never Binance contracts."""

    response.headers["Cache-Control"] = "public, max-age=2, stale-if-error=30"
    return FinnhubUsQuotesOut(**request.app.state.finnhub_us_quote_service.snapshot())


@router.get(
    "/integrations/finnhub/webhook",
    response_model=FinnhubWebhookStatusOut,
)
def finnhub_webhook_status(request: Request, response: Response) -> FinnhubWebhookStatusOut:
    """Public readiness probe; callbacks themselves must use authenticated POST."""

    response.headers["Cache-Control"] = "no-store"
    snapshot = request.app.state.finnhub_webhook_receiver.snapshot()
    return FinnhubWebhookStatusOut(
        status="ready" if snapshot.configured else "not_configured",
        configured=snapshot.configured,
        received_events=snapshot.received_events,
        last_received_at=snapshot.last_received_at,
    )


@router.post(
    "/integrations/finnhub/webhook",
    response_model=FinnhubWebhookAcceptedOut,
    status_code=status.HTTP_202_ACCEPTED,
)
async def receive_finnhub_webhook(
    request: Request,
    response: Response,
    background_tasks: BackgroundTasks,
) -> FinnhubWebhookAcceptedOut:
    """Authenticate and acknowledge a Finnhub event before deferred processing."""

    response.headers["Cache-Control"] = "no-store"
    receiver = request.app.state.finnhub_webhook_receiver
    if not receiver.configured:
        raise HTTPException(status_code=503, detail="Finnhub webhook is not configured")
    if not receiver.authenticated(request.headers.get("X-Finnhub-Secret")):
        raise HTTPException(status_code=401, detail="invalid webhook authentication")
    body = await request.body()
    if len(body) > 64 * 1024:
        raise HTTPException(status_code=413, detail="webhook body too large")
    try:
        payload = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise HTTPException(status_code=400, detail="invalid webhook JSON") from None
    if not isinstance(payload, (dict, list)):
        raise HTTPException(status_code=400, detail="invalid webhook event")
    if isinstance(payload, list) and len(payload) > 1_000:
        raise HTTPException(status_code=413, detail="too many webhook events")
    background_tasks.add_task(receiver.record, body)
    return FinnhubWebhookAcceptedOut()
