"""调度引擎：行情轮询 / K线更新 / 评分计算 / 提醒触发 / 持仓同步"""
import json
import math
import threading
import time
from collections.abc import Mapping

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
    "last_depth": 0,
    "last_depth_report": 0,
    "depth_complete_since": 0,
    "rest_blocked_until_persisted": 0,
    "last_kline_batch": {},
    "kline_cursor": {},
    "errors": [],
    "price_hist": {},   # symbol -> [(ts, price)] 用于短周期滚动涨跌
}
_lock = threading.Lock()
_ws_ingest_lock = threading.Lock()
_ws_last_write_at = 0.0
_ws_pending_rows: dict[str, tuple] = {}
_ticker_ingest_lock = threading.Lock()
_WS_STALE_SECONDS = 90
_WS_STARTUP_GRACE_SECONDS = 30
_REST_FALLBACK_SECONDS = 300
_REST_CIRCUIT_STATE_KEY = "binance_public_rest_blocked_until"
_ROLLING_PRICE_WINDOWS = {
    "pct_2m": 2 * 60,
    "pct_5m": 5 * 60,
    "pct_10m": 10 * 60,
}
# Keep a small margin beyond the longest displayed window.  The history is
# process-local on purpose: persisting a quote every two seconds for the whole
# universe would add millions of short-lived rows without improving accuracy.
_PRICE_HISTORY_RETENTION_SECONDS = max(_ROLLING_PRICE_WINDOWS.values()) + 60
_PRICE_HISTORY_MAX_SAMPLES = 1_024
_price_history_lock = threading.Lock()
_depth_metrics_lock = threading.Lock()
_depth_metrics_pending: dict[str, tuple] = {}
_depth_last_seen: dict[str, int] = {}
_depth_symbols: set[str] = set()
_depth_ready_event = threading.Event()
_depth_pause_lock = threading.Lock()
_depth_pause_cache = {"expires": 0.0, "paused": False}

def log_err(where, e):
    msg = f"{where}: {e}"
    print("[ERR]", msg)
    with _lock:
        _state["errors"].append({"ts": int(time.time()), "where": where, "msg": str(e)})
        _state["errors"] = _state["errors"][-50:]

def state_snapshot():
    with _lock, _price_history_lock:
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


def _price_at_or_before(
    history: list[tuple[int, float]], target_timestamp: int
) -> float | None:
    """Return the price in force at ``target_timestamp``.

    Binance's all-market mini-ticker stream only emits symbols that changed.
    Treating the last emitted price as effective until the next update is
    therefore more accurate than requiring a sample at the exact second.
    """

    for timestamp, price in reversed(history):
        if timestamp <= target_timestamp:
            return price
    return None


def _rolling_change(
    history: list[tuple[int, float]],
    current_price: float,
    *,
    now: int,
    window_seconds: int,
) -> float | None:
    baseline = _price_at_or_before(history, now - window_seconds)
    if baseline is None or baseline <= 0:
        return None
    return (current_price / baseline - 1.0) * 100.0


def _record_price_sample(symbol: str, price: float, timestamp: int) -> float | None:
    """Record one bounded price sample and return its exact rolling 5m move."""

    try:
        normalized_price = float(price)
        normalized_timestamp = int(timestamp)
    except (TypeError, ValueError, OverflowError):
        return None
    if (
        not symbol
        or not math.isfinite(normalized_price)
        or normalized_price <= 0
        or normalized_timestamp <= 0
    ):
        return None

    with _price_history_lock:
        histories = _state.setdefault("price_hist", {})
        history = histories.setdefault(symbol, [])
        if history and normalized_timestamp < history[-1][0]:
            # The collector timestamps samples on receipt, so an older sample
            # can only be a delayed duplicate and must not rewrite history.
            return _rolling_change(
                history,
                history[-1][1],
                now=history[-1][0],
                window_seconds=_ROLLING_PRICE_WINDOWS["pct_5m"],
            )
        if history and normalized_timestamp == history[-1][0]:
            history[-1] = (normalized_timestamp, normalized_price)
        else:
            history.append((normalized_timestamp, normalized_price))

        cutoff = normalized_timestamp - _PRICE_HISTORY_RETENTION_SECONDS
        # Preserve the final sample at/before the cutoff as a continuity
        # anchor.  Sparse symbols can remain unchanged for several minutes.
        keep_from = 0
        while keep_from + 1 < len(history) and history[keep_from + 1][0] <= cutoff:
            keep_from += 1
        if keep_from:
            del history[:keep_from]
        if len(history) > _PRICE_HISTORY_MAX_SAMPLES:
            del history[: len(history) - _PRICE_HISTORY_MAX_SAMPLES]

        return _rolling_change(
            history,
            normalized_price,
            now=normalized_timestamp,
            window_seconds=_ROLLING_PRICE_WINDOWS["pct_5m"],
        )


