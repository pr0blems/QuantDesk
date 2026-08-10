"""Add the tenant-scoped AI news and opportunity monitor workspace.

Revision ID: 0037_ai_monitor_workspace
Revises: 0036_prediction_algo_snapshot
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0037_ai_monitor_workspace"
down_revision: str | None = "0036_prediction_algo_snapshot"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

BIGINT = sa.BigInteger()


def _require_mysql() -> None:
    if op.get_bind().dialect.name not in {"mysql", "mariadb"}:
        raise RuntimeError("QuantDesk migrations require MySQL or MariaDB")


def upgrade() -> None:
    _require_mysql()
    op.create_table(
        "ai_monitor_configs",
        sa.Column("user_id", BIGINT, nullable=False, comment="所属用户 ID"),
        sa.Column(
            "enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("0"),
            comment="是否启用后台周期分析",
        ),
        sa.Column(
            "news_interval_minutes",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("30"),
            comment="AI 分析新新闻的间隔分钟数",
        ),
        sa.Column(
            "opportunity_interval_minutes",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("15"),
            comment="新闻与指标组合扫描间隔分钟数",
        ),
        sa.Column(
            "news_lookback_hours",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("24"),
            comment="机会扫描采用的新闻回看小时数",
        ),
        sa.Column(
            "timeframe",
            sa.String(8),
            nullable=False,
            server_default="1h",
            comment="技术指标扫描周期",
        ),
        sa.Column("indicator_keys_json", sa.JSON(), nullable=False, comment="全部需要满足的技术指标稳定键"),
        sa.Column(
            "minimum_news_confidence",
            sa.Numeric(5, 4),
            nullable=False,
            server_default=sa.text("0.6000"),
            comment="新闻最低置信度",
        ),
        sa.Column(
            "minimum_news_mentions",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("1"),
            comment="候选美股至少关联新闻数",
        ),
        sa.Column("last_news_run_at", sa.DateTime(), comment="最近一次新闻分析启动时间（UTC）"),
        sa.Column(
            "last_opportunity_run_at",
            sa.DateTime(),
            comment="最近一次机会扫描启动时间（UTC）",
        ),
        sa.Column("created_at", sa.DateTime(), nullable=False, comment="配置创建时间（UTC）"),
        sa.Column("updated_at", sa.DateTime(), nullable=False, comment="配置最后更新时间（UTC）"),
        sa.CheckConstraint(
            "news_interval_minutes BETWEEN 5 AND 1440",
            name="valid_news_interval",
        ),
        sa.CheckConstraint(
            "opportunity_interval_minutes BETWEEN 5 AND 1440",
            name="valid_opportunity_interval",
        ),
        sa.CheckConstraint(
            "news_lookback_hours BETWEEN 1 AND 168",
            name="valid_news_lookback",
        ),
        sa.CheckConstraint(
            "timeframe IN ('15m', '1h', '4h')",
            name="valid_timeframe",
        ),
        sa.CheckConstraint(
            "minimum_news_confidence BETWEEN 0 AND 1",
            name="valid_news_confidence",
        ),
        sa.CheckConstraint(
            "minimum_news_mentions BETWEEN 1 AND 20",
            name="valid_news_mentions",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_ai_monitor_configs_user_id_users",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("user_id", name="pk_ai_monitor_configs"),
        comment="用户隔离的 AI 新闻与技术指标机会扫描配置",
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )
    op.create_index(
        "ix_ai_monitor_configs_enabled",
        "ai_monitor_configs",
        ["enabled", "updated_at"],
    )

    op.create_table(
        "ai_monitor_runs",
        sa.Column("id", BIGINT, autoincrement=True, nullable=False, comment="执行记录主键"),
        sa.Column("public_id", sa.String(36), nullable=False, comment="执行记录公开 UUID"),
        sa.Column("user_id", BIGINT, nullable=False, comment="所属用户 ID"),
        sa.Column("run_type", sa.String(16), nullable=False, comment="执行类型"),
        sa.Column("status", sa.String(16), nullable=False, comment="执行状态"),
        sa.Column("news_batch_id", sa.String(36), comment="关联的新闻 AI 批次 UUID"),
        sa.Column("input_count", sa.Integer(), nullable=False, server_default=sa.text("0"), comment="输入数量"),
        sa.Column("matched_count", sa.Integer(), nullable=False, server_default=sa.text("0"), comment="成功数量"),
        sa.Column("summary_json", sa.JSON(), comment="执行摘要和脱敏统计"),
        sa.Column("error_message", sa.Text(), comment="面向用户的脱敏错误摘要"),
        sa.Column("started_at", sa.DateTime(), comment="开始时间（UTC）"),
        sa.Column("completed_at", sa.DateTime(), comment="完成时间（UTC）"),
        sa.Column("created_at", sa.DateTime(), nullable=False, comment="创建时间（UTC）"),
        sa.Column("updated_at", sa.DateTime(), nullable=False, comment="最后更新时间（UTC）"),
        sa.CheckConstraint(
            "run_type IN ('news', 'opportunity')",
            name="valid_run_type",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'running', 'completed', 'partial', 'failed', 'skipped')",
            name="valid_status",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_ai_monitor_runs_user_id_users",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["news_batch_id"],
            ["news_ai_batches.id"],
            name="fk_ai_monitor_runs_news_batch_id_news_ai_batches",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_ai_monitor_runs"),
        sa.UniqueConstraint("public_id", name="uq_ai_monitor_runs_public_id"),
        comment="AI 监控新闻分析与机会发现的用户级执行记录",
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )
    op.create_index(
        "ix_ai_monitor_runs_user_created",
        "ai_monitor_runs",
        ["user_id", "created_at"],
    )
    op.create_index(
        "ix_ai_monitor_runs_user_status",
        "ai_monitor_runs",
        ["user_id", "status", "updated_at"],
    )

    op.create_table(
        "ai_monitor_opportunities",
        sa.Column("id", BIGINT, autoincrement=True, nullable=False, comment="AI 机会主键"),
        sa.Column("public_id", sa.String(36), nullable=False, comment="AI 机会公开 UUID"),
        sa.Column("user_id", BIGINT, nullable=False, comment="所属用户 ID"),
        sa.Column("analysis_run_id", BIGINT, nullable=False, comment="产生该机会的执行记录"),
        sa.Column("symbol", sa.String(32), nullable=False, comment="标准美股代码"),
        sa.Column("contract_symbol", sa.String(32), nullable=False, comment="Binance TradFi 合约代码"),
        sa.Column("direction", sa.String(12), nullable=False, comment="机会方向"),
        sa.Column("status", sa.String(16), nullable=False, comment="机会状态"),
        sa.Column("timeframe", sa.String(8), nullable=False, comment="技术指标确认周期"),
        sa.Column("news_score", sa.Numeric(8, 4), nullable=False, comment="新闻侧置信评分"),
        sa.Column("indicator_score", sa.Numeric(8, 4), nullable=False, comment="指标满足评分"),
        sa.Column("combined_score", sa.Numeric(8, 4), nullable=False, comment="组合评分"),
        sa.Column("matched_indicator_keys_json", sa.JSON(), nullable=False, comment="满足的指标键"),
        sa.Column("news_ids_json", sa.JSON(), nullable=False, comment="采用的新闻稳定 ID"),
        sa.Column("evidence_json", sa.JSON(), nullable=False, comment="新闻、技术指标与行情证据"),
        sa.Column("dedup_key", sa.String(191), nullable=False, comment="输入幂等去重键"),
        sa.Column("discovered_at", sa.DateTime(), nullable=False, comment="发现时间（UTC）"),
        sa.Column("expires_at", sa.DateTime(), nullable=False, comment="机会失效时间（UTC）"),
        sa.Column("created_at", sa.DateTime(), nullable=False, comment="创建时间（UTC）"),
        sa.Column("updated_at", sa.DateTime(), nullable=False, comment="最后更新时间（UTC）"),
        sa.CheckConstraint(
            "direction IN ('long', 'short')",
            name="valid_direction",
        ),
        sa.CheckConstraint(
            "status IN ('discovered', 'expired', 'dismissed')",
            name="valid_status",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_ai_monitor_opportunities_user_id_users",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["analysis_run_id"],
            ["ai_monitor_runs.id"],
            name="fk_ai_monitor_opportunities_analysis_run_id_ai_monitor_runs",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_ai_monitor_opportunities"),
        sa.UniqueConstraint("public_id", name="uq_ai_monitor_opportunities_public_id"),
        sa.UniqueConstraint("dedup_key", name="uq_ai_monitor_opportunities_dedup_key"),
        comment="由 AI 新闻与用户配置指标共同确认的美股机会",
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )
    op.create_index(
        "ix_ai_monitor_opportunities_user_status_score",
        "ai_monitor_opportunities",
        ["user_id", "status", "combined_score"],
    )
    op.create_index(
        "ix_ai_monitor_opportunities_user_created",
        "ai_monitor_opportunities",
        ["user_id", "created_at"],
    )


def downgrade() -> None:
    _require_mysql()
    op.drop_index(
        "ix_ai_monitor_opportunities_user_created",
        table_name="ai_monitor_opportunities",
    )
    op.drop_index(
        "ix_ai_monitor_opportunities_user_status_score",
        table_name="ai_monitor_opportunities",
    )
    op.drop_table("ai_monitor_opportunities")
    op.drop_index("ix_ai_monitor_runs_user_status", table_name="ai_monitor_runs")
    op.drop_index("ix_ai_monitor_runs_user_created", table_name="ai_monitor_runs")
    op.drop_table("ai_monitor_runs")
    op.drop_index("ix_ai_monitor_configs_enabled", table_name="ai_monitor_configs")
    op.drop_table("ai_monitor_configs")
