"""舆情模块：多源 RSS 抓取 + 英文标题机器翻译 + 关键词情绪标注"""
import hashlib, json, re, threading, time, urllib.request, urllib.parse
import xml.etree.ElementTree as ET
from . import store
from .config_loader import settings

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
      "Accept": "application/rss+xml, application/xml, text/xml, */*",
      "Accept-Language": "en-US,en;q=0.9"}

BULL_WORDS = ["surge", "soar", "rally", "record high", "all-time high", "ath", "bull", "gain", "jump", "beat",
              "approval", "approve", "adoption", "inflow", "buy", "long", "突破", "大涨", "上涨", "利好", "新高", "增持", "获批"]
BEAR_WORDS = ["crash", "plunge", "drop", "fall", "bear", "loss", "hack", "exploit", "ban", "lawsuit", "sec sues",
              "outflow", "sell", "short", "liquidat", "fine", "fraud", "暴跌", "大跌", "下跌", "利空", "黑客", "起诉", "清算", "罚款"]
MACRO_WORDS = ["federal reserve", "fomc", "cpi", "inflation", "interest rate", "powell", "美联储", "通胀", "利率决议", "非农", " fed "]

def sentiment_of(text):
    t = text.lower()
    bull = sum(1 for w in BULL_WORDS if w in t)
    bear = sum(1 for w in BEAR_WORDS if w in t)
    if bull > bear: return "bull"
    if bear > bull: return "bear"
    return "neutral"

def is_macro(text):
    t = text.lower()
    return any(w in t for w in MACRO_WORDS)

def fetch_rss(url, timeout=20, retries=3):
    """带重试的RSS抓取：IncompleteRead/超时自动重试，指数退避"""
    last = None
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read()
            break
        except Exception as e:
            last = e
            time.sleep(1.5 * (i + 1))
    else:
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
            pub = (it.findtext("a:published", namespaces=ns) or it.findtext("a:updated", namespaces=ns) or "").strip()
            if title and link:
                items.append((title, link, pub))
    return items

_translate_cooldown_until = 0.0  # 全部翻译引擎熔断到该时间

def _tr_mymemory(text):
    q = urllib.parse.urlencode({"q": text[:450], "langpair": "en|zh-CN"})
    req = urllib.request.Request(f"https://api.mymemory.translated.net/get?{q}", headers=UA)
    with urllib.request.urlopen(req, timeout=15) as r:
        data = json.loads(r.read())
    t = data.get("responseData", {}).get("translatedText")
    if t and "MYMEMORY WARNING" not in t.upper() and "QUERY LENGTH LIMIT" not in t.upper():
        return t
    return None

def _tr_google_gtx(text):
    q = urllib.parse.urlencode({"client": "gtx", "sl": "en", "tl": "zh-CN", "dt": "t", "q": text[:450]})
    req = urllib.request.Request(f"https://translate.googleapis.com/translate_a/single?{q}", headers=UA)
    with urllib.request.urlopen(req, timeout=15) as r:
        data = json.loads(r.read())
    t = "".join(seg[0] for seg in (data[0] or []) if seg and seg[0])
    return t or None

def _tr_lingva(text):
    q = urllib.parse.quote(text[:450])
    req = urllib.request.Request(f"https://lingva.ml/api/v1/en/zh/{q}", headers=UA)
    with urllib.request.urlopen(req, timeout=15) as r:
        data = json.loads(r.read())
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
        except Exception:
            continue
    _translate_cooldown_until = time.time() + 1200
    print("[news] 翻译引擎全部限流，熔断 20 分钟（免费接口日限额，次日恢复）")
    return None

def parse_pub(pub):
    from email.utils import parsedate_to_datetime
    try:
        return int(parsedate_to_datetime(pub).timestamp())
    except Exception:
        return int(time.time())

_fail_streak = {}   # 源名 -> 连续失败次数
_skip_until = {}    # 源名 -> 暂停截止时间

_rr_index = 0

def news_once(batch=None):
    """batch=None 全量抓一轮；batch=N 轮转抓 N 个源（高频模式用，slow 源自动降频为每 5 轮 1 次）"""
    global _rr_index
    sources = settings.get("news_sources", [])
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
        except Exception as e:
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
        for title, link, pub in items[:10]:
            nid = hashlib.md5((src["name"] + link).encode()).hexdigest()
            if store.query("SELECT 1 FROM news WHERE id=?", (nid,)):
                continue
            ts = parse_pub(pub)
            lang = src.get("lang", "en")
            title_zh = None
            if lang == "en":
                title_zh = translate_en2zh(title)
                time.sleep(0.3)  # 翻译接口限速
            senti = sentiment_of(title)
            store.execute(
                "INSERT IGNORE INTO news(id,ts,source,lang,title,title_zh,link,sentiment) VALUES(?,?,?,?,?,?,?,?)",
                (nid, ts, src["name"], lang, title, title_zh, link, senti))
            added += 1
    # 每轮顺带补译最多4条历史未翻译的英文条目
    try:
        untranslated = store.query(
            "SELECT id, title FROM news WHERE lang='en' AND (title_zh IS NULL OR title_zh='') ORDER BY ts DESC LIMIT 4")
        for r in untranslated:
            t = translate_en2zh(r["title"])
            if t:
                store.execute("UPDATE news SET title_zh=? WHERE id=?", (t, r["id"]))
            time.sleep(0.3)
    except Exception:
        pass
    if added:
        print(f"[news] 新增 {added} 条")
    return added

def news_loop():
    batch = settings.get("news_batch_per_tick", 2)
    interval = settings.get("news_poll_seconds", 5)
    print(f"[news] 高频轮转模式：每 {interval}s 抓 {batch} 个源，全源轮转一遍约 {int(len(settings.get('news_sources', [1])) / batch * interval)}s")
    while True:
        try:
            news_once(batch=batch)
        except Exception as e:
            print("[news] 循环异常:", e)
        time.sleep(interval)