def rolling_price_changes(
    symbols: list[str] | tuple[str, ...],
    *,
    current_prices: dict[str, object] | None = None,
    now: int | None = None,
    require_fresh_stream: bool = False,
) -> dict[str, dict[str, float | None]]:
    """Return 2m/5m/10m rolling changes from the WebSocket price ring.

    A window remains ``None`` until the in-process stream has observed a
    price at or before the requested horizon.  This avoids inventing a move
    during the warm-up period after a service restart.
    """

    timestamp = int(time.time()) if now is None else int(now)
    empty = {name: None for name in _ROLLING_PRICE_WINDOWS}
    if require_fresh_stream and not _ws_ticker_fresh(timestamp):
        return {symbol: dict(empty) for symbol in symbols}
    with _price_history_lock:
        snapshots = {
            symbol: list(_state.get("price_hist", {}).get(symbol, []))
            for symbol in symbols
        }

    result: dict[str, dict[str, float | None]] = {}
    for symbol, history in snapshots.items():
        values = dict(empty)
        if not history:
            result[symbol] = values
            continue
        # The in-memory ring is updated before the ticker row is committed.
        # Using its own final sample keeps each rolling calculation internally
        # consistent during that very small cross-thread window.  The optional
        # argument remains accepted for backwards compatibility with callers.
        raw_current = history[-1][1]
        try:
            current_price = float(raw_current)
        except (TypeError, ValueError, OverflowError):
            result[symbol] = values
            continue
        if not math.isfinite(current_price) or current_price <= 0:
            result[symbol] = values
            continue
        for name, window_seconds in _ROLLING_PRICE_WINDOWS.items():
            change = _rolling_change(
                history,
                current_price,
                now=timestamp,
                window_seconds=window_seconds,
            )
            values[name] = round(change, 6) if change is not None else None
        result[symbol] = values
    return result


# ---------- order-book depth ----------
def _depth_paused() -> bool:
    """Read the admin switch at a bounded rate across all depth groups."""

    now = time.monotonic()
    with _depth_pause_lock:
        if now < float(_depth_pause_cache["expires"]):
            return bool(_depth_pause_cache["paused"])
    paused = store.collector_paused("depth")
    with _depth_pause_lock:
        _depth_pause_cache["paused"] = bool(paused)
        _depth_pause_cache["expires"] = now + 2.0
    return bool(paused)


