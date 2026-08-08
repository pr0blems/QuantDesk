"""DeepSeek-assisted, holdout-validated prediction weight optimization."""

from __future__ import annotations

import json
import math
from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
from http.client import HTTPException as HttpClientError
from typing import Any

from . import battle
from .prediction_optimizer import (
    MIN_HORIZON_SAMPLES,
    MIN_TRAINING_SAMPLES,
    PredictionOptimizationUnavailable,
    _chronological_purged_split,
    _config_version,
    _correlation,
    _metrics,
    _number,
    _sample,
)
from .strategy_ai import (
    StrategyAiError,
    _chat_http_transport,
    _chat_output_text,
    _strict_json_bytes,
    _strict_json_text,
    _validate_chat_configuration,
)

OPTIMIZER_KEY = "deepseek-history-v1"
MAX_REQUEST_BYTES = 64 * 1024
MAX_WEIGHT_DELTA = 0.15
MIN_UTILITY_GAIN_BPS = 0.01

Transport = Callable[[Any, bytes, dict[str, str], float], tuple[int, bytes]]


@dataclass(frozen=True, slots=True)
class PredictionAiDataset:
    """Prepared data with a private holdout that is never sent to the model."""

    current_config: dict[str, Any]
    current_config_version: int
    samples_by_horizon: dict[str, list[dict[str, Any]]]
    training_by_horizon: dict[str, list[dict[str, Any]]]
    validation_by_horizon: dict[str, list[dict[str, Any]]]
    model_context: dict[str, Any]
    sample_count: int
    history_start_ms: int | None
    history_end_ms: int | None


