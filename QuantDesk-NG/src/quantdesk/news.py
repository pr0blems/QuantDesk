"""舆情模块：多源 RSS 抓取 + 英文标题机器翻译 + 关键词情绪标注。"""

from __future__ import annotations

import hashlib
import json
import re
import time
import urllib.parse
import urllib.request
from collections.abc import Collection

from defusedxml import ElementTree as ET

from . import store
from .config_loader import settings

RSS_ALLOWED_HOSTS = frozenset(
    {
        "ambcrypto.com",
        "beincrypto.com",
        "bitcoinist.com",
        "bitcoinmagazine.com",
        "blockworks.co",
        "coingape.com",
        "coinjournal.net",
        "cointelegraph.com",
        "crypto.news",
        "cryptobriefing.com",
        "cryptonews.com",
        "cryptopotato.com",
        "dailyhodl.com",
        "decrypt.co",
        "feeds.feedburner.com",
        "feeds.marketwatch.com",
        "finance.yahoo.com",
        "oilprice.com",
        "thedefiant.io",
        "u.today",
        "www.benzinga.com",
        "www.cnbc.com",
        "www.coindesk.com",
        "www.forexlive.com",
        "www.ft.com",
        "www.fxstreet.com",
        "www.investing.com",
        "www.marketbeat.com",
        "www.newsbtc.com",
        "www.theblock.co",
        # Official primary sources.  Keeping these in the RSS allowlist makes
        # redirects and future admin-managed feeds subject to the same SSRF
        # protection as commercial sources.
        "sec.gov",
        "www.sec.gov",
        "federalreserve.gov",
        "www.federalreserve.gov",
        "bls.gov",
        "www.bls.gov",
        "bea.gov",
        "www.bea.gov",
        "eia.gov",
        "www.eia.gov",
        "hkex.com.hk",
        "www.hkex.com.hk",
        "hkexnews.hk",
        "www.hkexnews.hk",
    }
)
TRANSLATION_ALLOWED_HOSTS = frozenset(
    {
        "api.mymemory.translated.net",
        "lingva.ml",
        "translate.googleapis.com",
    }
)
MAX_RSS_BYTES = 2 * 1024 * 1024
MAX_TRANSLATION_BYTES = 512 * 1024


class NewsUrlRejected(ValueError):
    """Raised when a news integration tries to leave its HTTPS allowlist."""


def _validate_https_url(url: str, allowed_hosts: Collection[str]) -> str:
    try:
        parsed = urllib.parse.urlsplit(url)
        port = parsed.port
    except ValueError as exc:
        raise NewsUrlRejected("invalid integration URL") from exc
    if (
        parsed.scheme.lower() != "https"
        or parsed.hostname is None
        or parsed.hostname.lower() not in allowed_hosts
        or parsed.username is not None
        or parsed.password is not None
        or port not in (None, 443)
    ):
        raise NewsUrlRejected("integration URL must use an approved HTTPS host")
    return url


class _AllowlistRedirectHandler(urllib.request.HTTPRedirectHandler):
    def __init__(self, allowed_hosts: Collection[str]):
        super().__init__()
        self.allowed_hosts = allowed_hosts

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _validate_https_url(newurl, self.allowed_hosts)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _read_https(
    url: str,
    *,
    allowed_hosts: Collection[str],
    timeout: float,
    max_bytes: int,
) -> bytes:
    """Read a bounded HTTPS response while validating initial and redirect URLs."""

    if max_bytes < 1:
        raise ValueError("max_bytes must be positive")
    validated_url = _validate_https_url(url, allowed_hosts)
    request = urllib.request.Request(  # noqa: S310 - URL is HTTPS allowlist validated.
        validated_url, headers=UA
    )
    opener = urllib.request.build_opener(_AllowlistRedirectHandler(allowed_hosts))
    # The custom handler validates every redirect before urllib follows it.
    with opener.open(request, timeout=timeout) as response:  # noqa: S310
        _validate_https_url(response.geturl(), allowed_hosts)
        content_length = response.headers.get("Content-Length")
        if content_length:
            try:
                declared_length = int(content_length)
            except ValueError as exc:
                raise ValueError("invalid integration response length") from exc
            if declared_length < 0 or declared_length > max_bytes:
                raise ValueError("integration response exceeds size limit")
        payload = response.read(max_bytes + 1)
    if len(payload) > max_bytes:
        raise ValueError("integration response exceeds size limit")
    return payload