def queue_depth_metric(metric: object) -> bool:
    """Validate and coalesce one depth update without performing a DB write."""

    if hasattr(metric, "as_dict"):
        payload = metric.as_dict()
    elif isinstance(metric, Mapping):
        payload = dict(metric)
    else:
        return False
    try:
        symbol = str(payload["symbol"]).strip().upper()
        bid_notional = float(payload["bid_depth_notional"])
        ask_notional = float(payload["ask_depth_notional"])
        bid_notional_5 = float(payload.get("bid_depth_notional_5", 0))
        ask_notional_5 = float(payload.get("ask_depth_notional_5", 0))
        imbalance = float(payload["book_imbalance"])
        imbalance_5 = float(payload["book_imbalance_5"])
        depth_levels = int(payload["depth_levels"])
        bid_level_count = int(payload.get("bid_level_count", depth_levels))
        ask_level_count = int(payload.get("ask_level_count", depth_levels))
        optional_values = {
            key: (float(payload[key]) if payload.get(key) is not None else None)
            for key in (
                "spread_bps",
                "bid_depth_change_5s_pct",
                "ask_depth_change_5s_pct",
                "bid_depth_change_30s_pct",
                "ask_depth_change_30s_pct",
                "imbalance_change_5s",
            )
        }
        timestamp = int(payload["ts"])
    except (KeyError, TypeError, ValueError, OverflowError):
        return False
    if timestamp >= 100_000_000_000:
        timestamp //= 1_000
    if (
        not symbol
        or (_depth_symbols and symbol not in _depth_symbols)
        or not all(
            math.isfinite(value)
            for value in (
                bid_notional,
                ask_notional,
                bid_notional_5,
                ask_notional_5,
                imbalance,
                imbalance_5,
                *(value for value in optional_values.values() if value is not None),
            )
        )
        or bid_notional < 0
        or ask_notional < 0
        or bid_notional_5 < 0
        or ask_notional_5 < 0
        or not -1 <= imbalance <= 1
        or not -1 <= imbalance_5 <= 1
        or not 0 <= depth_levels <= 100
        or not 0 <= bid_level_count <= 100
        or not 0 <= ask_level_count <= 100
        or (optional_values["spread_bps"] is not None and optional_values["spread_bps"] < 0)
        or (
            optional_values["imbalance_change_5s"] is not None
            and not -2 <= optional_values["imbalance_change_5s"] <= 2
        )
        or timestamp <= 0
    ):
        return False
    row = (
        symbol,
        bid_notional,
        ask_notional,
        bid_notional_5,
        ask_notional_5,
        imbalance,
        imbalance_5,
        depth_levels,
        bid_level_count,
        ask_level_count,
        optional_values["spread_bps"],
        optional_values["bid_depth_change_5s_pct"],
        optional_values["ask_depth_change_5s_pct"],
        optional_values["bid_depth_change_30s_pct"],
        optional_values["ask_depth_change_30s_pct"],
        optional_values["imbalance_change_5s"],
        timestamp,
    )
    with _depth_metrics_lock:
        _depth_metrics_pending[symbol] = row
        _depth_last_seen[symbol] = int(time.time())
    return True


def _flush_depth_metrics() -> int:
    """Persist the latest update per symbol in one bounded transaction."""

    with _depth_metrics_lock:
        if not _depth_metrics_pending:
            return 0
        batch = dict(_depth_metrics_pending)
        _depth_metrics_pending.clear()
    rows = list(batch.values())
    try:
        store.realtime_executemany(
            "INSERT INTO market_microstructure("
            "symbol,bid_depth_notional,ask_depth_notional,bid_depth_notional_5,"
            "ask_depth_notional_5,book_imbalance,book_imbalance_5,depth_levels,"
            "bid_level_count,ask_level_count,spread_bps,bid_depth_change_5s_pct,"
            "ask_depth_change_5s_pct,bid_depth_change_30s_pct,"
            "ask_depth_change_30s_pct,imbalance_change_5s,ts) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON DUPLICATE KEY UPDATE "
            "bid_depth_notional=VALUES(bid_depth_notional),"
            "ask_depth_notional=VALUES(ask_depth_notional),"
            "bid_depth_notional_5=VALUES(bid_depth_notional_5),"
            "ask_depth_notional_5=VALUES(ask_depth_notional_5),"
            "book_imbalance=VALUES(book_imbalance),"
            "book_imbalance_5=VALUES(book_imbalance_5),"
            "depth_levels=VALUES(depth_levels),"
            "bid_level_count=VALUES(bid_level_count),"
            "ask_level_count=VALUES(ask_level_count),"
            "spread_bps=VALUES(spread_bps),"
            "bid_depth_change_5s_pct=VALUES(bid_depth_change_5s_pct),"
            "ask_depth_change_5s_pct=VALUES(ask_depth_change_5s_pct),"
            "bid_depth_change_30s_pct=VALUES(bid_depth_change_30s_pct),"
            "ask_depth_change_30s_pct=VALUES(ask_depth_change_30s_pct),"
            "imbalance_change_5s=VALUES(imbalance_change_5s),ts=VALUES(ts)",
            rows,
        )
    except Exception:
        # Do not replace a newer callback that arrived while this batch was
        # being written.  The next loop retries only the still-latest values.
        with _depth_metrics_lock:
            for symbol, row in batch.items():
                _depth_metrics_pending.setdefault(symbol, row)
        raise
    with _lock:
        _state["last_depth"] = int(time.time())
    return len(rows)


