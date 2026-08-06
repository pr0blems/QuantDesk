from __future__ import annotations

import time

import pytest

from quantdesk import realtime
from quantdesk.realtime import _RealtimeState


def test_realtime_snapshot_persists_short_window_extrema_with_event_times() -> None:
    state = _RealtimeState()
    now_ms = int(time.time() * 1000)
    for offset, price in enumerate((100.0, 96.5, 103.25, 101.0)):
        state.ingest(
            {
                "e": "aggTrade",
                "s": "TESTUSDT",
                "E": now_ms + offset,
                "p": str(price),
                "q": "2",
                "m": False,
            }
        )

    rows = state.snapshots({"TESTUSDT"})

    assert len(rows) == 1
    row = rows[0]
    assert row[13] == 96.5
    assert row[14] == now_ms + 1
    assert row[15] == 103.25
    assert row[16] == now_ms + 2


def test_realtime_snapshot_uses_executable_book_sides_without_trades() -> None:
    state = _RealtimeState()
    now_ms = int(time.time() * 1000)
    for offset, bid, ask in ((0, 99.9, 100.1), (1, 96.5, 96.7), (2, 103.1, 103.3)):
        state.ingest(
            {
                "e": "bookTicker",
                "s": "TESTUSDT",
                "E": now_ms + offset,
                "b": str(bid),
                "B": "5",
                "a": str(ask),
                "A": "6",
            }
        )

    row = state.snapshots({"TESTUSDT"})[0]

    assert row[13] == 96.5
    assert row[14] == now_ms + 1
    assert row[15] == 103.3
    assert row[16] == now_ms + 2


def test_realtime_snapshot_aggregates_partial_depth_liquidity_pool() -> None:
    state = _RealtimeState()
    now_ms = int(time.time() * 1_000)
    state.ingest(
        {
            "e": "depthUpdate",
            "s": "TESTUSDT",
            "E": now_ms,
            "b": [["100", "2"], ["99", "3"], ["98", "5"]],
            "a": [["101", "1"], ["102", "4"], ["103", "5"]],
        }
    )

    row = state.snapshots({"TESTUSDT"})[0]

    assert row[17] == 2.0
    assert row[18] == 1.0
    assert row[19] == 10.0
    assert row[20] == 10.0
    assert row[21] == 987.0
    assert row[22] == 1024.0
    assert row[7] == pytest.approx((987.0 - 1024.0) / (987.0 + 1024.0))
    assert row[23] == pytest.approx(row[7])
    assert row[24] == 3


def test_missing_partial_depth_is_recovered_from_bounded_rest_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _RealtimeState()
    realtime._LAST_DEPTH_FALLBACK_AT.clear()
    monkeypatch.setattr(realtime, "_STATE", state)

    def depth_snapshot(symbol: str, limit: int) -> dict[str, object]:
        assert symbol == "TESTUSDT"
        assert limit == 500
        return {
            "lastUpdateId": 100,
            "bids": [[str(100 - index / 10), "1"] for index in range(100)],
            "asks": [[str(101 + index / 10), "1"] for index in range(100)],
        }

    monkeypatch.setattr(realtime.binance_client, "fetch_order_book", depth_snapshot)

    assert realtime._restore_missing_depth({"TESTUSDT"}) == 1
    row = state.snapshots({"TESTUSDT"})[0]
    quality = row[25]

    assert row[24] == 100
    assert "binance_rest_depth_snapshot" in quality


def test_diff_depth_reconciles_a_hundred_level_snapshot() -> None:
    state = _RealtimeState()
    now_ms = int(time.time() * 1_000)
    state.ingest(
        {
            "e": "depthUpdate",
            "s": "TESTUSDT",
            "E": now_ms + 1,
            "U": 101,
            "u": 101,
            "pu": 100,
            "b": [["100", "7"]],
            "a": [["101", "0"], ["111", "1"]],
        }
    )
    assert state.install_depth_snapshot(
        "TESTUSDT",
        {
            "lastUpdateId": 100,
            "bids": [[str(100 - index / 10), "1"] for index in range(100)],
            "asks": [[str(101 + index / 10), "1"] for index in range(100)],
        },
        now_ms,
    )
    state.ingest(
        {
            "e": "depthUpdate",
            "s": "TESTUSDT",
            "E": now_ms + 2,
            "U": 150,
            "u": 160,
            "pu": 101,
            "b": [["100", "8"]],
            "a": [],
        }
    )

    row = state.snapshots({"TESTUSDT"})[0]

    assert row[17] == 8.0
    assert row[18] == 1.0
    assert row[24] == 100
    assert "binance_ws_diff_depth" in row[25]


def test_exchange_shallow_book_is_not_repeatedly_recovered_as_a_stream_failure() -> None:
    state = _RealtimeState()
    now_ms = int(time.time() * 1_000)

    assert state.install_depth_snapshot(
        "TESTUSDT",
        {
            "lastUpdateId": 10,
            "bids": [[str(100 - index / 10), "1"] for index in range(79)],
            "asks": [[str(101 + index / 10), "1"] for index in range(89)],
        },
        now_ms,
    )

    assert state.stale_depth_symbols({"TESTUSDT"}, now_ms + 1) == []


def test_price_move_rows_count_one_direction_per_flush() -> None:
    realtime._LAST_FLUSH_PRICES.clear()
    row = ("TESTUSDT", None, None, None, None, 100.0)

    assert realtime._price_move_rows([row], 1_800) == []
    assert realtime._price_move_rows([(*row[:5], 101.0)], 1_805) == [
        ("TESTUSDT", 1_800, 1, 0)
    ]
    assert realtime._price_move_rows([(*row[:5], 99.0)], 1_810) == [
        ("TESTUSDT", 1_800, 0, 1)
    ]
    assert realtime._price_move_rows([(*row[:5], 99.0)], 1_815) == []
