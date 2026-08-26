from __future__ import annotations

import json
import time
from urllib.parse import parse_qs, urlsplit

import pytest

from quantdesk_v2 import market_data_client, ws_depth
from quantdesk_v2.binance_rate_limit import rest_request_weight
from quantdesk_v2.ws_depth import (
    DepthOrderBook,
    DepthStreamCollector,
    _depth_uri,
    depth_symbol_groups,
)


def _snapshot(
    update_id: int = 100,
    *,
    bids: list[list[str]] | None = None,
    asks: list[list[str]] | None = None,
) -> dict:
    return {
        "lastUpdateId": update_id,
        "E": 1_800_000_000_000,
        "bids": bids or [["100", "1"], ["99", "2"]],
        "asks": asks or [["101", "1"], ["102", "3"]],
    }


def _event(
    first_id: int,
    final_id: int,
    previous_id: int,
    *,
    symbol: str = "BTCUSDT",
    bids: list[list[str]] | None = None,
    asks: list[list[str]] | None = None,
) -> dict:
    return {
        "e": "depthUpdate",
        "E": 1_800_000_005_000,
        "s": symbol,
        "U": first_id,
        "u": final_id,
        "pu": previous_id,
        "b": [] if bids is None else bids,
        "a": [] if asks is None else asks,
    }


def test_depth_book_reconciles_buffered_event_and_computes_top_notional() -> None:
    book = DepthOrderBook("btcusdt")
    assert (
        book.feed(
            _event(
                101,
                102,
                100,
                bids=[["100", "2"], ["98", "4"]],
                asks=[["101", "0"], ["103", "2"]],
            )
        )
        is None
    )

    metrics = book.load_snapshot(_snapshot(101))

    assert metrics is not None
    assert metrics.symbol == "BTCUSDT"
    assert metrics.bid_depth_notional == pytest.approx(790.0)
    assert metrics.ask_depth_notional == pytest.approx(512.0)
    assert metrics.book_imbalance == pytest.approx((790 - 512) / (790 + 512))
    assert metrics.book_imbalance_5 == pytest.approx(metrics.book_imbalance)
    assert metrics.depth_levels == 2
    assert metrics.ts == 1_800_000_005
    assert book.last_update_id == 102
    assert book.synced is True


def test_depth_book_retains_deeper_snapshot_but_exports_exact_top_100() -> None:
    bids = [[str(1_000 - index), "1"] for index in range(150)]
    asks = [[str(1_001 + index), "1"] for index in range(150)]
    book = DepthOrderBook("BTCUSDT")

    initial = book.load_snapshot(_snapshot(bids=bids, asks=asks))
    assert initial is not None
    assert initial.depth_levels == 100
    assert initial.bid_depth_notional == pytest.approx(95_050)
    assert initial.ask_depth_notional == pytest.approx(105_050)

    after_delete = book.feed(_event(101, 101, 100, bids=[["1000", "0"]], asks=[["1001", "0"]]))

    assert after_delete is not None
    assert after_delete.depth_levels == 100
    # Former level 101 moves into the visible pool rather than disappearing.
    assert after_delete.bid_depth_notional == pytest.approx(94_950)
    assert after_delete.ask_depth_notional == pytest.approx(105_150)


def test_depth_book_reports_side_counts_spread_and_rolling_growth() -> None:
    book = DepthOrderBook("BTCUSDT")
    initial = book.load_snapshot(_snapshot())
    assert initial is not None
    event = _event(101, 101, 100, bids=[["100", "2"]])
    event["E"] = 1_800_000_005_000

    metrics = book.feed(event)

    assert metrics is not None
    assert metrics.bid_level_count == 2
    assert metrics.ask_level_count == 2
    assert metrics.bid_depth_notional_5 == pytest.approx(metrics.bid_depth_notional)
    assert metrics.ask_depth_notional_5 == pytest.approx(metrics.ask_depth_notional)
    assert metrics.spread_bps > 0
    assert metrics.bid_depth_change_5s_pct == pytest.approx((398 - 298) / 298 * 100)
    assert metrics.ask_depth_change_5s_pct == pytest.approx(0)
    assert metrics.imbalance_change_5s is not None


