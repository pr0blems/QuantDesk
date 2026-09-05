from __future__ import annotations

import copy
import math
import uuid
from collections.abc import Mapping, Sequence
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.orm import Session

from .domain.martingale_tp4 import strategy_parameters_from_catalog_parameters
from .models import (
    AiMonitorConfig,
    StrategyRevision,
    StrategyTemplate,
    UserStrategy,
    utcnow,
)
from .strategy_runtime import (
    TREND_PULLBACK_SPEC_V1,
    build_trend_pullback_spec,
    full_strategy_parameter_schema,
    strategy_spec_hash,
)


class StrategyParameterError(ValueError):
    """Raised when persisted or user-supplied strategy parameters are unsafe."""


def _martingale_parameter_schema() -> list[dict[str, Any]]:
    """Flat strategy-center schema with lossless MQ4-compatible values."""

    def number(
        key: str,
        label: str,
        default: int | float,
        minimum: int | float,
        maximum: int | float,
        group: str,
        *,
        integer: bool = False,
        switch: bool = False,
        options: list[dict[str, Any]] | None = None,
        help: str | None = None,
    ) -> dict[str, Any]:
        item: dict[str, Any] = {
            "key": key,
            "label": label,
            "type": "integer" if integer or switch else "number",
            "default": default,
            "min": minimum,
            "max": maximum,
            "group": group,
        }
        if switch:
            item["control"] = "switch"
        if options is not None:
            item["options"] = options
        if help:
            item["help"] = help
        return item

    return [
        number("ChooseTrading", "交易模式", 0, 0, 2, "运行模式", integer=True, options=[
            {"value": 0, "label": "自动突破"},
            {"value": 1, "label": "恢复模式"},
            {"value": 2, "label": "网格模式"},
        ]),
        number("NewCycle", "允许新周期", 1, 0, 1, "运行模式", switch=True),
        number("Lot", "初始手数", 0.01, 0.000001, 1_000_000, "仓位递增"),
        number("Autolot", "自动手数", 0, 0, 1, "仓位递增", switch=True),
        number("Autolotsize", "自动手数余额单位", 10_000, 0.01, 1_000_000_000, "仓位递增"),
        number("mm", "手数倍增系数", 2, 0.01, 100, "仓位递增"),
        number("MaxLot", "单腿最大手数", 100, 0.000001, 1_000_000, "仓位递增"),
        number("MaxOrders", "最大订单数", 16, 1, 100, "仓位递增", integer=True),
        number(
            "GridDrift",
            "第几手后切换网格",
            100,
            1,
            100_000,
            "仓位递增",
            integer=True,
            help="已有 N 手后，从第 N+1 手开始按网格模式加仓；若 N 不小于最大订单数，本轮不会切换。",
        ),
        number("Distance", "网格间距（点）", 150, 0.000001, 1_000_000_000, "仓位递增"),
        number("MaxSpred", "最大点差（点）", 50, 0, 1_000_000_000, "执行约束"),
        number("TP", "基础止盈（点）", 100, 0.000001, 1_000_000_000, "分级止盈"),
        number("Kol_Ord_for_TP2", "TP2 起始订单数", 2, 1, 100_000, "分级止盈", integer=True),
        number("TP2", "TP2（点）", 80, 0.000001, 1_000_000_000, "分级止盈"),
        number("Kol_Ord_for_TP3", "TP3 起始订单数", 5, 1, 100_000, "分级止盈", integer=True),
        number("TP3", "TP3（点）", 50, 0.000001, 1_000_000_000, "分级止盈"),
        number("Kol_Ord_for_TP4", "TP4 起始订单数", 7, 1, 100_000, "分级止盈", integer=True),
        number("TP4", "TP4（点）", 30, 0.000001, 1_000_000_000, "分级止盈"),
        number("SL_Dollar", "篮子金额止损", 0, 0, 1_000_000_000, "止损与追踪"),
        number("TrailStart", "追踪启动（点）", 600, 0, 1_000_000_000, "止损与追踪"),
        number("TrailDistance", "追踪距离（点）", 100, 0.000001, 1_000_000_000, "止损与追踪"),
        number("Overlap", "启用首尾覆盖", 1, 0, 1, "首尾覆盖", switch=True),
        number("OverlapOrderNumber", "覆盖最少订单数", 7, 2, 100_000, "首尾覆盖", integer=True),
        number("OverlapPercent", "额外覆盖比例（%）", 11, 0, 1000, "首尾覆盖"),
        number("Start_Hour", "自动周期开始小时", 1, 0, 23, "运行时段", integer=True),
        number("End_Hour", "自动周期结束小时", 23, 0, 23, "运行时段", integer=True),
        number("BoxLength", "箱体长度", 22, 2, 999, "突破箱体", integer=True),
        number("BoxTimeFrameMinutes", "箱体周期", 15, 1, 60, "突破箱体", integer=True, options=[
            {"value": 1, "label": "1 分钟"},
            {"value": 5, "label": "5 分钟"},
            {"value": 15, "label": "15 分钟"},
            {"value": 30, "label": "30 分钟"},
            {"value": 60, "label": "1 小时"},
        ]),
        number("BoxRange", "固定箱体范围（点）", 30, 0.000001, 1_000_000_000, "突破箱体"),
        number("AutoBoxRange", "ATR 自适应箱体", 1, 0, 1, "突破箱体", switch=True),
        number("AutoBoxRangeDailyATRperiod", "日线 ATR 周期", 30, 2, 365, "突破箱体", integer=True),
        number("AutoBoxRangeDailyATRfactor", "日线 ATR 系数", 0.2, 0.000001, 10, "突破箱体"),
        number("BoxBufferPips", "箱体缓冲（点）", 5, 0, 1_000_000_000, "突破箱体"),
        number("Magic", "MQ4 Magic", 201_800, 0, 2_147_483_647, "MQ4 兼容", integer=True),
        number("Section", "旧版图表区间（点）", 1000, 0, 1_000_000_000, "MQ4 兼容"),
        number("ShowStat", "保留统计显示设置", 1, 0, 1, "MQ4 兼容", switch=True),
        number("ShowButton", "保留平仓按钮设置", 1, 0, 1, "MQ4 兼容", switch=True),
        number("ShowMainSetting", "保留主设置显示", 1, 0, 1, "MQ4 兼容", switch=True),
    ]


