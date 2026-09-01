#!/usr/bin/env python3
"""Build a redacted API catalog and Markdown documentation from a HAR capture."""

from __future__ import annotations

import argparse
import base64
import collections
import datetime as dt
import hashlib
import json
import re
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


COMMON_CONTEXT_PARAMS = {
    "__v_account__", "appName", "appVer", "channel", "community_region", "device",
    "deviceId", "device_model", "edition", "grayTest", "keyfrom", "lang",
    "langContent", "langShow", "lang_content", "license", "location", "openFlag",
    "os", "osVer", "platform", "region", "screenH", "screenW", "skin", "uuid",
    "vendor",
}

SENSITIVE_KEY_RE = re.compile(
    r"(?:authorization|token|cookie|session|signature|secret|password|license|device|account|"
    r"user|\buuid\b|(?:^|_)(?:id|uid)(?:$|_)|phone|mobile|email|avatar|nickname|real.?name|ip.?address)",
    re.I,
)
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
PHONE_RE = re.compile(r"^\+?\d[\d\s-]{7,}$")
JWT_RE = re.compile(r"^[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}$")
STATIC_MIME_PREFIXES = ("image/", "font/", "audio/", "video/", "text/css", "application/javascript", "text/javascript")
STATIC_EXT_RE = re.compile(r"\.(?:js|css|png|jpe?g|gif|webp|svg|ico|woff2?|ttf|otf|map|mp4|webm|mp3|wav|pdf|html?)$", re.I)


def redact_value(key: str, value: Any, depth: int = 0) -> Any:
    if SENSITIVE_KEY_RE.search(key):
        return "<redacted>"
    if depth > 4:
        return "<truncated>"
    if isinstance(value, dict):
        items = list(value.items())[:30]
        return {str(k): redact_value(str(k), v, depth + 1) for k, v in items}
    if isinstance(value, list):
        return [redact_value(key, item, depth + 1) for item in value[:2]]
    if isinstance(value, str):
        if EMAIL_RE.match(value) or PHONE_RE.match(value) or JWT_RE.match(value):
            return "<redacted>"
        return value if len(value) <= 180 else value[:177] + "..."
    return value


