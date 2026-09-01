from __future__ import annotations

import json
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

from quantdesk_v2.interfaces.api.monitor_public import monitor_tiger_news
from quantdesk_v2.tiger_news import TigerNewsClient, TigerNewsService


def test_tiger_news_client_merges_three_direct_feeds_and_deduplicates() -> None:
    observed: list[tuple[str, dict[str, str], dict[str, list[str]]]] = []

    def transport(url: str, headers: dict[str, str], _timeout: float):
        parsed = urlsplit(url)
        query = parse_qs(parsed.query)
        observed.append((parsed.path, headers, query))
        if parsed.path == "/v1/top/news/list":
            items = [
                {
                    "id": "top-1",
                    "title": "Top story",
                    "translate_title": "置顶新闻",
                    "summary": "重点摘要",
                    "media": "Tiger",
                    "pubTime": "2026-09-01 20:30",
                    "pubTimestamp": 1_788_280_200,
                    "url": "https://stock-news.laohu8.com/highlight/detail?id=top-1",
                }
            ]
        else:
            items = [
                {
                    "id": "shared-1",
                    "title": "Shared story",
                    "translate_title": "同一条新闻",
                    "summary": "合并后只保留一条",
                    "media": "Reuters",
                    "pubTime": "2026-09-01 20:20",
                    "pubTimestamp": 1_788_279_600,
                    "url": "https://stock-news.laohu8.com/highlight/detail?id=shared-1",
                    "labels": ["express"],
                    "symbols": ["QQQ"],
                }
            ]
        return 200, json.dumps({"items": items}).encode()

    client = TigerNewsClient(
        "https://stock-news.laohu8.com",
        "Bearer server-only-token",
        transport=transport,
    )
    result = client.news("QQQUSDT", limit=20)

    assert result["symbol"] == "QQQ"
    assert result["source_count"] == 3
    assert result["partial"] is False
    assert [item["title"] for item in result["items"]] == ["置顶新闻", "同一条新闻"]
    assert len({item["id"] for item in result["items"]}) == 2
    assert {path for path, _, _ in observed} == {
        "/v1/top/news/list",
        "/v1/news/suture/list",
        "/v2/news/list",
    }
    for path, headers, query in observed:
        assert headers["Authorization"] == "Bearer server-only-token"
        assert query["pageCount"] == ["1"]
        if path == "/v2/news/list":
            assert query["symbols"] == ["QQQ"]
        else:
            assert query["symbol"] == ["QQQ"]


def test_tiger_news_client_rejects_untrusted_article_links() -> None:
    def transport(_url: str, _headers: dict[str, str], _timeout: float):
        return 200, json.dumps(
            {
                "items": [
                    {
                        "id": "unsafe-1",
                        "title": "Unsafe link",
                        "pubTimestamp": 1_788_280_200,
                        "url": "https://example.invalid/redirect",
                    }
                ]
            }
        ).encode()

    result = TigerNewsClient(
        "https://stock-news.laohu8.com",
        "Bearer server-only-token",
        transport=transport,
    ).news("AAPL")

    assert result["items"][0]["url"] is None


def test_tiger_news_service_caches_and_monitor_route_returns_snapshot() -> None:
    calls = 0

    def transport(_url: str, _headers: dict[str, str], _timeout: float):
        nonlocal calls
        calls += 1
        return 200, json.dumps({"items": []}).encode()

    service = TigerNewsService(
        TigerNewsClient(
            "https://stock-news.laohu8.com",
            "Bearer server-only-token",
            transport=transport,
        ),
        cache_seconds=60,
    )
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(tiger_news_service=service)))

    first = monitor_tiger_news(request, None, symbol="NVDAUSDT", limit=30)
    second = monitor_tiger_news(request, None, symbol="NVDAUSDT", limit=30)

    assert calls == 3
    assert first["available"] is True
    assert first["symbol"] == "NVDA"
    assert second["items"] == []


def test_tiger_news_service_reports_missing_server_credential_without_leaking_it() -> None:
    service = TigerNewsService(
        TigerNewsClient("https://stock-news.laohu8.com", "")
    )

    result = service.latest("TSLAUSDT")

    assert result["available"] is False
    assert result["error_category"] == "not_configured"
    assert "authorization" not in result