def _depth_freshness(now: int | None = None) -> tuple[int, int]:
    timestamp = int(time.time()) if now is None else int(now)
    with _depth_metrics_lock:
        fresh = sum(
            1
            for symbol in _depth_symbols
            if timestamp - int(_depth_last_seen.get(symbol) or 0) <= 15
        )
    return fresh, len(_depth_symbols)


def _depth_health(now: int | None = None) -> dict[str, object]:
    timestamp = int(time.time()) if now is None else int(now)
    fresh, total = _depth_freshness(timestamp)
    complete = total > 0 and fresh == total
    with _lock:
        startup_age = timestamp - int(_state.get("started_at") or timestamp)
        complete_since = int(_state.get("depth_complete_since") or 0)
        if complete:
            if complete_since <= 0:
                complete_since = timestamp
                _state["depth_complete_since"] = complete_since
        else:
            complete_since = 0
            _state["depth_complete_since"] = 0
    stable_seconds = max(
        0,
        min(int(settings.get("depth_bootstrap_stable_seconds", 30)), 120),
    )
    stable_for = max(0, timestamp - complete_since) if complete_since else 0
    ready = complete and stable_for >= stable_seconds
    if ready:
        _depth_ready_event.set()
    bootstrap_seconds = max(
        60,
        min(int(settings.get("depth_bootstrap_priority_seconds", 240)), 600),
    )
    warming = startup_age < bootstrap_seconds and not ready
    state = "warming" if warming else "ready" if ready else "degraded"
    error = None
    if not ready and not warming:
        error = (
            f"{total - fresh} depth symbols not fresh"
            if not complete
            else "depth synchronization has not remained stable long enough"
        )
    return {
        "success": ready or warming,
        "error": error,
        "details": {
            "source": "websocket",
            "levels": 100,
            "fresh_symbols": fresh,
            "total_symbols": total,
            "state": state,
            "stable_seconds": stable_for,
        },
    }


def wait_for_depth_ready(timeout: float | None = None) -> bool:
    """Wait for the initial 150-symbol depth bootstrap without waiting forever."""

    seconds = (
        max(1.0, min(float(timeout), 600.0))
        if timeout is not None
        else max(
            60.0,
            min(
                float(settings.get("depth_bootstrap_priority_seconds", 240)),
                600.0,
            ),
        )
    )
    return _depth_ready_event.wait(seconds)


def depth_store_loop() -> None:
    interval = max(1.0, min(float(settings.get("depth_store_seconds", 2)), 10.0))
    while True:
        time.sleep(interval)
        if _depth_paused():
            continue
        try:
            written = _flush_depth_metrics()
        except Exception as exc:
            log_err("depth storage", exc)
            store.collector_report("depth", success=False, error=str(exc))
            continue
        now = int(time.time())
        with _lock:
            should_report = now - int(_state.get("last_depth_report") or 0) >= 10
            if should_report:
                _state["last_depth_report"] = now
        if should_report:
            health = _depth_health(now)
            store.collector_report(
                "depth",
                success=bool(health["success"]),
                items=written,
                error=health["error"],
                details=health["details"],
            )


