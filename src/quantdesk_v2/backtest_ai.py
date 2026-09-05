"""Bounded AI analysis for persisted backtest results.

The model receives a compact, server-built snapshot.  Raw candles and equity
curves are deliberately excluded so the prompt stays auditable and bounded.
"""

from __future__ import annotations

import json
import math
from collections import Counter
from collections.abc import Mapping
from decimal import Decimal
from typing import Any

from .ai_providers import get_ai_provider
from .models import BacktestRun
from .strategy_ai import (
    StrategyAiError,
    _chat_http_transport,
    _chat_output_text,
    _configure_chat_json_response,
    _strict_json_bytes,
    _strict_json_text,
)

BACKTEST_ANALYSIS_SYSTEM_PROMPT = """你是 QuantDesk 的量化回测审查助手。请只依据用户消息中由服务器生成的回测快照进行分析，不得假设未提供的数据，也不得承诺未来收益。

分析要求：
1. 明确区分可验证事实、合理推断和优化建议。
2. 同时检查收益、最大回撤、夏普比率、胜率、盈亏因子、交易次数、手续费与滑点、杠杆、持仓方向分布和数据质量。
3. 高胜率不等于策略可靠；样本过少、收益由少数交易贡献、回撤过大、成本敏感或多空失衡时必须指出。
4. 参数建议必须引用当前值，说明调整方向、理由以及重新回测的验证方法；不得直接修改策略参数或触发交易。
5. 输出简体中文和严格 JSON，不要输出 Markdown，不要展示隐藏思维过程。

严格返回以下结构：
{"verdict":"可继续研究|需要优化|样本不足|风险过高","summary":"结论摘要","findings":[{"title":"发现","evidence":"数据证据","impact":"可能影响"}],"optimization_suggestions":[{"priority":"高|中|低","parameter":"参数名或策略环节","current_value":"当前值","suggestion":"建议","reason":"理由","validation":"如何重新回测验证"}],"risk_warnings":["风险提示"]}"""

_MAX_SECTION_ITEMS = 12
_MAX_TEXT_LENGTH = 1_200
_ALLOWED_VERDICTS = frozenset({"可继续研究", "需要优化", "样本不足", "风险过高"})
_ALLOWED_PRIORITIES = frozenset({"高", "中", "低"})


def _json_value(value: Any, *, depth: int = 0) -> Any:
    if depth > 5:
        return None
    if isinstance(value, Decimal):
        return float(value) if value.is_finite() else None
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, Mapping):
        items = list(value.items())[:64]
        return {str(key)[:80]: _json_value(item, depth=depth + 1) for key, item in items}
    if isinstance(value, (list, tuple)):
        return [_json_value(item, depth=depth + 1) for item in value[:64]]
    return str(value)[:_MAX_TEXT_LENGTH]


def _number(value: Any) -> float | None:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    return numeric if math.isfinite(numeric) else None


def _round_number(value: Any, digits: int = 6) -> float | None:
    numeric = _number(value)
    return round(numeric, digits) if numeric is not None else None


def build_backtest_analysis_input(run: BacktestRun) -> dict[str, Any]:
    """Build the exact, compact user payload shown in the browser and sent to AI."""

    config = run.config_json if isinstance(run.config_json, dict) else {}
    metrics = run.metrics_json if isinstance(run.metrics_json, dict) else {}
    quality = run.data_quality_json if isinstance(run.data_quality_json, dict) else {}
    metadata = run.metadata_json if isinstance(run.metadata_json, dict) else {}
    account = metadata.get("account") if isinstance(metadata.get("account"), dict) else {}
    trades = list(run.trades or [])

    winners = [trade for trade in trades if (_number(trade.net_pnl) or 0) > 0]
    losers = [trade for trade in trades if (_number(trade.net_pnl) or 0) < 0]
    net_values = [_number(trade.net_pnl) or 0.0 for trade in trades]
    return_values = [_number(trade.return_pct) or 0.0 for trade in trades]
    exit_reasons = Counter(str(trade.exit_reason or "unknown") for trade in trades)
    directions = Counter(str(trade.side or "unknown") for trade in trades)

    selected_quality_keys = (
        "source",
        "market_data_source",
        "bars_used",
        "bars_requested",
        "coverage_pct",
        "actual_start_ts",
        "actual_end_ts",
        "trades_total",
        "trades_truncated",
        "warnings",
        "assumptions",
        "missing_bars",
    )
    execution_keys = (
        "initial_capital",
        "position_size_pct",
        "leverage",
        "margin_mode",
        "fee_bps",
        "slippage_bps",
        "stop_loss_pct",
        "take_profit_pct",
        "max_holding_bars",
        "market_data_source",
        "contract_rules",
    )
    selected_metric_keys = (
        "annualized_return_pct",
        "exposure_pct",
        "total_fees",
        "gross_profit",
        "gross_loss",
        "average_trade_pnl",
        "average_holding_bars",
        "liquidation_count",
    )

    return {
        "analysis_scope": "仅分析本次已完成回测；不修改参数，不触发交易",
        "run": {
            "id": run.id,
            "strategy_name": run.strategy_name,
            "strategy_id": run.strategy_id,
            "strategy_revision_id": run.strategy_revision_id,
            "symbol": run.symbol,
            "timeframe": run.timeframe,
            "start_at": run.start_at.isoformat(),
            "end_at": run.end_at.isoformat(),
            "initial_capital": _round_number(run.initial_capital),
            "final_equity": _round_number(run.final_equity),
            "net_profit": _round_number(run.net_profit),
        },
        "core_metrics": {
            "total_return_pct": _round_number(run.total_return_pct),
            "max_drawdown_pct": _round_number(run.max_drawdown_pct),
            "sharpe_ratio": _round_number(run.sharpe_ratio),
            "win_rate_pct": _round_number(run.win_rate_pct),
            "profit_factor": _round_number(run.profit_factor),
            "trade_count": run.trade_count,
            **{
                key: _json_value(metrics.get(key))
                for key in selected_metric_keys
                if metrics.get(key) is not None
            },
        },
        "execution_and_costs": {
            key: _json_value(config.get(key))
            for key in execution_keys
            if config.get(key) is not None
        },
        "strategy_parameters": _json_value(config.get("params") or {}),
        "trade_statistics": {
            "persisted_trade_count": len(trades),
            "winner_count": len(winners),
            "loser_count": len(losers),
            "flat_count": len(trades) - len(winners) - len(losers),
            "direction_counts": dict(directions),
            "exit_reason_counts": dict(exit_reasons.most_common(_MAX_SECTION_ITEMS)),
            "average_net_pnl": round(sum(net_values) / len(net_values), 6) if net_values else None,
            "average_return_pct": (
                round(sum(return_values) / len(return_values), 6) if return_values else None
            ),
            "best_trade_net_pnl": round(max(net_values), 6) if net_values else None,
            "worst_trade_net_pnl": round(min(net_values), 6) if net_values else None,
        },
        "data_quality": {
            key: _json_value(quality.get(key))
            for key in selected_quality_keys
            if quality.get(key) is not None
        },
        "account_snapshot": {
            key: _json_value(value)
            for key, value in list(account.items())[:24]
            if key not in {"equity_curve", "price_candles"}
        },
    }