def test_depth_book_exports_ranked_live_level_snapshot() -> None:
    book = DepthOrderBook("BTCUSDT")
    snapshot = book.load_snapshot(
        _snapshot(
            bids=[["100", "2"], ["99", "3"]],
            asks=[["101", "4"], ["102", "5"]],
        )
    )
    assert snapshot is not None

    levels = book.level_snapshot(20)

    assert levels is not None
    assert levels["source"] == "binance_futures_diff_depth"
    assert levels["best_bid"] == pytest.approx(100)
    assert levels["best_ask"] == pytest.approx(101)
    assert levels["mid_price"] == pytest.approx(100.5)
    assert levels["bid_depth_notional"] == pytest.approx(497)
    assert levels["ask_depth_notional"] == pytest.approx(914)
    assert levels["bids"][0]["rank"] == 1
    assert levels["bids"][0]["cumulative_notional"] == pytest.approx(200)
    assert levels["bids"][1]["cumulative_notional"] == pytest.approx(497)
    assert levels["asks"][0]["distance_bps"] > 0
    assert levels["bids"][0]["distance_bps"] < 0
    assert levels["largest_bid_wall"]["price"] == pytest.approx(99)
    assert levels["largest_ask_wall"]["price"] == pytest.approx(102)


def test_depth_book_level_snapshot_requires_a_synchronized_book() -> None:
    book = DepthOrderBook("BTCUSDT")

    assert book.level_snapshot(100) is None
    with pytest.raises(ValueError, match="20, 50, or 100"):
        book.level_snapshot(10)


@pytest.fixture
def empty_rest_depth_cache():
    with ws_depth._REST_BOOK_CACHE_LOCK:
        ws_depth._REST_BOOK_CACHE.clear()
        ws_depth._REST_BOOK_REFRESH_LOCKS.clear()
    yield
    with ws_depth._REST_BOOK_CACHE_LOCK:
        ws_depth._REST_BOOK_CACHE.clear()
        ws_depth._REST_BOOK_REFRESH_LOCKS.clear()


@pytest.fixture
def shared_depth_snapshot_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(ws_depth, "SHARED_DEPTH_SNAPSHOT_DIR", tmp_path)
    return tmp_path


def test_order_book_snapshot_prefers_process_local_websocket(monkeypatch) -> None:
    monkeypatch.setattr(
        ws_depth,
        "live_order_book_snapshot",
        lambda symbol, limit: {
            "symbol": symbol,
            "limit": limit,
            "source": "binance_futures_diff_depth",
        },
    )
    monkeypatch.setattr(
        ws_depth,
        "fetch_depth_snapshot",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("REST fallback must not run")
        ),
    )

    snapshot = ws_depth.order_book_snapshot("BTCUSDT", 50)

    assert snapshot["source"] == "binance_futures_diff_depth"
    assert snapshot["transport"] == "websocket"
    assert snapshot["stale_fallback"] is False


def test_order_book_snapshot_rest_fallback_is_cached_and_relimited(
    monkeypatch, empty_rest_depth_cache
) -> None:
    calls: list[tuple[str, int]] = []
    bids = [[str(1_000 - index), "1"] for index in range(120)]
    asks = [[str(1_001 + index), "1"] for index in range(120)]
    monkeypatch.setattr(ws_depth, "live_order_book_snapshot", lambda *_args: None)
    monkeypatch.setattr(ws_depth, "shared_order_book_snapshot", lambda *_args: None)

    def fetch(symbol: str, limit: int) -> dict:
        calls.append((symbol, limit))
        return _snapshot(bids=bids, asks=asks)

    monkeypatch.setattr(ws_depth, "fetch_depth_snapshot", fetch)

    top_20 = ws_depth.order_book_snapshot("btcusdt", 20)
    top_100 = ws_depth.order_book_snapshot("BTCUSDT", 100)

    assert calls == [("BTCUSDT", 100)]
    assert len(top_20["bids"]) == len(top_20["asks"]) == 20
    assert len(top_100["bids"]) == len(top_100["asks"]) == 100
    assert top_100["source"] == "binance_futures_rest_depth"
    assert top_100["transport"] == "rest_cache"
    assert top_100["stale_fallback"] is False


