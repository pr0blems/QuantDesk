"""API-key protected external feed for completed AI news analyses."""

from __future__ import annotations

import asyncio
import base64
import json
import secrets
from datetime import UTC, datetime
from decimal import Decimal
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, WebSocket
from sqlalchemy import and_, or_, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
from starlette.websockets import WebSocketDisconnect

from ...database import get_db
from ...models import News

router = APIRouter(prefix="/api/public/v1/news", tags=["public-news"])

_MAX_PAGE_SIZE = 100
_WS_HEARTBEAT_SECONDS = 15.0
_UNAUTHORIZED_WS_CODE = 4401


def _utc_iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _published_iso(timestamp: int) -> str | None:
    try:
        return _utc_iso(datetime.fromtimestamp(int(timestamp), UTC))
    except (OSError, OverflowError, TypeError, ValueError):
        return None


def _decimal_float(value: Decimal | float | int | None) -> float | None:
    return float(value) if value is not None else None


def serialize_analyzed_news(news: News) -> dict[str, Any]:
    """Return the stable public contract without internal claim/audit fields."""

    return {
        "id": news.id,
        "published_ts": int(news.ts),
        "published_at": _published_iso(news.ts),
        "analyzed_at": _utc_iso(news.ai_analyzed_at),
        "source": news.source,
        "language": news.lang,
        "title": news.title,
        "title_zh": news.title_zh,
        "link": news.link,
        "summary": news.summary,
        "analysis": {
            "sentiment": news.ai_sentiment,
            "confidence": _decimal_float(news.ai_confidence),
            "impact_strength": news.ai_impact_strength,
            "time_horizon": news.ai_time_horizon,
            "category": news.ai_category,
            "reason": news.ai_reason,
            "model": news.ai_model,
        },
        "related_us_stocks": list(news.related_us_stocks or []),
        "related_industries": list(news.related_industries or []),
    }


def _configured_key(connection: Request | WebSocket) -> str:
    return connection.app.state.settings.external_news_api_key.get_secret_value()


def _bearer_key(value: str | None) -> str | None:
    if not value:
        return None
    scheme, separator, credential = value.partition(" ")
    if not separator or scheme.lower() != "bearer":
        return None
    return credential.strip() or None


def _provided_http_key(request: Request) -> str | None:
    return request.headers.get("X-API-Key") or _bearer_key(
        request.headers.get("Authorization")
    )


def _provided_ws_key(websocket: WebSocket) -> str | None:
    return (
        websocket.headers.get("X-API-Key")
        or _bearer_key(websocket.headers.get("Authorization"))
        or websocket.query_params.get("key")
    )


def _key_matches(provided: str | None, expected: str) -> bool:
    if not provided or not expected:
        return False
    return secrets.compare_digest(provided.encode("utf-8"), expected.encode("utf-8"))


def _require_http_key(request: Request) -> None:
    if not _key_matches(_provided_http_key(request), _configured_key(request)):
        raise HTTPException(
            status_code=401,
            detail="invalid API key",
            headers={"WWW-Authenticate": "Bearer, X-API-Key"},
        )