def _split_samples(
    samples: Sequence[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    return _chronological_purged_split(samples)


def _feature_statistics(
    samples: Sequence[dict[str, Any]],
    enabled_features: Mapping[str, Any],
) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    sample_reliability = min(1.0, math.sqrt(len(samples) / 80.0))
    for name in battle.ALGORITHM_FEATURES:
        correlation, support = _correlation(samples, name)
        active_pairs = [
            (
                _number(sample["values"].get(name)),
                _number(sample["raw_return_bps"]),
            )
            for sample in samples
            if abs(_number(sample["values"].get(name))) > 1e-9
        ]
        aligned = sum(value * outcome > 0 for value, outcome in active_pairs)
        signed_returns = [
            math.copysign(outcome, value) for value, outcome in active_pairs
        ]
        positive_returns = [outcome for value, outcome in active_pairs if value > 0]
        negative_returns = [outcome for value, outcome in active_pairs if value < 0]
        support_reliability = 1.0
        if name in battle.KLINE_STRATEGY_FEATURES:
            support_reliability = min(1.0, math.sqrt(support / 12.0))
        result[name] = {
            "enabled": bool(enabled_features.get(name, True)),
            "correlation": round(correlation, 6),
            "support": support,
            "reliability": round(sample_reliability * support_reliability, 6),
            "signal_alignment_rate": (
                round(aligned / len(active_pairs), 6) if active_pairs else None
            ),
            "avg_signed_return_bps": (
                round(sum(signed_returns) / len(signed_returns), 4)
                if signed_returns
                else None
            ),
            "positive_signal_support": len(positive_returns),
            "positive_signal_avg_raw_bps": (
                round(sum(positive_returns) / len(positive_returns), 4)
                if positive_returns
                else None
            ),
            "negative_signal_support": len(negative_returns),
            "negative_signal_avg_raw_bps": (
                round(sum(negative_returns) / len(negative_returns), 4)
                if negative_returns
                else None
            ),
        }
    return result


def _average(values: Sequence[float | None]) -> float | None:
    present = [float(value) for value in values if value is not None and math.isfinite(value)]
    return round(sum(present) / len(present), 4) if present else None


def _percentile(values: Sequence[float], quantile: float) -> float | None:
    ordered = sorted(value for value in values if math.isfinite(value))
    if not ordered:
        return None
    position = (len(ordered) - 1) * quantile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return round(ordered[lower], 4)
    fraction = position - lower
    return round(ordered[lower] * (1.0 - fraction) + ordered[upper] * fraction, 4)


def _persisted_directional_return(sample: Mapping[str, Any]) -> float | None:
    direction = sample.get("prediction_result")
    if direction not in {"long", "short"}:
        return None
    stored = sample.get("directional_return_bps")
    if isinstance(stored, (int, float)) and math.isfinite(float(stored)):
        return float(stored)
    sign = 1.0 if direction == "long" else -1.0
    return sign * _number(sample.get("raw_return_bps")) - _number(sample.get("cost_bps"))


def _historical_performance(samples: Sequence[dict[str, Any]]) -> dict[str, Any]:
    directional = [
        (sample, net)
        for sample in samples
        if (net := _persisted_directional_return(sample)) is not None
    ]
    returns = [net for _, net in directional]
    positives = [value for value in returns if value > 0]
    negatives = [value for value in returns if value <= 0]
    cumulative = peak = max_drawdown = 0.0
    for value in returns:
        cumulative += value
        peak = max(peak, cumulative)
        max_drawdown = min(max_drawdown, cumulative - peak)
    result_counts = {
        direction: sum(sample.get("prediction_result") == direction for sample in samples)
        for direction in ("long", "short", "neutral")
    }
    hit_counts = {
        label: sum(sample.get("hit_result") == label for sample, _ in directional)
        for label in ("target", "stop", "neither")
    }
    directional_count = len(directional)
    sample_count = len(samples)
    avg_confidence = _average([sample.get("confidence_score") for sample in samples])
    hit_rate = round(len(positives) / directional_count, 6) if directional_count else None
    return {
        "sample_count": sample_count,
        "result_counts": result_counts,
        "directional_count": directional_count,
        "coverage": round(directional_count / sample_count, 6) if sample_count else 0.0,
        "hit_count": len(positives),
        "miss_count": len(negatives),
        "hit_rate": hit_rate,
        "avg_directional_return_bps": _average(returns),
        "median_directional_return_bps": _percentile(returns, 0.5),
        "p25_directional_return_bps": _percentile(returns, 0.25),
        "p75_directional_return_bps": _percentile(returns, 0.75),
        "total_directional_return_bps": round(sum(returns), 4),
        "utility_bps_per_prediction": (
            round(sum(returns) / sample_count, 4) if sample_count else 0.0
        ),
        "profit_factor": (
            round(sum(positives) / abs(sum(negatives)), 4)
            if negatives and abs(sum(negatives)) > 1e-9
            else None
        ),
        "max_sequential_drawdown_bps": round(max_drawdown, 4),
        "avg_max_favorable_bps": _average(
            [sample.get("max_favorable_bps") for sample, _ in directional]
        ),
        "avg_max_adverse_bps": _average(
            [sample.get("max_adverse_bps") for sample, _ in directional]
        ),
        "outcome_barrier_counts": hit_counts,
        "avg_battle_score": _average([sample.get("battle_score") for sample in samples]),
        "avg_confidence_score": avg_confidence,
        "confidence_calibration_gap": (
            round(hit_rate - avg_confidence, 6)
            if hit_rate is not None and avg_confidence is not None
            else None
        ),
        "avg_gross_edge_bps": _average(
            [sample.get("gross_edge_bps") for sample in samples]
        ),
        "avg_cost_bps": _average([sample.get("cost_bps") for sample in samples]),
        "avg_target_bps": _average([sample.get("target_bps") for sample in samples]),
        "avg_stop_bps": _average([sample.get("stop_bps") for sample in samples]),
    }


def _compact_performance(samples: Sequence[dict[str, Any]]) -> dict[str, Any]:
    full = _historical_performance(samples)
    return {
        key: full[key]
        for key in (
            "sample_count",
            "directional_count",
            "coverage",
            "hit_rate",
            "avg_directional_return_bps",
            "utility_bps_per_prediction",
            "profit_factor",
            "avg_max_favorable_bps",
            "avg_max_adverse_bps",
            "avg_confidence_score",
        )
    }


def _grouped_performance(
    samples: Sequence[dict[str, Any]],
    key: Callable[[dict[str, Any]], str | None],
    *,
    minimum_count: int = 1,
) -> dict[str, dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for sample in samples:
        label = key(sample)
        if label:
            grouped[label].append(sample)
    return {
        label: _compact_performance(group)
        for label, group in sorted(grouped.items())
        if len(group) >= minimum_count
    }


def _confidence_bucket(sample: Mapping[str, Any]) -> str | None:
    value = sample.get("confidence_score")
    if not isinstance(value, (int, float)):
        return None
    if value < 0.40:
        return "below_0.40"
    if value < 0.55:
        return "0.40_to_0.55"
    if value < 0.70:
        return "0.55_to_0.70"
    return "0.70_and_above"


def _training_history_analysis(samples: Sequence[dict[str, Any]]) -> dict[str, Any]:
    ordered = sorted(samples, key=lambda sample: sample["predicted_at_ms"])
    quarter = max(1, len(ordered) // 4)
    recency_groups = {
        "oldest_25_percent": ordered[:quarter],
        "middle_50_percent": ordered[quarter:-quarter] or ordered[:0],
        "newest_25_percent": ordered[-quarter:],
    }
    by_hour = _grouped_performance(
        ordered,
        lambda sample: f"{(int(sample['predicted_at_ms']) // 3_600_000) % 24:02d}:00_UTC",
        minimum_count=max(5, len(ordered) // 100),
    )
    ranked_hours = sorted(
        by_hour.items(),
        key=lambda item: item[1]["utility_bps_per_prediction"],
        reverse=True,
    )
    by_symbol = _grouped_performance(
        ordered,
        lambda sample: str(sample.get("symbol") or "") or None,
        minimum_count=max(5, len(ordered) // 200),
    )
    largest_symbols = sorted(
        by_symbol.items(), key=lambda item: item[1]["sample_count"], reverse=True
    )[:12]
    return {
        "summary": _historical_performance(ordered),
        "by_prediction_direction": _grouped_performance(
            ordered, lambda sample: str(sample.get("prediction_result") or "neutral")
        ),
        "by_confidence_label": _grouped_performance(
            ordered, lambda sample: str(sample.get("confidence_label") or "unknown")
        ),
        "by_confidence_bucket": _grouped_performance(ordered, _confidence_bucket),
        "by_outcome_barrier": _grouped_performance(
            ordered, lambda sample: str(sample.get("hit_result") or "unknown")
        ),
        "chronological_drift": {
            label: _compact_performance(group)
            for label, group in recency_groups.items()
            if group
        },
        "best_hours_utc": dict(ranked_hours[:5]),
        "worst_hours_utc": dict(ranked_hours[-5:]),
        "largest_symbol_samples": dict(largest_symbols),
    }


def build_prediction_ai_dataset(
    rows: Sequence[Mapping[str, Any]],
    current_config: dict[str, Any],
    *,
    current_config_version: int,
) -> PredictionAiDataset:
    """Aggregate exact-version history and reserve the newest 25% for validation."""

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
            "当前算法版本的已结算完整快照不足，暂时无法调用 DeepSeek 生成可靠的新版本"
        )

    samples_by_horizon: dict[str, list[dict[str, Any]]] = {}
    training_by_horizon: dict[str, list[dict[str, Any]]] = {}
    validation_by_horizon: dict[str, list[dict[str, Any]]] = {}
    horizons: dict[str, Any] = {}
    for seconds in battle.HORIZONS:
        name = battle.HORIZON_NAMES[seconds]
        horizon_samples = [sample for sample in samples if sample["horizon_seconds"] == seconds]
        training, validation = _split_samples(horizon_samples)
        samples_by_horizon[name] = horizon_samples
        training_by_horizon[name] = training
        validation_by_horizon[name] = validation
        horizons[name] = {
            "sample_count": len(horizon_samples),
            "training_count": len(training),
            "validation_count_reserved": len(validation),
            "purged_count": len(horizon_samples) - len(training) - len(validation),
            "minimum_samples": MIN_HORIZON_SAMPLES,
            "current_weights": deepcopy(config["weights"][name]),
            "training_metrics": _metrics(training, config),
            "training_history_analysis": _training_history_analysis(training),
            "feature_statistics": _feature_statistics(
                training,
                config["enabled_features"],
            ),
        }

    timestamps = [sample["predicted_at_ms"] for sample in samples]
    model_context = {
        "algorithm": {
            "model_key": battle.MODEL_KEY,
            "model_version": battle.MODEL_VERSION,
            "feature_schema_version": battle.FEATURE_SCHEMA_VERSION,
            "config_version": current_config_version,
            "scalars": {
                name: config[name]
                for name in (
                    "direction_threshold",
                    "min_data_quality",
                    "account_crowding_penalty",
                    "funding_crowding_penalty",
                )
            },
            "enabled_features": deepcopy(config["enabled_features"]),
        },
        "history": {
            "source": "database_settled_outcomes_for_exact_config_version",
            "analysis_scope": (
                "oldest_training_after_label_due_purge; "
                "newest_25_percent_reserved_and_hidden; equal_timestamps_grouped"
            ),
            "sample_count": len(samples),
            "start_ms": min(timestamps) if timestamps else None,
            "end_ms": max(timestamps) if timestamps else None,
            "horizons": horizons,
        },
    }
    return PredictionAiDataset(
        current_config=config,
        current_config_version=current_config_version,
        samples_by_horizon=samples_by_horizon,
        training_by_horizon=training_by_horizon,
        validation_by_horizon=validation_by_horizon,
        model_context=model_context,
        sample_count=len(samples),
        history_start_ms=min(timestamps) if timestamps else None,
        history_end_ms=max(timestamps) if timestamps else None,
    )


def _output_contract(config: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "summary": "用中文简述调优依据，不超过 300 字",
        "reasoning_steps": [
            "3 到 8 条可审计的中文推理摘要；每条不超过 240 字，不输出隐藏思维链"
        ],
        "weights": {
            horizon: {name: "0 到 1 之间的有限小数" for name in battle.ALGORITHM_FEATURES}
            for horizon in ("5m", "15m", "1h")
        },
        "constraints": {
            "exact_keys": True,
            "each_horizon_sum": 1.0,
            "disabled_feature_weights_unchanged": {
                name: {
                    horizon: config["weights"][horizon][name]
                    for horizon in ("5m", "15m", "1h")
                }
                for name in battle.ALGORITHM_FEATURES
                if not config["enabled_features"].get(name, True)
            },
            "maximum_absolute_change_per_weight": MAX_WEIGHT_DELTA,
            "insufficient_horizon_must_keep_current_weights": True,
            "server_side_safety_projection": (
                "模型建议会被服务端投影到合法权重空间，再使用隐藏验证集校验"
            ),
        },
    }


def _request_deepseek_weights(
    dataset: PredictionAiDataset,
    *,
    api_key: str,
    model_name: str,
    timeout_seconds: float,
    max_tokens: int,
    transport: Transport | None,
    thinking_enabled: bool = True,
    allow_fallback: bool = True,
) -> tuple[dict[str, Any], dict[str, Any]]:
    provider, endpoint = _validate_chat_configuration(
        provider_code="deepseek",
        api_key=api_key,
        model_name=model_name,
        timeout_seconds=timeout_seconds,
    )
    if provider != "deepseek":
        raise StrategyAiError("not_configured")
    if (
        isinstance(max_tokens, bool)
        or not isinstance(max_tokens, int)
        or not 4_000 <= max_tokens <= 64_000
    ):
        raise StrategyAiError("not_configured")
    system_prompt = (
        "You optimize weights for a quantitative prediction ensemble. "
        "Return exactly one JSON object. Use only the supplied aggregated "
        "training statistics; the holdout is intentionally hidden. Treat "
        "training_history_analysis as the authoritative settled database "
        "performance for this exact algorithm config version. Reconcile "
        "feature correlations with prediction direction, confidence, MFE/MAE, "
        "time, symbol, barrier outcome, and recency segments. Never "
        "invent samples, change scalar thresholds, enable or disable features, "
        "or output code. Respect every output_contract constraint. Prefer "
        "small robust changes when reliability or support is low. The JSON "
        "must contain exactly summary, reasoning_steps, and weights. "
        "reasoning_steps must be concise audit-friendly conclusions, not private "
        "chain-of-thought or hidden intermediate reasoning."
    )
    user_prompt = json.dumps(
        {
            "task": (
                "根据数据库中当前算法配置版本的已结算训练历史分析和特征统计，"
                "调优三个周期的权重，并输出 JSON"
            ),
            "training_statistics": dataset.model_context,
            "output_contract": _output_contract(dataset.current_config),
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
    request_payload = {
        "model": model_name,
        "messages": [
            {
                "role": "system",
                "content": system_prompt,
            },
            {
                "role": "user",
                "content": user_prompt,
            },
        ],
        "response_format": {"type": "json_object"},
        "thinking": {"type": "enabled" if thinking_enabled else "disabled"},
        "max_tokens": max_tokens,
        "stream": False,
    }
    submitted_prompt = {
        "model": model_name,
        "system": system_prompt,
        "user": user_prompt,
        "request_options": {
            "response_format": {"type": "json_object"},
            "thinking": {"type": "enabled" if thinking_enabled else "disabled"},
            "max_tokens": max_tokens,
            "stream": False,
        },
    }

    def traced_error(
        category: str,
        stage: str,
        *,
        status_code: int | None = None,
        response_payload: Mapping[str, Any] | None = None,
        response_size: int | None = None,
    ) -> StrategyAiError:
        choice = None
        if response_payload is not None:
            choices = response_payload.get("choices")
            if isinstance(choices, list) and choices and isinstance(choices[0], Mapping):
                choice = choices[0]
        message = choice.get("message") if isinstance(choice, Mapping) else None
        content = message.get("content") if isinstance(message, Mapping) else None
        raw_output = {
            "parse_stage": stage,
            "http_status": status_code,
            "response_bytes": response_size,
            "finish_reason": choice.get("finish_reason") if isinstance(choice, Mapping) else None,
            "content": content[:24_000] if isinstance(content, str) else None,
        }
        exc = StrategyAiError(category)
        exc.trace = {
            "optimizer_key": OPTIMIZER_KEY,
            "provider_code": "deepseek",
            "model_name": model_name,
            "response_model": (
                response_payload.get("model") if response_payload is not None else model_name
            ),
            "usage": (
                response_payload.get("usage", {})
                if response_payload is not None
                and isinstance(response_payload.get("usage"), dict)
                else {}
            ),
            "system_fingerprint": (
                response_payload.get("system_fingerprint")
                if response_payload is not None
                else None
            ),
            "raw_model_output": raw_output,
            "model_attempts": [
                {
                    "mode": "thinking" if thinking_enabled else "non-thinking",
                    "finish_reason": raw_output["finish_reason"],
                    "response_bytes": response_size,
                    "usage": (
                        response_payload.get("usage", {})
                        if response_payload is not None
                        and isinstance(response_payload.get("usage"), dict)
                        else {}
                    ),
                }
            ],
            "submitted_prompt": submitted_prompt,
            "source_config_version": dataset.current_config_version,
            "sample_count": dataset.sample_count,
            "history_start_ms": dataset.history_start_ms,
            "history_end_ms": dataset.history_end_ms,
            "status": "rejected",
            "failure_category": category,
            "summary": "DeepSeek 响应未形成完整的安全权重 JSON，当前算法未修改。",
            "reasoning_steps": [],
            "normalization": None,
            "horizons": [],
        }
        return exc
    try:
        body = json.dumps(
            request_payload,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, UnicodeEncodeError, ValueError):
        raise StrategyAiError("invalid_output") from None
    if len(body) > MAX_REQUEST_BYTES:
        raise StrategyAiError("invalid_output")

    caller = transport or _chat_http_transport
    try:
        status_code, response_body = caller(
            endpoint,
            body,
            {
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            float(timeout_seconds),
        )
    except StrategyAiError as exc:
        if not isinstance(getattr(exc, "trace", None), dict):
            exc.trace = traced_error(exc.category, "transport").trace
        raise
    except TimeoutError:
        raise traced_error("timeout", "transport") from None
    except (HttpClientError, OSError):
        raise traced_error("upstream", "transport") from None
    if status_code in {401, 403}:
        raise traced_error("not_configured", "http_status", status_code=status_code)
    if status_code in {408, 504}:
        raise traced_error("timeout", "http_status", status_code=status_code)
    if not 200 <= status_code < 300:
        raise traced_error("upstream", "http_status", status_code=status_code)

    try:
        response_payload = _strict_json_bytes(response_body)
    except StrategyAiError:
        raise traced_error(
            "invalid_output",
            "response_envelope",
            status_code=status_code,
            response_size=len(response_body),
        ) from None
    try:
        output = _strict_json_text(_chat_output_text(response_payload))
    except StrategyAiError:
        if thinking_enabled and allow_fallback:
            choices = response_payload.get("choices")
            choice = choices[0] if isinstance(choices, list) and choices else {}
            primary_attempt = {
                "mode": "thinking",
                "finish_reason": (
                    choice.get("finish_reason") if isinstance(choice, Mapping) else None
                ),
                "response_bytes": len(response_body),
                "usage": (
                    response_payload.get("usage", {})
                    if isinstance(response_payload.get("usage"), dict)
                    else {}
                ),
            }
            try:
                fallback_output, fallback_metadata = _request_deepseek_weights(
                    dataset,
                    api_key=api_key,
                    model_name=model_name,
                    timeout_seconds=timeout_seconds,
                    max_tokens=4_000,
                    transport=transport,
                    thinking_enabled=False,
                    allow_fallback=False,
                )
            except StrategyAiError as fallback_exc:
                trace = getattr(fallback_exc, "trace", None)
                if isinstance(trace, dict):
                    fallback_prompt = trace.get("submitted_prompt") or {}
                    trace["model_attempts"] = [
                        primary_attempt,
                        *(trace.get("model_attempts") or []),
                    ]
                    trace["submitted_prompt"] = {
                        "model": model_name,
                        "system": system_prompt,
                        "user": user_prompt,
                        "request_options": {
                            "primary": submitted_prompt["request_options"],
                            "fallback": fallback_prompt.get("request_options", {}),
                            "fallback_trigger": (
                                "thinking response had no complete JSON output"
                            ),
                        },
                    }
                raise
            fallback_attempts = fallback_metadata.get("model_attempts") or []
            fallback_usage = fallback_metadata.get("usage") or {}
            primary_usage = primary_attempt["usage"]
            combined_usage = {
                key: sum(
                    int(usage.get(key) or 0)
                    for usage in (primary_usage, fallback_usage)
                    if isinstance(usage, Mapping)
                )
                for key in ("prompt_tokens", "completion_tokens", "total_tokens")
            }
            combined_usage["completion_tokens_details"] = {
                "reasoning_tokens": int(
                    (primary_usage.get("completion_tokens_details") or {}).get(
                        "reasoning_tokens", 0
                    )
                )
            }
            fallback_metadata["usage"] = combined_usage
            fallback_metadata["model_attempts"] = [primary_attempt, *fallback_attempts]
            fallback_options = fallback_metadata["submitted_prompt"]["request_options"]
            fallback_metadata["submitted_prompt"] = {
                "model": model_name,
                "system": system_prompt,
                "user": user_prompt,
                "request_options": {
                    "primary": submitted_prompt["request_options"],
                    "fallback": fallback_options,
                    "fallback_trigger": "thinking response had no complete JSON output",
                },
            }
            return fallback_output, fallback_metadata
        raise traced_error(
            "invalid_output",
            "model_output",
            status_code=status_code,
            response_payload=response_payload,
            response_size=len(response_body),
        ) from None
    if not isinstance(output, dict):
        raise traced_error(
            "invalid_output",
            "model_output_shape",
            status_code=status_code,
            response_payload=response_payload,
            response_size=len(response_body),
        )
    return output, {
        "response_model": response_payload.get("model") or model_name,
        "usage": response_payload.get("usage") if isinstance(response_payload.get("usage"), dict) else {},
        "system_fingerprint": response_payload.get("system_fingerprint"),
        "raw_model_output": output,
        "model_attempts": [
            {
                "mode": "thinking" if thinking_enabled else "non-thinking",
                "finish_reason": (
                    response_payload["choices"][0].get("finish_reason")
                    if isinstance(response_payload.get("choices"), list)
                    and response_payload["choices"]
                    and isinstance(response_payload["choices"][0], dict)
                    else None
                ),
                "response_bytes": len(response_body),
                "usage": (
                    response_payload.get("usage", {})
                    if isinstance(response_payload.get("usage"), dict)
                    else {}
                ),
            }
        ],
        "submitted_prompt": submitted_prompt,
    }


def _bounded_weight_projection(
    proposed: Mapping[str, float],
    current: Mapping[str, Any],
    enabled_features: Mapping[str, Any],
) -> dict[str, float]:
    """Project model weights onto the safe bounded simplex.

    Disabled weights remain fixed. Enabled weights keep the per-feature delta
    guardrail while their total is made exactly one. The current configuration
    is always a feasible point, so the projection cannot widen a guardrail.
    """

    enabled = [
        name
        for name in battle.ALGORITHM_FEATURES
        if enabled_features.get(name, True)
    ]
    if not enabled:
        raise StrategyAiError("invalid_output")
    fixed = {
        name: _number(current[name])
        for name in battle.ALGORITHM_FEATURES
        if name not in enabled
    }
    target_total = 1.0 - sum(fixed.values())
    lower = {
        name: max(0.0, _number(current[name]) - MAX_WEIGHT_DELTA)
        for name in enabled
    }
    upper = {
        name: min(1.0, _number(current[name]) + MAX_WEIGHT_DELTA)
        for name in enabled
    }
    if target_total < sum(lower.values()) - 1e-9 or target_total > sum(upper.values()) + 1e-9:
        raise StrategyAiError("invalid_output")

    # Euclidean projection with box constraints: x_i = clip(y_i-lambda).
    low_lambda = -2.0
    high_lambda = 2.0
    for _ in range(100):
        midpoint = (low_lambda + high_lambda) / 2.0
        total = sum(
            min(upper[name], max(lower[name], proposed[name] - midpoint))
            for name in enabled
        )
        if total > target_total:
            low_lambda = midpoint
        else:
            high_lambda = midpoint
    offset = (low_lambda + high_lambda) / 2.0
    projected = dict(fixed)
    projected.update(
        {
            name: min(upper[name], max(lower[name], proposed[name] - offset))
            for name in enabled
        }
    )

    rounded = {name: round(projected[name], 6) for name in battle.ALGORITHM_FEATURES}
    remaining = round(1.0 - sum(rounded.values()), 6)
    if remaining:
        candidates = sorted(
            enabled,
            key=lambda name: (
                upper[name] - rounded[name]
                if remaining > 0
                else rounded[name] - lower[name]
            ),
            reverse=True,
        )
        for name in candidates:
            capacity = (
                upper[name] - rounded[name]
                if remaining > 0
                else rounded[name] - lower[name]
            )
            adjustment = math.copysign(min(abs(remaining), max(0.0, capacity)), remaining)
            rounded[name] = round(rounded[name] + adjustment, 6)
            remaining = round(1.0 - sum(rounded.values()), 6)
            if not remaining:
                break
    if not math.isclose(sum(rounded.values()), 1.0, abs_tol=1e-6):
        raise StrategyAiError("invalid_output")
    return rounded


def _validate_model_weights(
    output: Mapping[str, Any],
    dataset: PredictionAiDataset,
) -> tuple[str, list[str], dict[str, dict[str, float]], dict[str, Any]]:
    if not isinstance(output, Mapping):
        raise StrategyAiError("invalid_output")
    summary = output.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        summary = "DeepSeek 已返回权重建议，服务端完成安全投影与验证。"
    summary = summary.strip()[:300]
    raw_reasoning_steps = output.get("reasoning_steps")
    reasoning_steps = (
        [step.strip()[:240] for step in raw_reasoning_steps if isinstance(step, str) and step.strip()][:8]
        if isinstance(raw_reasoning_steps, list)
        else []
    )
    if not reasoning_steps:
        reasoning_steps = ["模型返回了权重建议，服务端未收到可展示的推理摘要。"]
    raw_horizons = output.get("weights")
    if not isinstance(raw_horizons, Mapping):
        raise StrategyAiError("invalid_output")

    weights: dict[str, dict[str, float]] = {}
    normalization_horizons: list[dict[str, Any]] = []
    for horizon in ("5m", "15m", "1h"):
        raw_weights = raw_horizons.get(horizon)
        if not isinstance(raw_weights, Mapping):
            raise StrategyAiError("invalid_output")
        current = dataset.current_config["weights"][horizon]
        proposed: dict[str, float] = {}
        replaced_features: list[str] = []
        for name in battle.ALGORITHM_FEATURES:
            raw_value = raw_weights.get(name)
            try:
                value = float(raw_value)
            except (TypeError, ValueError):
                value = _number(current[name])
                replaced_features.append(name)
            if isinstance(raw_value, bool) or not math.isfinite(value):
                value = _number(current[name])
                if name not in replaced_features:
                    replaced_features.append(name)
            proposed[name] = min(1.0, max(0.0, value))
        projected = _bounded_weight_projection(
            proposed,
            current,
            dataset.current_config["enabled_features"],
        )
        adjusted_features = [
            name
            for name in battle.ALGORITHM_FEATURES
            if not math.isclose(projected[name], proposed[name], abs_tol=1e-6)
        ]
        weights[horizon] = projected
        normalization_horizons.append(
            {
                "horizon": horizon,
                "raw_sum": round(sum(proposed.values()), 6),
                "projected_sum": round(sum(projected.values()), 6),
                "adjusted_feature_count": len(adjusted_features),
                "adjusted_features": adjusted_features,
                "invalid_or_missing_features_replaced": replaced_features,
            }
        )
    normalization = {
        "applied": any(
            item["adjusted_feature_count"] or item["invalid_or_missing_features_replaced"]
            for item in normalization_horizons
        ),
        "method": "bounded-simplex-projection",
        "maximum_absolute_change_per_weight": MAX_WEIGHT_DELTA,
        "horizons": normalization_horizons,
    }
    return summary, reasoning_steps, weights, normalization


def _validate_holdout(
    dataset: PredictionAiDataset,
    proposed_weights: Mapping[str, Mapping[str, float]],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    recommended = battle.normalize_algorithm_config(dataset.current_config)
    reports: list[dict[str, Any]] = []
    for horizon in ("5m", "15m", "1h"):
        current_weights = dataset.current_config["weights"][horizon]
        candidate_weights = dict(proposed_weights[horizon])
        training = dataset.training_by_horizon[horizon]
        validation = dataset.validation_by_horizon[horizon]
        baseline_training = _metrics(training, dataset.current_config)
        baseline_validation = _metrics(validation, dataset.current_config)
        candidate_config = battle.normalize_algorithm_config(dataset.current_config)
        candidate_config["weights"][horizon] = candidate_weights
        candidate_training = _metrics(training, candidate_config)
        candidate_validation = _metrics(validation, candidate_config)
        changed = any(
            abs(_number(candidate_weights[name]) - _number(current_weights[name])) >= 0.0005
            for name in battle.ALGORITHM_FEATURES
        )
        minimum_directional = max(2, int(baseline_validation["directional_count"] * 0.70))
        baseline_hit = baseline_validation["hit_rate"]
        candidate_hit = candidate_validation["hit_rate"]
        hit_rate_safe = (
            baseline_hit is None
            or candidate_hit is None
            or candidate_hit >= baseline_hit - 0.05
        )
        accepted = bool(
            len(training) >= MIN_TRAINING_SAMPLES
            and len(validation) >= 2
            and changed
            and candidate_validation["directional_count"] >= minimum_directional
            and candidate_validation["utility_bps"]
            >= baseline_validation["utility_bps"] + MIN_UTILITY_GAIN_BPS
            and hit_rate_safe
        )
        if accepted:
            recommended["weights"][horizon] = candidate_weights
            status = "optimized"
        elif len(training) < MIN_TRAINING_SAMPLES or len(validation) < 2:
            status = "insufficient_samples"
        else:
            status = "no_validated_improvement"

        changes = [
            {
                "feature": name,
                "enabled": bool(dataset.current_config["enabled_features"].get(name, True)),
                "before": round(_number(current_weights[name]), 6),
                "after": round(
                    _number(recommended["weights"][horizon][name]),
                    6,
                ),
                "proposed": round(_number(candidate_weights[name]), 6),
                "delta": round(
                    _number(recommended["weights"][horizon][name])
                    - _number(current_weights[name]),
                    6,
                ),
            }
            for name in battle.ALGORITHM_FEATURES
        ]
        changes.sort(key=lambda item: abs(item["delta"]), reverse=True)
        reports.append(
            {
                "horizon": horizon,
                "status": status,
                "sample_count": len(dataset.samples_by_horizon[horizon]),
                "minimum_samples": MIN_HORIZON_SAMPLES,
                "training_count": len(training),
                "validation_count": len(validation),
                "purged_count": (
                    len(dataset.samples_by_horizon[horizon])
                    - len(training)
                    - len(validation)
                ),
                "baseline": baseline_validation,
                "optimized": candidate_validation if accepted else baseline_validation,
                "training_baseline": baseline_training,
                "training_optimized": candidate_training if accepted else baseline_training,
                "utility_gain_bps": round(
                    (candidate_validation["utility_bps"] - baseline_validation["utility_bps"])
                    if accepted
                    else 0.0,
                    4,
                ),
                "changes": changes,
            }
        )
    return recommended, reports


def optimize_prediction_algorithm_with_deepseek(
    rows: Sequence[Mapping[str, Any]],
    current_config: dict[str, Any],
    *,
    current_config_version: int,
    api_key: str,
    model_name: str,
    timeout_seconds: float = 120.0,
    max_tokens: int = 16_000,
    transport: Transport | None = None,
) -> dict[str, Any]:
    """Ask DeepSeek for weights, validate on hidden history, and return a safe config."""

    dataset = build_prediction_ai_dataset(
        rows,
        current_config,
        current_config_version=current_config_version,
    )
    output, response_metadata = _request_deepseek_weights(
        dataset,
        api_key=api_key,
        model_name=model_name,
        timeout_seconds=timeout_seconds,
        max_tokens=max_tokens,
        transport=transport,
    )
    trace_base = {
        "optimizer_key": OPTIMIZER_KEY,
        "provider_code": "deepseek",
        "model_name": model_name,
        "response_model": response_metadata["response_model"],
        "usage": response_metadata["usage"],
        "system_fingerprint": response_metadata["system_fingerprint"],
        "raw_model_output": response_metadata["raw_model_output"],
        "model_attempts": response_metadata["model_attempts"],
        "submitted_prompt": response_metadata["submitted_prompt"],
        "source_config_version": current_config_version,
        "sample_count": dataset.sample_count,
        "history_start_ms": dataset.history_start_ms,
        "history_end_ms": dataset.history_end_ms,
    }
    try:
        summary, reasoning_steps, proposed_weights, normalization = _validate_model_weights(
            output, dataset
        )
    except StrategyAiError as exc:
        exc.trace = {
            **trace_base,
            "status": "rejected",
            "failure_category": exc.category,
            "summary": "DeepSeek 返回格式无法安全修正，当前算法未修改。",
            "reasoning_steps": [],
            "normalization": None,
            "horizons": [],
        }
        raise
    recommended, reports = _validate_holdout(dataset, proposed_weights)
    optimized_count = sum(report["status"] == "optimized" for report in reports)
    if optimized_count == 0:
        exc = PredictionOptimizationUnavailable(
            "DeepSeek 给出的权重没有通过时间外验证，当前版本保持不变，未创建新版本"
        )
        exc.trace = {
            **trace_base,
            "status": "rejected",
            "failure_category": "no_validated_improvement",
            "summary": summary,
            "reasoning_steps": reasoning_steps,
            "normalization": normalization,
            "horizons": reports,
        }
        raise exc
    return {
        **trace_base,
        "optimized_horizon_count": optimized_count,
        "recommendation_available": True,
        "recommended_config": recommended,
        "summary": summary,
        "reasoning_steps": reasoning_steps,
        "normalization": normalization,
        "horizons": reports,
        "guardrails": {
            "current_version_only": True,
            "aggregated_statistics_only": True,
            "chronological_holdout": True,
            "label_horizon_purge": True,
            "timestamp_groups_kept_together": True,
            "holdout_hidden_from_model": True,
            "disabled_features_preserved": True,
            "scalars_unchanged": True,
            "validated_before_save": True,
            "automatic_save": True,
        },
    }
