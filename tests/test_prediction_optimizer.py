from __future__ import annotations

import json

import pytest

from quantdesk_v2 import battle
from quantdesk_v2.prediction_optimizer import (
    PredictionOptimizationUnavailable,
    _chronological_purged_split,
    optimize_prediction_algorithm,
)


def _history_row(index: int, horizon_seconds: int, config_version: int) -> dict[str, object]:
    direction = 1.0 if index % 2 == 0 else -1.0
    kline_values = {
        name: 1.0 if direction > 0 else 0.0 for name in battle.KLINE_STRATEGY_FEATURES
    }
    features = {
        "data_quality": 1.0,
        "micro_age_ms": 1_000,
        "positioning_age_ms": 1_000,
        "aggressive_flow": direction,
        "book_imbalance": -direction,
        "book_imbalance_5": -direction,
        "velocity": direction,
        "flash_imbalance": direction,
        "taker_flow": direction,
        "price_oi_impulse": direction,
        "trend_15m": direction,
        "trend_1h": direction,
        "trend_4h": direction,
        "account_crowding": 0.0,
        "funding_crowding": 0.0,
        "realized_volatility_bps": 10.0,
        "kline_strategies": {
            "15m": {"values": kline_values},
            "1h": {"values": kline_values},
        },
    }
    return {
        "horizon_seconds": horizon_seconds,
        "predicted_at_ms": 1_000_000 + index * 4_000_000,
        "algorithm_config_json": json.dumps({"config_version": config_version}),
        "features_json": features,
        "raw_return_bps": 25.0 * direction,
        "cost_bps": 1.0,
    }


def test_optimizer_uses_only_current_version_and_preserves_weight_contract() -> None:
    rows = [
        _history_row(index, horizon, 3)
        for horizon in battle.HORIZONS
        for index in range(24)
    ]
    rows.extend(_history_row(index, 300, 2) for index in range(12))
    config = battle.default_algorithm_config()

    result = optimize_prediction_algorithm(rows, config, current_config_version=3)

    assert result["algorithm_config_version"] == 3
    assert result["sample_count"] == 72
    assert result["guardrails"] == {
        "current_version_only": True,
        "chronological_holdout": True,
        "label_horizon_purge": True,
        "timestamp_groups_kept_together": True,
        "disabled_features_preserved": True,
        "scalars_unchanged": True,
        "automatic_save": False,
    }
    assert result["optimized_horizon_count"] >= 1
    for horizon, weights in result["recommended_config"]["weights"].items():
        assert set(weights) == set(battle.ALGORITHM_FEATURES), horizon
        assert sum(weights.values()) == pytest.approx(1.0, abs=1e-6)
    for report in result["horizons"]:
        assert report["training_count"] > report["validation_count"] >= 2
        assert report["optimized"]["utility_bps"] >= report["baseline"]["utility_bps"]


def test_optimizer_keeps_disabled_feature_state_and_mass() -> None:
    rows = [_history_row(index, 300, 4) for index in range(24)]
    config = battle.default_algorithm_config()
    config["enabled_features"]["book_imbalance"] = False
    disabled_weight = config["weights"]["5m"]["book_imbalance"]

    result = optimize_prediction_algorithm(rows, config, current_config_version=4)

    recommended = result["recommended_config"]
    assert recommended["enabled_features"]["book_imbalance"] is False
    assert recommended["weights"]["5m"]["book_imbalance"] == disabled_weight
    assert sum(recommended["weights"]["5m"].values()) == pytest.approx(1.0, abs=1e-6)


def test_optimizer_rejects_history_without_current_version_snapshots() -> None:
    rows = [_history_row(index, 300, 1) for index in range(20)]

    with pytest.raises(PredictionOptimizationUnavailable, match="完整快照不足"):
        optimize_prediction_algorithm(
            rows,
            battle.default_algorithm_config(),
            current_config_version=2,
        )


def test_chronological_split_purges_unsettled_labels_and_keeps_time_groups_whole() -> None:
    samples = [
        {
            "predicted_at_ms": index * 600_000,
            "horizon_seconds": 300,
        }
        for index in range(16)
    ]
    samples[11]["predicted_at_ms"] = samples[12]["predicted_at_ms"]

    training, validation = _chronological_purged_split(samples)

    validation_start = min(item["predicted_at_ms"] for item in validation)
    assert all(
        item["predicted_at_ms"] + item["horizon_seconds"] * 1_000 < validation_start
        for item in training
    )
    assert {item["predicted_at_ms"] for item in training}.isdisjoint(
        {item["predicted_at_ms"] for item in validation}
    )
