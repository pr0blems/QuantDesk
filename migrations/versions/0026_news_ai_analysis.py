"""Add batched AI news analysis and US stock associations.

Revision ID: 0026_news_ai_analysis
Revises: 0025_news_source_feed_type
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0026_news_ai_analysis"
down_revision: str | None = "0025_news_source_feed_type"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "news_ai_batches",
        sa.Column("id", sa.String(length=36), nullable=False, comment="批次 UUID"),
        sa.Column("started_by", sa.BigInteger(), nullable=True, comment="发起分析的管理员用户 ID"),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="pending"),
        sa.Column("requested_count", sa.Integer(), nullable=False),
        sa.Column("selected_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("processed_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("failed_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("chunk_size", sa.Integer(), nullable=False, server_default="5"),
        sa.Column("provider_code", sa.String(length=32), nullable=True),
        sa.Column("model_name", sa.String(length=128), nullable=True),
        sa.Column("market_sentiment", sa.String(length=32), nullable=True),
        sa.Column("market_confidence", sa.Numeric(5, 4), nullable=True),
        sa.Column("market_summary", sa.Text(), nullable=True),
        sa.Column("result_json", sa.JSON(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "status IN ('pending', 'running', 'completed', 'partial', 'failed')",
            name="ck_news_ai_batches_valid_status",
        ),
        sa.CheckConstraint(
            "requested_count IN (300, 500)",
            name="ck_news_ai_batches_valid_requested_count",
        ),
        sa.CheckConstraint(
            "selected_count >= 0 AND processed_count >= 0 AND failed_count >= 0",
            name="ck_news_ai_batches_nonnegative_counts",
        ),
        sa.ForeignKeyConstraint(
            ["started_by"], ["users.id"], name="fk_news_ai_batches_started_by_users", ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_news_ai_batches"),
        comment="管理员发起的批量新闻 AI 研判任务与市场汇总结论",
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )
    op.create_index("ix_news_ai_batches_created", "news_ai_batches", ["created_at"])
    op.create_index(
        "ix_news_ai_batches_status", "news_ai_batches", ["status", "updated_at"]
    )

    op.add_column("news", sa.Column("rule_sentiment", sa.String(length=32), nullable=True))
    op.add_column("news", sa.Column("related_us_stocks", sa.JSON(), nullable=True))
    op.add_column("news", sa.Column("ai_sentiment", sa.String(length=32), nullable=True))
    op.add_column("news", sa.Column("ai_confidence", sa.Numeric(5, 4), nullable=True))
    op.add_column("news", sa.Column("ai_impact_strength", sa.String(length=16), nullable=True))
    op.add_column("news", sa.Column("ai_time_horizon", sa.String(length=32), nullable=True))
    op.add_column("news", sa.Column("ai_category", sa.String(length=32), nullable=True))
    op.add_column("news", sa.Column("ai_reason", sa.Text(), nullable=True))
    op.add_column("news", sa.Column("ai_model", sa.String(length=128), nullable=True))
    op.add_column("news", sa.Column("ai_batch_id", sa.String(length=36), nullable=True))
    op.add_column("news", sa.Column("ai_analyzed_at", sa.DateTime(), nullable=True))
    op.create_foreign_key(
        "fk_news_ai_batch_id_news_ai_batches",
        "news",
        "news_ai_batches",
        ["ai_batch_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index("ix_news_ai_batch", "news", ["ai_batch_id"])
    op.create_index("ix_news_ai_analyzed", "news", ["ai_analyzed_at"])
    op.execute("UPDATE news SET rule_sentiment=sentiment WHERE rule_sentiment IS NULL")


def downgrade() -> None:
    op.drop_index("ix_news_ai_analyzed", table_name="news")
    op.drop_index("ix_news_ai_batch", table_name="news")
    op.drop_constraint("fk_news_ai_batch_id_news_ai_batches", "news", type_="foreignkey")
    for column in (
        "ai_analyzed_at",
        "ai_batch_id",
        "ai_model",
        "ai_reason",
        "ai_category",
        "ai_time_horizon",
        "ai_impact_strength",
        "ai_confidence",
        "ai_sentiment",
        "related_us_stocks",
        "rule_sentiment",
    ):
        op.drop_column("news", column)
    op.drop_index("ix_news_ai_batches_status", table_name="news_ai_batches")
    op.drop_index("ix_news_ai_batches_created", table_name="news_ai_batches")
    op.drop_table("news_ai_batches")