ENGINE_PARAMETER_SCHEMAS: dict[str, list[dict[str, Any]]] = {
    "martingale_tp4": _martingale_parameter_schema(),
    "multi_factor": [
        {
            "key": "fast_period",
            "label": "快线周期",
            "type": "integer",
            "default": 20,
            "min": 2,
            "max": 200,
        },
        {
            "key": "slow_period",
            "label": "慢线周期",
            "type": "integer",
            "default": 50,
            "min": 3,
            "max": 500,
        },
        {
            "key": "rsi_period",
            "label": "RSI 周期",
            "type": "integer",
            "default": 14,
            "min": 2,
            "max": 100,
        },
        {
            "key": "threshold",
            "label": "入场分数",
            "type": "number",
            "default": 2,
            "min": 1,
            "max": 4,
        },
    ],
    "ma_cross": [
        {
            "key": "fast_period",
            "label": "快线周期",
            "type": "integer",
            "default": 20,
            "min": 2,
            "max": 200,
        },
        {
            "key": "slow_period",
            "label": "慢线周期",
            "type": "integer",
            "default": 50,
            "min": 3,
            "max": 500,
        },
    ],
    "macd_momentum": [
        {
            "key": "fast_period",
            "label": "快线周期",
            "type": "integer",
            "default": 12,
            "min": 2,
            "max": 100,
        },
        {
            "key": "slow_period",
            "label": "慢线周期",
            "type": "integer",
            "default": 26,
            "min": 3,
            "max": 200,
        },
        {
            "key": "signal_period",
            "label": "信号周期",
            "type": "integer",
            "default": 9,
            "min": 2,
            "max": 100,
        },
    ],
    "rsi_reversal": [
        {
            "key": "period",
            "label": "RSI 周期",
            "type": "integer",
            "default": 14,
            "min": 2,
            "max": 100,
        },
        {
            "key": "oversold",
            "label": "超卖线",
            "type": "number",
            "default": 30,
            "min": 1,
            "max": 49,
        },
        {
            "key": "overbought",
            "label": "超买线",
            "type": "number",
            "default": 70,
            "min": 51,
            "max": 99,
        },
    ],
    "bollinger_reversion": [
        {
            "key": "period",
            "label": "统计周期",
            "type": "integer",
            "default": 20,
            "min": 3,
            "max": 300,
        },
        {
            "key": "stddev",
            "label": "标准差倍数",
            "type": "number",
            "default": 2,
            "min": 0.5,
            "max": 5,
        },
    ],
    "strategy_dsl": full_strategy_parameter_schema(),
}

DEFAULT_RISK: dict[str, int | float] = {
    "position_size_pct": 10,
    "leverage": 2,
    "fee_bps": 5,
    "slippage_bps": 2,
    "stop_loss_pct": 2,
    "take_profit_pct": 5,
    "max_holding_bars": 120,
}


AI_MONITOR_DECISION_VERSION = "actionable_entry_v11"
AI_MONITOR_STRATEGY_TEMPLATE_KEY = "ai_monitor_actionable_entry_v11"
AI_MONITOR_DEFAULT_INDICATOR_KEYS = (
    "moving_average_bull",
    "prediction_trend",
)
AI_MONITOR_INDICATOR_LABELS: tuple[tuple[str, str], ...] = (
    ("bollinger_breakout", "布林突破"),
    ("low_volume_pullback", "缩量回踩"),
    ("ma_golden_cross", "均线金叉"),
    ("macd_golden_cross_volume", "MACD 金叉放量"),
    ("moving_average_bull", "均线多头"),
    ("moving_average_pullback_bounce", "均线回踩反弹"),
    ("new_low_reversal", "新低反转"),
    ("oversold_bounce", "超跌反弹"),
    ("oversold_reversal", "超跌反转"),
    ("prediction_aggressive_flow", "主动成交资金流"),
    ("prediction_book_imbalance", "盘口失衡"),
    ("prediction_book_imbalance_5", "前五档盘口失衡"),
    ("prediction_flash_imbalance", "瞬时盘口失衡"),
    ("prediction_price_oi_impulse", "价格与持仓量脉冲"),
    ("prediction_taker_flow", "Taker 资金流"),
    ("prediction_trend", "预测趋势"),
    ("prediction_velocity", "价格速度"),
    ("price_volume_rise", "量价齐升"),
    ("strong_gap_open", "强势跳空"),
    ("trend_breakout", "趋势突破"),
)


