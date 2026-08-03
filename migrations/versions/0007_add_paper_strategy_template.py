"""Add the repaired shared-paper strategy to the system catalog.

Revision ID: 0007_paper_strategy_template
Revises: 0006_strategy_tables
"""

import json
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.mysql import insert as mysql_insert

revision: str = "0007_paper_strategy_template"
down_revision: str | None = "0006_strategy_tables"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TEMPLATE_KEY = "paper_multifactor_atr_v1"

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

DESCRIPTION = (
    "系统共享模拟盘的可回测策略快照：使用 4h 七因子评分达到 ±60 入场，"
    "MA150 顺势过滤；按风险预算开仓，1.5×ATR 止损、2.5×ATR 固定止盈，"
    "反向评分、SuperTrend 翻转或持仓 48 小时退出。"
)


def _template_table() -> sa.TableClause:
    return sa.table(
        "strategy_templates",
        sa.column("id", sa.BigInteger()),
        sa.column("template_key", sa.String(length=64)),
        sa.column("name", sa.String(length=128)),
        sa.column("category", sa.String(length=32)),
        sa.column("description", sa.Text()),
        sa.column("engine_key", sa.String(length=32)),
        sa.column("parameter_schema_json", sa.Text()),
        sa.column("parameters_json", sa.Text()),
        sa.column("risk_defaults_json", sa.Text()),
        sa.column("version", sa.Integer()),
        sa.column("sort_order", sa.Integer()),
        sa.column("is_active", sa.Boolean()),
    )


def upgrade() -> None:
    table = _template_table()
    row = {
        "template_key": TEMPLATE_KEY,
        "name": "AI 模拟盘 ATR 趋势",
        "category": "模拟盘",
        "description": DESCRIPTION,
        "engine_key": "multi_factor",
        "parameter_schema_json": json.dumps(
            PARAMETER_SCHEMA, ensure_ascii=False, separators=(",", ":")
        ),
        "parameters_json": json.dumps(PARAMETERS, ensure_ascii=False, separators=(",", ":")),
        "risk_defaults_json": json.dumps(
            RISK_DEFAULTS, ensure_ascii=False, separators=(",", ":")
        ),
        "version": 1,
        "sort_order": 19,
        "is_active": True,
    }
    dialect = op.get_bind().dialect.name
    if dialect not in {"mysql", "mariadb"}:
        raise RuntimeError("QuantDesk migrations require MySQL")
    statement = mysql_insert(table).values(row).prefix_with("IGNORE")
    op.execute(statement)


def downgrade() -> None:
    templates = _template_table()
    user_strategies = sa.table(
        "user_strategies",
        sa.column("id", sa.BigInteger()),
        sa.column("source_template_id", sa.BigInteger()),
    )
    revisions = sa.table(
        "strategy_revisions",
        sa.column("user_strategy_id", sa.BigInteger()),
    )
    template_ids = sa.select(templates.c.id).where(templates.c.template_key == TEMPLATE_KEY)
    strategy_ids = sa.select(user_strategies.c.id).where(
        user_strategies.c.source_template_id.in_(template_ids)
    )
    op.execute(sa.delete(revisions).where(revisions.c.user_strategy_id.in_(strategy_ids)))
    op.execute(
        sa.delete(user_strategies).where(user_strategies.c.source_template_id.in_(template_ids))
    )
    op.execute(sa.delete(templates).where(templates.c.template_key == TEMPLATE_KEY))
