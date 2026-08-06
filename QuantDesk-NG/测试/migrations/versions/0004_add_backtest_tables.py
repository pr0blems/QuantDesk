"""Add tenant-isolated strategy backtest runs and trades.

Revision ID: 0004_add_backtest_tables
Revises: 0003_add_monitor_preferences
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004_add_backtest_tables"
down_revision: str | None = "0003_add_monitor_preferences"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "backtest_runs",
        sa.Column(
            "id", sa.BigInteger(), autoincrement=True, nullable=False, comment="回测任务主键"
        ),
        sa.Column(
            "user_id",
            sa.BigInteger(),
            nullable=False,
            comment="所属用户 ID，用于租户数据隔离",
        ),
        sa.Column(
            "strategy_id", sa.String(length=64), nullable=False, comment="策略目录中的稳定策略标识"
        ),
        sa.Column(
            "strategy_name",
            sa.String(length=128),
            nullable=False,
            comment="执行回测时的策略名称快照",
        ),
        sa.Column("symbol", sa.String(length=32), nullable=False, comment="回测交易标的代码"),
        sa.Column(
            "timeframe",
            sa.String(length=8),
            nullable=False,
            comment="回测行情周期，例如 15m、4h 或 1d",
        ),
        sa.Column(
            "status",
            sa.String(length=16),
            server_default=sa.text("'queued'"),
            nullable=False,
            comment="任务状态：排队、运行、完成、失败或取消",
        ),
        sa.Column("start_at", sa.DateTime(), nullable=False, comment="回测数据起始时间（UTC）"),
        sa.Column("end_at", sa.DateTime(), nullable=False, comment="回测数据结束时间（UTC）"),
        sa.Column(
            "initial_capital",
            sa.Numeric(precision=30, scale=8),
            nullable=False,
            comment="回测初始资金",
        ),
        sa.Column(
            "final_equity",
            sa.Numeric(precision=30, scale=8),
            nullable=True,
            comment="回测结束时账户权益",
        ),
        sa.Column(
            "net_profit",
            sa.Numeric(precision=30, scale=8),
            nullable=True,
            comment="扣除交易成本后的净利润",
        ),
        sa.Column(
            "total_return_pct",
            sa.Numeric(precision=18, scale=8),
            nullable=True,
            comment="总收益率百分比",
        ),
        sa.Column(
            "max_drawdown_pct",
            sa.Numeric(precision=18, scale=8),
            nullable=True,
            comment="最大回撤百分比",
        ),
        sa.Column(
            "sharpe_ratio",
            sa.Numeric(precision=18, scale=8),
            nullable=True,
            comment="年化夏普比率",
        ),
        sa.Column(
            "win_rate_pct",
            sa.Numeric(precision=18, scale=8),
            nullable=True,
            comment="盈利成交占比百分比",
        ),
        sa.Column(
            "profit_factor",
            sa.Numeric(precision=18, scale=8),
            nullable=True,
            comment="总盈利与总亏损绝对值之比",
        ),
        sa.Column(
            "trade_count",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
            comment="已完成成交总笔数",
        ),
        sa.Column(
            "config_json", sa.JSON(), nullable=False, comment="本次回测的完整参数与成本配置快照"
        ),
        sa.Column("metrics_json", sa.JSON(), nullable=True, comment="可扩展的回测指标集合"),
        sa.Column(
            "equity_curve_json", sa.JSON(), nullable=True, comment="按时间排序的账户权益曲线数据"
        ),
        sa.Column(
            "data_quality_json",
            sa.JSON(),
            nullable=True,
            comment="有效行情柱数、实际数据区间、截断与回测假设说明",
        ),
        sa.Column(
            "metadata_json", sa.JSON(), nullable=True, comment="数据源、引擎版本等扩展运行信息"
        ),
        sa.Column("error", sa.Text(), nullable=True, comment="任务失败时的脱敏错误说明"),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
            comment="回测任务创建时间（UTC）",
        ),
        sa.Column(
            "completed_at", sa.DateTime(), nullable=True, comment="回测任务完成或失败时间（UTC）"
        ),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'completed', 'failed', 'cancelled')",
            name="ck_backtest_runs_valid_status",
        ),
        sa.CheckConstraint("end_at >= start_at", name="ck_backtest_runs_valid_period"),
        sa.CheckConstraint("initial_capital > 0", name="ck_backtest_runs_positive_initial_capital"),
        sa.CheckConstraint("trade_count >= 0", name="ck_backtest_runs_nonnegative_trade_count"),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_backtest_runs_user_id_users",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_backtest_runs"),
        comment="用户策略回测任务、配置与汇总指标",
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )
    op.create_index(
        "ix_backtest_runs_user_created", "backtest_runs", ["user_id", "created_at"], unique=False
    )
    op.create_index(
        "ix_backtest_runs_user_status_created",
        "backtest_runs",
        ["user_id", "status", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_backtest_runs_user_strategy_created",
        "backtest_runs",
        ["user_id", "strategy_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_backtest_runs_user_symbol_timeframe",
        "backtest_runs",
        ["user_id", "symbol", "timeframe"],
        unique=False,
    )

    op.create_table(
        "backtest_trades",
        sa.Column(
            "id", sa.BigInteger(), autoincrement=True, nullable=False, comment="回测成交主键"
        ),
        sa.Column("run_id", sa.BigInteger(), nullable=False, comment="所属回测任务 ID"),
        sa.Column(
            "user_id",
            sa.BigInteger(),
            nullable=False,
            comment="所属用户 ID，用于租户数据隔离",
        ),
        sa.Column(
            "side",
            sa.String(length=8),
            nullable=False,
            comment="持仓方向：long 多头或 short 空头",
        ),
        sa.Column("entry_at", sa.DateTime(), nullable=False, comment="开仓成交时间（UTC）"),
        sa.Column("exit_at", sa.DateTime(), nullable=False, comment="平仓成交时间（UTC）"),
        sa.Column(
            "entry_price",
            sa.Numeric(precision=30, scale=12),
            nullable=False,
            comment="开仓成交价格",
        ),
        sa.Column(
            "exit_price", sa.Numeric(precision=30, scale=12), nullable=False, comment="平仓成交价格"
        ),
        sa.Column(
            "quantity",
            sa.Numeric(precision=48, scale=18),
            nullable=False,
            comment="成交标的数量，保留极小仓位精度",
        ),
        sa.Column(
            "gross_pnl",
            sa.Numeric(precision=30, scale=8),
            nullable=False,
            comment="扣除费用前的成交盈亏",
        ),
        sa.Column(
            "fees",
            sa.Numeric(precision=30, scale=8),
            nullable=False,
            comment="开仓与平仓交易费用合计",
        ),
        sa.Column(
            "net_pnl",
            sa.Numeric(precision=30, scale=8),
            nullable=False,
            comment="扣除费用后的成交净盈亏",
        ),
        sa.Column(
            "return_pct",
            sa.Numeric(precision=18, scale=8),
            nullable=False,
            comment="本笔成交收益率百分比",
        ),
        sa.Column(
            "holding_bars", sa.Integer(), nullable=False, comment="从开仓到平仓持有的行情柱数量"
        ),
        sa.Column(
            "exit_reason",
            sa.String(length=64),
            nullable=True,
            comment="平仓原因代码，例如止损、止盈或超时",
        ),
        sa.Column(
            "metadata_json", sa.JSON(), nullable=True, comment="信号、滑点与执行过程等扩展信息"
        ),
        sa.CheckConstraint("side IN ('long', 'short')", name="ck_backtest_trades_valid_side"),
        sa.CheckConstraint("quantity > 0", name="ck_backtest_trades_positive_quantity"),
        sa.CheckConstraint("holding_bars >= 0", name="ck_backtest_trades_nonnegative_holding_bars"),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["backtest_runs.id"],
            name="fk_backtest_trades_run_id_backtest_runs",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_backtest_trades_user_id_users",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_backtest_trades"),
        comment="回测任务产生的逐笔成交明细",
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )
    op.create_index(
        "ix_backtest_trades_run_entry", "backtest_trades", ["run_id", "entry_at"], unique=False
    )
    op.create_index(
        "ix_backtest_trades_user_entry",
        "backtest_trades",
        ["user_id", "entry_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_backtest_trades_user_entry", table_name="backtest_trades")
    op.drop_index("ix_backtest_trades_run_entry", table_name="backtest_trades")
    op.drop_table("backtest_trades")
    op.drop_index("ix_backtest_runs_user_symbol_timeframe", table_name="backtest_runs")
    op.drop_index("ix_backtest_runs_user_strategy_created", table_name="backtest_runs")
    op.drop_index("ix_backtest_runs_user_status_created", table_name="backtest_runs")
    op.drop_index("ix_backtest_runs_user_created", table_name="backtest_runs")
    op.drop_table("backtest_runs")