def _ai_monitor_parameter_schema() -> list[dict[str, Any]]:
    schema: list[dict[str, Any]] = [
        {"key": "monitor_enabled", "label": "自动监控", "type": "integer", "default": 0, "min": 0, "max": 1, "group": "运行设置", "control": "switch", "help": "开启后，AI Monitor 将按决策周期自动生成机会。"},
        {"key": "timeframe_minutes", "label": "决策周期（分钟）", "type": "integer", "default": 60, "min": 15, "max": 240, "group": "运行设置"},
        {"key": "prediction_max_holding_bars", "label": "最大持有 K 线", "type": "integer", "default": 4, "min": 1, "max": 24, "group": "运行设置"},
        {"key": "minimum_news_confidence", "label": "最低新闻置信度", "type": "number", "default": 0.6, "min": 0, "max": 1, "step": 0.01, "group": "准入门槛"},
        {"key": "minimum_news_mentions", "label": "最低新闻数量", "type": "integer", "default": 1, "min": 1, "max": 20, "group": "准入门槛"},
        {"key": "minimum_indicator_score", "label": "最低技术评分", "type": "number", "default": 65, "min": 0, "max": 100, "step": 0.1, "group": "准入门槛"},
        {"key": "minimum_combined_score", "label": "最低组合评分", "type": "number", "default": 75, "min": 75, "max": 100, "step": 0.1, "group": "准入门槛"},
        {"key": "maximum_market_age_seconds", "label": "最大行情延迟（秒）", "type": "integer", "default": 120, "min": 5, "max": 3600, "group": "数据质量与成本"},
        {"key": "minimum_feature_quality", "label": "最低特征质量", "type": "number", "default": 0.7, "min": 0, "max": 1, "step": 0.01, "group": "数据质量与成本"},
        {"key": "minimum_market_flow_quality", "label": "最低盘口质量", "type": "number", "default": 0.5, "min": 0, "max": 1, "step": 0.01, "group": "数据质量与成本"},
        {"key": "minimum_calibration_samples", "label": "最低校准样本数", "type": "integer", "default": 1000, "min": 30, "max": 5000, "group": "数据质量与成本"},
        {"key": "live_safety_margin_bps", "label": "成本安全边际（bp）", "type": "number", "default": 10, "min": 0, "max": 500, "step": 0.1, "group": "数据质量与成本"},
        {"key": "news_score_weight", "label": "新闻评分权重（%）", "type": "number", "default": 20, "min": 0, "max": 100, "step": 0.1, "group": "评分权重"},
        {"key": "technical_score_weight", "label": "技术评分权重（%）", "type": "number", "default": 50, "min": 0, "max": 100, "step": 0.1, "group": "评分权重"},
        {"key": "market_flow_score_weight", "label": "盘口评分权重（%）", "type": "number", "default": 30, "min": 0, "max": 100, "step": 0.1, "group": "评分权重"},
    ]
    schema.extend(
        {
            "key": f"indicator_{key}",
            "label": f"启用指标：{label}",
            "type": "integer",
            "default": int(key in AI_MONITOR_DEFAULT_INDICATOR_KEYS),
            "min": 0,
            "max": 1,
            "group": "技术指标开关",
            "control": "switch",
        }
        for key, label in AI_MONITOR_INDICATOR_LABELS
    )
    return schema


AI_MONITOR_STRATEGY_PARAMETER_SCHEMA = _ai_monitor_parameter_schema()


def _ai_monitor_default_parameters() -> dict[str, int | float]:
    return {
        str(item["key"]): copy.deepcopy(item["default"])
        for item in AI_MONITOR_STRATEGY_PARAMETER_SCHEMA
    }


def ai_monitor_strategy_parameters(
    config: AiMonitorConfig | None,
) -> dict[str, int | float]:
    """Return the exact editable AI Monitor policy values stored in strategy center."""

    values = _ai_monitor_default_parameters()
    if config is None:
        return values
    timeframe_minutes = {"15m": 15, "1h": 60, "4h": 240}.get(config.timeframe, 60)
    values.update(
        {
            "monitor_enabled": int(bool(config.enabled)),
            "timeframe_minutes": timeframe_minutes,
            "prediction_max_holding_bars": int(config.prediction_max_holding_bars),
            "minimum_news_confidence": float(config.minimum_news_confidence),
            "minimum_news_mentions": int(config.minimum_news_mentions),
            "minimum_indicator_score": float(config.minimum_indicator_score),
            "minimum_combined_score": max(75.0, float(config.minimum_combined_score)),
            "maximum_market_age_seconds": int(config.maximum_market_age_seconds),
            "minimum_feature_quality": float(config.minimum_feature_quality),
            "minimum_market_flow_quality": float(config.minimum_market_flow_quality),
            "minimum_calibration_samples": int(config.minimum_calibration_samples),
            "live_safety_margin_bps": float(config.live_safety_margin_bps),
            "news_score_weight": float(config.news_score_weight),
            "technical_score_weight": float(config.technical_score_weight),
            "market_flow_score_weight": float(config.market_flow_score_weight),
        }
    )
    enabled_indicators = set(
        config.indicator_keys_json or AI_MONITOR_DEFAULT_INDICATOR_KEYS
    )
    for key, _label in AI_MONITOR_INDICATOR_LABELS:
        values[f"indicator_{key}"] = int(key in enabled_indicators)
    return values


def validate_ai_monitor_strategy_parameters(
    parameters: Mapping[str, Any],
) -> dict[str, int | float]:
    """Validate the managed policy contract before it can affect live monitoring."""

    if not isinstance(parameters, Mapping):
        raise StrategyParameterError("AI 机会决策参数必须是对象")
    definitions = {
        str(item["key"]): item for item in AI_MONITOR_STRATEGY_PARAMETER_SCHEMA
    }
    unknown = sorted(set(parameters) - set(definitions))
    missing = sorted(set(definitions) - set(parameters))
    if unknown:
        raise StrategyParameterError(f"未知 AI 机会决策参数：{', '.join(unknown)}")
    if missing:
        raise StrategyParameterError(f"缺少 AI 机会决策参数：{', '.join(missing)}")
    normalized: dict[str, int | float] = {}
    for key, definition in definitions.items():
        raw = parameters[key]
        if isinstance(raw, bool) or not isinstance(raw, (int, float, Decimal)):
            raise StrategyParameterError(f"AI 机会决策参数 {key} 必须是数字")
        numeric = float(raw)
        if not math.isfinite(numeric):
            raise StrategyParameterError(f"AI 机会决策参数 {key} 必须是有限数字")
        if definition["type"] == "integer":
            if not numeric.is_integer():
                raise StrategyParameterError(f"AI 机会决策参数 {key} 必须是整数")
            value: int | float = int(numeric)
        else:
            value = int(numeric) if numeric.is_integer() else numeric
        if value < definition["min"] or value > definition["max"]:
            raise StrategyParameterError(
                f"AI 机会决策参数 {key} 必须在 {definition['min']} 到 {definition['max']} 之间"
            )
        normalized[key] = value
    if normalized["timeframe_minutes"] not in {15, 60, 240}:
        raise StrategyParameterError("决策周期只能是 15、60 或 240 分钟")
    weight_total = sum(
        float(normalized[key])
        for key in (
            "news_score_weight",
            "technical_score_weight",
            "market_flow_score_weight",
        )
    )
    if not math.isclose(weight_total, 100.0, abs_tol=0.01):
        raise StrategyParameterError("新闻、技术和盘口评分权重合计必须为 100%")
    indicator_keys = [
        key
        for key, _label in AI_MONITOR_INDICATOR_LABELS
        if normalized[f"indicator_{key}"] == 1
    ]
    if not indicator_keys:
        raise StrategyParameterError("至少启用一个 AI 机会决策指标")
    return normalized


