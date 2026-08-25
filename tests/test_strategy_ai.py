from __future__ import annotations

import json
from typing import Any

import pytest

from quantdesk_v2 import strategy_ai, strategy_routes
from quantdesk_v2.ai_providers import AI_PROVIDER_PRESETS
from quantdesk_v2.strategy_ai import (
    StrategyAiError,
    generate_strategy_code_preview,
    generate_strategy_preview,
    generate_strategy_source_preview,
    generate_user_model_strategy_code_preview,
    generate_user_model_strategy_preview,
    generate_user_model_strategy_source_preview,
)
from quantdesk_v2.strategy_runtime import build_trend_pullback_spec


def strategy() -> dict[str, Any]:
    return {
        "id": 41,
        "public_id": "strat_not_editable",
        "user_id": 7,
        "engine_key": "ma_cross",
        "status": "active",
        "version": 3,
        "name": "均线策略",
        "description": "快慢均线交叉信号",
        "category": "趋势",
        "parameters": {
            "fast_period": 5,
            "slow_period": 20,
            "period": 14,
            "threshold": 1.5,
        },
        "risk_defaults": {
            "stop_loss_pct": 2.0,
            "take_profit_pct": 5.0,
            "leverage": 3,
        },
    }


def output_for(current: dict[str, Any], **changes: Any) -> dict[str, Any]:
    output = {
        "name": current["name"],
        "description": current["description"],
        "category": current["category"],
        "parameters": dict(current["parameters"]),
        "risk_defaults": dict(current["risk_defaults"]),
        "summary": "按要求调整参数",
    }
    output.update(changes)
    return output


def responses_body(model_output: dict[str, Any] | str) -> bytes:
    text = model_output if isinstance(model_output, str) else json.dumps(model_output)
    return json.dumps(
        {
            "status": "completed",
            "output": [
                {
                    "type": "message",
                    "content": [{"type": "output_text", "text": text}],
                }
            ],
        }
    ).encode()


def chat_completions_body(model_output: dict[str, Any] | str) -> bytes:
    text = model_output if isinstance(model_output, str) else json.dumps(model_output)
    return json.dumps(
        {
            "id": "chatcmpl-redacted",
            "choices": [
                {
                    "index": 0,
                    "finish_reason": "stop",
                    "message": {"role": "assistant", "content": text},
                }
            ],
        }
    ).encode()


def test_no_key_uses_honest_local_semantic_preview_without_protected_fields() -> None:
    current = strategy()

    preview = generate_strategy_preview(
        current,
        "改名为趋势强化，快线改为8，慢线设为34，止损改为1.5%，止盈设置为6%，阈值改为2.25",
        api_key="",
        model="gpt-5.6-luna",
        timeout_seconds=10,
    )

    assert preview["provider"] == "local_semantic"
    assert preview["base_version"] == 3
    assert preview["proposed"] == {
        "name": "趋势强化",
        "description": "快慢均线交叉信号",
        "category": "趋势",
        "parameters": {
            "fast_period": 8,
            "slow_period": 34,
            "period": 14,
            "threshold": 2.25,
        },
        "risk_defaults": {
            "stop_loss_pct": 1.5,
            "take_profit_pct": 6.0,
            "leverage": 3,
        },
    }
    assert {change["path"] for change in preview["changes"]} == {
        "name",
        "parameters.fast_period",
        "parameters.slow_period",
        "parameters.threshold",
        "risk_defaults.stop_loss_pct",
        "risk_defaults.take_profit_pct",
    }
    assert not {
        "id",
        "public_id",
        "user_id",
        "engine_key",
        "status",
        "version",
    }.intersection(preview["proposed"])
    assert current["parameters"]["fast_period"] == 5


def test_local_ai_composer_selects_indicators_timeframe_and_direction() -> None:
    prompt = "使用 RSI、布林带和 ATR 做 15 分钟反转策略，只做多"
    preview = generate_strategy_preview(
        strategy_routes._indicator_ai_snapshot(),
        strategy_routes._indicator_model_prompt(prompt),
        api_key="",
        model="unused",
    )
    draft = strategy_routes._indicator_draft_from_proposed(
        preview["proposed"],
        prompt=prompt,
        local=True,
    )

    assert preview["provider"] == "local_semantic"
    assert draft["timeframe"] == "15m"
    assert draft["directions"] == ["long"]
    assert {item["key"] for item in draft["indicators"]} == {
        "rsi",
        "bollinger",
        "atr",
    }


