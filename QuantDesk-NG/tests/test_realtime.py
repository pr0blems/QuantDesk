from __future__ import annotations

import time

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