def is_ai_monitor_strategy(strategy: UserStrategy) -> bool:
    template = strategy.source_template
    if template is not None and template.template_key == AI_MONITOR_STRATEGY_TEMPLATE_KEY:
        return True
    spec = strategy.spec_json if isinstance(strategy.spec_json, Mapping) else {}
    return spec.get("managed_policy") == AI_MONITOR_STRATEGY_TEMPLATE_KEY


def strategy_management_mode(strategy: UserStrategy) -> str:
    """Expose one strategy-center contract without changing runtime dispatch."""

    if is_ai_monitor_strategy(strategy):
        return "managed_parameters"
    if strategy.strategy_kind == "source_strategy":
        return "python_source"
    if strategy.strategy_kind == "full_strategy":
        return "strategy_dsl"
    if strategy.strategy_kind == "basket_strategy":
        return "basket_parameters"
    return "parameterized_engine"


def apply_ai_monitor_strategy_parameters(
    db: Session,
    user_id: int,
    parameters: Mapping[str, Any],
) -> AiMonitorConfig:
    """Make a strategy-center edit the source of truth used by AI Monitor."""

    normalized = validate_ai_monitor_strategy_parameters(parameters)
    config = db.get(AiMonitorConfig, user_id)
    indicator_keys = [
        key
        for key, _label in AI_MONITOR_INDICATOR_LABELS
        if normalized[f"indicator_{key}"] == 1
    ]
    if config is None:
        config = AiMonitorConfig(
            user_id=user_id,
            indicator_keys_json=indicator_keys,
            monitor_symbols_json=[],
        )
        db.add(config)
    config.enabled = bool(normalized["monitor_enabled"])
    config.timeframe = {15: "15m", 60: "1h", 240: "4h"}[
        int(normalized["timeframe_minutes"])
    ]
    config.prediction_max_holding_bars = int(
        normalized["prediction_max_holding_bars"]
    )
    config.indicator_keys_json = indicator_keys
    config.minimum_news_confidence = Decimal(
        str(normalized["minimum_news_confidence"])
    )
    config.minimum_news_mentions = int(normalized["minimum_news_mentions"])
    config.minimum_indicator_score = Decimal(
        str(normalized["minimum_indicator_score"])
    )
    config.minimum_combined_score = Decimal(
        str(normalized["minimum_combined_score"])
    )
    config.maximum_market_age_seconds = int(
        normalized["maximum_market_age_seconds"]
    )
    config.minimum_feature_quality = Decimal(
        str(normalized["minimum_feature_quality"])
    )
    config.minimum_market_flow_quality = Decimal(
        str(normalized["minimum_market_flow_quality"])
    )
    config.minimum_calibration_samples = int(
        normalized["minimum_calibration_samples"]
    )
    config.live_safety_margin_bps = Decimal(
        str(normalized["live_safety_margin_bps"])
    )
    config.news_score_weight = Decimal(str(normalized["news_score_weight"]))
    config.technical_score_weight = Decimal(
        str(normalized["technical_score_weight"])
    )
    config.market_flow_score_weight = Decimal(
        str(normalized["market_flow_score_weight"])
    )
    db.flush()
    return config


def _template(
    template_key: str,
    name: str,
    category: str,
    description: str,
    engine_key: str,
    parameters: dict[str, int | float],
    risk_defaults: dict[str, int | float] | None = None,
    parameter_schema: list[dict[str, Any]] | None = None,
    version: int = 1,
    template_kind: str = "builtin_strategy",
    spec: dict[str, Any] | None = None,
    implementation_version: str = "builtin_v1",
) -> dict[str, Any]:
    return {
        "template_key": template_key,
        "name": name,
        "category": category,
        "description": description,
        "engine_key": engine_key,
        "parameter_schema_json": copy.deepcopy(
            parameter_schema or ENGINE_PARAMETER_SCHEMAS[engine_key]
        ),
        "parameters_json": parameters,
        "risk_defaults_json": copy.deepcopy(risk_defaults or DEFAULT_RISK),
        "template_kind": template_kind,
        "spec_schema_version": int(spec["schema_version"]) if spec else None,
        "spec_json": copy.deepcopy(spec),
        "implementation_version": implementation_version,
        "deprecated_at": None,
        "version": version,
        "is_active": True,
    }