def test_local_period_description_and_unknown_prompt_are_safe() -> None:
    current = strategy()
    edited = generate_strategy_preview(
        current,
        "周期改为30，描述改为趋势过滤后的均线交叉",
        api_key=" ",
        model="unused",
    )
    assert edited["proposed"]["parameters"]["period"] == 30
    assert edited["proposed"]["description"] == "趋势过滤后的均线交叉"

    unknown = generate_strategy_preview(
        current,
        "请帮我优化一下",
        api_key="",
        model="unused",
    )
    assert unknown["provider"] == "local_semantic"
    assert unknown["changes"] == []
    assert "未识别" in unknown["summary"]


def test_openai_request_is_server_side_structured_and_store_is_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current = strategy()
    model_output = output_for(
        current,
        parameters={**current["parameters"], "fast_period": 9},
    )
    captured: dict[str, Any] = {}

    def transport(
        body: bytes, headers: dict[str, str], timeout_seconds: float
    ) -> tuple[int, bytes]:
        captured["payload"] = json.loads(body)
        captured["headers"] = headers
        captured["timeout"] = timeout_seconds
        return 200, responses_body(model_output)

    monkeypatch.setattr(strategy_ai, "_http_transport", transport)
    preview = generate_strategy_preview(
        current,
        "把快线调到9",
        api_key="sk-test-abcdefghijklmnopqrstuvwxyz",
        model="gpt-5.6-luna",
        timeout_seconds=7,
        safety_identifier="qd_0123456789abcdef",
    )

    assert strategy_ai.OPENAI_RESPONSES_URL == "https://api.openai.com/v1/responses"
    payload = captured["payload"]
    assert payload["store"] is False
    assert payload["model"] == "gpt-5.6-luna"
    assert payload["safety_identifier"] == "qd_0123456789abcdef"
    assert payload["text"]["format"]["type"] == "json_schema"
    assert payload["text"]["format"]["strict"] is True
    assert payload["text"]["format"]["schema"]["additionalProperties"] is False
    assert captured["timeout"] == 7
    assert captured["headers"]["Authorization"].startswith("Bearer sk-test-")
    request_text = json.dumps(payload, ensure_ascii=False)
    assert "strat_not_editable" not in request_text
    assert '"user_id"' not in request_text
    assert '"engine_key"' not in request_text
    assert preview["provider"] == "openai"
    assert preview["changes"] == [{"path": "parameters.fast_period", "before": 5, "after": 9}]


@pytest.mark.parametrize("provider_code", sorted(AI_PROVIDER_PRESETS))
def test_user_model_uses_server_owned_chat_completion_endpoint(
    monkeypatch: pytest.MonkeyPatch,
    provider_code: str,
) -> None:
    current = strategy()
    preset = AI_PROVIDER_PRESETS[provider_code]
    model_output = output_for(
        current,
        risk_defaults={**current["risk_defaults"], "take_profit_pct": 8.0},
    )
    captured: dict[str, Any] = {}

    def transport(
        endpoint: Any,
        body: bytes,
        headers: dict[str, str],
        timeout_seconds: float,
    ) -> tuple[int, bytes]:
        captured.update(
            endpoint=endpoint,
            payload=json.loads(body),
            headers=headers,
            timeout=timeout_seconds,
        )
        return 200, chat_completions_body(model_output)

    monkeypatch.setattr(strategy_ai, "_chat_http_transport", transport)
    preview = generate_user_model_strategy_preview(
        current,
        "把止盈改成 8%",
        provider_code=provider_code,
        api_key="provider-key-abcdefghijklmnopqrstuvwxyz",
        model_name=preset.default_model,
        timeout_seconds=9,
    )

    assert captured["endpoint"] is preset
    assert captured["endpoint"].host == preset.host
    assert captured["endpoint"].path == preset.path
    assert captured["payload"]["model"] == preset.default_model
    assert captured["payload"]["stream"] is False
    if provider_code == "minimax":
        assert "response_format" not in captured["payload"]
        assert captured["payload"]["reasoning_split"] is True
    else:
        assert captured["payload"]["response_format"] == {"type": "json_object"}
        assert "reasoning_split" not in captured["payload"]
    if provider_code == "deepseek":
        assert captured["payload"]["thinking"] == {"type": "disabled"}
    else:
        assert "thinking" not in captured["payload"]
    token_field = (
        "max_completion_tokens"
        if provider_code in {"openai", "qwen", "kimi", "minimax"}
        else "max_tokens"
    )
    assert captured["payload"][token_field] == 2_000
    assert captured["headers"]["Authorization"].startswith("Bearer provider-key-")
    assert captured["timeout"] == 9
    assert preview["provider"] == provider_code
    assert preview["proposed"]["risk_defaults"]["take_profit_pct"] == 8.0


