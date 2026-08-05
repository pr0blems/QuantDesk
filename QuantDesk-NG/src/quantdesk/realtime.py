"""Event-driven Binance market stream and incremental microstructure aggregation."""

from __future__ import annotations

import json
import math
import statistics
import threading
import time
from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from websockets.sync.client import connect

from . import binance_client, store
from .engine import monitor_symbols

WS_URL = "wss://fstream.binance.com/ws"
STREAM_SHARD_SIZE = 60
FLUSH_SECONDS = 5
WINDOW_SECONDS = 60
PROTECTION_WINDOW_SECONDS = 15
BOOK_DEPTH_LEVELS = 100
DEPTH_SNAPSHOT_LEVELS = 500
DEPTH_STATE_LEVELS = BOOK_DEPTH_LEVELS * 2
DEPTH_FALLBACK_STALE_SECONDS = 15
DEPTH_FALLBACK_INTERVAL_SECONDS = 30
DEPTH_FALLBACK_BATCH_SIZE = 4
DEPTH_INITIAL_SYNC_WORKERS = 6


class _RealtimeState:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.books: dict[str, dict[str, Any]] = {}
        self.depth_buffers: dict[str, deque[dict[str, Any]]] = defaultdict(
            lambda: deque(maxlen=2_048)
        )
        self.quotes: dict[str, deque[tuple[int, float, float]]] = defaultdict(deque)
        self.trades: dict[str, deque[tuple[int, float, float, bool]]] = defaultdict(deque)
        self.prices: dict[str, deque[tuple[int, float]]] = defaultdict(deque)
        self.last_event_ms = 0

    def ingest(self, payload: Any) -> list[tuple]:
        now = int(time.time())
        mini_rows: list[tuple] = []
        with self.lock:
            if isinstance(payload, list):
                for item in payload:
                    if not isinstance(item, dict) or not item.get("s"):
                        continue
                    try:
                        close = float(item["c"])
                        open_price = float(item["o"])
                        mini_rows.append(
                            (
                                item["s"],
                                close,
                                (close - open_price) / open_price * 100 if open_price else 0.0,
                                float(item.get("q") or 0),
                                now,
                            )
                        )
                    except (KeyError, TypeError, ValueError, ZeroDivisionError):
                        continue
                return mini_rows

            if not isinstance(payload, dict):
                return mini_rows
            event = payload.get("e")
            symbol = str(payload.get("s") or "").upper()
            event_ms = int(payload.get("E") or payload.get("T") or now * 1000)
            self.last_event_ms = max(self.last_event_ms, event_ms)
            if event == "bookTicker" and symbol:
                try:
                    bid = float(payload["b"])
                    ask = float(payload["a"])
                    bid_qty = float(payload["B"])
                    ask_qty = float(payload["A"])
                    self.books[symbol] = {
                        "event_ms": event_ms,
                        "bids": [(bid, bid_qty)],
                        "asks": [(ask, ask_qty)],
                        "source": "binance_websocket",
                    }
                    self.quotes[symbol].append((event_ms, bid, ask))
                except (KeyError, TypeError, ValueError):
                    pass
            elif event == "depthUpdate" and symbol and "U" in payload and "u" in payload:
                self._ingest_depth_delta(symbol, payload, event_ms)
            elif symbol and (event == "depthUpdate" or "bids" in payload):
                bids = self._depth_levels(payload.get("bids", payload.get("b")))
                asks = self._depth_levels(payload.get("asks", payload.get("a")))
                if bids and asks:
                    self.books[symbol] = {
                        "event_ms": event_ms,
                        "bids": bids,
                        "asks": asks,
                        "source": str(payload.get("_source") or "binance_websocket"),
                    }
                    self.quotes[symbol].append((event_ms, bids[0][0], asks[0][0]))
            elif event == "aggTrade" and symbol:
                try:
                    price = float(payload["p"])
                    quantity = float(payload["q"])
                    # m=true means the buyer is the maker, therefore the aggressive side sold.
                    aggressive_buy = not bool(payload.get("m"))
                    self.trades[symbol].append((event_ms, price, quantity, aggressive_buy))
                    self.prices[symbol].append((event_ms, price))
                except (KeyError, TypeError, ValueError):
                    pass
        return mini_rows

    @staticmethod
    def _depth_levels(
        levels: Any, limit: int = BOOK_DEPTH_LEVELS
    ) -> list[tuple[float, float]]:
        """Normalize a bounded REST depth snapshot and retain positive levels."""

        if not isinstance(levels, list):
            return []
        normalized: list[tuple[float, float]] = []
        for level in levels[:limit]:
            if not isinstance(level, (list, tuple)) or len(level) < 2:
                continue
            try:
                price = float(level[0])
                quantity = float(level[1])
            except (TypeError, ValueError):
                continue
            if math.isfinite(price) and math.isfinite(quantity) and price > 0 and quantity > 0:
                normalized.append((price, quantity))
        return normalized

    @staticmethod
    def _depth_updates(levels: Any) -> list[tuple[float, float]]:
        """Normalize a diff-depth side, retaining zero quantities as deletions."""

        if not isinstance(levels, list):
            return []
        normalized: list[tuple[float, float]] = []
        for level in levels:
            if not isinstance(level, (list, tuple)) or len(level) < 2:
                continue
            try:
                price = float(level[0])
                quantity = float(level[1])
            except (TypeError, ValueError):
                continue
            if math.isfinite(price) and math.isfinite(quantity) and price > 0 and quantity >= 0:
                normalized.append((price, quantity))
        return normalized

    @staticmethod
    def _rebuild_book_sides(book: dict[str, Any]) -> None:
        bid_map = book.get("bid_map", {})
        ask_map = book.get("ask_map", {})
        kept_bids = sorted(bid_map.items(), reverse=True)[:DEPTH_STATE_LEVELS]
        kept_asks = sorted(ask_map.items())[:DEPTH_STATE_LEVELS]
        book["bid_map"] = dict(kept_bids)
        book["ask_map"] = dict(kept_asks)
        book["bids"] = kept_bids[:BOOK_DEPTH_LEVELS]
        book["asks"] = kept_asks[:BOOK_DEPTH_LEVELS]

    def _apply_depth_delta(self, book: dict[str, Any], delta: dict[str, Any]) -> None:
        bid_map = book["bid_map"]
        ask_map = book["ask_map"]
        for price, quantity in delta["bids"]:
            if quantity == 0:
                bid_map.pop(price, None)
            else:
                bid_map[price] = quantity
        for price, quantity in delta["asks"]:
            if quantity == 0:
                ask_map.pop(price, None)
            else:
                ask_map[price] = quantity
        self._rebuild_book_sides(book)

    def _ingest_depth_delta(self, symbol: str, payload: dict[str, Any], event_ms: int) -> None:
        """Buffer or apply Binance diff-depth events after a REST snapshot seed."""

        try:
            first_update_id = int(payload["U"])
            final_update_id = int(payload["u"])
        except (KeyError, TypeError, ValueError):
            return
        if final_update_id < first_update_id:
            return
        previous_update_id = payload.get("pu")
        try:
            previous_update_id = (
                int(previous_update_id) if previous_update_id is not None else None
            )
        except (TypeError, ValueError):
            previous_update_id = None
        delta = {
            "U": first_update_id,
            "u": final_update_id,
            "pu": previous_update_id,
            "event_ms": event_ms,
            "bids": self._depth_updates(payload.get("b")),
            "asks": self._depth_updates(payload.get("a")),
        }
        book = self.books.get(symbol)
        if not book or book.get("last_update_id") is None or book.get("needs_snapshot"):
            self.depth_buffers[symbol].append(delta)
            return
        last_update_id = int(book["last_update_id"])
        if final_update_id <= last_update_id:
            return
        awaiting_first_diff = bool(book.get("awaiting_first_diff"))
        if awaiting_first_diff:
            is_continuous = (
                first_update_id <= last_update_id <= final_update_id
                or first_update_id <= last_update_id + 1 <= final_update_id
            )
        elif previous_update_id is not None:
            is_continuous = previous_update_id == last_update_id
        else:
            is_continuous = first_update_id <= last_update_id + 1
        if not is_continuous:
            book["needs_snapshot"] = True
            self.depth_buffers[symbol].append(delta)
            return
        self._apply_depth_delta(book, delta)
        book["last_update_id"] = final_update_id
        book["event_ms"] = event_ms
        book["source"] = "binance_ws_diff_depth"
        book["awaiting_first_diff"] = False
        if book["bids"] and book["asks"]:
            self.quotes[symbol].append((event_ms, book["bids"][0][0], book["asks"][0][0]))

    def install_depth_snapshot(self, symbol: str, depth: dict[str, Any], now_ms: int) -> bool:
        """Seed a 100-level REST snapshot and reconcile any buffered diff events."""

        try:
            snapshot_update_id = int(depth["lastUpdateId"])
        except (KeyError, TypeError, ValueError):
            return False
        bids = self._depth_levels(depth.get("bids"), DEPTH_SNAPSHOT_LEVELS)
        asks = self._depth_levels(depth.get("asks"), DEPTH_SNAPSHOT_LEVELS)
        if not bids or not asks:
            return False
        with self.lock:
            book: dict[str, Any] = {
                "event_ms": now_ms,
                "bids": [],
                "asks": [],
                "bid_map": dict(bids),
                "ask_map": dict(asks),
                "last_update_id": snapshot_update_id,
                "needs_snapshot": False,
                "awaiting_first_diff": True,
                "depth_limited_by_exchange": min(len(bids), len(asks)) < BOOK_DEPTH_LEVELS,
                "source": "binance_rest_depth_snapshot",
            }
            self._rebuild_book_sides(book)
            pending = [
                event for event in self.depth_buffers[symbol] if int(event["u"]) > snapshot_update_id
            ]
            first_index = next(
                (
                    index
                    for index, event in enumerate(pending)
                    if (
                        int(event["U"]) <= snapshot_update_id <= int(event["u"])
                        or int(event["U"]) <= snapshot_update_id + 1 <= int(event["u"])
                    )
                ),
                None,
            )
            if pending and first_index is None:
                book["needs_snapshot"] = True
                self.books[symbol] = book
                self.depth_buffers[symbol] = deque(pending, maxlen=2_048)
                return False
            if first_index is not None:
                for index, event in enumerate(pending[first_index:]):
                    last_update_id = int(book["last_update_id"])
                    if int(event["u"]) <= last_update_id:
                        continue
                    if index == 0:
                        is_continuous = (
                            int(event["U"]) <= last_update_id <= int(event["u"])
                            or int(event["U"]) <= last_update_id + 1 <= int(event["u"])
                        )
                    elif event.get("pu") is not None:
                        is_continuous = int(event["pu"]) == last_update_id
                    else:
                        is_continuous = int(event["U"]) <= last_update_id + 1
                    if not is_continuous:
                        book["needs_snapshot"] = True
                        break
                    self._apply_depth_delta(book, event)
                    book["last_update_id"] = int(event["u"])
                    book["event_ms"] = int(event["event_ms"])
                    book["source"] = "binance_ws_diff_depth"
                    book["awaiting_first_diff"] = False
            last_update_id = int(book["last_update_id"])
            self.depth_buffers[symbol] = deque(
                (event for event in pending if int(event["u"]) > last_update_id), maxlen=2_048
            )
            self.books[symbol] = book
            if book["bids"] and book["asks"]:
                self.quotes[symbol].append((now_ms, book["bids"][0][0], book["asks"][0][0]))
            return not bool(book["needs_snapshot"])

    def stale_depth_symbols(self, symbols: set[str], now_ms: int) -> list[str]:
        """Return contracts that need a bounded REST depth recovery snapshot."""

        stale_after_ms = DEPTH_FALLBACK_STALE_SECONDS * 1000
        with self.lock:
            return sorted(
                symbol
                for symbol in symbols
                if (
                    (book := self.books.get(symbol)) is None
                    or bool(book.get("needs_snapshot"))
                    or (
                        min(len(book.get("bids", [])), len(book.get("asks", [])))
                        < BOOK_DEPTH_LEVELS
                        and not bool(book.get("depth_limited_by_exchange"))
                    )
                    or now_ms - int(book.get("event_ms") or 0) > stale_after_ms
                )
            )

    def snapshots(self, symbols: set[str]) -> list[tuple]:
        now_ms = int(time.time() * 1000)
        cutoff = now_ms - WINDOW_SECONDS * 1000
        output: list[tuple] = []
        with self.lock:
            for symbol in symbols:
                book = self.books.get(symbol)
                quotes = self.quotes[symbol]
                trades = self.trades[symbol]
                prices = self.prices[symbol]
                while trades and trades[0][0] < cutoff:
                    trades.popleft()
                while prices and prices[0][0] < cutoff:
                    prices.popleft()
                protection_cutoff = now_ms - PROTECTION_WINDOW_SECONDS * 1000
                while quotes and quotes[0][0] < protection_cutoff:
                    quotes.popleft()
                if not book and not trades:
                    continue
                bids = list(book.get("bids", [])) if book else []
                asks = list(book.get("asks", [])) if book else []
                bid = bids[0][0] if bids else None
                ask = asks[0][0] if asks else None
                mid = (bid + ask) / 2 if bid and ask else (prices[-1][1] if prices else None)
                spread_bps = (ask - bid) / mid * 10_000 if mid and bid and ask else None
                bid_qty = bids[0][1] if bids else 0.0
                ask_qty = asks[0][1] if asks else 0.0
                bid_depth_qty = sum(quantity for _, quantity in bids)
                ask_depth_qty = sum(quantity for _, quantity in asks)
                bid_depth_notional = sum(price * quantity for price, quantity in bids)
                ask_depth_notional = sum(price * quantity for price, quantity in asks)
                depth_total = bid_depth_notional + ask_depth_notional
                imbalance = (
                    (bid_depth_notional - ask_depth_notional) / depth_total
                    if depth_total
                    else None
                )
                near_bid_notional = sum(price * quantity for price, quantity in bids[:5])
                near_ask_notional = sum(price * quantity for price, quantity in asks[:5])
                near_total = near_bid_notional + near_ask_notional
                imbalance_5 = (
                    (near_bid_notional - near_ask_notional) / near_total
                    if near_total
                    else None
                )
                depth_levels = min(len(bids), len(asks))
                buy_quote = sum(price * qty for _, price, qty, buy in trades if buy)
                total_quote = sum(price * qty for _, price, qty, _ in trades)
                buy_ratio = buy_quote / total_quote if total_quote else None
                log_returns = [
                    math.log(current[1] / previous[1])
                    for previous, current in zip(prices, list(prices)[1:], strict=False)
                    if previous[1] > 0 and current[1] > 0
                ]
                realized_vol = (
                    statistics.pstdev(log_returns) * math.sqrt(len(log_returns)) * 10_000
                    if len(log_returns) >= 3
                    else None
                )
                velocity = (
                    (prices[-1][1] / prices[0][1] - 1) * 10_000
                    if len(prices) >= 2 and prices[0][1] > 0
                    else None
                )
                protection_prices = [item for item in prices if item[0] >= protection_cutoff]
                if quotes:
                    low_quote = min(quotes, key=lambda item: item[1])
                    high_quote = max(quotes, key=lambda item: item[2])
                    low_event = (low_quote[0], low_quote[1])
                    high_event = (high_quote[0], high_quote[2])
                    protection_source = "book_bid_ask"
                else:
                    low_event = (
                        min(protection_prices, key=lambda item: item[1])
                        if protection_prices
                        else None
                    )
                    high_event = (
                        max(protection_prices, key=lambda item: item[1])
                        if protection_prices
                        else None
                    )
                    protection_source = "agg_trade" if protection_prices else None
                event_ms = max(
                    int(book["event_ms"]) if book else 0,
                    trades[-1][0] if trades else 0,
                )
                age_ms = max(0, now_ms - event_ms)
                quality = {
                    "source": str(book.get("source") or "binance_websocket") if book else None,
                    "window_seconds": WINDOW_SECONDS,
                    "age_ms": age_ms,
                    "book_available": bool(book),
                    "depth_levels": depth_levels,
                    "depth_notional": bid_depth_notional + ask_depth_notional,
                    "depth_synchronized": bool(book and not book.get("needs_snapshot")),
                    "depth_limited_by_exchange": bool(
                        book and book.get("depth_limited_by_exchange")
                    ),
                    "trades_available": bool(trades),
                    "protection_source": protection_source,
                    "usable": age_ms <= 15_000,
                }
                output.append(
                    (
                        symbol,
                        event_ms,
                        now_ms,
                        bid,
                        ask,
                        mid,
                        spread_bps,
                        imbalance,
                        buy_ratio,
                        len(trades),
                        total_quote,
                        realized_vol,
                        velocity,
                        low_event[1] if low_event else None,
                        low_event[0] if low_event else None,
                        high_event[1] if high_event else None,
                        high_event[0] if high_event else None,
                        bid_qty,
                        ask_qty,
                        bid_depth_qty,
                        ask_depth_qty,
                        bid_depth_notional,
                        ask_depth_notional,
                        imbalance_5,
                        depth_levels,
                        json.dumps(quality, ensure_ascii=False),
                    )
                )
        return output


