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

from .models import StrategyRevision, StrategyTemplate, UserStrategy, utcnow
from .strategy_runtime import (
    TREND_PULLBACK_SPEC_V1,
    build_trend_pullback_spec,
    full_strategy_parameter_schema,
    strategy_spec_hash,
)


class StrategyParameterError(ValueError):
    """Raised when persisted or user-supplied strategy parameters are unsafe."""


ENGINE_PARAMETER_SCHEMAS: dict[str, list[dict[str, Any]]] = {
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
    template_kind: str = "legacy_signal",
    spec: dict[str, Any] | None = None,
    implementation_version: str = "legacy_v1",
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
                "strategy_kind": (
                    "full_strategy" if template.template_kind == "strategy" else "legacy_signal"
                ),
                "lifecycle_status": "published",
                "spec_schema_version": template.spec_schema_version,
                "spec_json": copy.deepcopy(template.spec_json),
                "spec_hash": (
                    strategy_spec_hash(template.spec_json) if template.spec_json else None
                ),
                "risk_level": "medium",
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
                {"valid": True, "engine": "strategy_runtime_v1"}
                if strategy.strategy_kind == "full_strategy"
                else {"valid": True, "legacy": True}
            ),
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