SYSTEM_STRATEGY_DEFINITIONS: tuple[dict[str, Any], ...] = (
    _template(
        AI_MONITOR_STRATEGY_TEMPLATE_KEY,
        "AI 机会决策策略",
        "AI 机会",
        (
            "发现机会当前实际运行的准入策略。决策版本 actionable_entry_v11，融合新闻、"
            "技术指标、盘口资金、宏观环境和数据质量生成多空候选；本策略参数与 AI Monitor "
            "双向同步，风险仓位与真实下单继续由统一执行内核管理。"
        ),
        "multi_factor",
        _ai_monitor_default_parameters(),
        parameter_schema=AI_MONITOR_STRATEGY_PARAMETER_SCHEMA,
        version=11,
        implementation_version=AI_MONITOR_DECISION_VERSION,
    ),
    _template(
        "trend_pullback_continuation_v1",
        "多周期趋势回踩延续",
        "完整策略",
        (
            "4h EMA 与 ADX 识别趋势，1h 等待 EMA/ATR 回踩，15m 通过结构突破和成交量确认入场；"
            "包含 ATR 止损、R 倍数止盈、持仓期限和组合风险边界。"
        ),
        "strategy_dsl",
        copy.deepcopy(TREND_PULLBACK_SPEC_V1["parameters"]),
        {
            "position_size_pct": 10,
            "leverage": 5,
            "fee_bps": 5,
            "slippage_bps": 3,
            "stop_loss_pct": 3,
            "take_profit_pct": 7.5,
            "max_holding_bars": 48,
        },
        parameter_schema=full_strategy_parameter_schema(),
        version=1,
        template_kind="strategy",
        spec=build_trend_pullback_spec(),
        implementation_version="strategy_runtime_v1",
    ),
    _template(
        "trend_breakout",
        "趋势突破",
        "趋势",
        "以快慢趋势、RSI 与综合评分确认突破，适合趋势启动阶段。",
        "multi_factor",
        {"fast_period": 20, "slow_period": 50, "rsi_period": 14, "threshold": 3},
    ),
    _template(
        "ma_golden_cross",
        "MA 金叉",
        "趋势",
        "快均线上穿慢均线时跟随趋势，并在反向交叉时退出或反向。",
        "ma_cross",
        {"fast_period": 20, "slow_period": 60},
    ),
    _template(
        "macd_golden_cross_volume",
        "MACD 金叉放量",
        "动量",
        "使用 MACD 动量交叉作为可回测信号，成交量条件由行情接入层继续扩展。",
        "macd_momentum",
        {"fast_period": 12, "slow_period": 26, "signal_period": 9},
    ),
    _template(
        "price_volume_rise",
        "量价齐升",
        "动量",
        "用短中期趋势与动量评分代理量价同步走强，避免执行任意策略代码。",
        "multi_factor",
        {"fast_period": 10, "slow_period": 30, "rsi_period": 14, "threshold": 3},
    ),
    _template(
        "low_volatility_leader",
        "低波动龙头",
        "稳健",
        "在较长统计窗口内寻找低波动后的均值回归机会。",
        "bollinger_reversion",
        {"period": 30, "stddev": 1.5},
    ),
    _template(
        "break_board_reversal",
        "断板反包",
        "反转",
        "使用短周期 RSI 恢复信号代理快速回撤后的反包行情。",
        "rsi_reversal",
        {"period": 9, "oversold": 35, "overbought": 75},
    ),
    _template(
        "oversold_bounce",
        "超跌反弹",
        "反转",
        "价格进入深度超卖区后，等待 RSI 离开超卖区再参与反弹。",
        "rsi_reversal",
        {"period": 14, "oversold": 25, "overbought": 70},
    ),
    _template(
        "bollinger_breakout",
        "布林突破",
        "突破",
        "以布林带波动区间及重新回到带内的确认信号控制追涨风险。",
        "bollinger_reversion",
        {"period": 20, "stddev": 2.5},
    ),
    _template(
        "moving_average_bull",
        "均线多头",
        "趋势",
        "短期均线保持在长期均线上方时顺势交易。",
        "ma_cross",
        {"fast_period": 10, "slow_period": 30},
    ),
    _template(
        "consecutive_limit_up",
        "连板股",
        "动量",
        "用快速 MACD 动量代理连续强势行情，适合高波动标的研究。",
        "macd_momentum",
        {"fast_period": 8, "slow_period": 21, "signal_period": 5},
    ),
    _template(
        "low_volume_pullback",
        "缩量回踩",
        "趋势",
        "以中期均线趋势代理回踩后的重新转强信号。",
        "ma_cross",
        {"fast_period": 20, "slow_period": 60},
    ),
    _template(
        "new_low_reversal",
        "新低反转",
        "反转",
        "创新低并进入极端超卖区后，等待 RSI 回升确认。",
        "rsi_reversal",
        {"period": 14, "oversold": 20, "overbought": 65},
    ),
    _template(
        "high_turnover_surge",
        "高换手拉升",
        "动量",
        "用快速 MACD 动量变化代理高换手拉升阶段。",
        "macd_momentum",
        {"fast_period": 6, "slow_period": 18, "signal_period": 5},
    ),
    _template(
        "consecutive_board_relay",
        "连板接力",
        "动量",
        "使用更灵敏的 MACD 参数研究强势行情的接力持续性。",
        "macd_momentum",
        {"fast_period": 5, "slow_period": 13, "signal_period": 4},
    ),
    _template(
        "near_limit_up",
        "逼近涨停",
        "突破",
        "以短周期多因子高评分代理快速逼近极端涨幅的行情。",
        "multi_factor",
        {"fast_period": 5, "slow_period": 20, "rsi_period": 9, "threshold": 3},
    ),
    _template(
        "oversold_reversal",
        "超跌反转",
        "反转",
        "采用更灵敏的 RSI 周期确认深度超跌后的方向反转。",
        "rsi_reversal",
        {"period": 9, "oversold": 20, "overbought": 75},
    ),
    _template(
        "moving_average_pullback_bounce",
        "均线回踩反弹",
        "趋势",
        "以短长均线重新交叉确认回踩后的趋势恢复。",
        "ma_cross",
        {"fast_period": 10, "slow_period": 50},
    ),
    _template(
        "strong_gap_open",
        "强势高开",
        "突破",
        "用短周期趋势、RSI 与评分阈值代理高开后的强势延续。",
        "multi_factor",
        {"fast_period": 5, "slow_period": 20, "rsi_period": 9, "threshold": 3},
    ),
    _template(
        "paper_multifactor_atr_v1",
        "AI 模拟盘 ATR 趋势",
        "模拟盘",
        (
            "系统模拟盘默认策略：在 4h 周期综合 MA20/MA50、MACD、RSI 与布林带，"
            "评分达到 3 时入场；默认使用 2% 保证金上限、10x 风控杠杆，"
            "1.5×ATR 止损、2.5×ATR 止盈，策略反转或持仓 48 小时退出。"
        ),
        "multi_factor",
        {"fast_period": 20, "slow_period": 50, "rsi_period": 14, "threshold": 3},
        {
            "position_size_pct": 2,
            "leverage": 10,
            "max_positions": 8,
            "margin_cap": 0.20,
            "risk_per_trade_pct": 0.5,
            "max_total_risk_pct": 4,
            "max_cluster_positions": 2,
            "risk_max_leverage": 10,
            "liquidation_buffer_pct": 1.5,
            "daily_loss_limit_pct": 2,
            "max_drawdown_pct": 6,
            "short_risk_multiplier": 0.5,
            "max_ticker_age_seconds": 120,
            "max_signal_age_seconds": 18_000,
            "block_high_risk_products": True,
            "fee_bps": 5,
            "slippage_bps": 3,
            "funding_rate_8h_bps": 1,
            "stop_loss_pct": 3,
            "take_profit_pct": 5,
            "max_holding_bars": 12,
        },
        parameter_schema=[
            {
                **item,
                "default": 3 if item["key"] == "threshold" else item["default"],
            }
            for item in ENGINE_PARAMETER_SCHEMAS["multi_factor"]
        ],
        version=3,
    ),
    _template(
        "martingale_tp4_v2",
        "马丁 TP4",
        "篮子策略",
        (
            "复刻马丁 TP4 的三模式篮子算法：自动模式按 Tiger 行情箱体突破开首单，"
            "恢复模式在箱体两端反向开腿，网格模式按逆向距离同向加腿，并按订单数切换"
            "分级止盈、金额止损、追踪和首尾覆盖。开放回测、Shadow 与模拟盘，不启用真实下单。"
        ),
        "martingale_tp4",
        {
            item["key"]: item["default"]
            for item in ENGINE_PARAMETER_SCHEMAS["martingale_tp4"]
        },
        {
            "position_size_pct": 1,
            "leverage": 2,
            "fee_bps": 5,
            "slippage_bps": 3,
            "stop_loss_pct": 0,
            "take_profit_pct": 0,
            "max_holding_bars": 0,
        },
        version=2,
        template_kind="basket_strategy",
        implementation_version="martingale_tp4_engine_v1",
    ),
)


