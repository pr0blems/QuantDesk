from datetime import datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr
from starlette.websockets import WebSocketDisconnect

from quantdesk_v2.database import get_db
from quantdesk_v2.interfaces.api import public_news
from quantdesk_v2.models import News

_TEST_API_KEY = "test-external-news-key"


def _news() -> News:
    return News(
        id="news-public-1",
        ts=1_776_470_400,
        source="Example Wire",
        lang="en",
        title="Company raises guidance",
        title_zh="公司上调指引",
        link="https://example.com/news/1",
        summary="Revenue guidance increased.",
        related_us_stocks=[{"symbol": "AAPL", "relevance": 0.9}],
        related_industries=[{"industry": "technology", "relevance": 0.8}],
        ai_sentiment="bullish",
        ai_confidence=Decimal("0.8700"),
        ai_impact_strength="high",
        ai_time_horizon="short_term",
        ai_category="earnings",
        ai_reason="Guidance was raised above consensus.",
        ai_model="deepseek-v4-flash",
        ai_batch_id="batch-internal",
        ai_analyzed_at=datetime(2026, 4, 18, 12, 0, 0),
    )


class _ScalarRows:
    def __init__(self, rows: list[News]) -> None:
        self.rows = rows

    def all(self) -> list[News]:
        return self.rows


class _FakeSession:
    def __init__(self, rows: list[News]) -> None:
        self.rows = rows

    def scalars(self, _statement: object) -> _ScalarRows:
        return _ScalarRows(self.rows)


def _app(rows: list[News]) -> FastAPI:
    app = FastAPI()
    app.state.settings = SimpleNamespace(
        external_news_api_key=SecretStr(_TEST_API_KEY),
        external_news_ws_poll_seconds=0.5,
    )
    app.state.database_engine = object()
    app.include_router(public_news.router)
    app.dependency_overrides[get_db] = lambda: _FakeSession(rows)
    return app


def test_http_feed_requires_key_and_exposes_only_public_analysis_fields() -> None:
    item = _news()
    with TestClient(_app([item])) as client:
        unauthorized = client.get("/api/public/v1/news")
        authorized = client.get(
            "/api/public/v1/news?limit=20",
            headers={"X-API-Key": _TEST_API_KEY},
        )

    assert unauthorized.status_code == 401
    assert authorized.status_code == 200
    assert authorized.headers["cache-control"] == "private, no-store"
    payload = authorized.json()
    assert payload["type"] == "news.analysis.list"
    assert payload["count"] == 1
    assert payload["next_cursor"]
    public_item = payload["items"][0]
    assert public_item["analysis"]["confidence"] == 0.87
    assert public_item["related_us_stocks"][0]["symbol"] == "AAPL"
    assert "ai_batch_id" not in public_item
    assert "ai_claim_batch_id" not in public_item


def test_http_feed_accepts_api_key_query_parameter() -> None:
    item = _news()
    with TestClient(_app([item])) as client:
        authorized = client.get(
            "/api/public/v1/news",
            params={"key": _TEST_API_KEY, "limit": 1},
        )
        rejected = client.get(
            "/api/public/v1/news",
            params={"key": "wrong-key", "limit": 1},
        )

    assert authorized.status_code == 200
    assert authorized.json()["items"][0]["id"] == item.id
    assert rejected.status_code == 401


def test_cursor_round_trip_preserves_utc_time_and_tie_breaker() -> None:
    item = _news()
    encoded = public_news._encode_cursor(item)
    analyzed_at, news_id = public_news._decode_cursor(encoded) or (None, None)

    assert analyzed_at == datetime(2026, 4, 18, 12, 0, 0)
    assert news_id == item.id
    with pytest.raises(ValueError, match="invalid cursor"):
        public_news._decode_cursor("not-a-cursor")


def test_websocket_rejects_invalid_key() -> None:
    with TestClient(_app([])) as client:
        with pytest.raises(WebSocketDisconnect) as exc_info:
            with client.websocket_connect(
                "/api/public/v1/news/ws?key=wrong-key"
            ):
                pass

    assert exc_info.value.code == 4401


def test_websocket_sends_initial_completed_analysis_snapshot(monkeypatch) -> None:
    item = _news()
    monkeypatch.setattr(
        public_news,
        "_fetch_from_engine",
        lambda _engine, cursor, _limit: [item] if cursor is None else [],
    )

    with TestClient(_app([])) as client:
        with client.websocket_connect(
            f"/api/public/v1/news/ws?key={_TEST_API_KEY}&limit=20"
        ) as websocket:
            payload = websocket.receive_json()

    assert payload["type"] == "news.analysis.snapshot"
    assert payload["count"] == 1
    assert payload["items"][0]["id"] == item.id
    assert payload["items"][0]["analyzed_at"].endswith("Z")
