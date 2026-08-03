from __future__ import annotations

import json
from typing import Any

import pytest

from quantdesk_v2 import strategy_ai
from quantdesk_v2.strategy_ai import StrategyAiError, generate_strategy_preview


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
