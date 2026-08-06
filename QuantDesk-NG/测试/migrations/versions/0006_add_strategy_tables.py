"""Add system strategy templates, tenant strategies and immutable revisions.

Revision ID: 0006_strategy_tables
Revises: 0005_backtest_qty_precision
"""

import json
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006_strategy_tables"
down_revision: str | None = "0005_backtest_qty_precision"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

BIGINT = sa.BigInteger()


PARAMETER_SCHEMAS = {
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
}

DEFAULT_RISK = {
    "position_size_pct": 10,
    "leverage": 2,
    "fee_bps": 5,
    "slippage_bps": 2,
    "stop_loss_pct": 2,
    "take_profit_pct": 5,
    "max_holding_bars": 120,
}

# Migration-owned seed data keeps historical upgrades reproducible even when the
# application catalog evolves in a later release.
SEED_TEMPLATES = (
    (
        "trend_breakout",
        "趋势突破",
        "趋势",
        "以快慢趋势、RSI 与综合评分确认突破，适合趋势启动阶段。",
        "multi_factor",
        {"fast_period": 20, "slow_period": 50, "rsi_period": 14, "threshold": 3},
    ),
    (
        "ma_golden_cross",
        "MA 金叉",
        "趋势",
        "快均线上穿慢均线时跟随趋势，并在反向交叉时退出或反向。",
        "ma_cross",
        {"fast_period": 20, "slow_period": 60},
    ),
    (
        "macd_golden_cross_volume",
        "MACD 金叉放量",
        "动量",
        "使用 MACD 动量交叉作为可回测信号，成交量条件由行情接入层继续扩展。",
        "macd_momentum",
        {"fast_period": 12, "slow_period": 26, "signal_period": 9},
    ),
    (
        "price_volume_rise",
        "量价齐升",
        "动量",
        "用短中期趋势与动量评分代理量价同步走强，避免执行任意策略代码。",
        "multi_factor",
        {"fast_period": 10, "slow_period": 30, "rsi_period": 14, "threshold": 3},
    ),
    (
        "low_volatility_leader",
        "低波动龙头",
        "稳健",
        "在较长统计窗口内寻找低波动后的均值回归机会。",
        "bollinger_reversion",
        {"period": 30, "stddev": 1.5},
    ),
    (
        "break_board_reversal",
        "断板反包",
        "反转",
        "使用短周期 RSI 恢复信号代理快速回撤后的反包行情。",
        "rsi_reversal",
        {"period": 9, "oversold": 35, "overbought": 75},
    ),
    (
        "oversold_bounce",
        "超跌反弹",
        "反转",
        "价格进入深度超卖区后，等待 RSI 离开超卖区再参与反弹。",
        "rsi_reversal",
        {"period": 14, "oversold": 25, "overbought": 70},
    ),
    (
        "bollinger_breakout",
        "布林突破",
        "突破",
        "以布林带波动区间及重新回到带内的确认信号控制追涨风险。",
        "bollinger_reversion",
        {"period": 20, "stddev": 2.5},
    ),
    (
        "moving_average_bull",
        "均线多头",
        "趋势",
        "短期均线保持在长期均线上方时顺势交易。",
        "ma_cross",
        {"fast_period": 10, "slow_period": 30},
    ),
    (
        "consecutive_limit_up",
        "连板股",
        "动量",
        "用快速 MACD 动量代理连续强势行情，适合高波动标的研究。",
        "macd_momentum",
        {"fast_period": 8, "slow_period": 21, "signal_period": 5},
    ),
    (
        "low_volume_pullback",
        "缩量回踩",
        "趋势",
        "以中期均线趋势代理回踩后的重新转强信号。",
        "ma_cross",
        {"fast_period": 20, "slow_period": 60},
    ),
    (
        "new_low_reversal",
        "新低反转",
        "反转",
        "创新低并进入极端超卖区后，等待 RSI 回升确认。",
        "rsi_reversal",
        {"period": 14, "oversold": 20, "overbought": 65},
    ),
    (
        "high_turnover_surge",
        "高换手拉升",
        "动量",
        "用快速 MACD 动量变化代理高换手拉升阶段。",
        "macd_momentum",
        {"fast_period": 6, "slow_period": 18, "signal_period": 5},
    ),
    (
        "consecutive_board_relay",
        "连板接力",
        "动量",
        "使用更灵敏的 MACD 参数研究强势行情的接力持续性。",
        "macd_momentum",
        {"fast_period": 5, "slow_period": 13, "signal_period": 4},
    ),
    (
        "near_limit_up",
        "逼近涨停",
        "突破",
        "以短周期多因子高评分代理快速逼近极端涨幅的行情。",
        "multi_factor",
        {"fast_period": 5, "slow_period": 20, "rsi_period": 9, "threshold": 3},
    ),
    (
        "oversold_reversal",
        "超跌反转",
        "反转",
        "采用更灵敏的 RSI 周期确认深度超跌后的方向反转。",
        "rsi_reversal",
        {"period": 9, "oversold": 20, "overbought": 75},
    ),
    (
        "moving_average_pullback_bounce",
        "均线回踩反弹",
        "趋势",
        "以短长均线重新交叉确认回踩后的趋势恢复。",
        "ma_cross",
        {"fast_period": 10, "slow_period": 50},
    ),
    (
        "strong_gap_open",
        "强势高开",
        "突破",
        "用短周期趋势、RSI 与评分阈值代理高开后的强势延续。",
        "multi_factor",
        {"fast_period": 5, "slow_period": 20, "rsi_period": 9, "threshold": 3},
    ),
)