def _text(value: Any, *, maximum: int = _MAX_TEXT_LENGTH) -> str:
    if not isinstance(value, str):
        raise StrategyAiError("invalid_output")
    normalized = value.strip()
    if not normalized or len(normalized) > maximum:
        raise StrategyAiError("invalid_output")
    return normalized


def _normalize_analysis(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise StrategyAiError("invalid_output")
    verdict = _text(value.get("verdict"), maximum=16)
    if verdict not in _ALLOWED_VERDICTS:
        raise StrategyAiError("invalid_output")

    findings: list[dict[str, str]] = []
    raw_findings = value.get("findings")
    if not isinstance(raw_findings, list):
        raise StrategyAiError("invalid_output")
    for item in raw_findings[:_MAX_SECTION_ITEMS]:
        if not isinstance(item, dict):
            raise StrategyAiError("invalid_output")
        findings.append(
            {
                "title": _text(item.get("title"), maximum=120),
                "evidence": _text(item.get("evidence")),
                "impact": _text(item.get("impact")),
            }
        )

    suggestions: list[dict[str, str]] = []
    raw_suggestions = value.get("optimization_suggestions")
    if not isinstance(raw_suggestions, list):
        raise StrategyAiError("invalid_output")
    for item in raw_suggestions[:_MAX_SECTION_ITEMS]:
        if not isinstance(item, dict):
            raise StrategyAiError("invalid_output")
        priority = _text(item.get("priority"), maximum=4)
        if priority not in _ALLOWED_PRIORITIES:
            raise StrategyAiError("invalid_output")
        current_value = item.get("current_value")
        suggestions.append(
            {
                "priority": priority,
                "parameter": _text(item.get("parameter"), maximum=120),
                "current_value": _text(
                    "--" if current_value is None else str(current_value),
                    maximum=240,
                ),
                "suggestion": _text(item.get("suggestion")),
                "reason": _text(item.get("reason")),
                "validation": _text(item.get("validation")),
            }
        )

    warnings = value.get("risk_warnings")
    if not isinstance(warnings, list):
        raise StrategyAiError("invalid_output")
    return {
        "verdict": verdict,
        "summary": _text(value.get("summary"), maximum=2_000),
        "findings": findings,
        "optimization_suggestions": suggestions,
        "risk_warnings": [_text(item) for item in warnings[:_MAX_SECTION_ITEMS]],
    }


def analyze_backtest_with_model(
    model_input: Mapping[str, Any],
    *,
    provider_code: str,
    api_key: str,
    model_name: str,
    timeout_seconds: float,
    max_tokens: int = 4_000,
) -> dict[str, Any]:
    preset = get_ai_provider(provider_code)
    if preset is None or not api_key or not model_name:
        raise StrategyAiError("not_configured")
    user_content = (
        "以下 JSON 是服务器从已持久化回测记录生成的只读快照。请按系统要求审查：\n"
        + json.dumps(model_input, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    )
    payload: dict[str, Any] = {
        "model": model_name,
        "messages": [
            {"role": "system", "content": BACKTEST_ANALYSIS_SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ],
        "stream": False,
        "temperature": 0.2,
    }
    _configure_chat_json_response(payload, provider_code, max_tokens=max_tokens)
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode(
        "utf-8"
    )
    try:
        status_code, response_body = _chat_http_transport(
            preset,
            body,
            {
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            timeout_seconds,
        )
    except TimeoutError:
        raise StrategyAiError("timeout") from None
    except OSError:
        raise StrategyAiError("upstream") from None
    if status_code in {408, 504}:
        raise StrategyAiError("timeout")
    if not 200 <= status_code < 300:
        raise StrategyAiError("upstream")
    response = _strict_json_bytes(response_body)
    return _normalize_analysis(_strict_json_text(_chat_output_text(response)))
