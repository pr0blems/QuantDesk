"""Align the default paper strategy with its executable ATR exit rules.

Revision ID: 0011_repair_paper_strategy
Revises: 0010_trading_schema_comments
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011_repair_paper_strategy"
down_revision: str | None = "0010_trading_schema_comments"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TEMPLATE_KEY = "paper_multifactor_atr_v1"
DESCRIPTION = (
    "系统模拟盘默认策略：在 4h 周期综合 MA20/MA50、MACD、RSI 与布林带，"
    "评分达到 3 时入场；默认使用 10% 保证金、20x 杠杆，"
    "1.5×ATR 止损、2.5×ATR 止盈，策略反转或持仓 48 小时退出。"
)
PARAMETER_SCHEMA = [
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
        "default": 3,
        "min": 1,
        "max": 4,
    },
]
PARAMETERS = {
    "fast_period": 20,
    "slow_period": 50,
    "rsi_period": 14,
    "threshold": 3,
}
RISK_DEFAULTS = {
    "position_size_pct": 10,
    "leverage": 20,
    "fee_bps": 5,
    "slippage_bps": 3,
    "stop_loss_pct": 3,
    "take_profit_pct": 5,
    "max_holding_bars": 12,
}


def _tables() -> tuple[sa.TableClause, sa.TableClause]:
    templates = sa.table(
        "strategy_templates",
        sa.column("id", sa.BigInteger()),
        sa.column("template_key", sa.String(length=64)),
        sa.column("name", sa.String(length=128)),
        sa.column("category", sa.String(length=32)),
        sa.column("description", sa.Text()),
        sa.column("engine_key", sa.String(length=32)),
        sa.column("parameter_schema_json", sa.JSON()),
        sa.column("parameters_json", sa.JSON()),
        sa.column("risk_defaults_json", sa.JSON()),
        sa.column("version", sa.Integer()),
        sa.column("sort_order", sa.Integer()),
        sa.column("is_active", sa.Boolean()),
        sa.column("updated_at", sa.DateTime()),
    )
    strategies = sa.table(
        "user_strategies",
        sa.column("id", sa.BigInteger()),
        sa.column("source_template_id", sa.BigInteger()),
        sa.column("name", sa.String(length=128)),
        sa.column("category", sa.String(length=32)),
        sa.column("description", sa.Text()),
        sa.column("engine_key", sa.String(length=32)),
        sa.column("parameter_schema_json", sa.JSON()),
        sa.column("parameters_json", sa.JSON()),
        sa.column("risk_defaults_json", sa.JSON()),
        sa.column("created_via", sa.String(length=24)),
        sa.column("version", sa.Integer()),
        sa.column("updated_at", sa.DateTime()),
    )
    return templates, strategies


def upgrade() -> None:
    if op.get_bind().dialect.name not in {"mysql", "mariadb"}:
        raise RuntimeError("QuantDesk migrations require MySQL")
    templates, strategies = _tables()
    template_ids = sa.select(templates.c.id).where(templates.c.template_key == TEMPLATE_KEY)
    values = {
        "name": "AI 模拟盘 ATR 趋势",
        "category": "模拟盘",
        "description": DESCRIPTION,
        "engine_key": "multi_factor",
        "parameter_schema_json": PARAMETER_SCHEMA,
        "parameters_json": PARAMETERS,
        "risk_defaults_json": RISK_DEFAULTS,
        "version": 2,
        "sort_order": 19,
        "is_active": True,
        "updated_at": sa.func.current_timestamp(),
    }
    op.execute(
        sa.update(templates)
        .where(templates.c.template_key == TEMPLATE_KEY)
        .values(**values)
    )
    op.execute(
        sa.update(strategies)
        .where(strategies.c.source_template_id.in_(template_ids))
        .where(strategies.c.created_via == "system_default")
        .where(strategies.c.version == 1)
        .values(
            name=values["name"],
            category=values["category"],
            description=values["description"],
            engine_key=values["engine_key"],
            parameter_schema_json=PARAMETER_SCHEMA,
            parameters_json=PARAMETERS,
            risk_defaults_json=RISK_DEFAULTS,
            version=2,
            updated_at=sa.func.current_timestamp(),
        )
    )
    op.execute(
        sa.text(
            """
            INSERT IGNORE INTO strategy_revisions(
                user_strategy_id,user_id,version,change_source,change_summary,
                snapshot_json,created_at
            )
            SELECT us.id,us.user_id,2,'system_default','修复模拟盘 ATR 止盈并同步系统默认策略',
                   JSON_OBJECT(
                       'public_id',us.public_id,'name',us.name,'category',us.category,
                       'description',us.description,'status',us.status,'version',us.version,
                       'engine_key',us.engine_key,
                       'parameter_schema',us.parameter_schema_json,
                       'parameters',us.parameters_json,'risk_defaults',us.risk_defaults_json
                   ),CURRENT_TIMESTAMP
            FROM user_strategies us
            JOIN strategy_templates st ON st.id=us.source_template_id
            WHERE st.template_key=:template_key AND us.version=2
              AND us.created_via='system_default'
              AND NOT EXISTS(
                  SELECT 1 FROM strategy_revisions sr
                  WHERE sr.user_strategy_id=us.id AND sr.version=2
              )
            """
        ).bindparams(template_key=TEMPLATE_KEY)
    )


def downgrade() -> None:
    templates, _ = _tables()
    op.execute(
        sa.update(templates)
        .where(templates.c.template_key == TEMPLATE_KEY)
        .values(version=1, updated_at=sa.func.current_timestamp())
    )
