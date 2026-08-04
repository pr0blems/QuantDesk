"""调度引擎：行情轮询 / K线更新 / 评分计算 / 提醒触发 / 持仓同步"""
import json
import threading
import time

from . import market_data_client as bc
from . import market_store as store
from . import signals
from .market_config import settings, tradfi_symbols

TF_MS = {"15m": 15 * 60 * 1000, "1h": 60 * 60 * 1000, "4h": 4 * 60 * 60 * 1000}

_state = {
    "started": False,
    "started_at": 0,
    "last_ticker": 0,
    "last_ws_ticker": 0,
    "last_ws_report": 0,
    "rest_blocked_until_persisted": 0,
    "last_kline_batch": {},
    "kline_cursor": {},
    "errors": [],
    "price_hist": {},   # symbol -> [(ts, price)] 用于5分钟异动
}
_lock = threading.Lock()
_ws_ingest_lock = threading.Lock()
_ws_last_write_at = 0.0
_ticker_ingest_lock = threading.Lock()
_WS_STALE_SECONDS = 90
_WS_STARTUP_GRACE_SECONDS = 30
_REST_FALLBACK_SECONDS = 300
_REST_CIRCUIT_STATE_KEY = "binance_public_rest_blocked_until"

def log_err(where, e):
    msg = f"{where}: {e}"
    print("[ERR]", msg)
    with _lock:
        _state["errors"].append({"ts": int(time.time()), "where": where, "msg": str(e)})
        _state["errors"] = _state["errors"][-50:]

def state_snapshot():
    with _lock:
        return json.loads(json.dumps(_state, default=str))


def _persist_public_rest_circuit(blocked_until=None):
    """Persist a future shared Binance REST deadline without blocking workers."""

    deadline = (
        bc.public_rest_blocked_until()
        if blocked_until is None
        else float(blocked_until)
    )
    if deadline <= time.time():
        return False
    with _lock:
        already_persisted = float(
            _state.get("rest_blocked_until_persisted") or 0
        )
    if deadline <= already_persisted:
        return True
    try:
        store.system_state_set(_REST_CIRCUIT_STATE_KEY, deadline)
    except Exception as exc:
        log_err("Binance REST circuit persistence", exc)
        return False
    with _lock:
        _state["rest_blocked_until_persisted"] = deadline
    return True


def _restore_public_rest_circuit():
    """Restore the last Binance IP-ban deadline before REST workers start."""

    try:
        persisted = store.system_state_get(_REST_CIRCUIT_STATE_KEY, 0)
    except Exception as exc:
        log_err("Binance REST circuit restore", exc)
        return False
    restored = bc.restore_public_rest_circuit(persisted)
    deadline = bc.public_rest_blocked_until()
    if deadline > time.time():
        with _lock:
            _state["rest_blocked_until_persisted"] = deadline
        try:
            persisted_deadline = float(persisted)
        except (TypeError, ValueError, OverflowError):
            persisted_deadline = 0.0
        if not restored or deadline > persisted_deadline:
            try:
                store.system_state_set(_REST_CIRCUIT_STATE_KEY, deadline)
            except Exception as exc:
                log_err("Binance REST circuit persistence", exc)
        print(
            "[market] restored Binance REST circuit for "
            f"{max(0, int(deadline - time.time()))}s"
        )
        return True
    with _lock:
        _state["rest_blocked_until_persisted"] = 0
    if persisted:
        try:
            store.system_state_set(_REST_CIRCUIT_STATE_KEY, 0)
        except Exception as exc:
            log_err("Binance REST circuit cleanup", exc)
    return False

# ---------- ticker ----------
def ingest_tickers(rows, full=True):
    """Serialize WebSocket and REST writes to the shared ticker table."""
    with _ticker_ingest_lock:
        return _ingest_tickers_locked(rows, full=full)


