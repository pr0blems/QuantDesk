from __future__ import annotations

import json
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from quantdesk_v2 import api as api_module
from quantdesk_v2 import battle
from quantdesk_v2.prediction_ai_optimizer import (
    optimize_prediction_algorithm_with_deepseek,
)
from quantdesk_v2.prediction_optimizer import PredictionOptimizationUnavailable
from quantdesk_v2.strategy_ai import StrategyAiError


def _history_row(index: int, horizon_seconds: int, config_version: int) -> dict[str, object]:
    direction = 1.0 if index % 2 == 0 else -1.0
    features = {
        "data_quality": 1.0,
        "micro_age_ms": 1_000,
        "positioning_age_ms": 1_000,
        "aggressive_flow": direction,
        "book_imbalance": 0.0,
        "book_imbalance_5": 0.0,
        "velocity": direction,
        "flash_imbalance": 0.0,
        "taker_flow": 0.0,
        "price_oi_impulse": 0.0,
        "trend_15m": 0.0,
        "trend_1h": 0.0,
        "trend_4h": 0.0,
        "account_crowding": 0.0,
        "funding_crowding": 0.0,
        "realized_volatility_bps": 10.0,
        "kline_strategies": {
            "15m": {"values": {name: 0.0 for name in battle.KLINE_STRATEGY_FEATURES}},
            "1h": {"values": {name: 0.0 for name in battle.KLINE_STRATEGY_FEATURES}},
        },
    }
    return {
        "symbol": f"TEST{index % 3}USDT",
        "horizon_seconds": horizon_seconds,
        "predicted_at_ms": 1_000_000 + index * 4_000_000,
        "completed_at_ms": 1_500_000 + index * 4_000_000,
        "prediction_result": "long" if direction > 0 else "short",
        "battle_score": 0.6 * direction,
        "long_probability": 0.72 if direction > 0 else 0.12,
        "short_probability": 0.12 if direction > 0 else 0.72,
        "neutral_probability": 0.16,
        "confidence_score": 0.60,
        "confidence_label": "medium",
        "gross_edge_bps": 25.0,
        "spread_bps": 1.0,
        "target_bps": 20.0,
        "stop_bps": 10.0,
        "algorithm_config_json": json.dumps({"config_version": config_version}),
        "features_json": features,
        "raw_return_bps": 25.0 * direction,
        "directional_return_bps": 24.0,
        "max_favorable_bps": 10.0 + index,
        "max_adverse_bps": -5.0,
        "hit_result": "target",
        "cost_bps": 1.0,
    }


def _proposed_weights(config: dict[str, object]) -> dict[str, dict[str, float]]:
    result: dict[str, dict[str, float]] = {}
    for horizon in ("5m", "15m", "1h"):
        current = deepcopy(config["weights"][horizon])
        current["aggressive_flow"] += 0.15
        current["velocity"] += 0.15
        remaining = 0.30
        for name in battle.ALGORITHM_FEATURES:
            if name in {"aggressive_flow", "velocity"}:
                continue
            reduction = min(current[name], remaining)
            current[name] -= reduction
            remaining -= reduction
            if remaining <= 1e-12:
                break
        assert remaining <= 1e-12
        result[horizon] = current
    return result


def _transport_for(output: dict[str, object]):
    def transport(endpoint, body: bytes, headers: dict[str, str], timeout: float):
        assert endpoint.code == "deepseek"
        assert headers["Authorization"] == "Bearer sk-test-deepseek-key"
        assert timeout == 120.0
        request = json.loads(body)
        prompt = json.loads(request["messages"][1]["content"])
        assert "training_statistics" in prompt
        history = prompt["training_statistics"]["history"]
        assert history["source"] == "database_settled_outcomes_for_exact_config_version"
        assert "newest_25_percent_reserved_and_hidden" in history["analysis_scope"]
        analysis = history["horizons"]["5m"]["training_history_analysis"]
        assert analysis["summary"]["sample_count"] == 18
        assert analysis["summary"]["hit_rate"] == 1.0
        assert analysis["summary"]["avg_max_favorable_bps"] == 18.5
        feature_stats = history["horizons"]["5m"]["feature_statistics"]
        assert feature_stats["aggressive_flow"]["signal_alignment_rate"] == 1.0
        assert "samples" not in prompt["training_statistics"]
        assert request["response_format"] == {"type": "json_object"}
        assert request["thinking"] == {"type": "enabled"}
        assert request["max_tokens"] == 16_000
        response = {
            "model": "deepseek-v4-flash",
            "system_fingerprint": "test-fingerprint",
            "usage": {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150},
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {"role": "assistant", "content": json.dumps(output)},
                }
            ],
        }
        return 200, json.dumps(response).encode()

    return transport