_STATE = _RealtimeState()
_LAST_FLUSH_PRICES: dict[str, float] = {}
_LAST_DEPTH_FALLBACK_AT: dict[str, int] = {}


def _price_move_rows(rows: list[tuple], now: int) -> list[tuple[str, int, int, int]]:
    """Count one direction change per persisted five-second market snapshot."""

    bucket_ts = now - now % 60
    movements: list[tuple[str, int, int, int]] = []
    for row in rows:
        symbol = str(row[0])
        try:
            current = float(row[5])
        except (TypeError, ValueError):
            continue
        if not math.isfinite(current) or current <= 0:
            continue
        previous = _LAST_FLUSH_PRICES.get(symbol)
        _LAST_FLUSH_PRICES[symbol] = current
        if previous is None or current == previous:
            continue
        movements.append((symbol, bucket_ts, int(current > previous), int(current < previous)))
    return movements


def _restore_missing_depth(symbols: set[str]) -> int:
    """Recover only absent or stale 100-level books without polling healthy streams."""

    now_ms = int(time.time() * 1000)
    interval_ms = DEPTH_FALLBACK_INTERVAL_SECONDS * 1000
    candidates = [
        symbol
        for symbol in _STATE.stale_depth_symbols(symbols, now_ms)
        if now_ms - _LAST_DEPTH_FALLBACK_AT.get(symbol, 0) >= interval_ms
    ][:DEPTH_FALLBACK_BATCH_SIZE]
    restored = 0
    for symbol in candidates:
        _LAST_DEPTH_FALLBACK_AT[symbol] = now_ms
        try:
            depth = binance_client.fetch_order_book(symbol, limit=DEPTH_SNAPSHOT_LEVELS)
            if _STATE.install_depth_snapshot(symbol, depth, now_ms):
                restored += 1
        except Exception as exc:
            _quality_event(
                "market_depth_fallback",
                "recovery_failed",
                {"symbol": symbol, "error": type(exc).__name__},
            )
    return restored