def test_order_book_snapshot_uses_brief_stale_cache_during_rest_failure(
    monkeypatch, empty_rest_depth_cache
) -> None:
    clock = [100.0]
    attempts = 0
    monkeypatch.setattr(ws_depth.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(ws_depth, "live_order_book_snapshot", lambda *_args: None)
    monkeypatch.setattr(ws_depth, "shared_order_book_snapshot", lambda *_args: None)

    def fetch(_symbol: str, _limit: int) -> dict:
        nonlocal attempts
        attempts += 1
        if attempts > 1:
            raise OSError("temporary upstream failure")
        return _snapshot()

    monkeypatch.setattr(ws_depth, "fetch_depth_snapshot", fetch)
    assert ws_depth.order_book_snapshot("BTCUSDT", 20)["stale_fallback"] is False

    clock[0] += ws_depth.REST_SNAPSHOT_CACHE_SECONDS + 1
    stale = ws_depth.order_book_snapshot("BTCUSDT", 20)

    assert stale["stale_fallback"] is True
    assert stale["server_cache_age_seconds"] == pytest.approx(3.0)


def test_order_book_snapshot_reports_chinese_error_without_any_source(
    monkeypatch, empty_rest_depth_cache
) -> None:
    monkeypatch.setattr(ws_depth, "live_order_book_snapshot", lambda *_args: None)
    monkeypatch.setattr(ws_depth, "shared_order_book_snapshot", lambda *_args: None)
    monkeypatch.setattr(
        ws_depth,
        "fetch_depth_snapshot",
        lambda *_args: (_ for _ in ()).throw(OSError("upstream unavailable")),
    )

    with pytest.raises(ws_depth.OrderBookUnavailableError, match="盘口快照暂不可用"):
        ws_depth.order_book_snapshot("BTCUSDT", 100)


def test_shared_websocket_snapshot_round_trip_and_relimit(
    shared_depth_snapshot_dir,
) -> None:
    now_ms = int(time.time() * 1_000)
    bids = [[str(1_000 - index), "1"] for index in range(100)]
    asks = [[str(1_001 + index), "1"] for index in range(100)]
    book = DepthOrderBook("BTCUSDT")
    assert book.load_snapshot({**_snapshot(bids=bids, asks=asks), "E": now_ms}) is not None
    live = book.level_snapshot(100)

    assert live is not None
    assert ws_depth._publish_shared_order_book_snapshot(live) is True

    shared = ws_depth.shared_order_book_snapshot("btcusdt", 20)

    assert shared is not None
    assert shared["source"] == "binance_futures_shared_ws_depth"
    assert shared["transport"] == "websocket_shared"
    assert shared["stale_fallback"] is False
    assert len(shared["bids"]) == len(shared["asks"]) == 20
    assert shared["last_update_id"] == live["last_update_id"]


def test_order_book_snapshot_prefers_shared_websocket_over_rest(
    monkeypatch, shared_depth_snapshot_dir
) -> None:
    now_ms = int(time.time() * 1_000)
    book = DepthOrderBook("BTCUSDT")
    assert book.load_snapshot({**_snapshot(), "E": now_ms}) is not None
    live = book.level_snapshot(100)
    assert live is not None
    assert ws_depth._publish_shared_order_book_snapshot(live) is True
    monkeypatch.setattr(ws_depth, "live_order_book_snapshot", lambda *_args: None)
    monkeypatch.setattr(
        ws_depth,
        "_cached_rest_order_book_snapshot",
        lambda *_args: (_ for _ in ()).throw(
            AssertionError("REST fallback must not run while shared WS is fresh")
        ),
    )

    snapshot = ws_depth.order_book_snapshot("BTCUSDT", 20)

    assert snapshot["transport"] == "websocket_shared"


def test_shared_websocket_snapshot_expires_before_rest_fallback(
    monkeypatch, shared_depth_snapshot_dir
) -> None:
    book = DepthOrderBook("BTCUSDT")
    stale_ms = int((time.time() - ws_depth.SHARED_SNAPSHOT_MAX_AGE_SECONDS - 2) * 1_000)
    assert book.load_snapshot({**_snapshot(), "E": stale_ms}) is not None
    live = book.level_snapshot(100)
    assert live is not None
    assert ws_depth._publish_shared_order_book_snapshot(live) is True
    monkeypatch.setattr(ws_depth, "live_order_book_snapshot", lambda *_args: None)
    monkeypatch.setattr(
        ws_depth,
        "_cached_rest_order_book_snapshot",
        lambda symbol, limit: {"symbol": symbol, "limit": limit, "transport": "rest_cache"},
    )

    snapshot = ws_depth.order_book_snapshot("BTCUSDT", 50)

    assert snapshot["transport"] == "rest_cache"


def test_shared_websocket_snapshot_rejects_invalid_metric(
    shared_depth_snapshot_dir,
) -> None:
    path = shared_depth_snapshot_dir / "BTCUSDT.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "symbol": "BTCUSDT",
                "captured_at": int(time.time()),
                "last_update_id": 100,
                "bids": [["100", "1"]],
                "asks": [["101", "1"]],
                "bid_depth_change_5s_pct": "not-a-number",
            }
        ),
        encoding="utf-8",
    )

    assert ws_depth.shared_order_book_snapshot("BTCUSDT", 20) is None


