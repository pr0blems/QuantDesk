"""调度引擎：行情轮询 / K线更新 / 评分计算 / 提醒触发 / 持仓同步"""

import json
import threading
import time

from . import binance_client as bc
from . import signals, store
from .config_loader import settings, tradfi_symbols

TF_MS = {"15m": 15 * 60 * 1000, "1h": 60 * 60 * 1000, "4h": 4 * 60 * 60 * 1000}

_state = {
    "started": False,
    "last_ticker": 0,
    "last_kline_batch": {},
    "errors": [],
    "price_hist": {},  # symbol -> [(ts, price)] 用于5分钟异动
}
_lock = threading.Lock()


def _stopped(stop_event):
    return stop_event is not None and stop_event.is_set()


def _wait(stop_event, seconds):
    if stop_event is None:
        time.sleep(seconds)
        return False
    return stop_event.wait(seconds)


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
    rules = store.admin_alert_rules()
    spike_threshold = float(
        rules.get("spike_alert_pct_5m", settings.get("spike_alert_pct_5m", 2.0))
    )
    for s, price, pct, qvol, _ts in rows:
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
            if abs(chg) >= spike_threshold:
                maybe_alert(
                    s,
                    "spike",
                    "long" if chg > 0 else "short",
                    None,
                    f"⚡ {s} 5分钟{'急涨' if chg > 0 else '急跌'} {chg:+.2f}%（{old[1]:.6g} → {price:.6g}）",
                    dedup_key=f"spike:{s}",
                    dedup_sec=300,
                )
    if out:
        store.executemany(
            "REPLACE INTO ticker(symbol,price,pct_24h,quote_volume,ts) VALUES(?,?,?,?,?)", out
        )
    if lite:
        store.executemany("UPDATE ticker SET price=?, ts=? WHERE symbol=?", lite)
    if out or lite:
        _state["last_ticker"] = now


def price_loop(stop_event=None):
    """高频价格轮询（2s）：ticker/price 全量权重仅 2，30次/分共60权重，远低于1200上限"""
    while not _stopped(stop_event):
        if store.collector_paused("price"):
            _wait(stop_event, 2)
            continue
        try:
            data = bc._get(f"{bc.FAPI}/fapi/v1/ticker/price", timeout=10)
            now = int(time.time())
            rows = [(t["symbol"], float(t["price"]), None, None, now) for t in data]
            ingest_tickers(rows, full=False)
            store.collector_report("price", success=True, items=len(rows))
        except Exception as e:
            log_err("price", e)
            store.collector_report("price", success=False, error=str(e))
        _wait(stop_event, settings.get("price_poll_seconds", 2))


def ticker_loop(stop_event=None):
    """低频完整统计（60s）：24h涨跌幅/成交额，权重40"""
    while not _stopped(stop_event):
        if store.collector_paused("ticker"):
            _wait(stop_event, 5)
            continue
        try:
            data = bc.fetch_tickers()
            rows = [
                (
                    t["symbol"],
                    float(t["lastPrice"]),
                    float(t["priceChangePercent"]),
                    float(t.get("quoteVolume", 0)),
                    int(time.time()),
                )
                for t in data
            ]
            ingest_tickers(rows)
            store.collector_report("ticker", success=True, items=len(rows))
        except Exception as e:
            log_err("ticker", e)
            store.collector_report("ticker", success=False, error=str(e))
        _wait(stop_event, settings.get("ticker_rest_seconds", 60))


# ---------- klines ----------
def kline_loop(stop_event=None):
    syms = tradfi_symbols()
    tfs = settings.get("timeframes", ["15m", "1h", "4h"])
    limit = settings.get("kline_limit", 300)
    first = True
    while not _stopped(stop_event):
        if store.collector_paused("kline"):
            _wait(stop_event, 5)
            continue
        now_ms = int(time.time() * 1000)
        cycle_ok = cycle_fail = 0
        for tf in tfs:
            tfms = TF_MS[tf]
            # 当前未收盘K线的开盘时间；上一根已收盘K线开盘时间 = 边界 - 2*tfms 之后
            boundary = now_ms - (now_ms % tfms)  # 当前周期起点
            last_closed_open = boundary - tfms  # 上一根已收盘K线
            if not first and _state["last_kline_batch"].get(tf, 0) >= last_closed_open:
                continue  # 本周期已抓过
            ok, fail = 0, 0
            for s in syms:
                if _stopped(stop_event):
                    break
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
            cycle_ok += ok
            cycle_fail += fail
            print(f"[kline] {tf} 更新完成 ok={ok} fail={fail}")
            # 触发评分
            try:
                score_all(tf, last_closed_open)
            except Exception as e:
                log_err(f"score {tf}", e)
        if cycle_ok:
            try:
                from . import opportunity

                opportunity_result = opportunity.scan_all(syms)
                store.collector_report(
                    "opportunity",
                    success=not opportunity_result["failed"],
                    items=opportunity_result["scanned"],
                    error=(
                        f"{opportunity_result['failed']} symbol scans failed"
                        if opportunity_result["failed"]
                        else None
                    ),
                    details=opportunity_result,
                )
                print(f"[opportunity] 扫描完成 {opportunity_result}")
            except Exception as e:
                log_err("opportunity", e)
                store.collector_report("opportunity", success=False, error=str(e))
        if first:
            first = False
            print("[kline] 首次全量回填完成")
        store.collector_report(
            "kline",
            success=cycle_fail == 0,
            items=cycle_ok,
            error=f"{cycle_fail} symbol updates failed" if cycle_fail else None,
            details={"ok": cycle_ok, "failed": cycle_fail},
        )
        _wait(stop_event, 30)