def _prime_depth_snapshots(symbols: list[str], stop_event: threading.Event) -> int:
    """Seed every monitored contract before applying its buffered diff-depth stream."""

    if not symbols:
        return 0
    restored = 0

    def fetch(symbol: str) -> tuple[str, dict[str, Any]]:
        return symbol, binance_client.fetch_order_book(symbol, limit=DEPTH_SNAPSHOT_LEVELS)

    with ThreadPoolExecutor(max_workers=DEPTH_INITIAL_SYNC_WORKERS) as executor:
        futures = {executor.submit(fetch, symbol): symbol for symbol in symbols}
        for future in as_completed(futures):
            if stop_event.is_set():
                break
            symbol = futures[future]
            try:
                _, depth = future.result()
                restored += int(_STATE.install_depth_snapshot(symbol, depth, int(time.time() * 1000)))
            except Exception as exc:
                _quality_event(
                    "market_depth_initial_sync",
                    "snapshot_failed",
                    {"symbol": symbol, "error": type(exc).__name__},
                )
    store.collector_report(
        "market_depth_initial_sync",
        success=restored == len(symbols),
        items=restored,
        details={
            "requested": len(symbols),
            "levels": BOOK_DEPTH_LEVELS,
            "snapshot_levels": DEPTH_SNAPSHOT_LEVELS,
        },
    )
    return restored