def parameter_schema_for_engine(engine_key: str) -> list[dict[str, Any]]:
    try:
        return copy.deepcopy(ENGINE_PARAMETER_SCHEMAS[engine_key])
    except KeyError as exc:
        raise StrategyParameterError("不支持的策略引擎") from exc


def validate_strategy_parameters(
    engine_key: str, parameters: Mapping[str, Any]
) -> dict[str, int | float]:
    """Return a normalized, complete parameter map accepted by the backtest engine."""

    if not isinstance(parameters, Mapping):
        raise StrategyParameterError("策略参数必须是对象")
    definitions = {item["key"]: item for item in parameter_schema_for_engine(engine_key)}
    unknown = sorted(set(parameters) - set(definitions))
    if unknown:
        raise StrategyParameterError(f"未知策略参数：{', '.join(unknown)}")

    normalized: dict[str, int | float] = {}
    for key, definition in definitions.items():
        raw = parameters.get(key, definition["default"])
        if isinstance(raw, bool) or not isinstance(raw, (int, float, Decimal)):
            raise StrategyParameterError(f"策略参数 {key} 必须是数字")
        numeric = float(raw)
        if not math.isfinite(numeric):
            raise StrategyParameterError(f"策略参数 {key} 必须是有限数字")
        if definition["type"] == "integer":
            if not numeric.is_integer():
                raise StrategyParameterError(f"策略参数 {key} 必须是整数")
            value: int | float = int(numeric)
        else:
            value = int(numeric) if numeric.is_integer() else numeric
        if value < definition["min"] or value > definition["max"]:
            raise StrategyParameterError(
                f"策略参数 {key} 必须在 {definition['min']} 到 {definition['max']} 之间"
            )
        normalized[key] = value

    if engine_key in {"multi_factor", "ma_cross", "macd_momentum"}:
        if normalized["fast_period"] >= normalized["slow_period"]:
            raise StrategyParameterError("快线周期必须小于慢线周期")
    if engine_key == "strategy_dsl":
        if normalized["regime_fast_ema"] >= normalized["regime_slow_ema"]:
            raise StrategyParameterError("4h 快速 EMA 必须小于慢速 EMA")
    if engine_key == "rsi_reversal":
        if normalized["oversold"] >= normalized["overbought"]:
            raise StrategyParameterError("超卖线必须小于超买线")
    if engine_key == "martingale_tp4":
        try:
            strategy_parameters_from_catalog_parameters(normalized)
        except ValueError as exc:
            raise StrategyParameterError(str(exc)) from None
    return normalized


def _insert_ignore(
    db: Session,
    model: type[StrategyTemplate] | type[UserStrategy] | type[StrategyRevision],
    rows: Sequence[dict[str, Any]],
    _conflict_columns: Sequence[str],
) -> None:
    if not rows:
        return
    dialect = db.get_bind().dialect.name
    table = model.__table__
    if dialect in {"mysql", "mariadb"}:
        db.execute(mysql_insert(table).values(list(rows)).prefix_with("IGNORE"))
        return
    raise RuntimeError("strategy storage requires MySQL")


def ensure_system_templates(db: Session) -> list[StrategyTemplate]:
    """Insert or reconcile all code-owned system templates without committing."""

    now = utcnow()
    existing_keys = set(db.scalars(select(StrategyTemplate.template_key)))
    rows = []
    for sort_order, definition in enumerate(SYSTEM_STRATEGY_DEFINITIONS, start=1):
        if definition["template_key"] in existing_keys:
            continue
        rows.append(
            {
                **copy.deepcopy(definition),
                "sort_order": sort_order,
                "created_at": now,
                "updated_at": now,
            }
        )
    _insert_ignore(db, StrategyTemplate, rows, ["template_key"])

    templates = list(
        db.scalars(select(StrategyTemplate).order_by(StrategyTemplate.sort_order)).all()
    )
    by_key = {item.template_key: item for item in templates}
    for sort_order, definition in enumerate(SYSTEM_STRATEGY_DEFINITIONS, start=1):
        template = by_key.get(definition["template_key"])
        if template is None:
            continue
        for attribute, value in definition.items():
            normalized = copy.deepcopy(value)
            if getattr(template, attribute) != normalized:
                setattr(template, attribute, normalized)
        if template.sort_order != sort_order:
            template.sort_order = sort_order
    db.flush()
    return list(db.scalars(select(StrategyTemplate).order_by(StrategyTemplate.sort_order)).all())


