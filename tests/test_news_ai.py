from __future__ import annotations

import json
from typing import Any

import pytest

from quantdesk_v2 import news_ai


def chat_body(output: dict[str, Any] | str) -> bytes:
    content = output if isinstance(output, str) else json.dumps(output, ensure_ascii=False)
    return json.dumps(
        {
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {"role": "assistant", "content": content},
                }
            ]
        },
        ensure_ascii=False,
    ).encode()


def sample_items() -> list[dict[str, Any]]:
    return [
        {
            "id": "news-1",
            "ts": 1_785_922_400,
            "source": "测试源",
            "lang": "zh-CN",
            "title": "英伟达发布新一代人工智能芯片",
            "title_zh": None,
            "summary": "公司称数据中心客户需求强劲",
        },
        {
            "id": "news-2",
            "ts": 1_785_922_401,
            "source": "测试源",
            "lang": "en",
            "title": "Fed keeps rates unchanged",
            "title_zh": "美联储维持利率不变",
            "summary": "Officials remain data dependent",
        },
    ]


def analysis_output() -> dict[str, Any]:
    return {
        "analyses": [
            {
                "id": "news-1",
                "related_us_stocks": [
                    {"symbol": "NVDA", "relevance": 0.98, "direction": "bull"}
                ],
                "sentiment": "bull",
                "confidence": 0.92,
                "impact_strength": "high",
                "time_horizon": "short_term",
                "category": "company",
                "reason": "新品与需求强劲直接改善英伟达增长预期",
            },
            {
                "id": "news-2",
                "related_us_stocks": [],
                "sentiment": "neutral",
                "confidence": 0.74,
                "impact_strength": "medium",
                "time_horizon": "intraday",
                "category": "macro",
                "reason": "利率不变符合预期，对美股方向影响有限",
            },
        ]
    }


def test_analyze_news_chunk_returns_validated_us_stock_decisions(monkeypatch) -> None:
    captured: dict[str, Any] = {}

    def transport(endpoint, body, headers, timeout):
        captured["endpoint"] = endpoint
        captured["request"] = json.loads(body)
        captured["authorization"] = headers["Authorization"]
        captured["timeout"] = timeout
        return 200, chat_body(analysis_output())

    monkeypatch.setattr(news_ai, "_chat_http_transport", transport)

    result = news_ai.analyze_news_chunk(
        sample_items(),
        provider_code="deepseek",
        api_key="provider-key-abcdefghijklmnopqrstuvwxyz",
        model_name="deepseek-v4-flash",
    )

    assert result[0]["related_us_stocks"] == [
        {"symbol": "NVDA", "relevance": 0.98, "direction": "bull"}
    ]
    assert result[0]["sentiment"] == "bull"
    assert result[1]["sentiment"] == "neutral"
    assert captured["endpoint"].host == "api.deepseek.com"
    assert captured["request"]["response_format"] == {"type": "json_object"}
    assert "provider-key" not in captured["request"]["messages"][1]["content"]
    assert captured["authorization"].startswith("Bearer provider-key-")


def test_analyze_news_chunk_rejects_missing_or_invented_items(monkeypatch) -> None:
    invalid = analysis_output()
    invalid["analyses"] = invalid["analyses"][:1]
    monkeypatch.setattr(
        news_ai,
        "_chat_http_transport",
        lambda *args: (200, chat_body(invalid)),
    )

    with pytest.raises(news_ai.NewsAiError) as exc_info:
        news_ai.analyze_news_chunk(
            sample_items(),
            provider_code="deepseek",
            api_key="provider-key-abcdefghijklmnopqrstuvwxyz",
            model_name="deepseek-v4-flash",
        )

    assert exc_info.value.category == "invalid_output"


def test_batch_summary_is_structured_and_bounded(monkeypatch) -> None:
    summary = {
        "market_sentiment": "bull",
        "confidence": 0.81,
        "summary": "科技股利好占优，美股短线风险偏好改善。",
        "key_drivers": ["人工智能芯片需求强劲", "利率决定基本符合预期"],
        "focus_stocks": [
            {
                "symbol": "NVDA",
                "direction": "bull",
                "score": 0.95,
                "reason": "新品和需求同时构成直接催化",
            }
        ],
    }
    monkeypatch.setattr(
        news_ai,
        "_chat_http_transport",
        lambda *args: (200, chat_body(summary)),
    )

    result = news_ai.summarize_news_batch(
        analysis_output()["analyses"],
        provider_code="deepseek",
        api_key="provider-key-abcdefghijklmnopqrstuvwxyz",
        model_name="deepseek-v4-flash",
    )

    assert result == summary


@pytest.mark.parametrize("count", [0, 6])
def test_chunk_size_is_bounded(count: int) -> None:
    items = [sample_items()[0] | {"id": f"news-{index}"} for index in range(count)]

    with pytest.raises(news_ai.NewsAiError) as exc_info:
        news_ai.analyze_news_chunk(
            items,
            provider_code="deepseek",
            api_key="provider-key-abcdefghijklmnopqrstuvwxyz",
            model_name="deepseek-v4-flash",
        )

    assert exc_info.value.category == "empty_batch"


def test_recovery_halves_invalid_groups_before_single_item_fallback(monkeypatch) -> None:
    calls: list[list[str]] = []

    def analyze(items, **_kwargs):
        ids = [item["id"] for item in items]
        calls.append(ids)
        if len(items) > 1:
            raise news_ai.NewsAiError("invalid_output")
        if ids == ["news-2"]:
            raise news_ai.NewsAiError("upstream")
        return [{"id": ids[0]}]

    monkeypatch.setattr(news_ai, "analyze_news_chunk", analyze)

    results, failed, error = news_ai._analyze_with_recovery(
        sample_items(),
        provider_code="deepseek",
        api_key="provider-key-abcdefghijklmnopqrstuvwxyz",
        model_name="deepseek-v4-flash",
    )

    assert results == [{"id": "news-1"}]
    assert failed == 1
    assert error is not None and error.category == "upstream"
    assert calls == [["news-1", "news-2"], ["news-1"], ["news-2"]]


def test_recovery_depth_is_bounded(monkeypatch) -> None:
    calls: list[int] = []

    def analyze(items, **_kwargs):
        calls.append(len(items))
        raise news_ai.NewsAiError("timeout")

    monkeypatch.setattr(news_ai, "analyze_news_chunk", analyze)
    items = [sample_items()[0] | {"id": f"news-{index}"} for index in range(5)]

    results, failed, error = news_ai._analyze_with_recovery(
        items,
        provider_code="deepseek",
        api_key="provider-key-abcdefghijklmnopqrstuvwxyz",
        model_name="deepseek-v4-flash",
    )

    assert results == []
    assert failed == 5
    assert error is not None and error.category == "timeout"
    assert calls == [5, 2, 3]


def test_analysis_parser_accepts_common_model_aliases() -> None:
    output = {
        "items": {
            "news-1": {
                "stocks": [{"ticker": "NASDAQ:NVDA", "score": "0.91"}],
                "tone": "positive",
                "score": "0.88",
                "impact": "moderate",
                "timeframe": "short-term",
                "type": "technology",
                "explanation": "AI 需求提升芯片业务预期",
            }
        }
    }

    result = news_ai._validate_analyses(output, {"news-1"})

    assert result[0]["sentiment"] == "bull"
    assert result[0]["related_us_stocks"] == [
        {"symbol": "NVDA", "relevance": 0.91, "direction": "bull"}
    ]
    assert result[0]["impact_strength"] == "medium"
    assert result[0]["category"] == "company"
