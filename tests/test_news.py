from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet
from defusedxml.common import EntitiesForbidden
from sqlalchemy import Computed

from quantdesk_v2 import news
from quantdesk_v2.models import News


class _FakeHttpsResponse:
    def __init__(self, payload: bytes, *, final_url: str, headers: dict[str, str] | None = None):
        self.payload = payload
        self.final_url = final_url
        self.headers = headers or {}

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def geturl(self) -> str:
        return self.final_url

    def read(self, size: int) -> bytes:
        return self.payload


def test_news_urls_require_an_allowlisted_https_origin() -> None:
    assert (
        news._validate_https_url(
            "https://www.coindesk.com/arc/outboundfeeds/rss/",
            news.RSS_ALLOWED_HOSTS,
        )
        == "https://www.coindesk.com/arc/outboundfeeds/rss/"
    )
    for rejected in (
        "http://www.coindesk.com/feed",
        "file:///etc/passwd",
        "https://127.0.0.1/feed",
        "https://attacker.example/feed",
        "https://user:password@www.coindesk.com/feed",
        "https://www.coindesk.com:8443/feed",
    ):
        with pytest.raises(news.NewsUrlRejected):
            news._validate_https_url(rejected, news.RSS_ALLOWED_HOSTS)


def test_bounded_https_reader_rejects_oversized_stream(monkeypatch) -> None:
    response = _FakeHttpsResponse(
        b"x" * 9,
        final_url="https://www.coindesk.com/feed",
    )
    monkeypatch.setattr(
        news.urllib.request,
        "build_opener",
        lambda *handlers: SimpleNamespace(open=lambda *args, **kwargs: response),
    )

    with pytest.raises(ValueError, match="exceeds size limit"):
        news._read_https(
            "https://www.coindesk.com/feed",
            allowed_hosts=news.RSS_ALLOWED_HOSTS,
            timeout=1,
            max_bytes=8,
        )


def test_bounded_https_reader_rejects_cross_allowlist_final_url(monkeypatch) -> None:
    response = _FakeHttpsResponse(b"", final_url="https://attacker.example/feed")
    monkeypatch.setattr(
        news.urllib.request,
        "build_opener",
        lambda *handlers: SimpleNamespace(open=lambda *args, **kwargs: response),
    )

    with pytest.raises(news.NewsUrlRejected):
        news._read_https(
            "https://www.coindesk.com/feed",
            allowed_hosts=news.RSS_ALLOWED_HOSTS,
            timeout=1,
            max_bytes=8,
        )


def test_fetch_rss_keeps_existing_rss_tuple_shape(monkeypatch) -> None:
    payload = b"""<?xml version="1.0"?>
    <rss><channel><item><title>Headline</title><link>https://example.com/a</link>
    <pubDate>Mon, 03 Aug 2026 01:00:00 GMT</pubDate></item></channel></rss>"""
    monkeypatch.setattr(news, "_read_https", lambda *args, **kwargs: payload)

    assert news.fetch_rss("https://www.coindesk.com/feed", retries=1) == [
        ("Headline", "https://example.com/a", "Mon, 03 Aug 2026 01:00:00 GMT")
    ]


def test_fetch_rss_rejects_xml_entities(monkeypatch) -> None:
    payload = b"""<?xml version="1.0"?>
    <!DOCTYPE rss [<!ENTITY local SYSTEM "file:///etc/passwd">]>
    <rss><channel><item><title>&local;</title><link>https://example.com/a</link></item>
    </channel></rss>"""
    monkeypatch.setattr(news, "_read_https", lambda *args, **kwargs: payload)

    with pytest.raises(EntitiesForbidden):
        news.fetch_rss("https://www.coindesk.com/feed", retries=1)


def test_fetch_taoz_flash_json_normalizes_content_and_stable_links(monkeypatch) -> None:
    payload = json.dumps(
        {
            "code": 0,
            "message": "ok",
            "data": {
                "items": [
                    {
                        "time": "2026-08-05 17:29:00",
                        "title": "",
                        "content": "金十快讯正文",
                        "url": "",
                        "source": "jin10",
                    },
                    {
                        "time": "2026-08-05 17:28:00",
                        "title": "东方财富标题",
                        "content": "东方财富正文",
                        "url": "https://example.com/article",
                        "source": "eastmoney",
                    },
                ],
                "total": 2,
            },
        },
        ensure_ascii=False,
    ).encode()
    monkeypatch.setattr(news, "_read_https", lambda *args, **kwargs: payload)

    items = news.fetch_flash_json(
        "https://admin.taoz.chat/api/v1/news/flash?source=jin10&limit=50",
        retries=1,
    )

    assert items[0]["title"] == "金十快讯正文"
    assert items[0]["summary"] == "金十快讯正文"
    assert items[0]["link"].startswith(
        "https://admin.taoz.chat/api/v1/news/flash?source=jin10&limit=50#flash-"
    )
    assert items[1]["link"] == "https://example.com/article"
    assert news.parse_pub(items[0]["published"]) == 1785922140


