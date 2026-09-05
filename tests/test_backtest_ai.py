from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

from quantdesk_v2 import backtest_ai


def _run_fixture() -> SimpleNamespace:
    trade = SimpleNamespace(
        net_pnl=Decimal("12.50"),
        return_pct=Decimal("1.25"),
        exit_reason="take_profit",
        side="long",
    )
    return SimpleNamespace(
        id=17,
        strategy_name="测试趋势策略",
        strategy_id="trend_test",
        strategy_revision_id=9,
        symbol="BTCUSDT",
        timeframe="15m",
        start_at=datetime(2026, 8, 1, tzinfo=UTC),
        end_at=datetime(2026, 9, 1, tzinfo=UTC),
        initial_capital=Decimal("1000"),
        final_equity=Decimal("1012.50"),
        net_profit=Decimal("12.50"),
        total_return_pct=Decimal("1.25"),
        max_drawdown_pct=Decimal("3.10"),
        sharpe_ratio=Decimal("0.82"),
        win_rate_pct=Decimal("55.0"),
        profit_factor=Decimal("1.20"),
        trade_count=1,
        status="completed",
        config_json={
            "initial_capital": 1000,
            "leverage": 10,
            "fee_bps": 4,
            "slippage_bps": 2,
            "market_data_source": "binance",
            "params": {"fast_period": 12, "slow_period": 26},
        },
        metrics_json={"total_fees": 0.8, "average_holding_bars": 8},
        data_quality_json={"source": "Binance", "bars_used": 2880, "coverage_pct": 100},
        metadata_json={
            "account": {
                "final_balance": 1012.5,
                "equity_curve": [1000, 1012.5],
                "price_candles": [{"close": 100}],
            }
        },
        trades=[trade],
    )


def test_analysis_input_uses_persisted_summary_without_raw_chart_series() -> None:
    model_input = backtest_ai.build_backtest_analysis_input(_run_fixture())

    assert model_input["run"]["strategy_name"] == "测试趋势策略"
    assert model_input["core_metrics"]["sharpe_ratio"] == 0.82
    assert model_input["execution_and_costs"]["leverage"] == 10
    assert model_input["strategy_parameters"] == {"fast_period": 12, "slow_period": 26}
    assert model_input["trade_statistics"]["direction_counts"] == {"long": 1}
    assert model_input["trade_statistics"]["exit_reason_counts"] == {"take_profit": 1}
    assert model_input["data_quality"]["bars_used"] == 2880
    assert model_input["account_snapshot"] == {"final_balance": 1012.5}
    serialized = json.dumps(model_input, ensure_ascii=False)
    assert "equity_curve" not in serialized
    assert "price_candles" not in serialized


def test_model_analysis_uses_system_prompt_and_normalizes_json(monkeypatch) -> None:
    captured: dict[str, object] = {}
    analysis = {
        "verdict": "需要优化",
        "summary": "收益为正，但样本较少且成本敏感。",
        "findings": [
            {"title": "样本不足", "evidence": "仅 1 笔交易", "impact": "结论不稳定"}
        ],
        "optimization_suggestions": [
            {
                "priority": "高",
                "parameter": "fast_period",
                "current_value": 12,
                "suggestion": "扩大候选区间后做滚动验证",
                "reason": "降低单一区间过拟合",
                "validation": "执行样本外和成本敏感性测试",
            }
        ],
        "risk_warnings": ["不要仅凭一次回测部署实盘"],
    }

    def fake_transport(preset, body, headers, timeout_seconds):
        captured["preset"] = preset
        captured["payload"] = json.loads(body.decode("utf-8"))
        captured["headers"] = headers
        captured["timeout"] = timeout_seconds
        response = {"choices": [{"message": {"content": json.dumps(analysis, ensure_ascii=False)}}]}
        return 200, json.dumps(response, ensure_ascii=False).encode("utf-8")

    monkeypatch.setattr(backtest_ai, "_chat_http_transport", fake_transport)
    result = backtest_ai.analyze_backtest_with_model(
        backtest_ai.build_backtest_analysis_input(_run_fixture()),
        provider_code="deepseek",
        api_key="secret",
        model_name="deepseek-chat",
        timeout_seconds=30,
    )

    payload = captured["payload"]
    assert isinstance(payload, dict)
    assert payload["messages"][0] == {
        "role": "system",
        "content": backtest_ai.BACKTEST_ANALYSIS_SYSTEM_PROMPT,
    }
    assert "测试趋势策略" in payload["messages"][1]["content"]
    assert result["verdict"] == "需要优化"
    assert result["optimization_suggestions"][0]["current_value"] == "12"
