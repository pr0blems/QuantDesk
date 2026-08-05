from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
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
        if "title_zh IS NULL" in sql:
            return []
        raise AssertionError(f"unexpected query: {sql!r} {params!r}")

    monkeypatch.setattr(news.store, "query", query)
    monkeypatch.setattr(news.store, "execute", lambda *args: writes.append(args))

    assert news.news_once() == 0
    assert writes == []


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
