from __future__ import annotations

import io
import zipfile
from datetime import date

import numpy as np
import pandas as pd

from quantdesk.two_hour_research import (
    ArchiveTask,
    MetricsArchiveTask,
    _multihead_event_metrics,
    _parse_metrics_archive,
    archive_tasks,
    choose_event_threshold,
    choose_multihead_selection,
    feature_frame_for_symbol,
)


def _bars(prices: list[float]) -> pd.DataFrame:
    rows = []
    start = 1_800_000_000_000
    for index, price in enumerate(prices):
        rows.append(
            {
                "symbol": "TESTUSDT",
                "market_type": "EQUITY",
                "open_time": start + index * 300_000,
                "open": price,
                "high": price * 1.002,
                "low": price * 0.998,
                "close": price,
                "volume": 100 + index,
                "quote_volume": (100 + index) * price,
                "trades": 50 + index,
                "taker_buy_volume": (100 + index) * 0.55,
                "taker_buy_quote_volume": (100 + index) * price * 0.55,
            }
        )
    return pd.DataFrame(rows)


def test_archive_tasks_include_previous_month_and_current_days() -> None:
    tasks = archive_tasks(
        [{"symbol": "AAPLUSDT", "market_type": "EQUITY", "onboard_ms": 1782864000000}],
        interval="5m",
        as_of=date(2026, 8, 5),
        lookback_days=60,
    )
    assert ArchiveTask("AAPLUSDT", "EQUITY", "5m", "monthly", "2026-07") in tasks
    assert ArchiveTask("AAPLUSDT", "EQUITY", "5m", "daily", "2026-08-04") in tasks


def test_feature_labels_use_exact_two_hour_horizon() -> None:
    prices = [100 + index * 0.03 for index in range(800)]
    frame = feature_frame_for_symbol(_bars(prices), sample_minutes=5)
    usable = frame.dropna(subset=["terminal_label", "barrier_bps"])
    assert not usable.empty
    assert set(usable["terminal_label"].unique()) == {1.0}
    assert usable["event_label"].isin([-1.0, 0.0, 1.0]).all()


def test_event_threshold_counts_no_event_predictions_as_errors() -> None:
    classes = np.array([-1, 0, 1])
    probabilities = np.array(
        [
            [0.9, 0.05, 0.05],
            [0.05, 0.05, 0.9],
            [0.05, 0.05, 0.9],
            [0.9, 0.05, 0.05],
        ]
    )
    actual = np.array([-1, 1, 0, 0])
    choice = choose_event_threshold(
        probabilities,
        classes,
        actual,
        minimum_samples=1,
        target_precision=0.8,
    )
    assert choice.samples == 4
    assert choice.precision == 0.5
    assert not choice.target_met


def test_metrics_archive_keeps_open_interest_when_ratios_are_blank() -> None:
    csv_text = """create_time,symbol,sum_open_interest,sum_open_interest_value,count_toptrader_long_short_ratio,sum_toptrader_long_short_ratio,count_long_short_ratio,sum_taker_long_short_vol_ratio
2026-08-03 00:00:00,AAPLUSDT,100,25000,,,,
"""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("AAPLUSDT-metrics-2026-08-03.csv", csv_text)
    rows = _parse_metrics_archive(
        buffer.getvalue(), MetricsArchiveTask("AAPLUSDT", "2026-08-03")
    )
    assert rows == [
        ("AAPLUSDT", 1785715200000, 100.0, 25000.0, 1.0, 1.0, 1.0, 1.0)
    ]


def test_multihead_selector_keeps_a_precise_directional_signal_set() -> None:
    actual = np.tile(np.array([-1, 1], dtype=np.int8), 100)
    direction_up = np.where(actual == 1, 0.90, 0.10)
    occurrence = np.full(len(actual), 0.90)
    selection = choose_multihead_selection(
        event_direction_up=direction_up,
        event_occurrence=occurrence,
        direction_up=direction_up,
        occurrence=occurrence,
        terminal_up=direction_up,
        actual=actual,
        minimum_samples=100,
        target_precision=0.755,
    )
    metrics = _multihead_event_metrics(
        event_direction_up=direction_up,
        event_occurrence=occurrence,
        direction_up=direction_up,
        occurrence=occurrence,
        terminal_up=direction_up,
        actual=actual,
        selection=selection,
    )
    assert selection.samples == 200
    assert selection.precision == 1.0
    assert metrics["samples"] == 200
    assert metrics["precision"] == 1.0