def infer_scalar(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return "string"


def infer_schema(value: Any, depth: int = 0) -> Any:
    if depth > 4:
        return {"type": "unknown"}
    if isinstance(value, dict):
        return {
            "type": "object",
            "properties": {str(k): infer_schema(v, depth + 1) for k, v in list(value.items())[:40]},
        }
    if isinstance(value, list):
        return {"type": "array", "items": infer_schema(value[0], depth + 1) if value else {"type": "unknown"}}
    return {"type": infer_scalar(value)}


def merge_schema(left: Any, right: Any) -> Any:
    if not left:
        return right
    if not right or left.get("type") != right.get("type"):
        return left
    if left.get("type") == "object":
        props = dict(left.get("properties", {}))
        for key, value in right.get("properties", {}).items():
            props[key] = merge_schema(props.get(key), value)
        return {"type": "object", "properties": props}
    if left.get("type") == "array":
        return {"type": "array", "items": merge_schema(left.get("items"), right.get("items"))}
    return left


def parse_json(text: str | None, encoding: str | None = None) -> Any:
    if not text:
        return None
    try:
        if encoding == "base64":
            text = base64.b64decode(text).decode("utf-8", errors="replace")
        return json.loads(text)
    except (ValueError, UnicodeDecodeError):
        return None


def parameter_description(name: str) -> str:
    descriptions = {
        "symbol": "证券代码", "symbols": "证券代码列表", "market": "市场代码",
        "period": "周期", "page": "页码", "pageSize": "每页数量", "pageCount": "页码/批次",
        "size": "返回数量", "limit": "数量上限", "beginIndex": "起始序号",
        "endIndex": "结束序号", "beginTime": "开始时间", "account_id": "账户标识（已脱敏）",
        "accountId": "账户标识（已脱敏）", "position": "业务位置", "scenes": "提醒场景",
        "property": "资讯属性", "type": "业务类型", "props": "行情字段集合",
    }
    if name in COMMON_CONTEXT_PARAMS:
        return "客户端上下文"
    return descriptions.get(name, "抓包中观察到的参数")


def classify(host: str, path: str, method: str) -> tuple[str, str, str]:
    text = f"{host}{path}".lower()
    rules = [
        (("/stock_info/detail",), "行情与市场数据", "股票详情行情", "获取股票最新价、涨跌、交易状态及扩展行情字段。"),
        (("trade_tick",), "行情与市场数据", "逐笔成交", "获取指定证券的逐笔成交记录与序号范围。"),
        (("trade_price_list",), "行情与市场数据", "成交价分布", "获取证券成交价档位及成交量分布。"),
        (("time_trend",), "行情与市场数据", "分时走势", "获取盘中、盘前或盘后分时走势明细。"),
        (("ask_bid",), "行情与市场数据", "买卖盘深度", "获取证券买一卖一或完整盘口深度。"),
        (("value_analysis",), "行情与市场数据", "估值分析", "获取指数估值、分位点及统计比较结果。"),
        (("package_indices",), "行情与市场数据", "指数成分包", "获取指数相关的成分或组合标的。"),
        (("relate/contract",), "行情与市场数据", "关联合约", "获取指数或证券关联的可交易合约。"),
        (("corporate_actions",), "行情与市场数据", "公司行动", "获取分红、拆股等公司行动数据。"),
        (("rank/hourtrading",), "行情与市场数据", "盘前盘后排行", "获取延长交易时段的股票排行。"),
        (("/v2/market",), "行情与市场数据", "市场总览配置", "获取市场板块、指数、ETF 与排行入口数据。"),
        (("related_bar",), "行情与市场数据", "关联行情栏", "获取标的关联的行情或导航数据。"),
        (("stock_latest",), "社区与内容", "股票最新讨论", "分页获取指定股票的最新社区内容。"),
        (("stock_recommend",), "社区与内容", "股票推荐讨论", "获取指定股票的推荐社区内容流。"),
        (("transaction-orders",), "社区与内容", "社区晒单", "获取与证券相关的公开交易分享内容。"),
        (("gpt/stock-daily",), "社区与内容", "股票每日摘要", "获取面向社区展示的股票每日智能摘要。"),
        (("tweet/theme",), "社区与内容", "主题内容", "获取或关联股票主题与社区帖子。"),
        (("exposure/watchlist", "exposure/symbol"), "社区与内容", "内容曝光", "获取关注标的内容或记录内容曝光统计。"),
        (("trend/attitude",), "社区与内容", "市场态度统计", "统计社区用户对标的走势的观点。"),
        (("symbol/discussion",), "社区与内容", "讨论曝光", "获取指定证券的讨论曝光信息。"),
        (("user/info", "concise-info", "user/symbol/recommend"), "账户与权限", "用户资料", "获取精简用户资料或用户相关标的推荐。"),
        (("message/unread", "message/red/dot"), "通知与提醒", "未读消息", "获取未读消息数量或红点状态。"),
        (("reminder/multi/get", "alerts/"), "通知与提醒", "行情提醒", "获取证券提醒配置或提醒状态。"),
        (("push/offline", "push/online", "global/exit/notify"), "通知与提醒", "客户端状态通知", "上报客户端在线、离线或退出状态，属于有副作用的通知接口。"),
        (("news/suture", "top/news", "/v2/news/list", "/notice/point"), "新闻资讯", "证券资讯", "获取证券新闻、置顶资讯或公告提示。"),
        (("scene/push/list",), "运营内容", "运营推送列表", "获取与账户状态相关的运营推送内容。"),
        (("activities/",), "运营内容", "市场活动", "获取标的或市场活动列表。"),
        (("college/",), "运营内容", "新手引导", "获取客户端学院或新手引导内容。"),
        (("ipo_message",), "运营内容", "IPO 直播信息", "获取 IPO 直播讲者或直播配置信息。"),
        (("stream/public/v2/popup",), "运营内容", "弹窗配置", "获取按用户类型投放的客户端弹窗。"),
        (("moduleRuleList".lower(),), "产品配置", "个性化模块规则", "读取或同步客户端个性化模块展示规则。"),
        (("graph/query/userid",), "产品配置", "用户图谱同步", "按同步时间读取用户图谱或个性化数据。"),
        (("permission", "account/status", "verification/status", "account/professional", "account/info"), "账户与权限", "账户权限与状态", "获取账户权限、认证状态或账户基础信息。"),
        (("virtual-trade", "/orders/active", "/portfolio/positions"), "模拟交易", "模拟账户持仓与订单", "获取模拟账户活动订单或证券持仓。"),
        (("/ipos",), "IPO 与交易服务", "IPO 列表", "获取当前可查看或可申购的 IPO 数据。"),
        (("metrics/", "/log", "tigerfintech.com/sa"), "遥测与分析", "客户端遥测", "上报性能、客户端事件或产品使用分析数据。"),
        (("ip/location",), "地理与环境", "IP 地理位置", "根据网络出口判断区域与客户端环境。"),
        (("morningeveningreport",), "运营内容", "晨晚报状态", "查询晨报或晚报的日期与可用状态。"),
        (("question/spread",), "运营内容", "问答分发", "获取客户端问答内容分发配置。"),
        (("object/report/reason",), "产品配置", "举报原因", "获取内容举报原因字典。"),
        (("diff/position/red/dot",), "通知与提醒", "位置红点", "查询指定业务位置是否显示红点。"),
        (("candlestick",), "社区与内容", "晒单K线", "获取订单分享场景需要的简化 K 线数据。"),
        (("app/hello",), "产品配置", "客户端握手", "获取服务端时间或客户端启动基础配置。"),
    ]
    for needles, category, purpose, description in rules:
        if any(needle in text for needle in needles):
            return category, purpose, description
    return "其他业务", "业务数据", f"{method} 请求，用于客户端业务数据读取或同步。"


def business_signal(payload: Any) -> tuple[bool, dict[str, Any]]:
    if not isinstance(payload, dict):
        return True, {}
    signals = {k: payload[k] for k in ("ret", "code", "status", "success", "msg", "message") if k in payload and isinstance(payload[k], (str, int, float, bool, type(None)))}
    ok = True
    if signals.get("success") is False:
        ok = False
    if "ret" in signals and signals["ret"] not in (0, 200, "0", "200"):
        ok = False
    if "code" in signals and signals["code"] not in (0, 200, 62000000, 91000000, "0", "200", "62000000", "91000000"):
        ok = False
    message = str(signals.get("msg", signals.get("message", ""))).lower()
    if any(word in message for word in ("not found", "unauthorized", "forbidden", "expired", "failed", "error")):
        ok = False
    return ok, signals


def request_preview(request: dict[str, Any]) -> tuple[Any, Any]:
    post = request.get("postData") or {}
    mime = post.get("mimeType", "")
    parsed = parse_json(post.get("text"))
    if parsed is not None:
        return redact_value("body", parsed), infer_schema(parsed)
    params = post.get("params") or []
    if params:
        body = {item.get("name", "field"): item.get("value", "") for item in params}
        return redact_value("body", body), infer_schema(body)
    if post.get("text"):
        if "x-www-form-urlencoded" in mime:
            body = dict(urllib.parse.parse_qsl(post["text"], keep_blank_values=True))
            return redact_value("body", body), infer_schema(body)
        return f"<{mime or 'binary'} body omitted>", {"type": "binary"}
    return None, None


def is_api_entry(entry: dict[str, Any]) -> bool:
    request = entry.get("request", {})
    response = entry.get("response", {})
    method = request.get("method", "")
    url = urllib.parse.urlsplit(request.get("url", ""))
    mime = (response.get("content", {}).get("mimeType") or "").lower()
    if method not in {"GET", "POST", "PUT", "PATCH", "DELETE"}:
        return False
    if url.netloc == "static.tigerbbs.com":
        return False
    if STATIC_EXT_RE.search(url.path) or mime.startswith(STATIC_MIME_PREFIXES):
        return False
    return True


def clean_order_book_payload(payload: Any, symbol: str) -> dict[str, Any] | None:
    """Keep only display-safe depth fields from a quote response."""
    if not isinstance(payload, dict):
        return None
    node = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    depth = node.get("askBidDepth") if isinstance(node, dict) else None
    if not isinstance(depth, dict):
        return None

    def clean_side(value: Any) -> list[dict[str, Any]]:
        levels: list[dict[str, Any]] = []
        for item in value if isinstance(value, list) else []:
            if not isinstance(item, dict) or not isinstance(item.get("price"), (int, float)):
                continue
            sub_volume = item.get("subVolume") if isinstance(item.get("subVolume"), list) else []
            levels.append({
                "price": item["price"],
                "volume": item.get("volume", 0),
                "orderCount": len(sub_volume),
            })
        return levels[:40]

    ask = clean_side(depth.get("ask"))
    bid = clean_side(depth.get("bid"))
    if not ask and not bid:
        return None
    return {
        "symbol": symbol,
        "source": "Blue Ocean Level 2",
        "timestamp": payload.get("timestamp") or payload.get("serverTime") or node.get("timestamp") or node.get("serverTime"),
        "ask": ask,
        "bid": bid,
    }


def extract_order_books(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Extract every captured 40-level Blue Ocean book without auth/context data."""
    books: dict[str, dict[str, Any]] = {}
    for entry in entries:
        url = urllib.parse.urlsplit(entry.get("request", {}).get("url", ""))
        prefix = "/stock_info/ask_bid/blue-ocean/"
        if not url.path.startswith(prefix):
            continue
        content = entry.get("response", {}).get("content", {})
        payload = parse_json(content.get("text"), content.get("encoding"))
        symbol = urllib.parse.unquote(url.path[len(prefix):])
        book = clean_order_book_payload(payload, symbol)
        if book:
            books[symbol] = book
    return list(books.values())


def fetch_order_books(entries: list[dict[str, Any]], symbols: list[str]) -> list[dict[str, Any]]:
    """Fetch additional read-only books using the captured request as a temporary template."""
    template = next((
        entry for entry in entries
        if "/stock_info/ask_bid/blue-ocean/" in entry.get("request", {}).get("url", "")
    ), None)
    if not template:
        return []
    request = template["request"]
    original = urllib.parse.urlsplit(request["url"])
    headers = {
        header["name"]: header["value"]
        for header in request.get("headers", [])
        if header.get("name", "").lower() not in {"host", "content-length", "accept-encoding", "connection"}
    }
    prefix = original.path.rsplit("/", 1)[0] + "/"
    books: list[dict[str, Any]] = []
    for symbol in symbols:
        target = urllib.parse.urlunsplit((
            original.scheme,
            original.netloc,
            prefix + urllib.parse.quote(symbol, safe=""),
            original.query,
            original.fragment,
        ))
        try:
            live_request = urllib.request.Request(target, headers=headers, method="GET")
            with urllib.request.urlopen(live_request, timeout=20) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except Exception as error:
            print(f"Depth fetch skipped for {symbol}: {type(error).__name__}")
            continue
        book = clean_order_book_payload(payload, symbol)
        if book:
            books.append(book)
    return books


NEWS_PATHS = (
    "/v1/top/news/list",
    "/v1/news/suture/list",
    "/v2/news/list",
)


def fetch_news_snapshots(entries: list[dict[str, Any]], symbols: list[str]) -> list[dict[str, Any]]:
    """Fetch and merge display-safe news from the three captured read-only endpoints."""
    templates: dict[str, dict[str, Any]] = {}
    for entry in entries:
        path = urllib.parse.urlsplit(entry.get("request", {}).get("url", "")).path
        if path in NEWS_PATHS and path not in templates:
            templates[path] = entry
    snapshots: list[dict[str, Any]] = []
    for symbol in symbols:
        merged: list[dict[str, Any]] = []
        seen: set[str] = set()
        for path in NEWS_PATHS:
            template = templates.get(path)
            if not template:
                continue
            request = template["request"]
            original = urllib.parse.urlsplit(request["url"])
            query = urllib.parse.parse_qsl(original.query, keep_blank_values=True)
            query = [
                (name, symbol if name in {"symbol", "symbols"} else value)
                for name, value in query
            ]
            target = urllib.parse.urlunsplit((
                original.scheme,
                original.netloc,
                original.path,
                urllib.parse.urlencode(query),
                original.fragment,
            ))
            headers = {
                header["name"]: header["value"]
                for header in request.get("headers", [])
                if header.get("name", "").lower() not in {"host", "content-length", "accept-encoding", "connection"}
            }
            try:
                live_request = urllib.request.Request(target, headers=headers, method="GET")
                with urllib.request.urlopen(live_request, timeout=20) as response:
                    payload = json.loads(response.read().decode("utf-8"))
            except Exception as error:
                print(f"News fetch skipped for {symbol} {path}: {type(error).__name__}")
                continue
            node = payload.get("data") if isinstance(payload, dict) and isinstance(payload.get("data"), dict) else payload
            items = node.get("items", []) if isinstance(node, dict) else []
            per_source_limit = 4 if path == "/v1/top/news/list" else (12 if path == "/v1/news/suture/list" else 8)
            for item in (items[:per_source_limit] if isinstance(items, list) else []):
                if not isinstance(item, dict):
                    continue
                title = str(item.get("title") or "").strip()
                published_at = str(item.get("pubTime") or "").strip()
                if not title:
                    continue
                dedupe_key = str(item.get("id") or f"{title}|{published_at}")
                if dedupe_key in seen:
                    continue
                seen.add(dedupe_key)
                summary = str(item.get("summary") or "").strip()
                url = str(item.get("url") or "").strip()
                merged.append({
                    "id": hashlib.sha256(f"{symbol}|{dedupe_key}".encode()).hexdigest()[:12],
                    "title": title,
                    "summary": summary[:360],
                    "source": str(item.get("media") or "未知来源"),
                    "publishedAt": published_at,
                    "url": url if url.startswith(("https://", "http://")) else None,
                    "kind": "置顶" if path == "/v1/top/news/list" else ("实时" if path == "/v2/news/list" else "资讯"),
                })
        if merged:
            snapshots.append({
                "symbol": symbol,
                "source": "Tiger Stock News",
                "items": merged[:24],
            })
    return snapshots


COMMUNITY_PATHS = {
    "count": "/v4/symbol/trend/attitude/statistic",
    "latest": "/v1/feed/stock_latest",
    "themes": "/v4/tweet/theme/symbol/page",
}
POSITIVE_TERMS = (
    "看多", "买入", "加仓", "上涨", "大涨", "反弹", "突破", "利好", "新高",
    "多头", "增长", "超预期", "机会", "bullish", "buy", "long", "upside",
    "rally", "gain", "beat", "strong",
)
NEGATIVE_TERMS = (
    "看空", "卖出", "减仓", "下跌", "暴跌", "利空", "风险", "恐慌", "抛售",
    "空头", "毁灭", "崩", "bearish", "sell", "short", "downside", "loss",
    "weak", "crash", "miss", "overvalued",
)


def fetch_community_snapshots(entries: list[dict[str, Any]], symbols: list[str]) -> list[dict[str, Any]]:
    """Fetch discussion counts/topics and derive a clearly-labelled text-sample tendency."""
    templates: dict[str, dict[str, Any]] = {}
    for entry in entries:
        path = urllib.parse.urlsplit(entry.get("request", {}).get("url", "")).path
        if path in COMMUNITY_PATHS.values() and path not in templates:
            templates[path] = entry
    snapshots: list[dict[str, Any]] = []
    for symbol in symbols:
        tweet_count = 0
        texts: list[str] = []
        topics: list[dict[str, Any]] = []
        for kind, path in COMMUNITY_PATHS.items():
            template = templates.get(path)
            if not template:
                continue
            request = template["request"]
            original = urllib.parse.urlsplit(request["url"])
            headers = {
                header["name"]: header["value"]
                for header in request.get("headers", [])
                if header.get("name", "").lower() not in {"host", "content-length", "accept-encoding", "connection"}
            }
            try:
                if request.get("method") == "POST":
                    body = json.loads(request.get("postData", {}).get("text") or "{}")
                    body["symbol"] = symbol
                    live_request = urllib.request.Request(
                        request["url"],
                        data=json.dumps(body).encode("utf-8"),
                        headers=headers,
                        method="POST",
                    )
                else:
                    query = [
                        (name, symbol if name == "symbol" else value)
                        for name, value in urllib.parse.parse_qsl(original.query, keep_blank_values=True)
                    ]
                    target = urllib.parse.urlunsplit((
                        original.scheme,
                        original.netloc,
                        original.path,
                        urllib.parse.urlencode(query),
                        original.fragment,
                    ))
                    live_request = urllib.request.Request(target, headers=headers, method="GET")
                with urllib.request.urlopen(live_request, timeout=20) as response:
                    payload = json.loads(response.read().decode("utf-8"))
            except Exception as error:
                print(f"Community fetch skipped for {symbol} {path}: {type(error).__name__}")
                continue
            node = payload.get("data") if isinstance(payload, dict) and isinstance(payload.get("data"), dict) else payload
            if not isinstance(node, dict):
                continue
            if kind == "count":
                tweet_count = int(node.get("tweetCount") or 0)
            elif kind == "latest":
                for card in node.get("list", []) if isinstance(node.get("list"), list) else []:
                    card_data = card.get("cardData", []) if isinstance(card, dict) else []
                    for item in card_data if isinstance(card_data, list) else []:
                        if not isinstance(item, dict):
                            continue
                        text = str(item.get("plainDigest") or item.get("digest") or item.get("title") or "").strip()
                        if text:
                            texts.append(text)
            else:
                for theme in node.get("list", []) if isinstance(node.get("list"), list) else []:
                    if not isinstance(theme, dict) or not theme.get("themeName"):
                        continue
                    topics.append({
                        "name": str(theme["themeName"])[:64],
                        "count": int(theme.get("tweetCounts") or 0),
                    })

        scores: list[int] = []
        for text in texts[:20]:
            lowered = text.lower()
            positive = sum(lowered.count(term.lower()) for term in POSITIVE_TERMS)
            negative = sum(lowered.count(term.lower()) for term in NEGATIVE_TERMS)
            scores.append(1 if positive > negative else (-1 if negative > positive else 0))
        sample_size = len(scores)
        if sample_size:
            bearish = round(sum(score < 0 for score in scores) * 100 / sample_size)
            bullish = round(sum(score > 0 for score in scores) * 100 / sample_size)
            neutral = 100 - bearish - bullish
        else:
            bearish, neutral, bullish = 0, 100, 0
        snapshots.append({
            "symbol": symbol,
            "source": "Tiger Community",
            "tweetCount": tweet_count,
            "sampleSize": sample_size,
            "bearish": bearish,
            "neutral": neutral,
            "bullish": bullish,
            "topics": topics[:3],
        })
    return snapshots


def build_catalog(
    har: dict[str, Any],
    source_name: str,
    depth_symbols: list[str] | None = None,
    news_symbols: list[str] | None = None,
    community_symbols: list[str] | None = None,
) -> dict[str, Any]:
    entries = [entry for entry in har.get("log", {}).get("entries", []) if is_api_entry(entry)]
    groups: dict[tuple[str, str, str], dict[str, Any]] = {}
    for entry in entries:
        request = entry["request"]
        response = entry["response"]
        url = urllib.parse.urlsplit(request["url"])
        key = (request["method"], url.netloc, url.path or "/")
        group = groups.setdefault(key, {"entries": [], "query": collections.defaultdict(list)})
        group["entries"].append(entry)
        for name, value in urllib.parse.parse_qsl(url.query, keep_blank_values=True):
            group["query"][name].append(value)

    endpoints: list[dict[str, Any]] = []
    for (method, host, path), group in sorted(groups.items(), key=lambda item: (item[0][1], item[0][2], item[0][0])):
        endpoint_entries = group["entries"]
        statuses = collections.Counter(entry["response"].get("status", 0) for entry in endpoint_entries)
        mimes = collections.Counter((entry["response"].get("content", {}).get("mimeType") or "").split(";")[0] for entry in endpoint_entries)
        response_schema: Any = None
        response_preview: Any = None
        request_body_preview: Any = None
        request_body_schema: Any = None
        business_ok_results: list[bool] = []
        signals: list[dict[str, Any]] = []
        header_names: set[str] = set()
        cookie_names: set[str] = set()
        bearer = False
        signed = False
        post_mimes: set[str] = set()
        for entry in endpoint_entries:
            request = entry["request"]
            response = entry["response"]
            headers = {header.get("name", "").lower(): header.get("value", "") for header in request.get("headers", [])}
            header_names.update(headers)
            bearer = bearer or headers.get("authorization", "").lower().startswith("bearer ")
            signed = signed or "x-ca-signature" in headers or "x-ca-key" in headers
            for pair in headers.get("cookie", "").split(";"):
                if "=" in pair:
                    cookie_names.add(pair.split("=", 1)[0].strip())
            if request.get("postData"):
                post_mimes.add(request["postData"].get("mimeType", ""))
                body_preview, body_schema = request_preview(request)
                if request_body_preview is None:
                    request_body_preview = body_preview
                request_body_schema = merge_schema(request_body_schema, body_schema)
            content = response.get("content", {})
            payload = parse_json(content.get("text"), content.get("encoding"))
            http_ok = 200 <= response.get("status", 0) < 400
            app_ok, signal = business_signal(payload)
            business_ok_results.append(http_ok and app_ok)
            if signal:
                signals.append(signal)
            if payload is not None:
                response_schema = merge_schema(response_schema, infer_schema(payload))
                if response_preview is None:
                    response_preview = redact_value("response", payload)

        category, purpose, description = classify(host, path, method)
        query_params = []
        for name, values in sorted(group["query"].items(), key=lambda item: (item[0] in COMMON_CONTEXT_PARAMS, item[0].lower())):
            raw = values[0] if values else ""
            redacted = redact_value(name, raw)
            query_params.append({
                "name": name,
                "type": "integer" if raw.isdigit() and name not in {"symbol", "account_id", "accountId"} else "string",
                "required": len(values) == len(endpoint_entries),
                "example": redacted,
                "description": parameter_description(name),
                "context": name in COMMON_CONTEXT_PARAMS,
            })
        safe_read = method == "GET"
        telemetry = category == "遥测与分析"
        risk = "只读" if safe_read else ("遥测上报" if telemetry else "可能产生副作用")
        body_sizes = [max(0, entry["response"].get("content", {}).get("size") or 0) for entry in endpoint_entries]
        durations = [float(entry.get("time") or 0) for entry in endpoint_entries]
        endpoint_id = hashlib.sha1(f"{method} {host}{path}".encode()).hexdigest()[:12]
        endpoints.append({
            "id": endpoint_id,
            "method": method,
            "host": host,
            "path": re.sub(r"/v1/user/\d+/", "/v1/user/{user_id}/", path),
            "calls": len(endpoint_entries),
            "statuses": [{"status": status, "count": count} for status, count in sorted(statuses.items())],
            "usable": any(business_ok_results),
            "category": category,
            "purpose": purpose,
            "description": description,
            "risk": risk,
            "replaySafety": "可生成只读模板，仍需合法凭据" if safe_read else "Demo 禁止自动重放",
            "avgDurationMs": round(sum(durations) / len(durations), 1),
            "maxDurationMs": round(max(durations), 1),
            "responseBytes": sum(body_sizes),
            "mimeTypes": [mime or "(none)" for mime in mimes],
            "queryParams": query_params,
            "requestBody": {"mimeTypes": sorted(post_mimes), "schema": request_body_schema, "preview": request_body_preview},
            "requestHeaderNames": sorted(name for name in header_names if name not in {"authorization", "cookie"}),
            "auth": {"bearerObserved": bearer, "cookieNames": sorted(cookie_names), "signedRequest": signed},
            "response": {"schema": response_schema, "preview": response_preview, "businessSignals": signals[:3]},
            "firstSeen": min(entry.get("startedDateTime", "") for entry in endpoint_entries),
            "lastSeen": max(entry.get("startedDateTime", "") for entry in endpoint_entries),
        })

    original_endpoint_count = len(endpoints)
    original_api_calls = sum(endpoint["calls"] for endpoint in endpoints)
    community_information_paths = {
        "/v1/feed/stock_latest",
        "/v1/feed/stock_recommend",
        "/v1/feed/symbol/transaction-orders",
        "/v1/gpt/stock-daily",
        "/v1/order-sharing/candlestick",
        "/v4/symbol/trend/attitude/statistic",
        "/v4/tweet/theme/symbol/page",
        "/v4/tweet/theme/symbol/relate/themes",
    }
    news_information_paths = {
        "/v1/news/suture/list",
        "/v1/top/news/list",
        "/v2/news/list",
    }

    focused_endpoints: list[dict[str, Any]] = []
    for endpoint in endpoints:
        category: str | None = None
        if endpoint["category"] == "行情与市场数据":
            category = "行情数据"
        elif endpoint["host"] == "stock-news.laohu8.com" and endpoint["path"] in news_information_paths:
            category = "新闻资讯"
        elif endpoint["host"] == "community-service.laohu8.com" and endpoint["path"] in community_information_paths:
            category = "社区资讯"
        elif endpoint["host"] == "trade.skytigris.cn" and endpoint["path"] == "/ipos":
            category = "IPO 数据"
        if not category:
            continue
        endpoint["category"] = category
        if endpoint["method"] == "POST":
            endpoint["risk"] = "数据查询（POST）"
            endpoint["replaySafety"] = "可生成数据查询模板，仍需合法凭据并人工确认"
        focused_endpoints.append(endpoint)
    endpoints = focused_endpoints

    categories = collections.Counter(endpoint["category"] for endpoint in endpoints)
    hosts = collections.Counter(endpoint["host"] for endpoint in endpoints)
    source_entries = har.get("log", {}).get("entries", [])
    jwt_expiry: str | None = None
    for entry in source_entries:
        for header in entry.get("request", {}).get("headers", []):
            if header.get("name", "").lower() != "authorization":
                continue
            parts = header.get("value", "").split(None, 1)
            if len(parts) != 2 or parts[1].count(".") != 2:
                continue
            try:
                chunk = parts[1].split(".")[1]
                chunk += "=" * ((4 - len(chunk) % 4) % 4)
                payload = json.loads(base64.urlsafe_b64decode(chunk))
                if payload.get("exp"):
                    jwt_expiry = dt.datetime.fromtimestamp(payload["exp"], dt.timezone.utc).isoformat()
                    break
            except (ValueError, KeyError, TypeError):
                pass
        if jwt_expiry:
            break
    timestamps = [entry.get("startedDateTime") for entry in source_entries if entry.get("startedDateTime")]
    summary = {
        "source": source_name,
        "capturedFrom": min(timestamps) if timestamps else None,
        "capturedTo": max(timestamps) if timestamps else None,
        "harEntries": len(source_entries),
        "apiCalls": sum(endpoint["calls"] for endpoint in endpoints),
        "sourceApiCalls": original_api_calls,
        "sourceEndpointCount": original_endpoint_count,
        "scope": "股票行情、证券新闻、社区资讯与 IPO 数据",
        "endpointCount": len(endpoints),
        "usableCount": sum(endpoint["usable"] for endpoint in endpoints),
        "getCount": sum(endpoint["method"] == "GET" for endpoint in endpoints),
        "postCount": sum(endpoint["method"] == "POST" for endpoint in endpoints),
        "hostCount": len(hosts),
        "authenticatedEndpointCount": sum(endpoint["auth"]["bearerObserved"] for endpoint in endpoints),
        "jwtExpiryUtc": jwt_expiry,
        "categoryCounts": [{"name": name, "count": count} for name, count in categories.most_common()],
        "hostCounts": [{"name": name, "count": count} for name, count in hosts.most_common()],
    }
    order_books = {book["symbol"]: book for book in extract_order_books(source_entries)}
    if depth_symbols:
        for book in fetch_order_books(source_entries, depth_symbols):
            order_books[book["symbol"]] = book
    news_snapshots = fetch_news_snapshots(source_entries, news_symbols or [])
    community_snapshots = fetch_community_snapshots(source_entries, community_symbols or [])
    return {
        "summary": summary,
        "orderBooks": list(order_books.values()),
        "newsSnapshots": news_snapshots,
        "communitySnapshots": community_snapshots,
        "endpoints": endpoints,
    }


def schema_summary(schema: Any) -> str:
    if not schema:
        return "未捕获结构（可能为 304 或空响应）"
    if schema.get("type") != "object":
        return schema.get("type", "unknown")
    keys = list(schema.get("properties", {}))
    return "、".join(f"`{key}`" for key in keys[:18]) + (" …" if len(keys) > 18 else "")


def render_markdown(catalog: dict[str, Any]) -> str:
    summary = catalog["summary"]
    endpoints = catalog["endpoints"]
    lines = [
        "# 股票资讯接口文档",
        "",
        "> 本文档只保留股票行情、证券新闻、社区资讯和 IPO 数据接口。认证值、Cookie 值、账户标识、用户标识和设备标识均已脱敏；快照参数只调用对应的只读查询接口，其中主题查询使用 POST。",
        "",
        "## 总览",
        "",
        "| 指标 | 数量 |",
        "|---|---:|",
        f"| HAR 总记录 | {summary['harEntries']} |",
        f"| 原始业务/API 调用 | {summary['sourceApiCalls']} |",
        f"| 保留的股票资讯调用 | {summary['apiCalls']} |",
        f"| 原始去重接口 | {summary['sourceEndpointCount']} |",
        f"| 保留接口（方法 + 域名 + 路径） | {summary['endpointCount']} |",
        f"| 在抓包中业务成功 | {summary['usableCount']} |",
        f"| GET / POST | {summary['getCount']} / {summary['postCount']} |",
        f"| 域名 | {summary['hostCount']} |",
        "",
        "## 使用边界",
        "",
        "- `GET` 仅表示语义上只读，仍然需要合法账户、有效凭据并遵守服务条款。",
        "- 保留的 `POST` 主要是行情详情、排行和主题内容查询，但仍需在人工确认后使用。",
        "- HAR 中观察到 Bearer Token 与 Cookie；本文档不保存它们的值。",
        "- HTTP 200 不一定代表业务成功；本文档同时检查了 `ret/code/status/success/msg`。",
        "",
        "## 按用途统计",
        "",
        "| 用途类别 | 接口数 |",
        "|---|---:|",
    ]
    for item in summary["categoryCounts"]:
        lines.append(f"| {item['name']} | {item['count']} |")
    lines += ["", "## 接口索引", "", "| 方法 | 域名与路径 | 用途 | 状态 | 风险 |", "|---|---|---|---|---|"]
    for endpoint in endpoints:
        status = "可用" if endpoint["usable"] else "业务失败"
        lines.append(f"| {endpoint['method']} | `{endpoint['host']}{endpoint['path']}` | {endpoint['purpose']} | {status} | {endpoint['risk']} |")
    lines += ["", "## 接口详情", ""]
    for index, endpoint in enumerate(endpoints, start=1):
        business_params = [item for item in endpoint["queryParams"] if not item["context"]]
        context_params = [item["name"] for item in endpoint["queryParams"] if item["context"]]
        signals = endpoint["response"]["businessSignals"]
        lines += [
            f"### {index}. `{endpoint['method']} {endpoint['host']}{endpoint['path']}`",
            "",
            f"- **用途：** {endpoint['category']} / {endpoint['purpose']}",
            f"- **说明：** {endpoint['description']}",
            f"- **抓包表现：** 调用 {endpoint['calls']} 次，平均 {endpoint['avgDurationMs']} ms，{'业务成功' if endpoint['usable'] else '业务失败'}",
            f"- **认证：** {'Bearer Token' if endpoint['auth']['bearerObserved'] else '未观察到 Bearer Token'}；Cookie 名称：{', '.join(endpoint['auth']['cookieNames']) or '无'}",
            f"- **重放边界：** {endpoint['replaySafety']}",
            "",
        ]
        if business_params:
            lines += ["**业务查询参数**", "", "| 参数 | 类型 | 抓包中每次出现 | 说明 |", "|---|---|---|---|"]
            for item in business_params:
                lines.append(f"| `{item['name']}` | {item['type']} | {'是' if item['required'] else '否'} | {item['description']} |")
            lines.append("")
        else:
            lines += ["**业务查询参数：** 未观察到独立业务参数。", ""]
        if context_params:
            lines += [f"**公共客户端上下文：** `{', '.join(context_params)}`", ""]
        body_schema = endpoint["requestBody"]["schema"]
        if body_schema:
            lines += [f"**请求体：** `{', '.join(endpoint['requestBody']['mimeTypes']) or 'unknown'}`；结构：{schema_summary(body_schema)}", ""]
        lines += [f"**响应结构：** {schema_summary(endpoint['response']['schema'])}", ""]
        if signals:
            lines += [f"**业务状态样例（脱敏）：** `{json.dumps(signals[0], ensure_ascii=False)}`", ""]
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--har", required=True, type=Path, help="Source HAR file")
    parser.add_argument("--json-out", type=Path, default=Path("src/data/catalog.json"))
    parser.add_argument("--docs-out", type=Path, default=Path("docs/API_CATALOG.md"))
    parser.add_argument("--depth-symbols", default="", help="Comma-separated symbols to fetch as read-only depth snapshots")
    parser.add_argument("--news-symbols", default="", help="Comma-separated symbols to fetch as read-only news snapshots")
    parser.add_argument("--community-symbols", default="", help="Comma-separated symbols to fetch community snapshots")
    args = parser.parse_args()
    with args.har.open("r", encoding="utf-8-sig") as handle:
        har = json.load(handle)
    depth_symbols = [symbol.strip() for symbol in args.depth_symbols.split(",") if symbol.strip()]
    news_symbols = [symbol.strip() for symbol in args.news_symbols.split(",") if symbol.strip()]
    community_symbols = [symbol.strip() for symbol in args.community_symbols.split(",") if symbol.strip()]
    catalog = build_catalog(har, args.har.name, depth_symbols, news_symbols, community_symbols)
    args.json_out.parent.mkdir(parents=True, exist_ok=True)
    args.docs_out.parent.mkdir(parents=True, exist_ok=True)
    args.json_out.write_text(json.dumps(catalog, ensure_ascii=False, indent=2), encoding="utf-8")
    args.docs_out.write_text(render_markdown(catalog), encoding="utf-8")
    print(json.dumps(catalog["summary"], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
