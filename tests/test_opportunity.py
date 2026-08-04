from __future__ import annotations

import pytest

from quantdesk_v2.opportunity import (
    OpportunityDataError,
    classify_opportunity,
    compute_feature_snapshot,
)


def _bars(
    count: int,
    *,
    start: float,
    step: float,
    interval: int,
    volume: float = 100.0,
) -> list[dict]:
    rows = []
    price = start
    for index in range(count):
        close = price + step
        rows.append(
            {
                "open_time": 1_700_000_000 + index * interval,
                "open": price,
                "high": max(price, close) + 0.4,
                "low": min(price, close) - 0.4,
                "close": close,
                "volume": volume,
            }
        )
        price = close
    return rows


def _long_features(*, milliseconds: bool = False) -> dict[str, dict]:
    rows_by_timeframe = {
        "4h": _bars(90, start=100, step=0.8, interval=14_400),
        "1h": _bars(90, start=150, step=0.35, interval=3_600),
        "15m": _bars(90, start=175, step=0.08, interval=900),
    }
    trigger = rows_by_timeframe["15m"]
    trigger[-1]["close"] = max(item["high"] for item in trigger[-21:-1]) + 1
    trigger[-1]["high"] = trigger[-1]["close"] + 0.4
    trigger[-1]["volume"] = 250
    if milliseconds:
        for rows in rows_by_timeframe.values():
            for row in rows:
                row["open_time"] *= 1_000
    return {
        timeframe: compute_feature_snapshot(rows, timeframe)
        for timeframe, rows in rows_by_timeframe.items()
    }


def test_feature_snapshot_uses_closed_bars_and_reports_data_quality() -> None:
    rows = _bars(90, start=100, step=0.5, interval=3_600)

    feature = compute_feature_snapshot(rows, "1h")

    assert feature["bar_open_time"] == rows[-1]["open_time"]
    assert feature["fast_ema"] > feature["slow_ema"]
    assert feature["quality"]["closed_bar_only"] is True
    assert feature["quality"]["recent_missing_bars"] == 0
    assert feature["quality"]["coverage_pct"] == 100


def test_opportunity_classifier_emits_explainable_confirmed_long() -> None:
    opportunity = classify_opportunity(_long_features())

    assert opportunity["direction"] == "long"
    assert opportunity["status"] == "confirmed"
    assert opportunity["quality_score"] > 50
    assert "STRUCTURE_BREAKOUT" in opportunity["evidence"]["reason_codes"]
    assert opportunity["evidence"]["conditions"]["volume_confirmed"] is True


def test_opportunity_expiry_preserves_millisecond_units() -> None:
    opportunity = classify_opportunity(_long_features(milliseconds=True))

    assert opportunity["expires_bar_time"] - opportunity["detected_bar_time"] == 1_800_000


def test_feature_snapshot_rejects_insufficient_history() -> None:
    with pytest.raises(OpportunityDataError, match="55"):
        compute_feature_snapshot(
            _bars(30, start=100, step=0.5, interval=900),
            "15m",
        )