def test_fetch_unusual_whales_news_authenticates_and_normalizes(monkeypatch) -> None:
    payload = json.dumps(
        {
            "data": [
                {
                    "created_at": "2026-08-16T01:02:03Z",
                    "headline": "Apple supplier raises its outlook",
                    "is_major": True,
                    "meta": {"url": "https://example.com/apple-outlook"},
                    "sentiment": "positive",
                    "source": "BusinessWire",
                    "tags": ["earnings"],
                    "tickers": ["AAPL"],
                }
            ]
        }
    ).encode()
    captured: dict[str, object] = {}

    def fake_read(url, **kwargs):
        captured["url"] = url
        captured.update(kwargs)
        return payload

    monkeypatch.setattr(news, "_read_https", fake_read)
    monkeypatch.setattr(news, "_unusual_whales_api_key", lambda: "server-side-secret")

    items = news.fetch_unusual_whales_news(
        "https://api.unusualwhales.com/api/news/headlines?major_only=true&limit=100&page=0",
        retries=1,
    )

    assert captured["allowed_hosts"] == news.UNUSUAL_WHALES_ALLOWED_HOSTS
    assert captured["headers"] == {
        "Authorization": "Bearer server-side-secret",
        "Accept": "application/json",
    }
    assert items == [
        {
            "title": "Apple supplier raises its outlook",
            "link": "https://example.com/apple-outlook",
            "published": "2026-08-16T01:02:03Z",
            "summary": "Publisher: BusinessWire; Tickers: AAPL; Tags: earnings",
            "sentiment": "bull",
        }
    ]
    assert news.parse_pub(items[0]["published"]) == 1786842123
    assert "server-side-secret" not in repr(items)


def test_unusual_whales_news_requires_key_and_exact_endpoint(monkeypatch) -> None:
    monkeypatch.setattr(news, "_unusual_whales_api_key", lambda: "")
    with pytest.raises(RuntimeError, match="not configured"):
        news.fetch_unusual_whales_news(
            "https://api.unusualwhales.com/api/news/headlines", retries=1
        )
    with pytest.raises(news.NewsUrlRejected, match="headlines endpoint"):
        news.fetch_unusual_whales_news(
            "https://api.unusualwhales.com/api/stock/AAPL/quote", retries=1
        )


def test_unusual_whales_key_prefers_encrypted_database_credential(monkeypatch) -> None:
    master_key = Fernet.generate_key().decode("ascii")
    encrypted = news.CredentialCipher(master_key).encrypt("database-secret")
    monkeypatch.setattr(
        news.store,
        "query",
        lambda sql, params=(): [{"value_json": {"api_key_encrypted": encrypted}}],
    )
    monkeypatch.setattr(
        news,
        "get_settings",
        lambda: SimpleNamespace(
            credential_master_key=SimpleNamespace(get_secret_value=lambda: master_key),
            unusual_whales_api_key=SimpleNamespace(
                get_secret_value=lambda: "environment-secret"
            ),
        ),
    )

    assert news._unusual_whales_api_key() == "database-secret"


def test_fetch_source_rejects_unknown_format() -> None:
    with pytest.raises(ValueError, match="unsupported"):
        news.fetch_source({"feed_type": "unknown", "url": "https://example.com"})


def test_news_identifier_is_stable_sha256() -> None:
    assert news._news_id("CoinDesk", "https://example.com/a") == (
        "8d083bd2a35d6266e27ddd56f66a80c2f03bc15f6cb9e418f9b77c7d73a5200e"
    )
    assert news._news_id("CoinDesk", "https://example.com/a") != news._news_id(
        "OtherSource", "https://example.com/a"
    )