@pytest.mark.parametrize(
    "provider_code",
    [
        "custom",
        "http://api.deepseek.com",
        "openai@attacker.invalid",
        "api.openai.com",
    ],
)
def test_user_model_rejects_non_allowlisted_provider_before_network(
    monkeypatch: pytest.MonkeyPatch,
    provider_code: str,
) -> None:
    monkeypatch.setattr(
        strategy_ai,
        "_chat_http_transport",
        lambda *_: pytest.fail("network must not be called"),
    )

    with pytest.raises(StrategyAiError) as caught:
        generate_user_model_strategy_preview(
            strategy(),
            "修改止盈",
            provider_code=provider_code,
            api_key="provider-key-abcdefghijklmnopqrstuvwxyz",
            model_name="model-name",
        )
    assert caught.value.category == "not_configured"


def test_user_model_does_not_follow_redirect_or_leak_upstream_details(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        strategy_ai,
        "_chat_http_transport",
        lambda *_: (302, b'{"location":"https://attacker.invalid/?key=secret"}'),
    )

    with pytest.raises(StrategyAiError) as caught:
        generate_user_model_strategy_preview(
            strategy(),
            "修改止盈",
            provider_code="deepseek",
            api_key="provider-key-abcdefghijklmnopqrstuvwxyz",
            model_name="deepseek-v4-flash",
        )
    assert caught.value.category == "upstream"
    assert "attacker" not in str(caught.value)
    assert "secret" not in str(caught.value)


def test_user_model_accepts_schema_compatible_model_name_with_slash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current = strategy()
    captured: dict[str, Any] = {}

    def transport(
        _: Any,
        body: bytes,
        _headers: dict[str, str],
        _timeout_seconds: float,
    ) -> tuple[int, bytes]:
        captured["payload"] = json.loads(body)
        return 200, chat_completions_body(output_for(current))

    monkeypatch.setattr(strategy_ai, "_chat_http_transport", transport)
    preview = generate_user_model_strategy_preview(
        current,
        "保持现有设置",
        provider_code="qwen",
        api_key="provider-key-abcdefghijklmnopqrstuvwxyz",
        model_name="workspace/team-model:v2",
    )

    assert captured["payload"]["model"] == "workspace/team-model:v2"
    assert preview["provider"] == "qwen"


def test_user_model_rejects_model_name_beyond_schema_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        strategy_ai,
        "_chat_http_transport",
        lambda *_: pytest.fail("network must not be called"),
    )
    with pytest.raises(StrategyAiError) as caught:
        generate_user_model_strategy_preview(
            strategy(),
            "保持现有设置",
            provider_code="qwen",
            api_key="provider-key-abcdefghijklmnopqrstuvwxyz",
            model_name="m" * 129,
        )
    assert caught.value.category == "not_configured"


@pytest.mark.parametrize("api_key", ["k" * 8, "k" * 2_048])
def test_user_model_accepts_persisted_api_key_length_boundaries(
    monkeypatch: pytest.MonkeyPatch,
    api_key: str,
) -> None:
    current = strategy()
    monkeypatch.setattr(
        strategy_ai,
        "_chat_http_transport",
        lambda *_: (200, chat_completions_body(output_for(current))),
    )

    preview = generate_user_model_strategy_preview(
        current,
        "保持现有设置",
        provider_code="deepseek",
        api_key=api_key,
        model_name="deepseek-v4-flash",
    )

    assert preview["provider"] == "deepseek"


