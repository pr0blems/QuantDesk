from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd

from quantdesk.two_hour_research import (
    ArchiveTask,
    archive_tasks,
    choose_event_threshold,
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