def test_sha256_migration_does_not_duplicate_an_existing_source_link(monkeypatch) -> None:
    source = {
        "name": "CoinDesk",
        "url": "https://www.coindesk.com/feed",
        "lang": "zh",
    }
    monkeypatch.setattr(news, "settings", {"news_sources": [source]})
    monkeypatch.setattr(
        news,
        "fetch_rss",
        lambda url: [("Headline", "https://example.com/a", "")],
    )
    monkeypatch.setattr(news, "_fail_streak", {})
    monkeypatch.setattr(news, "_skip_until", {})
    writes = []

    def query(sql, params=()):
        if "WHERE id=" in sql:
            return []
        if "WHERE source=" in sql:
            assert params == ("CoinDesk", "https://example.com/a")
            return [{"existing": 1}]
        if "WHERE title=" in sql:
            return []
        if "title_zh IS NULL" in sql:
            return []
        raise AssertionError(f"unexpected query: {sql!r} {params!r}")

    monkeypatch.setattr(news.store, "query", query)
    monkeypatch.setattr(news.store, "execute", lambda *args: writes.append(args))

    assert news.news_once() == 0
    assert writes == []


def test_newly_inserted_news_immediately_notifies_ai_worker(monkeypatch) -> None:
    source = {
        "name": "CoinDesk",
        "url": "https://www.coindesk.com/feed",
        "lang": "zh",
    }
    monkeypatch.setattr(news, "settings", {"news_sources": [source]})
    monkeypatch.setattr(
        news,
        "fetch_rss",
        lambda url, **kwargs: [("Headline", "https://example.com/immediate", "")],
    )
    monkeypatch.setattr(news, "_fail_streak", {})
    monkeypatch.setattr(news, "_skip_until", {})
    monkeypatch.setattr(
        news.store,
        "query",
        lambda sql, params=(): [] if "SELECT" in sql else [],
    )
    monkeypatch.setattr(news.store, "execute", lambda *args: 1)
    notified: list[list[str]] = []
    monkeypatch.setattr(
        news,
        "_notify_news_ingested",
        lambda news_ids: notified.append(news_ids),
    )

    assert news.news_once() == 1
    assert notified == [
        [news._news_id("CoinDesk", "https://example.com/immediate")]
    ]


def test_news_quality_rejects_stale_short_and_promotional_titles() -> None:
    now = 1_800_000_000

    assert news._news_quality_rejection("Apple raises guidance", now, now_ts=now) is None
    assert news._news_quality_rejection("财料", now, now_ts=now) == "short_title"
    assert news._news_quality_rejection("金十交易学院正在直播中", now, now_ts=now) == "promotion"
    assert (
        news._news_quality_rejection(
            "Old but otherwise valid headline",
            now - news.NEWS_ANALYZED_RETENTION_SECONDS - 1,
            now_ts=now,
        )
        == "stale"
    )


def test_news_retention_expires_old_pending_before_analyzed_memory(monkeypatch) -> None:
    captured: list[tuple[str, object]] = []

    def execute(sql, params=()):
        captured.append((sql, params))
        return 6

    monkeypatch.setattr(news.store, "execute", execute)

    assert news.cleanup_news_retention(now_ts=1_800_000_000) == 12
    assert "ai_analyzed_at IS NULL" in captured[0][0]
    assert captured[0][1][:2] == (
        1_800_000_000 - news.NEWS_ANALYZED_RETENTION_SECONDS,
        1_800_000_000 - news.NEWS_PENDING_RETENTION_SECONDS,
    )
    assert "ROW_NUMBER() OVER" in captured[1][0]


def test_news_model_exposes_generated_unique_dedup_hash() -> None:
    column = News.__table__.c.source_link_hash
    assert isinstance(column.computed, Computed)
    assert column.computed.persisted is True
    assert "SHA2" in str(column.computed.sqltext)
    assert any(
        index.name == "uq_news_source_link_hash" and index.unique
        for index in News.__table__.indexes
    )


def test_news_dedup_migration_follows_current_head(monkeypatch) -> None:
    migration_path = (
        Path(__file__).parents[1] / "migrations" / "versions" / "0013_add_news_dedup_hash.py"
    )
    spec = importlib.util.spec_from_file_location("news_dedup_migration_0013", migration_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    assert module.revision == "0013_news_dedup_hash"
    assert module.down_revision == "0012_bind_paper_strategy"
    assert module.NEWS_DEDUP_INDEX == "uq_news_source_link_hash"
    assert "SHA2" in module.NEWS_DEDUP_EXPRESSION

    calls = []
    fake_op = SimpleNamespace(
        get_bind=lambda: SimpleNamespace(dialect=SimpleNamespace(name="mysql")),
        drop_index=lambda name, table_name: calls.append(("index", name, table_name)),
        drop_column=lambda table_name, name: calls.append(("column", table_name, name)),
    )
    monkeypatch.setattr(module, "op", fake_op)
    module.downgrade()

    assert calls == [
        ("index", "uq_news_source_link_hash", "news"),
        ("column", "news", "source_link_hash"),
    ]