def strategy_snapshot(strategy: UserStrategy) -> dict[str, Any]:
    return {
        "public_id": strategy.public_id,
        "name": strategy.name,
        "category": strategy.category,
        "description": strategy.description,
        "status": strategy.status,
        "version": strategy.version,
        "engine_key": strategy.engine_key,
        "strategy_kind": strategy.strategy_kind,
        "complete_strategy": True,
        "management_mode": strategy_management_mode(strategy),
        "lifecycle_status": strategy.lifecycle_status,
        "spec_schema_version": strategy.spec_schema_version,
        "spec": copy.deepcopy(strategy.spec_json),
        "spec_hash": strategy.spec_hash,
        "source_language": strategy.source_language,
        "source_code": strategy.source_code,
        "source_hash": strategy.source_hash,
        "source_runtime_version": strategy.source_runtime_version,
        "source_validation": copy.deepcopy(strategy.source_validation_json),
        "risk_level": strategy.risk_level,
        "parameter_schema": copy.deepcopy(strategy.parameter_schema_json),
        "parameters": copy.deepcopy(strategy.parameters_json),
        "risk_defaults": copy.deepcopy(strategy.risk_defaults_json),
    }


def list_user_strategies(
    db: Session, user_id: int, *, include_archived: bool = False
) -> list[UserStrategy]:
    statement = select(UserStrategy).where(UserStrategy.user_id == user_id)
    if not include_archived:
        statement = statement.where(UserStrategy.status == "active")
    statement = statement.order_by(UserStrategy.created_at, UserStrategy.id)
    return list(db.scalars(statement).all())


def get_user_strategy(db: Session, user_id: int, public_id: str) -> UserStrategy | None:
    return db.scalar(
        select(UserStrategy).where(
            UserStrategy.user_id == user_id,
            UserStrategy.public_id == public_id,
        )
    )


def _record_ai_monitor_strategy_revision(
    db: Session,
    strategy: UserStrategy,
    *,
    source: str,
    summary: str,
) -> None:
    now = utcnow()
    revision = StrategyRevision(
        user_strategy_id=strategy.id,
        user_id=strategy.user_id,
        version=strategy.version,
        change_source=source,
        change_summary=summary[:500],
        snapshot_json=strategy_snapshot(strategy),
        spec_schema_version=strategy.spec_schema_version,
        spec_json=copy.deepcopy(strategy.spec_json),
        spec_hash=strategy.spec_hash,
        source_language=None,
        source_code=None,
        source_hash=None,
        source_runtime_version=None,
        validation_json={
            "valid": True,
            "builtin": True,
            "managed_runtime": "ai_monitor",
            "decision_version": AI_MONITOR_DECISION_VERSION,
        },
        lifecycle_status="published",
        published_at=now,
        created_at=now,
    )
    db.add(revision)
    from .strategy_artifacts import record_revision_artifact

    record_revision_artifact(db, strategy, revision)


def _reconcile_ai_monitor_strategy(
    db: Session,
    user_id: int,
    strategies: Sequence[UserStrategy],
) -> None:
    strategy = next((item for item in strategies if is_ai_monitor_strategy(item)), None)
    if strategy is None:
        return
    config = db.get(AiMonitorConfig, user_id)
    desired_parameters = ai_monitor_strategy_parameters(config)
    desired_spec = {
        "managed_policy": AI_MONITOR_STRATEGY_TEMPLATE_KEY,
        "decision_version": AI_MONITOR_DECISION_VERSION,
    }
    desired_schema = copy.deepcopy(AI_MONITOR_STRATEGY_PARAMETER_SCHEMA)
    changed = any(
        (
            strategy.status != "active",
            strategy.engine_key != "multi_factor",
            strategy.strategy_kind != "builtin_strategy",
            strategy.lifecycle_status != "published",
            strategy.spec_json != desired_spec,
            strategy.parameter_schema_json != desired_schema,
            strategy.parameters_json != desired_parameters,
        )
    )
    if not changed:
        return
    has_revision = db.scalar(
        select(StrategyRevision.id).where(
            StrategyRevision.user_strategy_id == strategy.id,
            StrategyRevision.user_id == user_id,
            StrategyRevision.version == strategy.version,
        )
    ) is not None
    strategy.status = "active"
    strategy.engine_key = "multi_factor"
    strategy.strategy_kind = "builtin_strategy"
    strategy.lifecycle_status = "published"
    strategy.spec_schema_version = None
    strategy.spec_json = desired_spec
    strategy.spec_hash = None
    strategy.parameter_schema_json = desired_schema
    strategy.parameters_json = desired_parameters
    strategy.updated_at = utcnow()
    if has_revision:
        strategy.version += 1
        _record_ai_monitor_strategy_revision(
            db,
            strategy,
            source="system_default",
            summary="同步 AI Monitor 当前决策参数",
        )
    db.flush()


