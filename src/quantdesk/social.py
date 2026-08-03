"""社交情绪层：Stocktwits（牛熊标签+热榜）+ ApeWisdom（Reddit/4chan 提及量）
容错策略：超时/截断自动重试；403/429 触发 45 分钟冷却；404/非美股标的永久跳过不刷屏。"""
import json, threading, time, urllib.request, urllib.parse, urllib.error
from . import store
from .config_loader import settings, symbols_meta

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) quantdesk-local"}

class RateLimited(Exception):
    pass

_cooldown_until = 0.0      # Stocktwits 冷却截止时间
_unsupported = set()       # 404 过的标的，永久跳过

# Stocktwits/ApeWisdom 只覆盖美股个股与美股ETF；港股/韩股/大宗/Pre-IPO 直接跳过
_COVERED_TYPES = {"EQUITY"}
_BASE_TYPE = {s["symbol"].replace("USDT", "").replace("USD1", ""): s.get("underlyingType", "")
              for s in symbols_meta.get("symbols", [])}
_SKIP_BASES = {"SPCX", "TENCENT"}  # 映射特殊或重复的

def _get_json(url, timeout=20, retries=3):
    last = None
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code in (403, 418, 429):
                raise RateLimited(f"HTTP {e.code}")
            if e.code == 404:
                raise
            last = f"HTTP {e.code}"
        except Exception as e:
            last = str(e)
        time.sleep(2 * (i + 1))
    raise RuntimeError(last)

def st_symbol_sentiment(base):
    d = _get_json(f"https://api.stocktwits.com/api/2/streams/symbol/{urllib.parse.quote(base)}.json")
    bull = bear = 0
    for m in d.get("messages", []):
        s = ((m.get("entities") or {}).get("sentiment") or {}).get("basic")
        if s == "Bullish": bull += 1
        elif s == "Bearish": bear += 1
    return {"bull": bull, "bear": bear, "msgs": len(d.get("messages", []))}

def st_trending():
    d = _get_json("https://api.stocktwits.com/api/2/trending/symbols.json")
    return [s.get("symbol") for s in d.get("symbols", [])][:30]

def apewisdom_stocks():
    return _get_json("https://apewisdom.io/api/v1.0/filter/all-stocks").get("results", [])

def base_of(symbol):
    return symbol.replace("USDT", "").replace("USD1", "")

def _covered(base):
    return _BASE_TYPE.get(base) in _COVERED_TYPES and base not in _SKIP_BASES and base not in _unsupported

def social_once(priority_bases):
    global _cooldown_until
    now = int(time.time())
    # 1. ApeWisdom 全榜（独立于 Stocktwits，一次请求覆盖全部）
    ape_map = {}
    try:
        for r in apewisdom_stocks():
            ape_map[r.get("ticker", "").upper()] = r
    except Exception as e:
        print("[social] apewisdom 本轮失败（下轮重试）:", str(e)[:60])
    # 2. Stocktwits（冷却期跳过）
    trending = []
    if time.time() < _cooldown_until:
        pass
    else:
        try:
            trending = st_trending()
            store.system_state_set("st_trending", {"ts": now, "symbols": trending})
        except RateLimited:
            _cooldown_until = time.time() + 45 * 60
            print("[social] Stocktwits 触发限流，冷却 45 分钟")
            trending = None
        except Exception as e:
            print("[social] stocktwits trending 本轮失败:", str(e)[:60])
            trending = None
    # 3. 重点标的情绪（仅美股覆盖范围）
    for base in priority_bases[:25]:
        if not _covered(base):
            continue
        st = {"bull": None, "bear": None, "msgs": None}
        if trending is not None and time.time() >= _cooldown_until:
            try:
                st = st_symbol_sentiment(base)
                time.sleep(1.2)  # 温和限流
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    _unsupported.add(base)  # 该标的不存在，永久跳过
            except RateLimited:
                _cooldown_until = time.time() + 45 * 60
                print("[social] Stocktwits 触发限流，冷却 45 分钟")
            except Exception:
                pass  # 单标的失败不刷屏，下轮再试
        ape = ape_map.get(base.upper(), {})
        if st["bull"] is None and not ape:
            continue
        store.execute(
            "REPLACE INTO social(symbol, st_bull, st_bear, st_msgs, ape_mentions, ape_upvotes, ape_rank, ape_rank_24h, ts) VALUES(?,?,?,?,?,?,?,?,?)",
            (base, st["bull"], st["bear"], st["msgs"],
             ape.get("mentions"), ape.get("upvotes"), ape.get("rank"), ape.get("rank_24h_ago"), now))

def priority_symbols():
    from . import engine
    held = [p["symbol"] for p in engine.state_snapshot()["positions"]]
    watch = settings.get("watchlist", [])
    return list(dict.fromkeys([base_of(s) for s in held + watch]))

def social_loop():
    while True:
        try:
            social_once(priority_symbols())
        except Exception as e:
            print("[social] 循环异常（下轮继续）:", str(e)[:60])
        time.sleep(900)  # 15分钟一轮，避免触发限流