def test_deepseek_optimizer_uses_aggregates_and_hidden_holdout() -> None:
    rows = [
        _history_row(index, horizon, 7)
        for horizon in battle.HORIZONS
        for index in range(24)
    ]
    config = battle.default_algorithm_config()
    config["direction_threshold"] = 0.4
    proposed = _proposed_weights(config)

    result = optimize_prediction_algorithm_with_deepseek(
        rows,
        config,
        current_config_version=7,
        api_key="sk-test-deepseek-key",
        model_name="deepseek-v4-flash",
        transport=_transport_for(
            {
                "summary": "提高可靠正相关因子的权重。",
                "reasoning_steps": [
                    "三个周期的主动成交与价格速度均为可靠正相关。",
                    "低支持度因子保持较低权重，避免过拟合。",
                    "在单项变化上限内增加两个稳定因子的权重。",
                ],
                "weights": proposed,
            }
        ),
    )

    assert result["provider_code"] == "deepseek"
    assert result["source_config_version"] == 7
    assert result["sample_count"] == 72
    assert result["optimized_horizon_count"] == 3
    assert result["guardrails"]["holdout_hidden_from_model"] is True
    assert result["guardrails"]["automatic_save"] is True
    assert result["usage"]["total_tokens"] == 150
    assert len(result["reasoning_steps"]) == 3
    assert result["submitted_prompt"]["model"] == "deepseek-v4-flash"
    assert "sk-test-deepseek-key" not in json.dumps(result["submitted_prompt"])
    for report in result["horizons"]:
        assert report["status"] == "optimized"
        assert report["optimized"]["utility_bps"] > report["baseline"]["utility_bps"]
    for weights in result["recommended_config"]["weights"].values():
        assert sum(weights.values()) == pytest.approx(1.0, abs=1e-6)


def test_legacy_trace_recomputes_actual_database_values_at_audit_time(monkeypatch) -> None:
    audit_created_at = datetime(2026, 8, 8, 12, 0, 0)
    rows = [
        {
            **_history_row(index, horizon, 7),
            "outcome_updated_at_ms": int(
                (audit_created_at - timedelta(minutes=1))
                .replace(tzinfo=UTC)
                .timestamp()
                * 1_000
            ),
        }
        for horizon in battle.HORIZONS
        for index in range(24)
    ]
    rows.append(
        {
            **_history_row(25, 300, 7),
            "outcome_updated_at_ms": int(
                (audit_created_at + timedelta(minutes=1))
                .replace(tzinfo=UTC)
                .timestamp()
                * 1_000
            ),
        }
    )

    class Repository:
        def prediction_algorithm_history(self):
            return rows

    monkeypatch.setattr(api_module, "_monitor", lambda request: Repository())
    metadata = {
        "source_version": 7,
        "sample_count": 72,
        "history_end_ms": 100_000_000,
        "submitted_prompt": {
            "user": json.dumps(
                {
                    "training_statistics": {
                        "history": {
                            "horizons": {
                                horizon: {"training_count": 18}
                                for horizon in ("5m", "15m", "1h")
                            }
                        }
                    }
                }
            )
        },
    }
    result = api_module._prediction_ai_trace_database_analysis(
        SimpleNamespace(),
        metadata,
        audit_created_at=audit_created_at,
    )

    assert result["available"] is True
    assert result["history"]["sample_count"] == 72
    summary = result["history"]["horizons"]["5m"]["training_history_analysis"]["summary"]
    assert summary["directional_count"] == 18
    assert summary["hit_rate"] == 1.0
    assert summary["avg_directional_return_bps"] == 24.0


def test_deepseek_optimizer_projects_unsafe_model_weights_to_guardrails() -> None:
    rows = [
        _history_row(index, horizon, 2)
        for horizon in battle.HORIZONS
        for index in range(24)
    ]
    config = battle.default_algorithm_config()
    proposed = _proposed_weights(config)
    for horizon in ("5m", "15m", "1h"):
        proposed[horizon]["aggressive_flow"] = 0.99

    result = optimize_prediction_algorithm_with_deepseek(
        rows,
        config,
        current_config_version=2,
        api_key="sk-test-deepseek-key",
        model_name="deepseek-v4-flash",
        transport=_transport_for(
            {
                "summary": "危险权重",
                "reasoning_steps": ["步骤一", "步骤二", "步骤三"],
                "weights": proposed,
            }
        ),
    )
    projected = result["recommended_config"]["weights"]["5m"]
    assert result["normalization"]["applied"] is True
    assert projected["aggressive_flow"] <= (
        config["weights"]["5m"]["aggressive_flow"] + 0.15 + 1e-6
    )
    assert sum(projected.values()) == pytest.approx(1.0, abs=1e-6)