def _quality_event(stream_key: str, event_type: str, details: dict[str, Any]) -> None:
    try:
        store.execute(
            "INSERT INTO market_data_quality_events(stream_key,symbol,event_type,severity,"
            "event_time,details_json,created_at) VALUES(?,NULL,?,'warning',?,?,CURRENT_TIMESTAMP)",
            (
                stream_key,
                event_type,
                int(time.time() * 1000),
                json.dumps(details, ensure_ascii=False),
            ),
        )
    except Exception:
        return


def _receiver(shard_id: int, symbols: list[str], stop_event: threading.Event) -> None:
    streams = [
        stream
        for symbol in symbols
        for stream in (f"{symbol.lower()}@depth@100ms", f"{symbol.lower()}@aggTrade")
    ]
    if shard_id == 0:
        streams.append("!miniTicker@arr")
    backoff = 2
    while not stop_event.is_set():
        try:
            with connect(
                WS_URL,
                open_timeout=15,
                close_timeout=3,
                ping_interval=20,
                ping_timeout=20,
                max_size=8 * 1024 * 1024,
            ) as websocket:
                websocket.send(
                    json.dumps({"method": "SUBSCRIBE", "params": streams, "id": shard_id + 1})
                )
                backoff = 2
                store.collector_report(
                    f"market_stream_{shard_id}", success=True, details={"streams": len(streams)}
                )
                while not stop_event.is_set():
                    try:
                        message = websocket.recv(timeout=1)
                    except TimeoutError:
                        continue
                    payload = json.loads(message)
                    if (
                        isinstance(payload, dict)
                        and payload.get("result") is None
                        and "id" in payload
                    ):
                        continue
                    mini_rows = _STATE.ingest(payload)
                    if mini_rows:
                        from .engine import ingest_tickers

                        ingest_tickers(mini_rows, full=True)
        except Exception as exc:
            _quality_event(
                f"market_stream_{shard_id}",
                "disconnected",
                {"error": type(exc).__name__, "message": str(exc)[:300], "retry_seconds": backoff},
            )
            store.collector_report(f"market_stream_{shard_id}", success=False, error=str(exc))
            stop_event.wait(backoff)
            backoff = min(backoff * 2, 60)


