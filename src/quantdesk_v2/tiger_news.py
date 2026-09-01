from __future__ import annotations

import hashlib
import json
import re
import time
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from http.client import HTTPSConnection
from threading import Lock
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit

TIGER_NEWS_ORIGIN = "https://stock-news.laohu8.com"
TIGER_NEWS_PATHS = (
    "/v1/top/news/list",
    "/v1/news/suture/list",
    "/v2/news/list",
)
TIGER_NEWS_KIND = {
    "/v1/top/news/list": "置顶",
    "/v1/news/suture/list": "资讯",
    "/v2/news/list": "实时",
}
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_NEWS_ITEMS = 50
SYMBOL_PATTERN = re.compile(r"^[A-Z][A-Z0-9.-]{0,15}$")
SYMBOL_ALIASES = {"BRKB": "BRK.B", "PAYP": "PYPL"}
ALLOWED_ARTICLE_HOSTS = {"stock-news.laohu8.com", "www.laohunote.com"}
ALLOWED_QUERY_KEYS = {
    "appName",
    "appVer",
    "channel",
    "device_model",
    "edition",
    "include_expiry",
    "isLive",
    "keyfrom",
    "lang",
    "lang_content",
    "license",
    "location",
    "os",
    "osVer",
    "pageCount",
    "pageSize",
    "platform",
    "property",
    "region",
    "screenH",
    "screenW",
    "skin",
    "symbol",
    "symbols",
    "vendor",
}
CLIENT_CONTEXT = {
    "skin": "1",
    "location": "CHN",
    "lang": "zh_CN",
    "lang_content": "all",
    "screenH": "956",
    "region": "CHN",
    "channel": "modfut",
    "vendor": "AppStore",
    "screenW": "440",
    "edition": "full",
    "appVer": "9.6.1.1",
    "os": "iOS",
    "keyfrom": "TigerBrokers.9.6.1.1.iPhone",
    "platform": "iOS",
    "appName": "TigerTrade",
    "osVer": "26.5.2",
    "device_model": "iPhone",
    "license": "TBNZ",
}

NewsTransport = Callable[[str, dict[str, str], float], tuple[int, bytes]]


class TigerNewsClientError(RuntimeError):
    """A redacted Tiger news error safe for API responses and logs."""

    def __init__(self, category: str):
        super().__init__("Tiger news request failed")
        self.category = category


def _news_https_transport(
    url: str,
    headers: dict[str, str],
    timeout: float,
) -> tuple[int, bytes]:
    parsed = urlsplit(url)
    query = parse_qsl(parsed.query, keep_blank_values=True)
    if (
        parsed.scheme != "https"
        or parsed.hostname != "stock-news.laohu8.com"
        or parsed.port not in (None, 443)
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in TIGER_NEWS_PATHS
        or parsed.fragment
        or any(key not in ALLOWED_QUERY_KEYS for key, _ in query)
        or len(query) != len({key for key, _ in query})
    ):
        raise TigerNewsClientError("rejected")
    connection = HTTPSConnection(parsed.hostname, parsed.port or 443, timeout=timeout)
    try:
        target = parsed.path + (f"?{parsed.query}" if parsed.query else "")
        connection.request("GET", target, headers=headers)
        response = connection.getresponse()
        payload = response.read(MAX_RESPONSE_BYTES + 1)
        if len(payload) > MAX_RESPONSE_BYTES:
            raise TigerNewsClientError("invalid_response")
        return response.status, payload
    except TimeoutError as exc:
        raise TigerNewsClientError("timeout") from exc
    except TigerNewsClientError:
        raise
    except OSError as exc:
        raise TigerNewsClientError("network") from exc
    finally:
        connection.close()


def _compact_text(value: object, *, limit: int) -> str:
    text = " ".join(str(value or "").split())
    return text[:limit]


def _safe_article_url(value: object) -> str | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    parsed = urlsplit(raw)
    if (
        parsed.scheme != "https"
        or parsed.hostname not in ALLOWED_ARTICLE_HOSTS
        or parsed.port not in (None, 443)
        or parsed.username is not None
        or parsed.password is not None
    ):
        return None
    return raw


