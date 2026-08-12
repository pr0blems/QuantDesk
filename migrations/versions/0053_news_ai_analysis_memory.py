"""Add seven-day rolling AI news analysis memory.

Revision ID: 0053_news_ai_memory
Revises: 0052_ai_news_prompt
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0053_news_ai_memory"
down_revision: str | None = "0052_ai_news_prompt"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _require_mysql() -> None:
    if op.get_bind().dialect.name not in {"mysql", "mariadb"}:
        raise RuntimeError("QuantDesk migrations require MySQL or MariaDB")


def upgrade() -> None:
    _require_mysql()
    op.create_table(
        "news_ai_analysis_records",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("batch_id", sa.String(length=36), nullable=False),
        sa.Column("news_id", sa.String(length=255), nullable=False),
        sa.Column("symbol", sa.String(length=16), nullable=False),
        sa.Column("direction", sa.String(length=16), nullable=False),
        sa.Column("confidence", sa.Numeric(precision=5, scale=4), nullable=False),
        sa.Column("relevance", sa.Numeric(precision=5, scale=4), nullable=False),
        sa.Column("impact_strength", sa.String(length=16), nullable=False),
        sa.Column("time_horizon", sa.String(length=32), nullable=False),
        sa.Column("category", sa.String(length=32), nullable=False),
        sa.Column("analysis_reason", sa.Text(), nullable=False),
        sa.Column("memory_effect", sa.String(length=16), nullable=False),
        sa.Column("memory_reason", sa.Text(), nullable=False),
        sa.Column("previous_direction", sa.String(length=16), nullable=True),
        sa.Column("previous_confidence", sa.Numeric(precision=5, scale=4), nullable=True),
        sa.Column("prior_record_id", sa.BigInteger(), nullable=True),
        sa.Column("context_record_ids_json", sa.JSON(), nullable=False),
        sa.Column("model_name", sa.String(length=128), nullable=False),
        sa.Column("news_published_at", sa.BigInteger(), nullable=False),
        sa.Column("analyzed_at", sa.DateTime(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "direction IN ('bull', 'neutral', 'bear')",
            name=op.f("ck_news_ai_analysis_records_valid_direction"),
        ),
        sa.CheckConstraint(
            "memory_effect IN ('initial', 'maintain', 'strengthen', 'weaken', 'reverse')",
            name=op.f("ck_news_ai_analysis_records_valid_memory_effect"),
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"],
            name=op.f("fk_news_ai_analysis_records_user_id_users"), ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["batch_id"], ["news_ai_batches.id"],
            name=op.f("fk_news_ai_analysis_records_batch_id_news_ai_batches"), ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["news_id"], ["news.id"],
            name=op.f("fk_news_ai_analysis_records_news_id_news"), ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["prior_record_id"], ["news_ai_analysis_records.id"],
            name="fk_news_ai_memory_prior_record",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_news_ai_analysis_records")),
        sa.UniqueConstraint(
            "batch_id", "news_id", "symbol",
            name="uq_news_ai_analysis_record_batch_news_symbol",
        ),
        comment="美股新闻 AI 一周滚动研判记忆与判断变化记录",
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )
    op.create_index(
        "ix_news_ai_analysis_records_user_symbol_time",
        "news_ai_analysis_records",
        ["user_id", "symbol", "analyzed_at"],
        unique=False,
    )
    op.create_index(
        "ix_news_ai_analysis_records_news",
        "news_ai_analysis_records",
        ["news_id", "analyzed_at"],
        unique=False,
    )


def downgrade() -> None:
    _require_mysql()
    op.drop_index(
        "ix_news_ai_analysis_records_news", table_name="news_ai_analysis_records"
    )
    op.drop_index(
        "ix_news_ai_analysis_records_user_symbol_time",
        table_name="news_ai_analysis_records",
    )
    op.drop_table("news_ai_analysis_records")