def test_collector_heartbeat_publishes_shared_websocket_snapshot(monkeypatch) -> None:
    emitted = []
    published = []
    collector = DepthStreamCollector(["BTCUSDT"], emitted.append)
    generation = collector._activate_session()
    assert (
        collector.books["BTCUSDT"].load_snapshot({**_snapshot(), "E": int(time.time() * 1_000)})
        is not None
    )
    monkeypatch.setattr(
        ws_depth,
        "_publish_shared_order_book_snapshot",
        lambda snapshot: published.append(snapshot) or True,
    )

    collector._emit_heartbeats(generation)

    assert len(published) == 1
    assert published[0]["symbol"] == "BTCUSDT"
    assert len(emitted) == 1


def test_depth_gap_invalidates_until_fresh_snapshot_bridges_it() -> None:
    book = DepthOrderBook("BTCUSDT")
    assert book.load_snapshot(_snapshot()) is not None
    assert book.feed(_event(101, 101, 100, bids=[["100", "2"]])) is not None

    # Futures continuity is authoritative through pu; a mismatch must not be published.
    assert book.feed(_event(102, 103, 999, asks=[["102", "2"]])) is None
    assert book.synced is False
    assert book.metrics() is None
    assert book.pending_events == 1

    recovered = book.load_snapshot(_snapshot(103))
    assert recovered is not None
    assert book.synced is True
    assert book.last_update_id == 103


def test_snapshot_that_cannot_bridge_buffer_stays_unsynchronized() -> None:
    book = DepthOrderBook("BTCUSDT")
    book.feed(_event(110, 110, 109))

    assert book.load_snapshot(_snapshot(100)) is None
    assert book.needs_snapshot is True
    assert book.pending_events == 1


def test_snapshot_rejects_contradictory_predecessor_with_skipped_range() -> None:
    book = DepthOrderBook("BTCUSDT")
    book.feed(_event(110, 110, 100))

    assert book.load_snapshot(_snapshot(100)) is None
    assert book.needs_snapshot is True


def test_contiguous_pu_accepts_an_aggregated_update_id_range() -> None:
    book = DepthOrderBook("BTCUSDT")
    assert book.load_snapshot(_snapshot()) is not None

    metrics = book.feed(_event(101, 105, 100, bids=[["100", "2"]]))

    assert metrics is not None
    assert book.last_update_id == 105


def test_first_event_can_bridge_a_snapshot_ahead_of_the_receive_buffer() -> None:
    book = DepthOrderBook("BTCUSDT")
    # Only an event older than the REST snapshot has reached the buffer.
    assert book.feed(_event(98, 99, 97)) is None
    assert book.load_snapshot(_snapshot(105)) is not None

    # The next event overlaps update 105, but pu correctly refers to the
    # previous stream event rather than the independently fetched snapshot.
    metrics = book.feed(_event(103, 107, 102, bids=[["100", "2"]]))

    assert metrics is not None
    assert book.synced is True
    assert book.last_update_id == 107


def test_first_event_after_snapshot_must_bridge_or_directly_continue_it() -> None:
    book = DepthOrderBook("BTCUSDT")
    assert book.load_snapshot(_snapshot(105)) is not None

    assert book.feed(_event(110, 111, 109)) is None
    assert book.needs_snapshot is True


def test_depth_disconnect_invalidation_discards_old_session_state() -> None:
    book = DepthOrderBook("BTCUSDT")
    assert book.load_snapshot(_snapshot()) is not None

    book.invalidate()

    assert book.metrics() is None
    assert book.last_update_id is None
    assert book.pending_events == 0


def test_depth_heartbeat_only_refreshes_a_synchronized_book() -> None:
    book = DepthOrderBook("BTCUSDT")
    assert book.heartbeat(1_900_000_000) is None
    assert book.load_snapshot(_snapshot()) is not None

    heartbeat = book.heartbeat(1_900_000_000)

    assert heartbeat is not None
    assert heartbeat.ts == 1_900_000_000
    assert heartbeat.bid_depth_notional == pytest.approx(298)