def _timestamp_ms(item: Mapping[str, Any]) -> int | None:
    value = item.get("pubTimestamp")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    timestamp = int(value)
    if timestamp <= 0:
        return None
    return timestamp if timestamp > 10_000_000_000 else timestamp * 1_000


class TigerNewsClient:
    """Server-side client for Tiger's three read-only stock-news feeds."""

    def __init__(
        self,
        base_url: str,
        authorization: str,
        *,
        timeout_seconds: float = 5.0,
        transport: NewsTransport | None = None,
    ) -> None:
        parsed = urlsplit(base_url)
        if (
            parsed.scheme != "https"
            or parsed.hostname != "stock-news.laohu8.com"
            or parsed.port not in (None, 443)
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in ("", "/")
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("Tiger news base URL must be the approved HTTPS origin")
        self.base_url = TIGER_NEWS_ORIGIN
        self.authorization = authorization.strip()
        self.timeout_seconds = float(timeout_seconds)
        self.transport = transport or _news_https_transport

    @property
    def configured(self) -> bool:
        return bool(self.authorization)

    @staticmethod
    def normalize_symbol(symbol: str) -> str:
        normalized = str(symbol or "").strip().upper()
        if normalized.endswith("USDT") or normalized.endswith("USD1"):
            normalized = normalized[:-4]
        return SYMBOL_ALIASES.get(normalized, normalized)

    def _request_path(self, path: str, symbol: str, page_size: int) -> list[Mapping[str, Any]]:
        query = {
            **CLIENT_CONTEXT,
            "pageCount": "1",
            "pageSize": str(page_size),
        }
        if path == "/v2/news/list":
            query.update({"symbols": symbol, "isLive": "1"})
        else:
            query["symbol"] = symbol
            if path == "/v1/top/news/list":
                query["include_expiry"] = "0"
        url = f"{self.base_url}{path}?{urlencode(query)}"
        status, raw = self.transport(
            url,
            {
                "Accept": "application/json",
                "Authorization": self.authorization,
                "User-Agent": "QuantDesk/2 TigerNews",
                "X-API-Version": "v2",
            },
            self.timeout_seconds,
        )
        if status in {401, 403}:
            raise TigerNewsClientError("authentication")
        if status == 429:
            raise TigerNewsClientError("rate_limit")
        if status != 200:
            raise TigerNewsClientError("upstream")
        try:
            payload = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise TigerNewsClientError("invalid_response") from exc
        node = (
            payload.get("data")
            if isinstance(payload, Mapping) and isinstance(payload.get("data"), Mapping)
            else payload
        )
        items = node.get("items") if isinstance(node, Mapping) else None
        if not isinstance(items, list) or len(items) > 500:
            raise TigerNewsClientError("invalid_response")
        return [item for item in items if isinstance(item, Mapping)]

    def news(self, symbol: str, *, limit: int = MAX_NEWS_ITEMS) -> dict[str, Any]:
        if not self.configured:
            raise TigerNewsClientError("not_configured")
        normalized = self.normalize_symbol(symbol)
        if not SYMBOL_PATTERN.fullmatch(normalized):
            raise TigerNewsClientError("invalid_symbol")
        page_size = min(MAX_NEWS_ITEMS, max(20, int(limit)))
        collected: list[tuple[str, Mapping[str, Any]]] = []
        successful_paths: list[str] = []
        failures: list[str] = []
        with ThreadPoolExecutor(max_workers=len(TIGER_NEWS_PATHS)) as executor:
            futures = {
                executor.submit(self._request_path, path, normalized, page_size): path
                for path in TIGER_NEWS_PATHS
            }
            for future in as_completed(futures):
                path = futures[future]
                try:
                    items = future.result()
                except TigerNewsClientError as exc:
                    failures.append(exc.category)
                    continue
                successful_paths.append(path)
                collected.extend((path, item) for item in items)
        if not successful_paths:
            category = "authentication" if "authentication" in failures else failures[0]
            raise TigerNewsClientError(category)

        output: list[dict[str, Any]] = []
        seen: set[str] = set()
        for path, item in collected:
            title = _compact_text(item.get("translate_title") or item.get("title"), limit=280)
            if not title:
                continue
            published_at = _compact_text(item.get("pubTime"), limit=40)
            upstream_id = _compact_text(item.get("id"), limit=120)
            dedupe_key = upstream_id or f"{title}|{published_at}"
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)
            original_title = _compact_text(item.get("title"), limit=280)
            labels = item.get("labels")
            symbols = item.get("symbols")
            output.append(
                {
                    "id": hashlib.sha256(
                        f"{normalized}|{dedupe_key}".encode()
                    ).hexdigest()[:16],
                    "title": title,
                    "original_title": original_title if original_title != title else None,
                    "summary": _compact_text(item.get("summary"), limit=600),
                    "source": _compact_text(item.get("media") or item.get("source"), limit=80)
                    or "老虎证券资讯",
                    "published_at": published_at,
                    "published_at_ms": _timestamp_ms(item),
                    "url": _safe_article_url(item.get("url") or item.get("share")),
                    "kind": TIGER_NEWS_KIND[path],
                    "sentiment": _compact_text(item.get("sentiment"), limit=24),
                    "labels": [
                        _compact_text(value, limit=28)
                        for value in (labels if isinstance(labels, list) else [])[:5]
                        if _compact_text(value, limit=28)
                    ],
                    "symbols": [
                        _compact_text(value, limit=20)
                        for value in (symbols if isinstance(symbols, list) else [])[:8]
                        if _compact_text(value, limit=20)
                    ],
                }
            )
        output.sort(key=lambda item: int(item.get("published_at_ms") or 0), reverse=True)
        return {
            "symbol": normalized,
            "source": "Tiger Stock News",
            "items": output[: min(MAX_NEWS_ITEMS, max(1, int(limit)))],
            "source_count": len(successful_paths),
            "partial": len(successful_paths) < len(TIGER_NEWS_PATHS),
        }