def _ingest_tickers_locked(rows, full=True):
    """行情统一入口：写库 + 5分钟异动检测。
    full=True 完整行（价格+涨跌幅+成交额，INSERT OR REPLACE）；
    full=False 轻量行（只更新价格与 ts，不动涨跌幅/成交额）"""
    syms = set(tradfi_symbols())
    now = int(time.time())
    out, lite = [], []
    rules = store.admin_alert_rules()
    spike_threshold = float(rules.get("spike_alert_pct_5m", settings.get("spike_alert_pct_5m", 2.0)))
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
                maybe_alert(s, "spike", "long" if chg > 0 else "short", None,
                            f"⚡ {s} 5分钟{'急涨' if chg>0 else '急跌'} {chg:+.2f}%（{old[1]:.6g} → {price:.6g}）",
                            dedup_key=f"spike:{s}", dedup_sec=300)
    if out:
        store.executemany(
            "INSERT INTO ticker(symbol,price,pct_24h,quote_volume,ts) "
            "VALUES(?,?,?,?,?) ON DUPLICATE KEY UPDATE "
            "price=VALUES(price),pct_24h=VALUES(pct_24h),"
            "quote_volume=VALUES(quote_volume),ts=VALUES(ts)",
            out,
        )
    if lite:
        store.executemany("UPDATE ticker SET price=?, ts=? WHERE symbol=?", lite)
    if out or lite:
        _state["last_ticker"] = now


def ingest_ws_tickers(rows):
    """Primary price/24h ingestion path for Binance miniTicker messages."""
    global _ws_last_write_at
    if store.collector_paused("price") and store.collector_paused("ticker"):
        return
    if not rows:
        return
    now_monotonic = time.monotonic()
    write_interval = max(
        0.5,
        float(settings.get("websocket_ticker_write_seconds", 2)),
    )
    with _ws_ingest_lock:
        if now_monotonic - _ws_last_write_at < write_interval:
            return
        _ws_last_write_at = now_monotonic
    ingest_tickers(rows, full=True)
    now = int(time.time())
    _state["last_ws_ticker"] = now
    if now - int(_state.get("last_ws_report") or 0) >= 10:
        _state["last_ws_report"] = now
        details = {"source": "websocket"}
        store.collector_report("price", success=True, items=len(rows), details=details)
        store.collector_report("ticker", success=True, items=len(rows), details=details)


def _ws_ticker_fresh(now=None):
    now = int(time.time()) if now is None else int(now)
    last_update = int(_state.get("last_ws_ticker") or 0)
    stale_seconds = max(
        30,
        int(settings.get("ticker_rest_fallback_stale_seconds", _WS_STALE_SECONDS)),
    )
    return last_update > 0 and now - last_update <= stale_seconds

def price_loop():
    """Legacy public entry point routed to the one REST fallback worker."""
    ticker_loop()


def ticker_loop():
    """Low-frequency REST fallback used only while the WebSocket is stale."""
    fallback_seconds = max(
        _REST_FALLBACK_SECONDS,
        int(settings.get("ticker_rest_seconds", _REST_FALLBACK_SECONDS)),
    )
    while True:
        retry_after = bc.public_rest_retry_after()
        if retry_after > 0:
            _persist_public_rest_circuit()
            time.sleep(max(15, retry_after + 1))
            continue
        if store.collector_paused("price") and store.collector_paused("ticker"):
            time.sleep(5)
            continue
        now = int(time.time())
        startup_age = now - int(_state.get("started_at") or now)
        if _ws_ticker_fresh(now) or startup_age < _WS_STARTUP_GRACE_SECONDS:
            time.sleep(15)
            continue
        try:
            data = bc.fetch_tickers()
            rows = [
                (
                    item["symbol"],
                    float(item["lastPrice"]),
                    float(item["priceChangePercent"]),
                    float(item.get("quoteVolume", 0)),
                    int(time.time()),
                )
                for item in data
            ]
            ingest_tickers(rows)
            details = {"source": "rest_fallback"}
            store.collector_report("price", success=True, items=len(rows), details=details)
            store.collector_report("ticker", success=True, items=len(rows), details=details)
        except bc.BinancePublicRateLimit as exc:
            _persist_public_rest_circuit(exc.blocked_until)
            log_err("ticker rest circuit", exc)
            store.collector_report("ticker", success=False, error=str(exc))
        except Exception as exc:
            log_err("ticker", exc)
            store.collector_report("ticker", success=False, error=str(exc))
        time.sleep(fallback_seconds)