@pytest.mark.parametrize("api_key", ["k" * 7, "k" * 2_049])
def test_user_model_rejects_api_key_outside_persistence_contract(
    monkeypatch: pytest.MonkeyPatch,
    api_key: str,
) -> None:
    monkeypatch.setattr(
        strategy_ai,
        "_chat_http_transport",
        lambda *_: pytest.fail("network must not be called"),
    )
    with pytest.raises(StrategyAiError) as caught:
        generate_user_model_strategy_preview(
            strategy(),
            "保持现有设置",
            provider_code="deepseek",
            api_key=api_key,
            model_name="deepseek-v4-flash",
        )
    assert caught.value.category == "not_configured"


@pytest.mark.parametrize(
    "extra",
    [
        {"engine_key": "arbitrary_python"},
        {"public_id": "another-user-strategy"},
        {"status": "live"},
        {"version": 99},
    ],
)
def test_openai_output_cannot_add_protected_or_unknown_fields(
    monkeypatch: pytest.MonkeyPatch, extra: dict[str, Any]
) -> None:
    current = strategy()
    unsafe = {**output_for(current), **extra}
    monkeypatch.setattr(
        strategy_ai,
        "_http_transport",
        lambda *_: (200, responses_body(unsafe)),
    )

    with pytest.raises(StrategyAiError) as caught:
        generate_strategy_preview(
            current,
            "修改策略",
            api_key="sk-test-abcdefghijklmnopqrstuvwxyz",
            model="gpt-5.6-luna",
        )
    assert caught.value.category == "invalid_output"


@pytest.mark.parametrize(
    "mutator",
    [
        lambda value: {**value, "description": "详情见 https://attacker.invalid"},
        lambda value: {**value, "description": "```python\nprint('trade')\n```"},
        lambda value: {**value, "description": "api_key=abcdefghijklmnop"},
        lambda value: {
            **value,
            "parameters": {**value["parameters"], "injected_parameter": 1},
        },
        lambda value: {**value, "parameters": {**value["parameters"], "threshold": float("inf")}},
    ],
)
def test_unsafe_model_content_is_rejected(monkeypatch: pytest.MonkeyPatch, mutator: Any) -> None:
    unsafe = mutator(output_for(strategy()))
    monkeypatch.setattr(
        strategy_ai,
        "_http_transport",
        lambda *_: (200, responses_body(unsafe)),
    )

    with pytest.raises(StrategyAiError) as caught:
        generate_strategy_preview(
            strategy(),
            "修改策略",
            api_key="sk-test-abcdefghijklmnopqrstuvwxyz",
            model="gpt-5.6-luna",
        )
    assert caught.value.category == "invalid_output"