def test_deepseek_optimizer_rejects_missing_weight_horizon() -> None:
    rows = [_history_row(index, 300, 2) for index in range(24)]
    config = battle.default_algorithm_config()
    proposed = deepcopy(config["weights"])
    proposed.pop("1h")

    with pytest.raises(StrategyAiError) as exc:
        optimize_prediction_algorithm_with_deepseek(
            rows,
            config,
            current_config_version=2,
            api_key="sk-test-deepseek-key",
            model_name="deepseek-v4-flash",
            transport=_transport_for(
                {
                    "summary": "缺少周期",
                    "reasoning_steps": ["返回不完整"],
                    "weights": proposed,
                }
            ),
        )
    assert exc.value.category == "invalid_output"
    assert exc.value.trace["status"] == "rejected"
    assert exc.value.trace["raw_model_output"]["summary"] == "缺少周期"


def test_deepseek_optimizer_records_truncated_model_output() -> None:
    rows = [_history_row(index, 300, 2) for index in range(24)]
    config = battle.default_algorithm_config()

    def truncated_transport(endpoint, body, headers, timeout):
        return 200, json.dumps(
            {
                "model": "deepseek-v4-flash",
                "usage": {"total_tokens": 4_000},
                "choices": [
                    {
                        "finish_reason": "length",
                        "message": {"content": '{"summary":"未完成"'},
                    }
                ],
            }
        ).encode()

    with pytest.raises(StrategyAiError) as exc:
        optimize_prediction_algorithm_with_deepseek(
            rows,
            config,
            current_config_version=2,
            api_key="sk-test-deepseek-key",
            model_name="deepseek-v4-flash",
            transport=truncated_transport,
        )
    assert exc.value.category == "invalid_output"
    assert exc.value.trace["raw_model_output"]["finish_reason"] == "length"
    assert exc.value.trace["raw_model_output"]["parse_stage"] == "model_output"
    options = exc.value.trace["submitted_prompt"]["request_options"]
    assert options["primary"]["max_tokens"] == 16_000
    assert options["fallback"]["max_tokens"] == 4_000
    assert [item["mode"] for item in exc.value.trace["model_attempts"]] == [
        "thinking",
        "non-thinking",
    ]


def test_deepseek_optimizer_retries_without_thinking_after_reasoning_exhaustion() -> None:
    rows = [
        _history_row(index, horizon, 4)
        for horizon in battle.HORIZONS
        for index in range(24)
    ]
    config = battle.default_algorithm_config()
    proposed = _proposed_weights(config)
    requests: list[dict[str, object]] = []

    def fallback_transport(endpoint, body, headers, timeout):
        request = json.loads(body)
        requests.append(request)
        if request["thinking"] == {"type": "enabled"}:
            response = {
                "model": "deepseek-v4-flash",
                "usage": {
                    "prompt_tokens": 100,
                    "completion_tokens": 16_000,
                    "total_tokens": 16_100,
                    "completion_tokens_details": {"reasoning_tokens": 16_000},
                },
                "choices": [
                    {
                        "finish_reason": "length",
                        "message": {"content": ""},
                    }
                ],
            }
        else:
            response = {
                "model": "deepseek-v4-flash",
                "usage": {
                    "prompt_tokens": 100,
                    "completion_tokens": 500,
                    "total_tokens": 600,
                },
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {
                            "content": json.dumps(
                                {
                                    "summary": "非思考重试完成",
                                    "reasoning_steps": ["根据聚合指标调整权重"],
                                    "weights": proposed,
                                }
                            )
                        },
                    }
                ],
            }
        return 200, json.dumps(response).encode()

    result = optimize_prediction_algorithm_with_deepseek(
        rows,
        config,
        current_config_version=4,
        api_key="sk-test-deepseek-key",
        model_name="deepseek-v4-flash",
        transport=fallback_transport,
    )
    assert [request["thinking"] for request in requests] == [
        {"type": "enabled"},
        {"type": "disabled"},
    ]
    assert requests[0]["max_tokens"] == 16_000
    assert requests[1]["max_tokens"] == 4_000
    assert result["usage"]["total_tokens"] == 16_700
    assert [item["mode"] for item in result["model_attempts"]] == [
        "thinking",
        "non-thinking",
    ]


def test_deepseek_optimizer_does_not_create_unvalidated_version() -> None:
    rows = [
        _history_row(index, horizon, 3)
        for horizon in battle.HORIZONS
        for index in range(24)
    ]
    config = battle.default_algorithm_config()

    with pytest.raises(PredictionOptimizationUnavailable, match="未创建新版本") as exc:
        optimize_prediction_algorithm_with_deepseek(
            rows,
            config,
            current_config_version=3,
            api_key="sk-test-deepseek-key",
            model_name="deepseek-v4-flash",
            transport=_transport_for(
                {
                    "summary": "维持原权重。",
                    "reasoning_steps": ["样本统计稳定。", "候选调整有限。", "因此保持原权重。"],
                    "weights": deepcopy(config["weights"]),
                }
            ),
        )
    assert exc.value.trace["failure_category"] == "no_validated_improvement"
    assert exc.value.trace["submitted_prompt"]["model"] == "deepseek-v4-flash"
