"""Event-driven Binance market stream and incremental microstructure aggregation."""

from __future__ import annotations

import json
import math
import statistics
import threading
import time
from collections import defaultdict, deque
from typing import Any

from websockets.sync.client import connect

from . import store
from .config_loader import tradfi_symbols

WS_URL = "wss://fstream.binance.com/ws"
STREAM_SHARD_SIZE = 60
FLUSH_SECONDS = 5
WINDOW_SECONDS = 60
PROTECTION_WINDOW_SECONDS = 15


class _RealtimeState:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.books: dict[str, dict[str, float | int]] = {}
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
                    self.books[symbol] = {
                        "event_ms": event_ms,
                        "bid": bid,
                        "bid_qty": float(payload["B"]),
                        "ask": ask,
                        "ask_qty": float(payload["A"]),
                    }
                    self.quotes[symbol].append((event_ms, bid, ask))
                except (KeyError, TypeError, ValueError):
                    pass
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
                bid = float(book["bid"]) if book else None
                ask = float(book["ask"]) if book else None
                mid = (bid + ask) / 2 if bid and ask else (prices[-1][1] if prices else None)
                spread_bps = (ask - bid) / mid * 10_000 if mid and bid and ask else None
                bid_qty = float(book["bid_qty"]) if book else 0.0
                ask_qty = float(book["ask_qty"]) if book else 0.0
                book_total = bid_qty + ask_qty
                imbalance = (bid_qty - ask_qty) / book_total if book_total else None
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
                    "source": "binance_websocket",
                    "window_seconds": WINDOW_SECONDS,
                    "age_ms": age_ms,
                    "book_available": bool(book),
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
                        json.dumps(quality, ensure_ascii=False),
                    )
                )
        return output


_STATE = _RealtimeState()


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
        for stream in (f"{symbol.lower()}@bookTicker", f"{symbol.lower()}@aggTrade")
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


def market_stream_loop(stop_event: threading.Event | None = None) -> None:
    """Run sharded book/trade streams and persist one bounded snapshot per symbol."""
    stop = stop_event or threading.Event()
    symbols = tradfi_symbols()
    symbol_set = set(symbols)
    shards = [
        symbols[index : index + STREAM_SHARD_SIZE]
        for index in range(0, len(symbols), STREAM_SHARD_SIZE)
    ]
    threads = [
        threading.Thread(
            target=_receiver, args=(index, shard, stop), name=f"market-ws-{index}", daemon=True
        )
        for index, shard in enumerate(shards)
    ]
    for thread in threads:
        thread.start()
    while not stop.wait(FLUSH_SECONDS):
        try:
            rows = _STATE.snapshots(symbol_set)
            if rows:
                store.executemany(
                    """INSERT INTO market_microstructure(
                           symbol,event_time,received_at,bid_price,ask_price,mid_price,spread_bps,
                           book_imbalance,aggressive_buy_ratio,trade_count_60s,quote_volume_60s,
                           realized_volatility_60s,price_velocity_bps_60s,window_low_price,
                           window_low_event_time,window_high_price,window_high_event_time,
                           quality_json,updated_at
                       ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP)
                       ON DUPLICATE KEY UPDATE event_time=VALUES(event_time),received_at=VALUES(received_at),
                           bid_price=VALUES(bid_price),ask_price=VALUES(ask_price),mid_price=VALUES(mid_price),
                           spread_bps=VALUES(spread_bps),book_imbalance=VALUES(book_imbalance),
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
            store.collector_report("market_stream", success=True, items=len(rows))
        except Exception as exc:
            store.collector_report("market_stream", success=False, error=str(exc))
    for thread in threads:
        thread.join(timeout=5)