class TigerNewsService:
    """Small per-symbol cache that retains the last good read-only news snapshot."""

    def __init__(self, client: TigerNewsClient, *, cache_seconds: float = 60.0) -> None:
        self.client = client
        self.cache_seconds = max(10.0, float(cache_seconds))
        self._lock = Lock()
        self._fetch_lock = Lock()
        self._snapshots: dict[str, tuple[float, dict[str, Any]]] = {}

    def latest(self, symbol: str, *, limit: int = 30) -> dict[str, Any]:
        normalized = self.client.normalize_symbol(symbol)
        if not normalized or not SYMBOL_PATTERN.fullmatch(normalized):
            return self._unavailable(normalized, "invalid_symbol")
        if not self.client.configured:
            return self._unavailable(normalized, "not_configured")
        now = time.monotonic()
        with self._lock:
            cached = self._snapshots.get(normalized)
            if cached and now - cached[0] < self.cache_seconds:
                return self._slice(cached[1], limit)
        with self._fetch_lock:
            now = time.monotonic()
            with self._lock:
                cached = self._snapshots.get(normalized)
                if cached and now - cached[0] < self.cache_seconds:
                    return self._slice(cached[1], limit)
            try:
                result = self.client.news(normalized, limit=MAX_NEWS_ITEMS)
            except TigerNewsClientError as exc:
                if cached:
                    stale = {**cached[1], "stale": True, "error_category": exc.category}
                    return self._slice(stale, limit)
                unavailable = self._unavailable(normalized, exc.category)
                with self._lock:
                    self._snapshots[normalized] = (now, unavailable)
                return self._slice(unavailable, limit)
            snapshot = {
                **result,
                "available": True,
                "stale": False,
                "error_category": None,
                "fetched_at": datetime.now(UTC).isoformat(),
            }
            with self._lock:
                self._snapshots[normalized] = (now, snapshot)
            return self._slice(snapshot, limit)

    @staticmethod
    def _unavailable(symbol: str, category: str) -> dict[str, Any]:
        return {
            "symbol": symbol,
            "source": "Tiger Stock News",
            "available": False,
            "stale": False,
            "partial": False,
            "source_count": 0,
            "items": [],
            "error_category": category,
            "fetched_at": None,
        }

    @staticmethod
    def _slice(snapshot: Mapping[str, Any], limit: int) -> dict[str, Any]:
        items = snapshot.get("items")
        safe_items = list(items) if isinstance(items, list) else []
        return {
            **snapshot,
            "items": safe_items[: min(MAX_NEWS_ITEMS, max(1, int(limit)))],
        }
