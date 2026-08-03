"""调度引擎：行情轮询 / K线更新 / 评分计算 / 提醒触发 / 持仓同步"""
import json, threading, time, traceback
from . import store, signals, binance_client as bc
from .config_loader import settings, api_keys, tradfi_symbols

TF_MS = {"15m": 15 * 60 * 1000, "1h": 60 * 60 * 1000, "4h": 4 * 60 * 60 * 1000}

_state = {
    "started": False,
    "last_ticker": 0,
    "last_kline_batch": {},
    "positions": [],
    "errors": [],
    "price_hist": {},   # symbol -> [(ts, price)] 用于5分钟异动
}
_lock = threading.Lock()

def log_err(where, e):
    msg = f"{where}: {e}"
    print("[ERR]", msg)
    with _lock:
        _state["errors"].append({"ts": int(time.time()), "where": where, "msg": str(e)})
        _state["errors"] = _state["errors"][-50:]

def state_snapshot():
    with _lock:
        return json.loads(json.dumps(_state, default=str))

# ---------- ticker ----------
def ingest_tickers(rows, full=True):
    """行情统一入口：写库 + 5分钟异动检测。
    full=True 完整行（价格+涨跌幅+成交额，INSERT OR REPLACE）；
    full=False 轻量行（只更新价格与 ts，不动涨跌幅/成交额）"""
    syms = set(tradfi_symbols())
    now = int(time.time())
    out, lite = [], []
    for s, price, pct, qvol, ts in rows:
        if s not in syms:
            continue
        if full:
            out.append((s, price, pct, qvol, now))
        else:
            lite.append((price, now, s))
        # 5分钟异动检测
        hist = _state["price_hist"].setdefault(s, [])
        hist.append((now, price))
        cutoff = now - 360
        _state["price_hist"][s] = [h for h in hist if h[0] >= cutoff]
        old = _state["price_hist"][s][0]
        if now - old[0] >= 300 and old[1] > 0:
            chg = (price - old[1]) / old[1] * 100
            if abs(chg) >= settings.get("spike_alert_pct_5m", 2.0):
                maybe_alert(s, "spike", "long" if chg > 0 else "short", None,
                            f"⚡ {s} 5分钟{'急涨' if chg>0 else '急跌'} {chg:+.2f}%（{old[1]:.6g} → {price:.6g}）",
                            dedup_key=f"spike:{s}", dedup_sec=300)
    if out:
        store.executemany("INSERT OR REPLACE INTO ticker(symbol,price,pct_24h,quote_volume,ts) VALUES(?,?,?,?,?)", out)
    if lite:
        store.executemany("UPDATE ticker SET price=?, ts=? WHERE symbol=?", lite)
    if out or lite:
        _state["last_ticker"] = now

def price_loop():
    """高频价格轮询（2s）：ticker/price 全量权重仅 2，30次/分共60权重，远低于1200上限"""
    while True:
        try:
            data = bc._get(f"{bc.FAPI}/fapi/v1/ticker/price", timeout=10)
            now = int(time.time())
            rows = [(t["symbol"], float(t["price"]), None, None, now) for t in data]
            ingest_tickers(rows, full=False)
        except Exception as e:
            log_err("price", e)
        time.sleep(settings.get("price_poll_seconds", 2))

def ticker_loop():
    """低频完整统计（60s）：24h涨跌幅/成交额，权重40"""
    while True:
        try:
            data = bc.fetch_tickers()
            rows = [(t["symbol"], float(t["lastPrice"]), float(t["priceChangePercent"]),
                     float(t.get("quoteVolume", 0)), int(time.time())) for t in data]
            ingest_tickers(rows)
        except Exception as e:
            log_err("ticker", e)
        time.sleep(settings.get("ticker_rest_seconds", 60))

# ---------- klines ----------
def kline_loop():
    syms = tradfi_symbols()
    tfs = settings.get("timeframes", ["15m", "1h", "4h"])
    limit = settings.get("kline_limit", 300)
    first = True
    while True:
        now_ms = int(time.time() * 1000)
        for tf in tfs:
            tfms = TF_MS[tf]
            # 当前未收盘K线的开盘时间；上一根已收盘K线开盘时间 = 边界 - 2*tfms 之后
            boundary = now_ms - (now_ms % tfms)          # 当前周期起点
            last_closed_open = boundary - tfms           # 上一根已收盘K线
            key = f"batch:{tf}"
            if not first and _state["last_kline_batch"].get(tf, 0) >= last_closed_open:
                continue  # 本周期已抓过
            ok, fail = 0, 0
            for s in syms:
                try:
                    rows = bc.fetch_klines(s, tf, limit if first else 5)
                    # 只保留已收盘的
                    rows = [r for r in rows if r[0] <= last_closed_open]
                    store.upsert_klines(s, tf, rows)
                    ok += 1
                except Exception as e:
                    fail += 1
                    log_err(f"kline {s} {tf}", e)
                if not first:
                    time.sleep(0.06)  # 控制速率
            _state["last_kline_batch"][tf] = last_closed_open
            print(f"[kline] {tf} 更新完成 ok={ok} fail={fail}")
            # 触发评分
            try:
                score_all(tf, last_closed_open)
            except Exception as e:
                log_err(f"score {tf}", e)
        if first:
            first = False
            print("[kline] 首次全量回填完成")
        time.sleep(30)