def _start_receiver_threads(symbols: list[str], stop_event: threading.Event) -> list[threading.Thread]:
    shards = [
        symbols[index : index + STREAM_SHARD_SIZE]
        for index in range(0, len(symbols), STREAM_SHARD_SIZE)
    ]
    threads = [
        threading.Thread(
            target=_receiver, args=(index, shard, stop_event), name=f"market-ws-{index}", daemon=True
        )
        for index, shard in enumerate(shards)
    ]
    for thread in threads:
        thread.start()
    return threads


def market_stream_loop(stop_event: threading.Event | None = None) -> None:
    """Run diff-depth and trade streams, persisting a reconciled 100-level snapshot."""
    stop = stop_event or threading.Event()
    symbols = monitor_symbols()
    symbol_set = set(symbols)
    stream_stop = threading.Event()
    threads = _start_receiver_threads(symbols, stream_stop)
    if not stop.wait(1):
        _prime_depth_snapshots(symbols, stop)
    last_cleanup_bucket = 0
    while not stop.wait(FLUSH_SECONDS):
        try:
            refreshed_symbols = monitor_symbols()
            if refreshed_symbols != symbols:
                stream_stop.set()
                for thread in threads:
                    thread.join(timeout=5)
                symbols = refreshed_symbols
                symbol_set = set(symbols)
                stream_stop = threading.Event()
                threads = _start_receiver_threads(symbols, stream_stop)
                _prime_depth_snapshots(symbols, stop)
            depth_recovered = _restore_missing_depth(symbol_set)
            rows = _STATE.snapshots(symbol_set)
            if rows:
                store.executemany(
                    """INSERT INTO market_microstructure(
                           symbol,event_time,received_at,bid_price,ask_price,mid_price,spread_bps,
                           book_imbalance,aggressive_buy_ratio,trade_count_60s,quote_volume_60s,
                           realized_volatility_60s,price_velocity_bps_60s,window_low_price,
                           window_low_event_time,window_high_price,window_high_event_time,
                           bid_qty,ask_qty,bid_depth_qty,ask_depth_qty,bid_depth_notional,
                           ask_depth_notional,book_imbalance_5,depth_levels,quality_json,updated_at
                       ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP)
                       ON DUPLICATE KEY UPDATE event_time=VALUES(event_time),received_at=VALUES(received_at),
                           bid_price=VALUES(bid_price),ask_price=VALUES(ask_price),mid_price=VALUES(mid_price),
                           bid_qty=VALUES(bid_qty),ask_qty=VALUES(ask_qty),
                           bid_depth_qty=VALUES(bid_depth_qty),ask_depth_qty=VALUES(ask_depth_qty),
                           bid_depth_notional=VALUES(bid_depth_notional),
                           ask_depth_notional=VALUES(ask_depth_notional),
                           spread_bps=VALUES(spread_bps),book_imbalance=VALUES(book_imbalance),
                           book_imbalance_5=VALUES(book_imbalance_5),depth_levels=VALUES(depth_levels),
                           aggressive_buy_ratio=VALUES(aggressive_buy_ratio),
                           trade_count_60s=VALUES(trade_count_60s),quote_volume_60s=VALUES(quote_volume_60s),
                           realized_volatility_60s=VALUES(realized_volatility_60s),
                           price_velocity_bps_60s=VALUES(price_velocity_bps_60s),
                           window_low_price=VALUES(window_low_price),
                           window_low_event_time=VALUES(window_low_event_time),
                           window_high_price=VALUES(window_high_price),
                           window_high_event_time=VALUES(window_high_event_time),
                           quality_json=VALUES(quality_json),updated_at=VALUES(updated_at)""",
                    rows,
                )
                now = int(time.time())
                movement_rows = _price_move_rows(rows, now)
                if movement_rows:
                    store.executemany(
                        """INSERT INTO contract_price_move_buckets(
                               symbol,bucket_ts,up_count,down_count
                           ) VALUES(?,?,?,?) ON DUPLICATE KEY UPDATE
                               up_count=up_count+VALUES(up_count),
                               down_count=down_count+VALUES(down_count)""",
                        movement_rows,
                    )
                current_bucket = now - now % 60
                if current_bucket != last_cleanup_bucket:
                    store.execute(
                        "DELETE FROM contract_price_move_buckets WHERE bucket_ts<?",
                        (now - 31 * 60,),
                    )
                    last_cleanup_bucket = current_bucket
            store.collector_report(
                "market_stream",
                success=True,
                items=len(rows),
                details={"depth_recovered": depth_recovered},
            )
        except Exception as exc:
            store.collector_report("market_stream", success=False, error=str(exc))
    stream_stop.set()
    for thread in threads:
        thread.join(timeout=5)