UA = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Accept": "application/rss+xml, application/xml, text/xml, */*",
    "Accept-Language": "en-US,en;q=0.9",
}

BULL_WORDS = [
    "surge",
    "soar",
    "rally",
    "record high",
    "all-time high",
    "ath",
    "bull",
    "gain",
    "jump",
    "beat",
    "approval",
    "approve",
    "adoption",
    "inflow",
    "buy",
    "long",
    "突破",
    "大涨",
    "上涨",
    "利好",
    "新高",
    "增持",
    "获批",
]
BEAR_WORDS = [
    "crash",
    "plunge",
    "drop",
    "fall",
    "bear",
    "loss",
    "hack",
    "exploit",
    "ban",
    "lawsuit",
    "injunction",
    "sec sues",
    "outflow",
    "sell",
    "short",
    "liquidat",
    "fine",
    "fraud",
    "暴跌",
    "大跌",
    "下跌",
    "利空",
    "黑客",
    "起诉",
    "清算",
    "罚款",
]
MACRO_WORDS = [
    "federal reserve",
    "fomc",
    "cpi",
    "inflation",
    "interest rate",
    "powell",
    "美联储",
    "通胀",
    "利率决议",
    "非农",
    " fed ",
]


def _contains_sentiment_term(text: str, term: str) -> bool:
    """Match English terms on token boundaries and CJK terms as phrases."""

    value = term.strip().lower()
    if not value:
        return False
    if re.fullmatch(r"[a-z0-9][a-z0-9 .&'/-]*", value):
        return re.search(
            r"(?<![a-z0-9])" + re.escape(value) + r"(?![a-z0-9])",
            text.lower(),
        ) is not None
    return value in text.lower()


def sentiment_of(text):
    # Legacy display-only label. Trading/reference decisions are produced by
    # news_intelligence and must never consume this article-wide label.
    bull = sum(1 for word in BULL_WORDS if _contains_sentiment_term(text, word))
    bear = sum(1 for word in BEAR_WORDS if _contains_sentiment_term(text, word))
    if bull > bear:
        return "bull"
    if bear > bull:
        return "bear"
    return "neutral"


def is_macro(text):
    t = text.lower()
    return any(w in t for w in MACRO_WORDS)


def fetch_rss(url, timeout=20, retries=3):
    """带重试的RSS抓取：IncompleteRead/超时自动重试，指数退避"""
    if retries < 1:
        raise ValueError("retries must be positive")
    last = None
    for i in range(retries):
        try:
            raw = _read_https(
                url,
                allowed_hosts=RSS_ALLOWED_HOSTS,
                timeout=timeout,
                max_bytes=MAX_RSS_BYTES,
            )
            break
        except NewsUrlRejected:
            raise
        except Exception as e:
            last = e
            time.sleep(1.5 * (i + 1))
    else:
        if last is None:
            raise RuntimeError("RSS fetch failed without an error")
        raise last
    root = ET.fromstring(raw)
    items = []
    # RSS 2.0
    for it in root.iter("item"):
        title = (it.findtext("title") or "").strip()
        link = (it.findtext("link") or "").strip()
        pub = (it.findtext("pubDate") or it.findtext("date") or "").strip()
        if title and link:
            items.append((title, link, pub))
    # Atom
    if not items:
        ns = {"a": "http://www.w3.org/2005/Atom"}
        for it in root.iter("{http://www.w3.org/2005/Atom}entry"):
            title = (it.findtext("a:title", namespaces=ns) or "").strip()
            lk = it.find("a:link", ns)
            link = lk.get("href") if lk is not None else ""
            pub = (
                it.findtext("a:published", namespaces=ns)
                or it.findtext("a:updated", namespaces=ns)
                or ""
            ).strip()
            if title and link:
                items.append((title, link, pub))
    return items


_translate_cooldown_until = 0.0  # 全部翻译引擎熔断到该时间


def _tr_mymemory(text):
    q = urllib.parse.urlencode({"q": text[:450], "langpair": "en|zh-CN"})
    raw = _read_https(
        f"https://api.mymemory.translated.net/get?{q}",
        allowed_hosts=TRANSLATION_ALLOWED_HOSTS,
        timeout=15,
        max_bytes=MAX_TRANSLATION_BYTES,
    )
    data = json.loads(raw)
    t = data.get("responseData", {}).get("translatedText")
    if t and "MYMEMORY WARNING" not in t.upper() and "QUERY LENGTH LIMIT" not in t.upper():
        return t
    return None


def _tr_google_gtx(text):
    q = urllib.parse.urlencode(
        {"client": "gtx", "sl": "en", "tl": "zh-CN", "dt": "t", "q": text[:450]}
    )
    raw = _read_https(
        f"https://translate.googleapis.com/translate_a/single?{q}",
        allowed_hosts=TRANSLATION_ALLOWED_HOSTS,
        timeout=15,
        max_bytes=MAX_TRANSLATION_BYTES,
    )
    data = json.loads(raw)
    t = "".join(seg[0] for seg in (data[0] or []) if seg and seg[0])
    return t or None


def _tr_lingva(text):
    q = urllib.parse.quote(text[:450])
    raw = _read_https(
        f"https://lingva.ml/api/v1/en/zh/{q}",
        allowed_hosts=TRANSLATION_ALLOWED_HOSTS,
        timeout=15,
        max_bytes=MAX_TRANSLATION_BYTES,
    )
    data = json.loads(raw)
    t = data.get("translation")
    return t or None


_ENGINES = [_tr_mymemory, _tr_google_gtx, _tr_lingva]


def translate_en2zh(text):
    """多引擎轮换翻译；全部失败则熔断 20 分钟（免费接口日限流，避免狂刷）"""
    global _translate_cooldown_until
    if time.time() < _translate_cooldown_until:
        return None
    for eng in _ENGINES:
        try:
            t = eng(text)
            if t:
                return t
        except Exception as exc:
            print(f"[news] 翻译引擎 {eng.__name__} 失败: {type(exc).__name__}: {str(exc)[:120]}")
            continue
    _translate_cooldown_until = time.time() + 1200
    print("[news] 翻译引擎全部限流，熔断 20 分钟（免费接口日限额，次日恢复）")
    return None


def parse_pub(pub):
    from email.utils import parsedate_to_datetime

    try:
        return int(parsedate_to_datetime(pub).timestamp())
    except Exception:
        # Never make an undated article look newly published. The verifier
        # treats zero as failed provenance and therefore cannot promote it.
        return 0


_fail_streak = {}  # 源名 -> 连续失败次数
_skip_until = {}  # 源名 -> 暂停截止时间

_rr_index = 0


def _news_id(source: str, link: str) -> str:
    """Build a deterministic, collision-resistant identifier for one source URL."""

    payload = f"{source}\0{link}".encode()
    return hashlib.sha256(payload).hexdigest()


def news_once(batch=None):
    """batch=None 全量抓一轮；batch=N 轮转抓 N 个源（高频模式用，slow 源自动降频为每 5 轮 1 次）"""
    global _rr_index
    sources = store.admin_news_sources(settings.get("news_sources", []))
    if batch and sources:
        n = min(batch, len(sources))
        sel = []
        i = _rr_index
        while len(sel) < n:
            src = sources[i % len(sources)]
            i += 1
            # slow 源（易被限流）：每 5 个 tick 才抓一次
            if src.get("slow") and (_rr_index // max(n, 1)) % 5 != 0:
                continue
            sel.append(src)
        _rr_index = i % len(sources)
    else:
        sel = sources
    added = 0
    for src in sel:
        name = src["name"]
        if time.time() < _skip_until.get(name, 0):
            continue
        try:
            items = fetch_rss(src["url"])
            _fail_streak[name] = 0
            if src.get("_admin_managed"):
                store.news_source_result(name, success=True, fetched=len(items))
        except Exception as e:
            if src.get("_admin_managed"):
                store.news_source_result(name, success=False, error=str(e))
            msg = str(e)
            if "429" in msg:
                # 源站速率限制（共享代理出口易触发）：暂停 10 分钟，不计失败
                _skip_until[name] = time.time() + 600
                print(f"[news] {name} 被源站限流(429)，暂停 10 分钟后自动恢复")
                continue
            streak = _fail_streak.get(name, 0) + 1
            _fail_streak[name] = streak
            if streak >= 5:
                _skip_until[name] = time.time() + 3600
                print(f"[news] {name} 连续失败 {streak} 次，暂停 1 小时后自动恢复")
            else:
                print(f"[news] {name} 本轮失败({streak}/5): {str(e)[:50]}")
            continue
        remaining = 10
        if src.get("_admin_managed"):
            hourly_limit = int(src.get("hourly_limit", 600))
            current_hour = store.query(
                "SELECT COUNT(*) n FROM news WHERE source=? AND ts>=?",
                (name, int(time.time()) - 3600),
            )
            remaining = max(0, hourly_limit - int(current_hour[0]["n"] if current_hour else 0))
        source_added = 0
        for title, link, pub in items[: min(10, remaining)]:
            nid = _news_id(name, link)
            if store.query("SELECT 1 FROM news WHERE id=? LIMIT 1", (nid,)):
                continue
            # Older releases used MD5 identifiers. Checking the natural source/link key
            # avoids re-inserting the same article when moving to SHA-256 identifiers.
            if store.query("SELECT 1 FROM news WHERE source=? AND link=? LIMIT 1", (name, link)):
                continue
            ts = parse_pub(pub)
            lang = src.get("lang", "en")
            title_zh = None
            if lang == "en":
                title_zh = translate_en2zh(title)
                time.sleep(0.3)  # 翻译接口限速
            senti = sentiment_of(title)
            inserted = store.execute(
                "INSERT IGNORE INTO news(id,ts,source,lang,title,title_zh,link,sentiment) VALUES(?,?,?,?,?,?,?,?)",
                (nid, ts, src["name"], lang, title, title_zh, link, senti),
            )
            added += int(inserted > 0)
            source_added += int(inserted > 0)
        if source_added and src.get("_admin_managed"):
            store.news_source_result(name, success=True, inserted=source_added)
    # 每轮顺带补译最多4条历史未翻译的英文条目
    try:
        untranslated = store.query(
            "SELECT id, title FROM news WHERE lang='en' AND (title_zh IS NULL OR title_zh='') ORDER BY ts DESC LIMIT 4"
        )
        for r in untranslated:
            t = translate_en2zh(r["title"])
            if t:
                store.execute("UPDATE news SET title_zh=? WHERE id=?", (t, r["id"]))
            time.sleep(0.3)
    except Exception as exc:
        print(f"[news] 历史新闻补译失败: {type(exc).__name__}: {str(exc)[:120]}")
    if added:
        print(f"[news] 新增 {added} 条")
    return added


def news_loop(stop_event=None):
    batch = settings.get("news_batch_per_tick", 2)
    interval = settings.get("news_poll_seconds", 5)
    print(
        f"[news] 高频轮转模式：每 {interval}s 抓 {batch} 个源，全源轮转一遍约 {int(len(settings.get('news_sources', [1])) / batch * interval)}s"
    )
    while stop_event is None or not stop_event.is_set():
        if store.collector_paused("news"):
            if stop_event is not None and stop_event.wait(5):
                break
            if stop_event is None:
                time.sleep(5)
            continue
        try:
            added = news_once(batch=batch)
            store.collector_report("news", success=True, items=added)
        except Exception as e:
            print("[news] 循环异常:", e)
            store.collector_report("news", success=False, error=str(e))
        if stop_event is not None:
            stop_event.wait(interval)
        else:
            time.sleep(interval)