# ---------- scoring ----------
def score_all(tf, open_time):
    for s in tradfi_symbols():
        kl = store.get_klines(s, tf, settings.get("kline_limit", 300))
        if not kl:
            continue
        score, factors = signals.score_klines(kl)
        if score is None:
            continue
        store.execute(
            "REPLACE INTO scores(symbol,tf,open_time,score,detail) VALUES(?,?,?,?,?)",
            (s, tf, kl[-1]["open_time"], score, json.dumps(factors, ensure_ascii=False)),
        )
        check_score_alert(s, tf, score, factors)


def check_score_alert(symbol, tf, score, factors):
    rules = store.admin_alert_rules()
    if tf not in rules.get("enabled_timeframes", settings.get("timeframes", ["15m", "1h", "4h"])):
        return
    users = store.query(
        "SELECT u.id,u.monitor_watchlist,CASE WHEN p.symbol IS NULL THEN 0 ELSE 1 END AS is_held "
        "FROM users u LEFT JOIN positions p ON p.user_id=u.id AND p.symbol=? "
        "WHERE u.is_active=1",
        (symbol,),
    )
    for user in users:
        user_id = int(user["id"])
        is_held = bool(user["is_held"])
        watchlist = user.get("monitor_watchlist") or []
        if isinstance(watchlist, str):
            try:
                watchlist = json.loads(watchlist)
            except json.JSONDecodeError:
                watchlist = []
        if rules.get("watchlist_only", False) and not is_held and symbol not in watchlist:
            continue
        held_threshold = int(
            rules.get("score_alert_position", settings.get("score_alert_position", 40))
        )
        long_th = (
            held_threshold
            if is_held
            else int(rules.get("score_alert_long", settings.get("score_alert_long", 60)))
        )
        short_th = (
            -held_threshold
            if is_held
            else int(rules.get("score_alert_short", settings.get("score_alert_short", -60)))
        )
        if score >= long_th:
            maybe_alert(
                symbol,
                "score",
                "long",
                score,
                f"📈 {symbol} {tf} 评分 {score:+d} 触发偏多阈值(+{long_th})"
                + ("（你的实盘持仓）" if is_held else ""),
                detail=factors,
                dedup_key=f"score:{symbol}:{tf}:long",
                dedup_sec=TF_MS[tf] // 1000,
                user_id=user_id,
            )
        elif score <= short_th:
            maybe_alert(
                symbol,
                "score",
                "short",
                score,
                f"📉 {symbol} {tf} 评分 {score:+d} 触发偏空阈值({short_th})"
                + ("（你的实盘持仓）" if is_held else ""),
                detail=factors,
                dedup_key=f"score:{symbol}:{tf}:short",
                dedup_sec=TF_MS[tf] // 1000,
                user_id=user_id,
            )


def maybe_alert(
    symbol,
    kind,
    direction,
    score,
    message,
    detail=None,
    dedup_key=None,
    dedup_sec=900,
    user_id=None,
):
    if dedup_key:
        state_key = f"alert:{dedup_key}"
        last = (
            store.user_state_get(user_id, state_key, 0)
            if user_id is not None
            else store.system_state_get(state_key, 0)
        )
        if time.time() - last < dedup_sec:
            return
        if user_id is not None:
            store.user_state_set(user_id, state_key, time.time())
        else:
            store.system_state_set(state_key, time.time())
    store.add_alert(symbol, kind, direction, score, message, detail, user_id=user_id)
    print("[ALERT]", message)
    if user_id is not None:
        return
    try:
        from . import notify

        notify.windows_toast("量化工作台信号", message)
    except Exception as e:
        log_err("toast", e)


# ---------- start ----------
def start():
    if _state["started"]:
        return
    _state["started"] = True
    threading.Thread(target=price_loop, daemon=True, name="price").start()
    threading.Thread(target=ticker_loop, daemon=True, name="ticker").start()
    threading.Thread(target=kline_loop, daemon=True, name="kline").start()
    from . import news, social

    threading.Thread(target=news.news_loop, daemon=True, name="news").start()
    threading.Thread(target=social.social_loop, daemon=True, name="social").start()
    if settings.get("paper_trading", True):
        from . import paper

        threading.Thread(target=paper.paper_loop, daemon=True, name="paper").start()
        print("[engine] workers started: price / ticker / kline / news / social / paper")
    else:
        print("[engine] workers started: price / ticker / kline / news / social")