def test_pending_depth_buffer_is_bounded() -> None:
    book = DepthOrderBook("BTCUSDT", max_pending_events=2)
    book.feed(_event(101, 101, 100))
    book.feed(_event(102, 102, 101))
    book.feed(_event(103, 103, 102))

    assert book.pending_events == 2


@pytest.mark.parametrize(
    "payload",
    [
        _event(101, 101, 100, symbol="ETHUSDT"),
        _event(102, 101, 100),
        _event(101, 101, 100, bids=[["NaN", "1"]]),
        _event(101, 101, 100, bids=[["100", "-1"]]),
        {**_event(101, 101, 100), "pu": None},
        {**_event(101, 101, 100), "E": None},
    ],
)
def test_depth_book_rejects_malformed_or_cross_symbol_events(payload: dict) -> None:
    book = DepthOrderBook("BTCUSDT")
    with pytest.raises(ValueError):
        book.feed(payload)


@pytest.mark.parametrize("invalid_id", [1.5, True, "1.5", object()])
def test_depth_book_rejects_non_integral_sequence_ids(invalid_id) -> None:
    book = DepthOrderBook("BTCUSDT")
    with pytest.raises(ValueError):
        book.feed({**_event(101, 101, 100), "U": invalid_id})


def test_crossed_depth_update_forces_resnapshot() -> None:
    book = DepthOrderBook("BTCUSDT")
    book.load_snapshot(_snapshot())

    assert book.feed(_event(101, 101, 100, bids=[["102", "2"]])) is None
    assert book.needs_snapshot is True


def test_fetch_depth_snapshot_uses_default_500_and_bounded_endpoint(monkeypatch) -> None:
    calls = []

    def fake_get(url, **kwargs):
        calls.append((url, kwargs))
        return {"lastUpdateId": 1, "bids": [], "asks": []}

    monkeypatch.setattr(market_data_client, "_get", fake_get)

    payload = market_data_client.fetch_depth_snapshot("btcusdt")

    assert payload["lastUpdateId"] == 1
    parsed = urlsplit(calls[0][0])
    assert parsed.scheme == "https"
    assert parsed.netloc == "fapi.binance.com"
    assert parsed.path == "/fapi/v1/depth"
    assert parse_qs(parsed.query) == {"symbol": ["BTCUSDT"], "limit": ["500"]}
    assert calls[0][1] == {"timeout": 10, "retries": 1}


@pytest.mark.parametrize("limit", [0, 99, 101, 2_000, True])
def test_fetch_depth_snapshot_rejects_unsupported_limit_without_network(limit, monkeypatch) -> None:
    monkeypatch.setattr(
        market_data_client,
        "_get",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("network must not run")),
    )
    with pytest.raises(ValueError):
        market_data_client.fetch_depth_snapshot("BTCUSDT", limit)


def test_fetch_depth_snapshot_rejects_non_string_symbol_without_network(monkeypatch) -> None:
    monkeypatch.setattr(
        market_data_client,
        "_get",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("network must not run")),
    )
    with pytest.raises(ValueError):
        market_data_client.fetch_depth_snapshot(None)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("limit", "weight"),
    [(5, 2), (50, 2), (100, 5), (500, 10), (1_000, 20)],
)
def test_depth_rest_weight_matches_binance_tiers(limit: int, weight: int) -> None:
    url = f"https://fapi.binance.com/fapi/v1/depth?symbol=BTCUSDT&limit={limit}"
    assert rest_request_weight("GET", url) == weight


def test_depth_symbols_are_deduplicated_and_split_into_50_symbol_groups() -> None:
    symbols = [f"S{index}USDT" for index in range(120)] + ["S0USDT"]
    groups = depth_symbol_groups(symbols)

    assert [len(group) for group in groups] == [50, 50, 20]
    assert groups[0][0] == "S0USDT"
    assert groups[-1][-1] == "S119USDT"
    assert _depth_uri(groups[0]).startswith("wss://fstream.binance.com/public/stream?streams=")
    assert "@depth@500ms" in _depth_uri(groups[0])


def test_one_collector_rejects_more_than_one_group() -> None:
    with pytest.raises(ValueError, match="at most 50"):
        DepthStreamCollector(
            [f"S{index}USDT" for index in range(51)],
            lambda _metrics: None,
        )