def test_strict_json_rejects_duplicate_keys_and_refusals(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    duplicate = (
        '{"name":"一","name":"二","description":"描述","category":"趋势",'
        '"parameters":{"fast_period":5,"slow_period":20,"period":14,'
        '"threshold":1.5},"risk_defaults":{"stop_loss_pct":2.0,'
        '"take_profit_pct":5.0,"leverage":3},"summary":"摘要"}'
    )
    monkeypatch.setattr(
        strategy_ai,
        "_http_transport",
        lambda *_: (200, responses_body(duplicate)),
    )
    with pytest.raises(StrategyAiError) as duplicate_error:
        generate_strategy_preview(
            strategy(),
            "改名",
            api_key="sk-test-abcdefghijklmnopqrstuvwxyz",
            model="gpt-5.6-luna",
        )
    assert duplicate_error.value.category == "invalid_output"

    refusal = json.dumps(
        {
            "status": "completed",
            "output": [
                {
                    "type": "message",
                    "content": [{"type": "refusal", "refusal": "cannot comply"}],
                }
            ],
        }
    ).encode()
    monkeypatch.setattr(strategy_ai, "_http_transport", lambda *_: (200, refusal))
    with pytest.raises(StrategyAiError) as refusal_error:
        generate_strategy_preview(
            strategy(),
            "修改策略",
            api_key="sk-test-abcdefghijklmnopqrstuvwxyz",
            model="gpt-5.6-luna",
        )
    assert refusal_error.value.category == "invalid_output"


def test_error_categories_are_redacted_and_stable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(StrategyAiError) as invalid_key_type:
        generate_strategy_preview(  # type: ignore[arg-type]
            strategy(),
            "修改策略",
            api_key=None,
            model="gpt-5.6-luna",
        )
    assert invalid_key_type.value.category == "not_configured"

    with pytest.raises(StrategyAiError) as not_configured:
        generate_strategy_preview(
            strategy(),
            "修改策略",
            api_key="short",
            model="gpt-5.6-luna",
        )
    assert not_configured.value.category == "not_configured"

    def timeout(*_: Any) -> tuple[int, bytes]:
        raise TimeoutError

    monkeypatch.setattr(strategy_ai, "_http_transport", timeout)
    with pytest.raises(StrategyAiError) as timed_out:
        generate_strategy_preview(
            strategy(),
            "修改策略",
            api_key="sk-test-abcdefghijklmnopqrstuvwxyz",
            model="gpt-5.6-luna",
        )
    assert timed_out.value.category == "timeout"

    monkeypatch.setattr(
        strategy_ai,
        "_http_transport",
        lambda *_: (429, b'{"error":{"message":"secret upstream detail"}}'),
    )
    with pytest.raises(StrategyAiError) as upstream:
        generate_strategy_preview(
            strategy(),
            "修改策略",
            api_key="sk-test-abcdefghijklmnopqrstuvwxyz",
            model="gpt-5.6-luna",
        )
    assert upstream.value.category == "upstream"
    assert "secret upstream detail" not in str(upstream.value)


def test_local_edits_still_enforce_numeric_and_content_bounds() -> None:
    with pytest.raises(StrategyAiError) as leverage:
        generate_strategy_preview(
            strategy(),
            "leverage 改为999",
            api_key="",
            model="unused",
        )
    assert leverage.value.category == "invalid_output"

    with pytest.raises(StrategyAiError) as content:
        generate_strategy_preview(
            strategy(),
            "描述改为访问 https://attacker.invalid",
            api_key="",
            model="unused",
        )
    assert content.value.category == "invalid_output"


def code_strategy() -> dict[str, Any]:
    return {
        "version": 7,
        "spec": build_trend_pullback_spec(),
        "public_id": "must-not-leak",
        "user_id": 91,
    }


def test_local_strategy_code_preview_edits_only_supported_dsl_values() -> None:
    preview = generate_strategy_code_preview(
        code_strategy(),
        "只做多，take_profit_r = 3.5，max_holding_bars = 72",
        api_key="",
        model="unused",
    )

    assert preview["provider"] == "local_semantic"
    assert preview["base_version"] == 7
    assert preview["proposed_spec"]["directions"] == ["long"]
    assert preview["proposed_spec"]["exit"]["take_profit_r"] == 3.5
    assert preview["proposed_spec"]["exit"]["max_holding_bars"] == 72
    assert "public_id" not in preview["strategy_code"]
    assert {item["path"] for item in preview["changes"]} >= {
        "spec.directions",
        "spec.exit.take_profit_r",
        "spec.exit.max_holding_bars",
    }


def test_openai_strategy_code_preview_is_structured_and_review_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current = code_strategy()
    proposed = build_trend_pullback_spec()
    proposed["exit"]["take_profit_r"] = 4.0
    model_output = {
        "strategy_code": json.dumps(proposed),
        "summary": "把止盈目标调整为 4R。",
    }
    captured: dict[str, Any] = {}

    def transport(
        body: bytes, headers: dict[str, str], timeout_seconds: float
    ) -> tuple[int, bytes]:
        captured["payload"] = json.loads(body)
        captured["headers"] = headers
        captured["timeout"] = timeout_seconds
        return 200, responses_body(model_output)

    monkeypatch.setattr(strategy_ai, "_http_transport", transport)
    preview = generate_strategy_code_preview(
        current,
        "把止盈目标改为 4R",
        api_key="sk-test-abcdefghijklmnopqrstuvwxyz",
        model="gpt-5.6-luna",
        timeout_seconds=11,
        safety_identifier="qd_strategy_code_test",
    )

    payload = captured["payload"]
    assert payload["store"] is False
    assert payload["text"]["format"]["name"] == "strategy_code_edit_preview"
    assert payload["text"]["format"]["strict"] is True
    request_text = json.dumps(payload, ensure_ascii=False)
    assert "must-not-leak" not in request_text
    assert "user_id" not in request_text
    assert preview["proposed_spec"]["exit"]["take_profit_r"] == 4.0
    assert preview["changes"] == [
        {"path": "spec.exit.take_profit_r", "before": 2.5, "after": 4.0}
    ]
    assert captured["timeout"] == 11


def test_user_model_strategy_code_preview_uses_allowlisted_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proposed = build_trend_pullback_spec()
    proposed["directions"] = ["short"]
    output = {
        "strategy_code": json.dumps(proposed),
        "summary": "改为仅做空。",
    }
    captured: dict[str, Any] = {}

    def transport(
        endpoint: Any,
        body: bytes,
        _headers: dict[str, str],
        _timeout_seconds: float,
    ) -> tuple[int, bytes]:
        captured["endpoint"] = endpoint
        captured["payload"] = json.loads(body)
        return 200, chat_completions_body(output)

    monkeypatch.setattr(strategy_ai, "_chat_http_transport", transport)
    preview = generate_user_model_strategy_code_preview(
        code_strategy(),
        "只做空",
        provider_code="deepseek",
        api_key="provider-key-abcdefghijklmnopqrstuvwxyz",
        model_name=AI_PROVIDER_PRESETS["deepseek"].default_model,
    )

    assert captured["endpoint"] is AI_PROVIDER_PRESETS["deepseek"]
    assert captured["payload"]["response_format"] == {"type": "json_object"}
    assert preview["provider"] == "deepseek"
    assert preview["proposed_spec"]["directions"] == ["short"]


def test_python_source_composer_sends_selected_indicators_as_trusted_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = '''TIMEFRAMES = ("15m",)
TRIGGER_TIMEFRAME = "15m"
LOOKBACK_BARS = 80
DIRECTIONS = ("long",)
def evaluate(context, params):
    return {"decision": "HOLD", "evidence": {"period": params["ema_fast_period"]}}
'''
    captured: dict[str, Any] = {}

    def transport(
        body: bytes, _headers: dict[str, str], _timeout_seconds: float
    ) -> tuple[int, bytes]:
        captured["payload"] = json.loads(body)
        return 200, responses_body(
            {"strategy_code": source, "summary": "按所选 EMA 与成交量生成源码。"}
        )

    monkeypatch.setattr(strategy_ai, "_http_transport", transport)
    preview = generate_strategy_source_preview(
        {"version": 1, "source_code": source},
        "EMA 趋势向上且成交量确认时做多",
        api_key="sk-test-abcdefghijklmnopqrstuvwxyz",
        model="gpt-5.6-luna",
        generation_context={
            "timeframe": "15m",
            "directions": ["long"],
            "selected_indicators": [
                {"key": "ema", "parameter_keys": ["ema_fast_period"]},
                {"key": "volume_ratio", "parameter_keys": ["volume_ratio_period"]},
            ],
            "required_parameter_keys": ["ema_fast_period", "volume_ratio_period"],
        },
    )

    user_payload = json.loads(captured["payload"]["input"][1]["content"])
    assert user_payload["indicator_blueprint"]["timeframe"] == "15m"
    assert [item["key"] for item in user_payload["indicator_blueprint"]["selected_indicators"]] == [
        "ema",
        "volume_ratio",
    ]
    assert preview["source_code"] == source


def test_user_model_python_source_composer_forwards_generation_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = '''TIMEFRAMES = ("1h",)
TRIGGER_TIMEFRAME = "1h"
LOOKBACK_BARS = 80
DIRECTIONS = ("long",)
def evaluate(context, params):
    return {"decision": "HOLD", "evidence": {"period": params["ema_fast_period"]}}
'''
    captured: dict[str, Any] = {}

    def transport(
        _endpoint: Any,
        body: bytes,
        _headers: dict[str, str],
        _timeout_seconds: float,
    ) -> tuple[int, bytes]:
        captured["payload"] = json.loads(body)
        return 200, chat_completions_body(
            {"strategy_code": source, "summary": "生成 EMA Python 源码。"}
        )

    monkeypatch.setattr(strategy_ai, "_chat_http_transport", transport)
    preview = generate_user_model_strategy_source_preview(
        {"version": 1, "source_code": source},
        "使用 EMA 生成策略",
        provider_code="deepseek",
        api_key="provider-key-abcdefghijklmnopqrstuvwxyz",
        model_name=AI_PROVIDER_PRESETS["deepseek"].default_model,
        generation_context={
            "timeframe": "1h",
            "selected_indicators": [{"key": "ema"}],
            "required_parameter_keys": ["ema_fast_period"],
        },
    )

    user_payload = json.loads(captured["payload"]["messages"][1]["content"])
    assert user_payload["indicator_blueprint"]["selected_indicators"] == [
        {"key": "ema"}
    ]
    assert captured["payload"]["thinking"] == {"type": "disabled"}
    assert captured["payload"]["max_tokens"] == 8_000
    assert preview["provider"] == "deepseek"


def test_user_model_python_source_composer_repairs_invalid_python_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    invalid_source = '''import math
TIMEFRAMES = ("1h",)
TRIGGER_TIMEFRAME = "1h"
LOOKBACK_BARS = 80
DIRECTIONS = ("long",)
def evaluate(context, params):
    return {"decision": "HOLD"}
'''
    valid_source = '''TIMEFRAMES = ("1h",)
TRIGGER_TIMEFRAME = "1h"
LOOKBACK_BARS = 80
DIRECTIONS = ("long",)
def evaluate(context, params):
    period = params["ema_fast_period"]
    return {"decision": "HOLD", "evidence": {"period": period}}
'''
    requests: list[dict[str, Any]] = []

    def transport(
        _endpoint: Any,
        body: bytes,
        _headers: dict[str, str],
        _timeout_seconds: float,
    ) -> tuple[int, bytes]:
        requests.append(json.loads(body))
        source = invalid_source if len(requests) == 1 else valid_source
        return 200, chat_completions_body(
            {"strategy_code": source, "summary": "生成并修复策略源码。"}
        )

    monkeypatch.setattr(strategy_ai, "_chat_http_transport", transport)
    preview = generate_user_model_strategy_source_preview(
        {"version": 1, "source_code": valid_source},
        "使用 EMA 生成策略",
        provider_code="deepseek",
        api_key="provider-key-abcdefghijklmnopqrstuvwxyz",
        model_name=AI_PROVIDER_PRESETS["deepseek"].default_model,
        generation_context={
            "timeframe": "1h",
            "directions": ["long"],
            "required_parameter_keys": ["ema_fast_period"],
        },
    )

    assert len(requests) == 2
    repair_payload = json.loads(requests[1]["messages"][1]["content"])
    assert "未通过平台校验" in repair_payload["edit_request"]
    assert "不得添加 import" in repair_payload["edit_request"]
    assert preview["source_code"] == valid_source


@pytest.mark.parametrize(
    "strategy_code",
    [
        "```json\n{}\n```",
        '{"python":"import os"}',
        '{"endpoint":"https://attacker.invalid"}',
    ],
)
def test_strategy_code_preview_rejects_non_dsl_content(
    monkeypatch: pytest.MonkeyPatch, strategy_code: str
) -> None:
    monkeypatch.setattr(
        strategy_ai,
        "_http_transport",
        lambda *_: (
            200,
            responses_body(
                {"strategy_code": strategy_code, "summary": "修改策略代码。"}
            ),
        ),
    )
    with pytest.raises(StrategyAiError) as caught:
        generate_strategy_code_preview(
            code_strategy(),
            "修改策略",
            api_key="sk-test-abcdefghijklmnopqrstuvwxyz",
            model="gpt-5.6-luna",
        )
    assert caught.value.category == "invalid_output"
