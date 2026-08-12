from __future__ import annotations

from quantdesk_v2.prediction_feature_indicators import (
    PREDICTION_FEATURE_COUNT,
    evaluate_prediction_feature_indicators,
)

NOW_MS = 2_000_000


def _snapshot(**overrides):
    features = {
        "aggressive_flow": 0.31,
        "aggressive_flow_available": True,
        "book_imbalance": -0.22,
        "book_imbalance_5": 0.01,
        "velocity": 0.08,
        "velocity_available": True,
        "flash_imbalance": -0.12,
        "taker_flow": 0.20,
        "price_oi_impulse": -0.04,
        "trend_15m": 0.40,
        "trend_1h": -0.30,
        "trend_4h": 0.25,
        "micro_age_ms": 10_000,
        "positioning_age_ms": 20_000,
        "data_quality": 0.9,
    }
    features.update(overrides.pop("features", {}))
    return {
        "as_of_ms": NOW_MS - 30_000,
        "quality_score": 0.9,
        "features": features,
        **overrides,
    }


def test_prediction_feature_scan_returns_eight_real_engine_inputs() -> None:
    result = evaluate_prediction_feature_indicators(_snapshot(), "15m", now_ms=NOW_MS)
    by_name = {item["name"]: item for item in result["items"]}

    assert result["count"] == PREDICTION_FEATURE_COUNT
    assert list(by_name) == [
        "主动成交",
        "订单失衡",
        "近百档失衡",
        "价格速度",
        "闪动失衡",
        "Taker流向",
        "价格×持仓量",
        "周期趋势",
    ]
    assert by_name["主动成交"]["status"] == "bullish"
    assert by_name["订单失衡"]["status"] == "bearish"
    assert by_name["近百档失衡"]["status"] == "neutral"
    assert by_name["周期趋势"]["metrics"][0]["value"] == "+0.400"
    assert by_name["周期趋势"]["normalized_value"] == 0.4
    assert by_name["周期趋势"]["bullish_strength"] > 50
    assert by_name["周期趋势"]["bearish_strength"] < 50
    assert by_name["主动成交"]["quality_score"] == 0.9


def test_prediction_feature_scan_uses_selected_trend_and_source_freshness() -> None:
    snapshot = _snapshot(
        features={
            "micro_age_ms": 600_000,
            "positioning_age_ms": 20_000,
        }
    )
    result = evaluate_prediction_feature_indicators(snapshot, "1h", now_ms=NOW_MS)
    by_name = {item["name"]: item for item in result["items"]}

    assert by_name["主动成交"]["status"] == "insufficient"
    assert by_name["订单失衡"]["status"] == "insufficient"
    assert by_name["Taker流向"]["status"] == "bullish"
    assert by_name["周期趋势"]["status"] == "bearish"
    assert by_name["周期趋势"]["metrics"][0]["value"] == "-0.300"


def test_prediction_feature_scan_never_treats_missing_snapshot_as_neutral_data() -> None:
    result = evaluate_prediction_feature_indicators(None, "4h", now_ms=NOW_MS)

    assert result["count"] == PREDICTION_FEATURE_COUNT
    assert result["insufficient_count"] == PREDICTION_FEATURE_COUNT
    assert {item["status"] for item in result["items"]} == {"insufficient"}


def test_prediction_feature_scan_does_not_promote_legacy_neutral_fallbacks() -> None:
    snapshot = _snapshot(
        features={
            "aggressive_flow": 0.0,
            "aggressive_flow_available": False,
            "velocity": 0.0,
            "velocity_available": False,
        }
    )
    result = evaluate_prediction_feature_indicators(snapshot, "15m", now_ms=NOW_MS)
    by_key = {item["key"]: item for item in result["items"]}

    assert by_key["prediction_aggressive_flow"]["status"] == "insufficient"
    assert by_key["prediction_velocity"]["status"] == "insufficient"