def test_collector_resyncs_only_invalid_symbol_without_raising() -> None:
    emitted = []
    collector = DepthStreamCollector(["BTCUSDT", "ETHUSDT"], emitted.append)
    generation = collector._activate_session()
    assert collector.books["ETHUSDT"].load_snapshot(
        _snapshot(bids=[["200", "1"]], asks=[["201", "1"]])
    )

    invalid_btc = _event(101, 101, 100, bids=[["NaN", "1"]])
    collector._process_message(
        json.dumps({"stream": "btcusdt@depth@500ms", "data": invalid_btc}),
        generation,
    )

    assert collector.books["BTCUSDT"].needs_snapshot is True
    assert collector.books["ETHUSDT"].synced is True
    assert collector._tasks.get_nowait() == (generation, "BTCUSDT")


def test_new_depth_session_drops_stale_snapshot_queue() -> None:
    collector = DepthStreamCollector(["BTCUSDT"], lambda _metrics: None)
    stale_generation = collector._activate_session()
    collector._request_snapshot(stale_generation, "BTCUSDT")

    current_generation = collector._activate_session()
    collector._request_snapshot(current_generation, "BTCUSDT")

    assert collector._tasks.get_nowait() == (current_generation, "BTCUSDT")
    assert collector._tasks.empty()


def test_heartbeat_requeues_unsynced_book_after_transient_full_queue() -> None:
    collector = DepthStreamCollector(["BTCUSDT"], lambda _metrics: None)
    generation = collector._activate_session()
    for index in range(collector._tasks.maxsize):
        collector._tasks.put_nowait((generation - 1, f"STALE{index}"))

    collector._request_snapshot(generation, "BTCUSDT")
    assert (generation, "BTCUSDT") not in collector._queued
    collector._tasks.get_nowait()

    collector._emit_heartbeats(generation)

    assert (generation, "BTCUSDT") in collector._queued


def test_collector_accepts_combined_stream_and_heartbeat_emits_storage_rows(
    shared_depth_snapshot_dir,
) -> None:
    emitted = []
    collector = DepthStreamCollector(["BTCUSDT"], emitted.append)
    generation = collector._activate_session()
    collector.books["BTCUSDT"].load_snapshot(_snapshot())

    collector._process_message(
        json.dumps(
            {
                "stream": "btcusdt@depth@500ms",
                "data": _event(101, 101, 100, bids=[["100", "2"]]),
            }
        ),
        generation,
    )
    collector._emit_heartbeats(generation)

    assert len(emitted) == 1
    assert emitted[0].bid_depth_notional == pytest.approx(398)
    assert set(emitted[0].as_dict()) == {
        "symbol",
        "bid_depth_notional",
        "ask_depth_notional",
        "bid_depth_notional_5",
        "ask_depth_notional_5",
        "book_imbalance",
        "book_imbalance_5",
        "depth_levels",
        "bid_level_count",
        "ask_level_count",
        "spread_bps",
        "bid_depth_change_5s_pct",
        "ask_depth_change_5s_pct",
        "bid_depth_change_30s_pct",
        "ask_depth_change_30s_pct",
        "imbalance_change_5s",
        "ts",
    }
    assert emitted[0].ts >= int(time.time()) - 1


def test_collector_swallowing_consumer_error_keeps_book_synchronized() -> None:
    collector = DepthStreamCollector(
        ["BTCUSDT"],
        lambda _metrics: (_ for _ in ()).throw(RuntimeError("database busy")),
    )
    generation = collector._activate_session()
    collector.books["BTCUSDT"].load_snapshot(_snapshot())

    collector._process_message(
        json.dumps(
            {
                "stream": "btcusdt@depth@500ms",
                "data": _event(101, 101, 100),
            }
        ),
        generation,
    )

    assert collector.books["BTCUSDT"].synced is True


def test_paused_collector_opens_neither_websocket_nor_rest() -> None:
    import threading

    stop = threading.Event()
    calls = {"connect": 0, "snapshot": 0}

    def paused() -> bool:
        stop.set()
        return True

    def connect_forbidden(_symbols):
        calls["connect"] += 1
        raise AssertionError("WebSocket must not connect while paused")

    def snapshot_forbidden(_symbol, _limit):
        calls["snapshot"] += 1
        raise AssertionError("REST must not run while paused")

    collector = DepthStreamCollector(
        ["BTCUSDT"],
        lambda _metrics: None,
        snapshot_fetcher=snapshot_forbidden,
        connect_factory=connect_forbidden,
    )

    collector.run_forever(stop, paused)

    assert calls == {"connect": 0, "snapshot": 0}
