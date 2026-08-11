from __future__ import annotations

from quantdesk_v2.strategy_indicators import (
    STRATEGY_INDICATOR_COUNT,
    evaluate_directional_strategy_indicators,
    evaluate_strategy_indicators,
)

EXPECTED_NAMES = [
    "布林突破",
    "均线回踩反弹",
    "趋势突破",
    "量价齐升",
    "新低反转",
    "缩量回踩",
    "强势高开",
    "均线多头",
    "MA金叉",
    "MACD金叉放量",
    "超跌反弹",
    "超跌反转",
]


def _candle(index: int, close: float, *, volume: float = 100.0) -> dict[str, float | int]:
    return {
        "open_time": index * 3_600_000,
        "open": close,
        "high": close + 1,
        "low": close - 1,
        "close": close,
        "volume": volume,
    }


def test_strategy_indicator_scan_always_returns_the_twelve_named_items() -> None:
    result = evaluate_strategy_indicators([], "1h")

    assert result["count"] == STRATEGY_INDICATOR_COUNT
    assert result["triggered_count"] == 0
    assert [item["name"] for item in result["items"]] == EXPECTED_NAMES
    assert {item["status"] for item in result["items"]} == {"insufficient"}


def test_strategy_indicator_scan_uses_price_volume_and_cross_conditions() -> None:
    candles = [_candle(index, 100.0) for index in range(79)]
    candles.append(
        {
            "open_time": 79 * 3_600_000,
            "open": 120.0,
            "high": 132.0,
            "low": 119.0,
            "close": 130.0,
            "volume": 200.0,
        }
    )

    result = evaluate_strategy_indicators(candles, "1h")
    by_name = {item["name"]: item for item in result["items"]}

    assert result["count"] == STRATEGY_INDICATOR_COUNT
    assert by_name["布林突破"]["triggered"] is True
    assert by_name["趋势突破"]["triggered"] is True
    assert by_name["量价齐升"]["triggered"] is True
    assert by_name["强势高开"]["triggered"] is True
    assert by_name["MA金叉"]["triggered"] is True
    assert by_name["MACD金叉放量"]["triggered"] is True
    assert all(item["metrics"] for item in result["items"])
    triggered_strengths = [
        item["strength"] for item in result["items"] if item["triggered"] is True
    ]
    assert all(70 <= strength <= 100 for strength in triggered_strengths)
    assert all(isinstance(strength, float) for strength in triggered_strengths)


def test_directional_strategy_scan_exposes_inverse_bearish_conditions() -> None:
    candles = [_candle(index, 100.0) for index in range(79)]
    candles.append(
        {
            "open_time": 79 * 3_600_000,
            "open": 80.0,
            "high": 81.0,
            "low": 69.0,
            "close": 70.0,
            "volume": 200.0,
        }
    )

    result = evaluate_directional_strategy_indicators(candles, "1h")
    by_key = {item["key"]: item for item in result["items"]}

    assert result["bearish_triggered_count"] > 0
    assert by_key["strong_gap_open"]["triggered"] is False
    assert by_key["strong_gap_open"]["bearish_triggered"] is True
    assert by_key["strong_gap_open"]["bearish_name"] == "强势低开"
    assert by_key["strong_gap_open"]["direction"] == "bearish"
    assert 70 <= by_key["strong_gap_open"]["bearish_strength"] <= 100
    assert by_key["strong_gap_open"]["bullish_strength"] < 70