def _encode_cursor(news: News) -> str:
    payload = json.dumps(
        [_utc_iso(news.ai_analyzed_at), news.id],
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def _decode_cursor(value: str | None) -> tuple[datetime, str] | None:
    if not value:
        return None
    if len(value) > 512:
        raise ValueError("cursor is too long")
    try:
        padded = value + "=" * (-len(value) % 4)
        decoded = json.loads(base64.urlsafe_b64decode(padded).decode("utf-8"))
        if not isinstance(decoded, list) or len(decoded) != 2:
            raise ValueError
        raw_time, raw_id = decoded
        if not isinstance(raw_time, str) or not isinstance(raw_id, str) or not raw_id:
            raise ValueError
        analyzed_at = datetime.fromisoformat(raw_time.replace("Z", "+00:00"))
        if analyzed_at.tzinfo is not None:
            analyzed_at = analyzed_at.astimezone(UTC).replace(tzinfo=None)
        return analyzed_at, raw_id
    except (UnicodeError, ValueError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError("invalid cursor") from exc


def fetch_analyzed_news(
    db: Session,
    *,
    cursor: tuple[datetime, str] | None,
    limit: int,
) -> list[News]:
    statement = select(News).where(News.ai_analyzed_at.is_not(None))
    if cursor is not None:
        analyzed_at, news_id = cursor
        statement = statement.where(
            or_(
                News.ai_analyzed_at > analyzed_at,
                and_(News.ai_analyzed_at == analyzed_at, News.id > news_id),
            )
        ).order_by(News.ai_analyzed_at.asc(), News.id.asc())
    else:
        statement = statement.order_by(News.ai_analyzed_at.desc(), News.id.desc())
    return list(db.scalars(statement.limit(limit)).all())


def _latest_cursor(items: list[News]) -> str | None:
    if not items:
        return None
    latest = max(items, key=lambda item: (item.ai_analyzed_at or datetime.min, item.id))
    return _encode_cursor(latest)


def _response_payload(items: list[News], *, event_type: str) -> dict[str, Any]:
    return {
        "version": "1.0",
        "type": event_type,
        "generated_at": _utc_iso(datetime.now(UTC)),
        "count": len(items),
        "next_cursor": _latest_cursor(items),
        "items": [serialize_analyzed_news(item) for item in items],
    }


@router.get("")
def public_analyzed_news(
    request: Request,
    response: Response,
    db: Annotated[Session, Depends(get_db)],
    limit: int = Query(default=20, ge=1, le=_MAX_PAGE_SIZE),
    cursor: str | None = Query(default=None, max_length=512),
) -> dict[str, Any]:
    """Return completed AI news analyses using an opaque incremental cursor."""

    _require_http_key(request)
    try:
        decoded_cursor = _decode_cursor(cursor)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    try:
        items = fetch_analyzed_news(db, cursor=decoded_cursor, limit=limit)
    except SQLAlchemyError:
        raise HTTPException(status_code=503, detail="news feed unavailable") from None
    response.headers["Cache-Control"] = "private, no-store"
    return _response_payload(items, event_type="news.analysis.list")


def _fetch_from_engine(
    engine: Any,
    cursor: tuple[datetime, str] | None,
    limit: int,
) -> list[News]:
    with Session(engine) as db:
        return fetch_analyzed_news(db, cursor=cursor, limit=limit)


@router.websocket("/ws")
async def public_analyzed_news_websocket(websocket: WebSocket) -> None:
    """Send an initial snapshot, then stream newly completed analyses."""

    if not _key_matches(_provided_ws_key(websocket), _configured_key(websocket)):
        await websocket.close(code=_UNAUTHORIZED_WS_CODE, reason="invalid API key")
        return

    raw_limit = websocket.query_params.get("limit", "20")
    try:
        limit = int(raw_limit)
    except ValueError:
        await websocket.close(code=4400, reason="invalid limit")
        return
    if not 1 <= limit <= _MAX_PAGE_SIZE:
        await websocket.close(code=4400, reason="invalid limit")
        return

    await websocket.accept()
    engine = websocket.app.state.database_engine
    poll_seconds = float(websocket.app.state.settings.external_news_ws_poll_seconds)
    try:
        snapshot = await asyncio.to_thread(_fetch_from_engine, engine, None, limit)
        await websocket.send_json(
            _response_payload(snapshot, event_type="news.analysis.snapshot")
        )
        cursor_value = _latest_cursor(snapshot)
        cursor = _decode_cursor(cursor_value)
        if cursor is None:
            cursor = (datetime.now(UTC).replace(tzinfo=None), "")
        heartbeat_elapsed = 0.0
        while True:
            await asyncio.sleep(poll_seconds)
            items = await asyncio.to_thread(
                _fetch_from_engine,
                engine,
                cursor,
                _MAX_PAGE_SIZE,
            )
            if items:
                for item in items:
                    await websocket.send_json(
                        {
                            "version": "1.0",
                            "type": "news.analysis.completed",
                            "generated_at": _utc_iso(datetime.now(UTC)),
                            "data": serialize_analyzed_news(item),
                            "cursor": _encode_cursor(item),
                        }
                    )
                cursor = _decode_cursor(_latest_cursor(items)) or cursor
                heartbeat_elapsed = 0.0
                continue
            heartbeat_elapsed += poll_seconds
            if heartbeat_elapsed >= _WS_HEARTBEAT_SECONDS:
                await websocket.send_json(
                    {
                        "version": "1.0",
                        "type": "heartbeat",
                        "generated_at": _utc_iso(datetime.now(UTC)),
                    }
                )
                heartbeat_elapsed = 0.0
    except WebSocketDisconnect:
        return
    except (SQLAlchemyError, RuntimeError):
        await websocket.close(code=1011, reason="news feed unavailable")