# ---------- scoring ----------
def score_all(tf, open_time):
    for s in tradfi_symbols():
        kl = store.get_klines(s, tf, settings.get("kline_limit", 300))
        if not kl:
            continue
        score, factors = signals.score_klines(kl)
        if score is None:
            continue
        store.execute("INSERT OR REPLACE INTO scores(symbol,tf,open_time,score,detail) VALUES(?,?,?,?,?)",
                      (s, tf, kl[-1]["open_time"], score, json.dumps(factors, ensure_ascii=False)))
        check_score_alert(s, tf, score, factors)

def check_score_alert(symbol, tf, score, factors):
    held = {p["symbol"] for p in _state["positions"]}
    if not held:  # 引擎刚启动 positions 尚未同步时，从数据库兜底
        held = {r["symbol"] for r in store.query("SELECT symbol FROM positions")}
    is_held = symbol in held
    th = settings.get("score_alert_position", 40) if is_held else None
    long_th = th if th else settings.get("score_alert_long", 60)
    short_th = -th if th else settings.get("score_alert_short", -60)
    # 持仓+综合阈值的判定在 combined 里做；这里做各周期独立判定
    if score >= long_th:
        maybe_alert(symbol, "score", "long", score,
                    f"📈 {symbol} {tf} 评分 {score:+d} 触发偏多阈值(+{long_th})" + ("（持仓）" if is_held else ""),
                    detail=factors, dedup_key=f"score:{symbol}:{tf}:long", dedup_sec=TF_MS[tf] // 1000)
    elif score <= short_th:
        maybe_alert(symbol, "score", "short", score,
                    f"📉 {symbol} {tf} 评分 {score:+d} 触发偏空阈值({short_th})" + ("（持仓）" if is_held else ""),
                    detail=factors, dedup_key=f"score:{symbol}:{tf}:short", dedup_sec=TF_MS[tf] // 1000)

def maybe_alert(symbol, kind, direction, score, message, detail=None, dedup_key=None, dedup_sec=900):
    if dedup_key:
        last = store.kv_get(f"alert:{dedup_key}", 0)
        if time.time() - last < dedup_sec:
            return
        store.kv_set(f"alert:{dedup_key}", time.time())
    store.add_alert(symbol, kind, direction, score, message, detail)
    print("[ALERT]", message)
    try:
        from . import notify
        notify.windows_toast("量化工作台信号", message)
    except Exception as e:
        log_err("toast", e)

# ---------- positions ----------
def positions_loop():
    while True:
        try:
            bk = api_keys.get("binance", {})
            if bk.get("verified") and bk.get("futures_enabled"):
                pos = bc.fetch_positions(bk["api_key"], bk["api_secret"])
                with _lock:
                    _state["positions"] = pos
                now = int(time.time())
                store.execute("DELETE FROM positions")
                store.executemany(
                    "INSERT INTO positions(symbol,amt,side,entry_price,mark_price,upnl,leverage,ts) VALUES(?,?,?,?,?,?,?,?)",
                    [(p["symbol"], p["amt"], p["side"], p["entry"], p["mark"], p["upnl"], p["leverage"], now) for p in pos])
        except Exception as e:
            log_err("positions", e)
        time.sleep(settings.get("positions_poll_seconds", 60))

# ---------- start ----------
def start():
    if _state["started"]:
        return
    _state["started"] = True
    threading.Thread(target=price_loop, daemon=True, name="price").start()
    threading.Thread(target=ticker_loop, daemon=True, name="ticker").start()
    threading.Thread(target=kline_loop, daemon=True, name="kline").start()
    threading.Thread(target=positions_loop, daemon=True, name="positions").start()
    if settings.get("paper_trading", True):
        from . import paper
        threading.Thread(target=paper.paper_loop, daemon=True, name="paper").start()
        print("[engine] 五个后台循环已启动: price(2s) / ticker(60s统计) / kline / positions(30s) / paper(5s)")
    else:
        print("[engine] 四个后台循环已启动: price / ticker / kline / positions")