# ---------- klines ----------
def _run_kline_batch(symbols, tf, last_closed_open, limit, batch_size):
    """Process one bounded/resumable kline batch for a timeframe."""
    cursor = _state["kline_cursor"].get(tf)
    if not isinstance(cursor, dict) or cursor.get("target") != last_closed_open:
        cursor = {"target": last_closed_open, "index": 0}
    start = max(0, min(int(cursor.get("index") or 0), len(symbols)))
    stop = min(start + batch_size, len(symbols))
    ok = fail = 0
    rate_limited = False
    retryable_error = False
    retry_at = 0.0
    next_index = start
    for index in range(start, stop):
        symbol = symbols[index]
        try:
            latest = store.latest_closed_time(symbol, tf)
            if latest >= last_closed_open:
                ok += 1
                next_index = index + 1
                continue
            missing = (
                max(1, int((last_closed_open - latest) // TF_MS[tf]))
                if latest
                else int(limit)
            )
            request_limit = min(int(limit), max(5, missing + 2))
            rows = bc.fetch_klines(symbol, tf, request_limit)
            rows = [row for row in rows if row[0] <= last_closed_open]
            store.upsert_klines(symbol, tf, rows)
            ok += 1
            next_index = index + 1
        except bc.BinancePublicRateLimit as exc:
            rate_limited = True
            retry_at = exc.blocked_until
            log_err(f"kline circuit {symbol} {tf}", exc)
            break
        except bc.BinancePublicRequestError as exc:
            fail += 1
            log_err(f"kline {symbol} {tf}", exc)
            if exc.retryable:
                retryable_error = True
                break
            next_index = index + 1
        except Exception as exc:
            fail += 1
            retryable_error = True
            log_err(f"kline {symbol} {tf}", exc)
            break
        time.sleep(0.1)

    completed = (
        not rate_limited
        and not retryable_error
        and next_index >= len(symbols)
    )
    if completed:
        _state["kline_cursor"].pop(tf, None)
        _state["last_kline_batch"][tf] = last_closed_open
    else:
        _state["kline_cursor"][tf] = {
            "target": last_closed_open,
            "index": next_index,
        }
    return {
        "ok": ok,
        "failed": fail,
        "completed": completed,
        "rate_limited": rate_limited,
        "retryable_error": retryable_error,
        "retry_at": retry_at,
        "next_index": next_index,
    }


def kline_loop():
    syms = tradfi_symbols()
    tfs = settings.get("timeframes", ["15m", "1h", "4h"])
    limit = max(5, min(int(settings.get("kline_limit", 300)), 1_500))
    batch_size = max(1, min(int(settings.get("kline_batch_size", 20)), 50))
    while True:
        retry_after = bc.public_rest_retry_after()
        if retry_after > 0:
            _persist_public_rest_circuit()
            store.collector_report(
                "kline",
                success=False,
                error=f"public REST circuit open for {int(retry_after)}s",
                details={"rate_limited": True},
            )
            time.sleep(max(15, retry_after + 1))
            continue
        if store.collector_paused("kline"):
            time.sleep(5)
            continue
        now_ms = int(time.time() * 1000)
        cycle_ok = cycle_fail = 0
        completed_timeframes = 0
        rate_limited = False
        retryable_error = False
        for tf in tfs:
            tfms = TF_MS.get(tf)
            if tfms is None:
                continue
            boundary = now_ms - (now_ms % tfms)
            last_closed_open = boundary - tfms
            if _state["last_kline_batch"].get(tf, 0) >= last_closed_open:
                continue
            result = _run_kline_batch(
                syms,
                tf,
                last_closed_open,
                limit,
                batch_size,
            )
            cycle_ok += result["ok"]
            cycle_fail += result["failed"]
            if result["rate_limited"]:
                rate_limited = True
                _persist_public_rest_circuit(result["retry_at"])
                break
            if result["retryable_error"]:
                retryable_error = True
                break
            if result["completed"]:
                completed_timeframes += 1
                print(
                    f"[kline] {tf} update complete "
                    f"ok={result['ok']} failed={result['failed']}"
                )
                try:
                    score_all(tf, last_closed_open)
                except Exception as exc:
                    log_err(f"score {tf}", exc)
        if completed_timeframes:
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
            except Exception as exc:
                log_err("opportunity", exc)
                store.collector_report("opportunity", success=False, error=str(exc))
        store.collector_report(
            "kline",
            success=cycle_fail == 0 and not rate_limited and not retryable_error,
            items=cycle_ok,
            error=(
                "Binance public REST rate limited"
                if rate_limited
                else f"{cycle_fail} symbol updates failed"
                if cycle_fail
                else None
            ),
            details={
                "ok": cycle_ok,
                "failed": cycle_fail,
                "rate_limited": rate_limited,
                "retryable_error": retryable_error,
                "cursor": dict(_state["kline_cursor"]),
            },
        )
        retry_after = bc.public_rest_retry_after() if rate_limited else 0
        time.sleep(max(30, retry_after + 1))

# ---------- scoring ----------
def score_all(tf, open_time):
    for s in tradfi_symbols():
        kl = store.get_klines(s, tf, settings.get("kline_limit", 300))
        if not kl:
            continue
        score, factors = signals.score_klines(kl)
        if score is None:
            continue
        store.execute("REPLACE INTO scores(symbol,tf,open_time,score,detail) VALUES(?,?,?,?,?)",
                      (s, tf, kl[-1]["open_time"], score, json.dumps(factors, ensure_ascii=False)))
        check_score_alert(s, tf, score, factors)

def check_score_alert(symbol, tf, score, factors):
    rules = store.admin_alert_rules()
    if tf not in rules.get("enabled_timeframes", settings.get("timeframes", ["15m", "1h", "4h"])):
        return
    users = store.query(
        "SELECT u.id,u.monitor_watchlist,CASE WHEN EXISTS("
        "SELECT 1 FROM positions p WHERE p.user_id=u.id AND p.symbol=?"
        ") THEN 1 ELSE 0 END AS is_held FROM users u "
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
        held_threshold = int(rules.get("score_alert_position", settings.get("score_alert_position", 40)))
        long_th = held_threshold if is_held else int(rules.get("score_alert_long", settings.get("score_alert_long", 60)))
        short_th = -held_threshold if is_held else int(rules.get("score_alert_short", settings.get("score_alert_short", -60)))
        if score >= long_th:
            maybe_alert(
                symbol, "score", "long", score,
                f"📈 {symbol} {tf} 评分 {score:+d} 触发偏多阈值(+{long_th})"
                + ("（你的实盘持仓）" if is_held else ""),
                detail=factors, dedup_key=f"score:{symbol}:{tf}:long",
                dedup_sec=TF_MS[tf] // 1000, user_id=user_id,
            )
        elif score <= short_th:
            maybe_alert(
                symbol, "score", "short", score,
                f"📉 {symbol} {tf} 评分 {score:+d} 触发偏空阈值({short_th})"
                + ("（你的实盘持仓）" if is_held else ""),
                detail=factors, dedup_key=f"score:{symbol}:{tf}:short",
                dedup_sec=TF_MS[tf] // 1000, user_id=user_id,
            )

def maybe_alert(
    symbol, kind, direction, score, message, detail=None, dedup_key=None,
    dedup_sec=900, user_id=None,
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
    with _lock:
        if _state["started"]:
            return
        _state["started"] = True
        _state["started_at"] = int(time.time())
    _restore_public_rest_circuit()
    from . import ws_ticker

    threading.Thread(
        target=ws_ticker.ws_loop,
        args=(ingest_ws_tickers,),
        daemon=True,
        name="ticker-ws",
    ).start()
    threading.Thread(target=ticker_loop, daemon=True, name="ticker-rest-fallback").start()
    threading.Thread(target=kline_loop, daemon=True, name="kline").start()
    from . import news, social
    threading.Thread(target=news.news_loop, daemon=True, name="news").start()
    threading.Thread(target=social.social_loop, daemon=True, name="social").start()
    if settings.get("paper_trading", True):
        from . import paper_engine as paper
        threading.Thread(target=paper.paper_loop, daemon=True, name="paper").start()
        print("[engine] workers started: ticker-ws / REST fallback / kline / news / social / paper")
    else:
        print("[engine] workers started: ticker-ws / REST fallback / kline / news / social")