def ensure_user_default_strategies(db: Session, user_id: int) -> list[UserStrategy]:
    """Copy every active system template once for one user, without committing."""

    templates = [template for template in ensure_system_templates(db) if template.is_active]
    existing_source_ids = set(
        db.scalars(
            select(UserStrategy.source_template_id).where(
                UserStrategy.user_id == user_id,
                UserStrategy.source_template_id.is_not(None),
            )
        )
    )
    now = utcnow()
    rows: list[dict[str, Any]] = []
    for template in templates:
        if template.id in existing_source_ids:
            continue
        rows.append(
            {
                "public_id": str(uuid.uuid4()),
                "user_id": user_id,
                "source_template_id": template.id,
                "name": template.name,
                "category": template.category,
                "description": template.description,
                "status": "active",
                "version": 1,
                "engine_key": template.engine_key,
                "strategy_kind": {
                    "strategy": "full_strategy",
                    "basket_strategy": "basket_strategy",
                }.get(template.template_kind, "builtin_strategy"),
                "lifecycle_status": "validated",
                "spec_schema_version": template.spec_schema_version,
                "spec_json": copy.deepcopy(template.spec_json),
                "spec_hash": (
                    strategy_spec_hash(template.spec_json) if template.spec_json else None
                ),
                "risk_level": (
                    "high" if template.template_kind == "basket_strategy" else "medium"
                ),
                "parameter_schema_json": copy.deepcopy(template.parameter_schema_json),
                "parameters_json": copy.deepcopy(template.parameters_json),
                "risk_defaults_json": copy.deepcopy(template.risk_defaults_json),
                "created_via": "system_default",
                "created_at": now,
                "updated_at": now,
            }
        )
    _insert_ignore(
        db,
        UserStrategy,
        rows,
        ["user_id", "source_template_id"],
    )
    db.flush()

    strategies = list_user_strategies(db, user_id, include_archived=True)
    _reconcile_ai_monitor_strategy(db, user_id, strategies)
    existing_revision_ids = set(
        db.scalars(
            select(StrategyRevision.user_strategy_id).where(
                StrategyRevision.user_id == user_id,
                StrategyRevision.version == 1,
            )
        )
    )
    revision_rows = [
        {
            "user_strategy_id": strategy.id,
            "user_id": user_id,
            "version": 1,
            "change_source": "system_default",
            "change_summary": "首次登录时从系统默认策略复制",
            "snapshot_json": strategy_snapshot(strategy),
            "spec_schema_version": strategy.spec_schema_version,
            "spec_json": copy.deepcopy(strategy.spec_json),
            "spec_hash": strategy.spec_hash,
            "source_language": strategy.source_language,
            "source_code": strategy.source_code,
            "source_hash": strategy.source_hash,
            "source_runtime_version": strategy.source_runtime_version,
            "validation_json": (
                {
                    "valid": True,
                    "builtin": True,
                    "managed_runtime": "ai_monitor",
                    "decision_version": AI_MONITOR_DECISION_VERSION,
                }
                if is_ai_monitor_strategy(strategy)
                else (
                    {"valid": True, "engine": "strategy_runtime_v1"}
                    if strategy.strategy_kind == "full_strategy"
                    else (
                        {
                            "valid": True,
                            "engine": "martingale_tp4_engine_v1",
                            "live_ready": False,
                        }
                        if strategy.strategy_kind == "basket_strategy"
                        else {"valid": True, "builtin": True}
                    )
                )
            ),
            "lifecycle_status": strategy.lifecycle_status,
            "published_at": now,
            "created_at": now,
        }
        for strategy in strategies
        if strategy.source_template_id is not None and strategy.id not in existing_revision_ids
    ]
    _insert_ignore(
        db,
        StrategyRevision,
        revision_rows,
        ["user_strategy_id", "version"],
    )
    db.flush()
    if revision_rows:
        from .strategy_artifacts import record_revision_artifact

        by_id = {strategy.id: strategy for strategy in strategies}
        created_revisions = db.scalars(
            select(StrategyRevision).where(
                StrategyRevision.user_id == user_id,
                StrategyRevision.user_strategy_id.in_(by_id),
                StrategyRevision.version == 1,
            )
        ).all()
        for revision in created_revisions:
            record_revision_artifact(db, by_id[revision.user_strategy_id], revision)
    return list_user_strategies(db, user_id)


def serialize_user_strategy(strategy: UserStrategy) -> dict[str, Any]:
    return {
        "id": strategy.public_id,
        "public_id": strategy.public_id,
        "name": strategy.name,
        "category": strategy.category,
        "description": strategy.description,
        "status": strategy.status,
        "version": strategy.version,
        "engine_key": strategy.engine_key,
        "strategy_kind": strategy.strategy_kind,
        "complete_strategy": True,
        "management_mode": strategy_management_mode(strategy),
        "lifecycle_status": strategy.lifecycle_status,
        "spec_schema_version": strategy.spec_schema_version,
        "spec": copy.deepcopy(strategy.spec_json),
        "spec_hash": strategy.spec_hash,
        "source_language": strategy.source_language,
        "source_code": strategy.source_code,
        "source_hash": strategy.source_hash,
        "source_runtime_version": strategy.source_runtime_version,
        "source_validation": copy.deepcopy(strategy.source_validation_json),
        "risk_level": strategy.risk_level,
        "parameter_schema": copy.deepcopy(strategy.parameter_schema_json),
        "parameters": copy.deepcopy(strategy.parameters_json),
        "risk_defaults": copy.deepcopy(strategy.risk_defaults_json),
        "created_via": strategy.created_via,
        "is_default": strategy.source_template_id is not None,
        "source_template_key": (
            strategy.source_template.template_key if strategy.source_template else None
        ),
        "created_at": _isoformat(strategy.created_at),
        "updated_at": _isoformat(strategy.updated_at),
    }


def strategy_to_catalog_item(strategy: UserStrategy) -> dict[str, Any]:
    params = copy.deepcopy(strategy.parameter_schema_json)
    for definition in params:
        key = definition.get("key")
        if key in strategy.parameters_json:
            definition["default"] = strategy.parameters_json[key]
    return {
        "id": strategy.public_id,
        "name": strategy.name,
        "category": strategy.category,
        "description": strategy.description,
        "engine_key": strategy.engine_key,
        "strategy_kind": strategy.strategy_kind,
        "complete_strategy": True,
        "management_mode": strategy_management_mode(strategy),
        "lifecycle_status": strategy.lifecycle_status,
        "spec_schema_version": strategy.spec_schema_version,
        "spec": copy.deepcopy(strategy.spec_json),
        "source_language": strategy.source_language,
        "source_code": strategy.source_code,
        "source_hash": strategy.source_hash,
        "source_runtime_version": strategy.source_runtime_version,
        "source_validation": copy.deepcopy(strategy.source_validation_json),
        "risk_level": strategy.risk_level,
        "version": strategy.version,
        "params": params,
        "risk_defaults": copy.deepcopy(strategy.risk_defaults_json),
    }


def serialize_strategy_catalog(strategies: Sequence[UserStrategy]) -> list[dict[str, Any]]:
    return [strategy_to_catalog_item(strategy) for strategy in strategies]


def _isoformat(value: datetime | None) -> str | None:
    return value.isoformat(timespec="seconds") + "Z" if value else None