def depth_loop() -> None:
    """Supervise grouped Binance depth streams without REST polling."""

    from . import ws_depth

    global _depth_symbols
    symbols = tradfi_symbols()
    _depth_symbols = set(symbols)
    group_size = max(1, min(int(settings.get("depth_stream_group_size", 50)), 50))
    snapshot_limit = int(settings.get("depth_snapshot_limit", 500))
    if snapshot_limit not in {100, 500, 1_000}:
        snapshot_limit = 500
    while True:
        try:
            ws_depth.ws_depth_loop(
                symbols,
                queue_depth_metric,
                should_pause=_depth_paused,
                group_size=group_size,
                snapshot_limit=snapshot_limit,
            )
        except Exception as exc:
            log_err("depth websocket", exc)
            store.collector_report("depth", success=False, error=str(exc))
            time.sleep(15)


def kline_after_depth_loop() -> None:
    """Give the one-time depth snapshots first claim on public REST weight."""

    if not wait_for_depth_ready():
        print("[kline] depth bootstrap wait expired; starting with degraded depth")
    kline_loop()

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
        # Reuse one bounded ring for the monitor and the existing 5m alert.
        change_5m = _record_price_sample(s, price, now)
        if change_5m is not None and abs(change_5m) >= spike_threshold:
            maybe_alert(
                s,
                "spike",
                "long" if change_5m > 0 else "short",
                None,
                f"⚡ {s} 5分钟{'急涨' if change_5m > 0 else '急跌'} {change_5m:+.2f}%",
                dedup_key=f"spike:{s}",
                dedup_sec=300,
            )
    if out:
        store.realtime_executemany(
            "INSERT INTO ticker(symbol,price,pct_24h,quote_volume,ts) "
            "VALUES(?,?,?,?,?) ON DUPLICATE KEY UPDATE "
            "price=VALUES(price),pct_24h=VALUES(pct_24h),"
            "quote_volume=VALUES(quote_volume),ts=VALUES(ts)",
            out,
        )
    if lite:
        store.realtime_executemany(
            "UPDATE ticker SET price=?, ts=? WHERE symbol=?", lite
        )
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
        # ``!miniTicker@arr`` contains only symbols that changed in that push.
        # Dropping throttled pushes would therefore leave individual symbols
        # stale indefinitely.  Coalesce the latest row per symbol and flush the
        # whole changed set at the configured DB cadence instead.
        for row in rows:
            if row:
                _ws_pending_rows[str(row[0])] = tuple(row)
        if now_monotonic - _ws_last_write_at < write_interval:
            return
        batch = list(_ws_pending_rows.values())
        _ws_pending_rows.clear()
        _ws_last_write_at = now_monotonic
    try:
        ingest_tickers(batch, full=True)
    except Exception:
        with _ws_ingest_lock:
            for row in batch:
                _ws_pending_rows.setdefault(str(row[0]), row)
        raise
    now = int(time.time())
    _state["last_ws_ticker"] = now
    if now - int(_state.get("last_ws_report") or 0) >= 10:
        _state["last_ws_report"] = now
        details = {"source": "websocket"}
        store.collector_report("price", success=True, items=len(batch), details=details)
        store.collector_report("ticker", success=True, items=len(batch), details=details)


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
        _state["depth_complete_since"] = 0
        _depth_ready_event.clear()
    _restore_public_rest_circuit()
    from . import ws_ticker

    threading.Thread(
        target=ws_ticker.ws_loop,
        args=(ingest_ws_tickers,),
        daemon=True,
        name="ticker-ws",
    ).start()
    threading.Thread(target=ticker_loop, daemon=True, name="ticker-rest-fallback").start()
    threading.Thread(target=depth_loop, daemon=True, name="depth-ws").start()
    threading.Thread(target=depth_store_loop, daemon=True, name="depth-store").start()
    threading.Thread(target=kline_after_depth_loop, daemon=True, name="kline").start()
    from . import news, social
    threading.Thread(target=news.news_loop, daemon=True, name="news").start()
    threading.Thread(target=social.social_loop, daemon=True, name="social").start()
    if settings.get("paper_trading", True):
        from . import paper_engine as paper
        threading.Thread(target=paper.paper_loop, daemon=True, name="paper").start()
        print(
            "[engine] workers started: ticker-ws / REST fallback / depth / "
            "kline / news / social / paper"
        )
    else:
        print(
            "[engine] workers started: ticker-ws / REST fallback / depth / "
            "kline / news / social"
        )
