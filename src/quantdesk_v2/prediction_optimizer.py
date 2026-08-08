"""Leakage-aware constrained weight optimization for battle predictions."""

from __future__ import annotations

import json
import math
from collections.abc import Mapping, Sequence
from copy import deepcopy
from typing import Any

from . import battle

OPTIMIZER_KEY = "constrained-history-v1"
MIN_HORIZON_SAMPLES = 10
MIN_TRAINING_SAMPLES = 8
MAX_ABS_RETURN_BPS = 500.0


class PredictionOptimizationUnavailable(ValueError):
    """Raised when the current algorithm version has no usable settled history."""


def _number(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result if math.isfinite(result) else default


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _config_version(value: Any) -> int | None:
    config = _json_object(value)
    try:
        return int(config.get("config_version"))
    except (TypeError, ValueError):
        return None


def _sample(row: Mapping[str, Any]) -> dict[str, Any] | None:
    horizon = int(_number(row.get("horizon_seconds"), -1))
    if horizon not in battle.HORIZONS:
        return None
    features = _json_object(row.get("features_json"))
    raw_return = _number(row.get("raw_return_bps"), math.nan)
    if not features or not math.isfinite(raw_return):
        return None

    def optional_number(name: str) -> float | None:
        value = _number(row.get(name), math.nan)
        return value if math.isfinite(value) else None

    return {
        "horizon_seconds": horizon,
        "predicted_at_ms": int(_number(row.get("predicted_at_ms"))),
        "completed_at_ms": int(_number(row.get("completed_at_ms"))),
        "symbol": str(row.get("symbol") or "").upper(),
        "prediction_result": str(row.get("prediction_result") or "neutral"),
        "confidence_label": str(row.get("confidence_label") or "unknown"),
        "hit_result": str(row.get("hit_result") or "unknown"),
        "battle_score": optional_number("battle_score"),
        "long_probability": optional_number("long_probability"),
        "short_probability": optional_number("short_probability"),
        "neutral_probability": optional_number("neutral_probability"),
        "confidence_score": optional_number("confidence_score"),
        "gross_edge_bps": optional_number("gross_edge_bps"),
        "spread_bps": optional_number("spread_bps"),
        "target_bps": optional_number("target_bps"),
        "stop_bps": optional_number("stop_bps"),
        "directional_return_bps": optional_number("directional_return_bps"),
        "max_favorable_bps": optional_number("max_favorable_bps"),
        "max_adverse_bps": optional_number("max_adverse_bps"),
        "features": features,
        "values": battle.algorithm_feature_values(features, horizon),
        "raw_return_bps": max(
            -MAX_ABS_RETURN_BPS,
            min(MAX_ABS_RETURN_BPS, raw_return),
        ),
        "cost_bps": max(0.0, _number(row.get("cost_bps"))),
    }


def _chronological_purged_split(
    samples: Sequence[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Reserve a time-grouped holdout and purge labels unavailable at its start.

    A prediction's forward label is not safe training data until its complete
    horizon has elapsed.  Grouping equal prediction timestamps also prevents a
    single market snapshot from appearing on both sides of the split.
    """

    ordered = sorted(samples, key=lambda item: int(item["predicted_at_ms"]))
    if len(ordered) < MIN_HORIZON_SAMPLES:
        return ordered, []
    validation_count = max(2, int(round(len(ordered) * 0.25)))
    split = max(MIN_TRAINING_SAMPLES, len(ordered) - validation_count)
    if split >= len(ordered):
        return ordered, []
    validation_start_ms = int(ordered[split]["predicted_at_ms"])
    while split > 0 and int(ordered[split - 1]["predicted_at_ms"]) == validation_start_ms:
        split -= 1
    validation = ordered[split:]
    training = [
        sample
        for sample in ordered[:split]
        if int(sample["predicted_at_ms"])
        + int(sample["horizon_seconds"]) * 1_000
        < validation_start_ms
    ]
    return training, validation


def _correlation(samples: Sequence[dict[str, Any]], feature: str) -> tuple[float, int]:
    pairs = [
        (
            _number(sample["values"].get(feature)),
            math.tanh(_number(sample["raw_return_bps"]) / 35.0),
        )
        for sample in samples
    ]
    support = sum(abs(value) > 1e-9 for value, _ in pairs)
    if len(pairs) < 3:
        return 0.0, support
    mean_x = sum(value for value, _ in pairs) / len(pairs)
    mean_y = sum(target for _, target in pairs) / len(pairs)
    numerator = sum((value - mean_x) * (target - mean_y) for value, target in pairs)
    x_square = sum((value - mean_x) ** 2 for value, _ in pairs)
    y_square = sum((target - mean_y) ** 2 for _, target in pairs)
    denominator = math.sqrt(x_square * y_square)
    return (numerator / denominator if denominator > 1e-12 else 0.0), support


def _metrics(
    samples: Sequence[dict[str, Any]],
    config: dict[str, Any],
) -> dict[str, Any]:
    directional = 0
    hits = 0
    net_total = 0.0
    for sample in samples:
        prediction = battle.predict(
            sample["features"],
            sample["horizon_seconds"],
            config,
        )
        direction = prediction["result"]
        if direction not in {"long", "short"}:
            continue
        directional += 1
        sign = 1.0 if direction == "long" else -1.0
        net_return = sign * sample["raw_return_bps"] - sample["cost_bps"]
        net_total += net_return
        hits += net_return > 0
    total = len(samples)
    return {
        "sample_count": total,
        "directional_count": directional,
        "coverage": round(directional / total, 6) if total else 0.0,
        "hit_rate": round(hits / directional, 6) if directional else None,
        "avg_net_bps": round(net_total / directional, 4) if directional else None,
        "utility_bps": round(net_total / total, 4) if total else 0.0,
    }


def _candidate_weights(
    samples: Sequence[dict[str, Any]],
    current_weights: Mapping[str, Any],
    enabled_features: Mapping[str, Any],
    blend: float,
) -> tuple[dict[str, float], dict[str, dict[str, Any]]]:
    enabled = [name for name in battle.ALGORITHM_FEATURES if enabled_features.get(name, True)]
    disabled = [name for name in battle.ALGORITHM_FEATURES if name not in enabled]
    disabled_mass = sum(max(0.0, _number(current_weights.get(name))) for name in disabled)
    enabled_mass = max(0.0, 1.0 - disabled_mass)
    current_enabled_total = sum(
        max(0.0, _number(current_weights.get(name))) for name in enabled
    )
    if not enabled or current_enabled_total <= 0 or enabled_mass <= 0:
        return (
            {name: _number(current_weights.get(name)) for name in battle.ALGORITHM_FEATURES},
            {},
        )

    diagnostics: dict[str, dict[str, Any]] = {}
    adjusted: dict[str, float] = {}
    sample_reliability = min(1.0, math.sqrt(len(samples) / 80.0))
    for name in enabled:
        correlation, support = _correlation(samples, name)
        support_reliability = 1.0
        if name in battle.KLINE_STRATEGY_FEATURES:
            support_reliability = min(1.0, math.sqrt(support / 12.0))
        reliable_correlation = max(
            -0.60,
            min(0.60, correlation * sample_reliability * support_reliability),
        )
        multiplier = max(0.35, min(2.85, math.exp(2.4 * reliable_correlation)))
        adjusted[name] = max(1e-9, _number(current_weights.get(name)) * multiplier)
        diagnostics[name] = {
            "correlation": round(correlation, 4),
            "reliability": round(sample_reliability * support_reliability, 4),
            "support": support,
        }
    adjusted_total = sum(adjusted.values())
    result = {
        name: max(0.0, _number(current_weights.get(name))) for name in battle.ALGORITHM_FEATURES
    }
    for name in enabled:
        current = max(0.0, _number(current_weights.get(name)))
        current_share = current / current_enabled_total
        learned_share = adjusted[name] / adjusted_total
        result[name] = enabled_mass * (
            current_share * (1.0 - blend) + learned_share * blend
        )
    result = _rounded_weights(result, enabled)
    return result, diagnostics


def _rounded_weights(weights: dict[str, float], enabled: Sequence[str]) -> dict[str, float]:
    result = {name: round(max(0.0, weights[name]), 6) for name in battle.ALGORITHM_FEATURES}
    difference = round(1.0 - sum(result.values()), 6)
    if enabled:
        anchor = max(enabled, key=lambda name: result[name])
        result[anchor] = round(max(0.0, result[anchor] + difference), 6)
    return result


def _optimize_horizon(
    samples: Sequence[dict[str, Any]],
    current_config: dict[str, Any],
    horizon_name: str,
) -> tuple[dict[str, float], dict[str, Any]]:
    current_weights = current_config["weights"][horizon_name]
    if len(samples) < MIN_HORIZON_SAMPLES:
        metrics = _metrics(samples, current_config)
        return deepcopy(current_weights), {
            "horizon": horizon_name,
            "status": "insufficient_samples",
            "sample_count": len(samples),
            "minimum_samples": MIN_HORIZON_SAMPLES,
            "training_count": 0,
            "validation_count": 0,
            "baseline": metrics,
            "optimized": metrics,
            "utility_gain_bps": 0.0,
            "changes": [],
        }

    ordered = sorted(samples, key=lambda item: item["predicted_at_ms"])
    training, validation = _chronological_purged_split(ordered)
    purged_count = len(ordered) - len(training) - len(validation)
    if len(training) < MIN_TRAINING_SAMPLES or len(validation) < 2:
        metrics = _metrics(ordered, current_config)
        return deepcopy(current_weights), {
            "horizon": horizon_name,
            "status": "insufficient_samples",
            "sample_count": len(ordered),
            "minimum_samples": MIN_HORIZON_SAMPLES,
            "training_count": len(training),
            "validation_count": len(validation),
            "purged_count": purged_count,
            "baseline": metrics,
            "optimized": metrics,
            "utility_gain_bps": 0.0,
            "changes": [],
        }
    baseline_training = _metrics(training, current_config)
    baseline_validation = _metrics(validation, current_config)
    candidates: list[tuple[float, dict[str, float], dict[str, Any], dict[str, Any]]] = []
    diagnostics: dict[str, dict[str, Any]] = {}
    for blend in (0.25, 0.50, 0.75, 1.0):
        weights, candidate_diagnostics = _candidate_weights(
            training,
            current_weights,
            current_config["enabled_features"],
            blend,
        )
        config = deepcopy(current_config)
        config["weights"][horizon_name] = weights
        training_metrics = _metrics(training, config)
        candidates.append((blend, weights, training_metrics, _metrics(validation, config)))
        diagnostics = candidate_diagnostics

    candidates.sort(key=lambda item: item[2]["utility_bps"], reverse=True)
    selected = None
    minimum_directional = max(2, int(baseline_validation["directional_count"] * 0.70))
    for candidate in candidates:
        validation_metrics = candidate[3]
        if (
            validation_metrics["directional_count"] >= minimum_directional
            and validation_metrics["utility_bps"] >= baseline_validation["utility_bps"]
        ):
            selected = candidate
            break
    if selected is None:
        selected_weights = deepcopy(current_weights)
        optimized_training = baseline_training
        optimized_validation = baseline_validation
        status = "no_validated_improvement"
        selected_blend = 0.0
    else:
        selected_blend, selected_weights, optimized_training, optimized_validation = selected
        changed = any(
            abs(_number(selected_weights[name]) - _number(current_weights[name])) >= 0.0005
            for name in battle.ALGORITHM_FEATURES
        )
        status = "optimized" if changed else "no_validated_improvement"

    changes = []
    for name in battle.ALGORITHM_FEATURES:
        before = _number(current_weights[name])
        after = _number(selected_weights[name])
        detail = diagnostics.get(name, {})
        changes.append(
            {
                "feature": name,
                "enabled": bool(current_config["enabled_features"].get(name, True)),
                "before": round(before, 6),
                "after": round(after, 6),
                "delta": round(after - before, 6),
                **detail,
            }
        )
    changes.sort(key=lambda item: abs(item["delta"]), reverse=True)
    return selected_weights, {
        "horizon": horizon_name,
        "status": status,
        "sample_count": len(ordered),
        "minimum_samples": MIN_HORIZON_SAMPLES,
        "training_count": len(training),
        "validation_count": len(validation),
        "purged_count": purged_count,
        "selected_blend": selected_blend,
        "baseline": baseline_validation,
        "optimized": optimized_validation,
        "training_baseline": baseline_training,
        "training_optimized": optimized_training,
        "utility_gain_bps": round(
            optimized_validation["utility_bps"] - baseline_validation["utility_bps"],
            4,
        ),
        "changes": changes,
    }


def optimize_prediction_algorithm(
    rows: Sequence[Mapping[str, Any]],
    current_config: dict[str, Any],
    *,
    current_config_version: int,
) -> dict[str, Any]:
    """Learn a guarded recommendation from the current config version only."""

    config = battle.normalize_algorithm_config(current_config)
    config["config_version"] = current_config_version
    matching_rows = [
        row
        for row in rows
        if _config_version(row.get("algorithm_config_json")) == current_config_version
    ]
    samples = [sample for row in matching_rows if (sample := _sample(row)) is not None]
    if len(samples) < MIN_HORIZON_SAMPLES:
        raise PredictionOptimizationUnavailable(
            "当前算法版本的已结算完整快照不足，暂时无法生成可靠的优化建议"
        )

    recommended = battle.normalize_algorithm_config(config)
    reports = []
    for horizon_seconds in battle.HORIZONS:
        horizon_name = battle.HORIZON_NAMES[horizon_seconds]
        horizon_samples = [
            sample for sample in samples if sample["horizon_seconds"] == horizon_seconds
        ]
        weights, report = _optimize_horizon(horizon_samples, config, horizon_name)
        recommended["weights"][horizon_name] = weights
        reports.append(report)

    timestamps = [sample["predicted_at_ms"] for sample in samples]
    optimized_count = sum(report["status"] == "optimized" for report in reports)
    return {
        "optimizer_key": OPTIMIZER_KEY,
        "algorithm_config_version": current_config_version,
        "sample_count": len(samples),
        "history_start_ms": min(timestamps) if timestamps else None,
        "history_end_ms": max(timestamps) if timestamps else None,
        "optimized_horizon_count": optimized_count,
        "recommendation_available": optimized_count > 0,
        "recommended_config": recommended,
        "horizons": reports,
        "guardrails": {
            "current_version_only": True,
            "chronological_holdout": True,
            "label_horizon_purge": True,
            "timestamp_groups_kept_together": True,
            "disabled_features_preserved": True,
            "scalars_unchanged": True,
            "automatic_save": False,
        },
    }