def upgrade() -> None:
    op.create_table(
        "strategy_templates",
        sa.Column("id", BIGINT, autoincrement=True, nullable=False, comment="系统策略模板主键"),
        sa.Column(
            "template_key",
            sa.String(length=64),
            nullable=False,
            comment="系统策略模板稳定标识，全局唯一",
        ),
        sa.Column("name", sa.String(length=128), nullable=False, comment="系统策略模板显示名称"),
        sa.Column(
            "category",
            sa.String(length=32),
            nullable=False,
            comment="策略分类，例如趋势、动量或反转",
        ),
        sa.Column(
            "description", sa.Text(), nullable=False, comment="系统策略模板用途与信号逻辑说明"
        ),
        sa.Column(
            "engine_key",
            sa.String(length=32),
            nullable=False,
            comment="受支持的安全回测引擎标识",
        ),
        sa.Column(
            "parameter_schema_json",
            sa.JSON(),
            nullable=False,
            comment="策略参数定义、类型、默认值及上下界",
        ),
        sa.Column("parameters_json", sa.JSON(), nullable=False, comment="系统模板的默认策略参数"),
        sa.Column(
            "risk_defaults_json",
            sa.JSON(),
            nullable=False,
            comment="系统模板的默认仓位、成本与风控参数",
        ),
        sa.Column(
            "version",
            sa.Integer(),
            server_default=sa.text("1"),
            nullable=False,
            comment="系统策略模板版本号",
        ),
        sa.Column(
            "sort_order",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
            comment="策略中心的默认展示顺序",
        ),
        sa.Column(
            "is_active",
            sa.Boolean(),
            server_default=sa.text("1"),
            nullable=False,
            comment="系统策略模板是否允许复制给用户",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
            comment="系统策略模板创建时间（UTC）",
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
            comment="系统策略模板最后更新时间（UTC）",
        ),
        sa.CheckConstraint("version > 0", name="ck_strategy_templates_positive_version"),
        sa.CheckConstraint(
            "engine_key IN ('multi_factor', 'ma_cross', 'macd_momentum', "
            "'rsi_reversal', 'bollinger_reversion')",
            name="ck_strategy_templates_supported_engine",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_strategy_templates"),
        sa.UniqueConstraint("template_key", name="uq_strategy_templates_template_key"),
        comment="平台维护的系统默认策略模板",
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )
    op.create_index(
        "ix_strategy_templates_active_sort",
        "strategy_templates",
        ["is_active", "sort_order"],
        unique=False,
    )

    op.create_table(
        "user_strategies",
        sa.Column("id", BIGINT, autoincrement=True, nullable=False, comment="用户策略内部主键"),
        sa.Column(
            "public_id",
            sa.String(length=36),
            nullable=False,
            comment="对外使用的随机 UUID，避免暴露自增主键",
        ),
        sa.Column("user_id", BIGINT, nullable=False, comment="所属用户 ID，用于租户数据隔离"),
        sa.Column(
            "source_template_id",
            BIGINT,
            nullable=True,
            comment="首次复制来源的系统策略模板 ID，自建策略可为空",
        ),
        sa.Column("name", sa.String(length=128), nullable=False, comment="用户策略显示名称"),
        sa.Column(
            "category",
            sa.String(length=32),
            nullable=False,
            comment="策略分类，例如趋势、动量或反转",
        ),
        sa.Column("description", sa.Text(), nullable=False, comment="用户策略逻辑说明"),
        sa.Column(
            "status",
            sa.String(length=16),
            server_default=sa.text("'active'"),
            nullable=False,
            comment="策略状态：启用或已归档",
        ),
        sa.Column(
            "version",
            sa.Integer(),
            server_default=sa.text("1"),
            nullable=False,
            comment="用户策略当前版本号",
        ),
        sa.Column(
            "engine_key",
            sa.String(length=32),
            nullable=False,
            comment="受支持的安全回测引擎标识",
        ),
        sa.Column(
            "parameter_schema_json",
            sa.JSON(),
            nullable=False,
            comment="策略参数定义、类型、默认值及上下界快照",
        ),
        sa.Column("parameters_json", sa.JSON(), nullable=False, comment="用户当前生效的策略参数"),
        sa.Column(
            "risk_defaults_json",
            sa.JSON(),
            nullable=False,
            comment="用户策略默认仓位、成本与风控参数",
        ),
        sa.Column(
            "created_via",
            sa.String(length=24),
            server_default=sa.text("'manual'"),
            nullable=False,
            comment="策略创建来源：系统默认复制、手工新建或 AI 新建",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
            comment="用户策略创建时间（UTC）",
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
            comment="用户策略最后更新时间（UTC）",
        ),
        sa.CheckConstraint("version > 0", name="ck_user_strategies_positive_version"),
        sa.CheckConstraint(
            "status IN ('active', 'archived')", name="ck_user_strategies_valid_status"
        ),
        sa.CheckConstraint(
            "created_via IN ('system_default', 'manual', 'ai')",
            name="ck_user_strategies_valid_created_via",
        ),
        sa.CheckConstraint(
            "engine_key IN ('multi_factor', 'ma_cross', 'macd_momentum', "
            "'rsi_reversal', 'bollinger_reversion')",
            name="ck_user_strategies_supported_engine",
        ),
        sa.ForeignKeyConstraint(
            ["source_template_id"],
            ["strategy_templates.id"],
            name="fk_user_strategies_source_template_id_strategy_templates",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_user_strategies_user_id_users",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_user_strategies"),
        sa.UniqueConstraint("public_id", name="uq_user_strategies_public_id"),
        sa.UniqueConstraint(
            "user_id",
            "source_template_id",
            name="uq_user_strategies_user_source_template",
        ),
        sa.UniqueConstraint("id", "user_id", name="uq_user_strategies_id_user_id"),
        comment="用户独立拥有并可编辑的策略配置",
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )
    op.create_index(
        "ix_user_strategies_user_status_updated",
        "user_strategies",
        ["user_id", "status", "updated_at"],
        unique=False,
    )
    op.create_index(
        "ix_user_strategies_user_name",
        "user_strategies",
        ["user_id", "name"],
        unique=False,
    )

    op.create_table(
        "strategy_revisions",
        sa.Column("id", BIGINT, autoincrement=True, nullable=False, comment="策略修订记录主键"),
        sa.Column("user_strategy_id", BIGINT, nullable=False, comment="所属用户策略内部 ID"),
        sa.Column(
            "user_id",
            BIGINT,
            nullable=False,
            comment="所属用户 ID，用于数据库级租户一致性校验",
        ),
        sa.Column("version", sa.Integer(), nullable=False, comment="该用户策略内单调递增的版本号"),
        sa.Column(
            "change_source",
            sa.String(length=24),
            nullable=False,
            comment="修改来源：系统默认复制、手工修改或 AI 修改",
        ),
        sa.Column(
            "change_summary", sa.String(length=500), nullable=False, comment="本次修改的简短说明"
        ),
        sa.Column(
            "snapshot_json",
            sa.JSON(),
            nullable=False,
            comment="该版本完整且可复现的策略配置快照",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
            comment="策略修订创建时间（UTC）",
        ),
        sa.CheckConstraint("version > 0", name="ck_strategy_revisions_positive_version"),
        sa.CheckConstraint(
            "change_source IN ('system_default', 'manual', 'ai')",
            name="ck_strategy_revisions_valid_change_source",
        ),
        sa.ForeignKeyConstraint(
            ["user_strategy_id", "user_id"],
            ["user_strategies.id", "user_strategies.user_id"],
            name="fk_strategy_revisions_strategy_tenant",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_strategy_revisions"),
        sa.UniqueConstraint(
            "user_strategy_id",
            "version",
            name="uq_strategy_revisions_strategy_version",
        ),
        comment="用户策略每次修改后的不可变版本快照",
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )
    op.create_index(
        "ix_strategy_revisions_user_created",
        "strategy_revisions",
        ["user_id", "created_at"],
        unique=False,
    )

    seed_table = sa.table(
        "strategy_templates",
        sa.column("template_key", sa.String(length=64)),
        sa.column("name", sa.String(length=128)),
        sa.column("category", sa.String(length=32)),
        sa.column("description", sa.Text()),
        sa.column("engine_key", sa.String(length=32)),
        # Text-typed literals produce valid JSON text both in live migrations and
        # in ``alembic upgrade --sql`` offline scripts.
        sa.column("parameter_schema_json", sa.Text()),
        sa.column("parameters_json", sa.Text()),
        sa.column("risk_defaults_json", sa.Text()),
        sa.column("version", sa.Integer()),
        sa.column("sort_order", sa.Integer()),
        sa.column("is_active", sa.Boolean()),
    )
    op.bulk_insert(
        seed_table,
        [
            {
                "template_key": template_key,
                "name": name,
                "category": category,
                "description": description,
                "engine_key": engine_key,
                "parameter_schema_json": json.dumps(
                    PARAMETER_SCHEMAS[engine_key], ensure_ascii=False, separators=(",", ":")
                ),
                "parameters_json": json.dumps(
                    parameters, ensure_ascii=False, separators=(",", ":")
                ),
                "risk_defaults_json": json.dumps(
                    DEFAULT_RISK, ensure_ascii=False, separators=(",", ":")
                ),
                "version": 1,
                "sort_order": sort_order,
                "is_active": True,
            }
            for sort_order, (
                template_key,
                name,
                category,
                description,
                engine_key,
                parameters,
            ) in enumerate(SEED_TEMPLATES, start=1)
        ],
    )


def downgrade() -> None:
    op.drop_index("ix_strategy_revisions_user_created", table_name="strategy_revisions")
    op.drop_table("strategy_revisions")
    op.drop_index("ix_user_strategies_user_name", table_name="user_strategies")
    op.drop_index("ix_user_strategies_user_status_updated", table_name="user_strategies")
    op.drop_table("user_strategies")
    op.drop_index("ix_strategy_templates_active_sort", table_name="strategy_templates")
    op.drop_table("strategy_templates")
