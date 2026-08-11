"""Persist exact news AI prompts and raw model responses.

Revision ID: 0044_news_ai_call_audit
Revises: 0043_ai_monitor_claims
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision: str = "0044_news_ai_call_audit"
down_revision: str | None = "0043_ai_monitor_claims"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _require_mysql() -> None:
    if op.get_bind().dialect.name not in {"mysql", "mariadb"}:
        raise RuntimeError("QuantDesk migrations require MySQL or MariaDB")


def upgrade() -> None:
    _require_mysql()
    op.create_table(
        "news_ai_model_calls",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("batch_id", sa.String(length=36), nullable=False),
        sa.Column("call_type", sa.String(length=16), nullable=False),
        sa.Column("attempt_depth", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("provider_code", sa.String(length=32), nullable=False),
        sa.Column("model_name", sa.String(length=128), nullable=False),
        sa.Column("news_ids_json", sa.JSON(), nullable=False),
        sa.Column("request_json", sa.JSON(), nullable=False),
        sa.Column("response_text", mysql.LONGTEXT(), nullable=True),
        sa.Column("response_envelope", mysql.LONGTEXT(), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("error_category", sa.String(length=32), nullable=True),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("completed_at", sa.DateTime(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "call_type IN ('analysis', 'summary')",
            name=op.f("ck_news_ai_model_calls_valid_call_type"),
        ),
        sa.CheckConstraint(
            "status IN ('completed', 'failed')",
            name=op.f("ck_news_ai_model_calls_valid_status"),
        ),
        sa.ForeignKeyConstraint(
            ["batch_id"],
            ["news_ai_batches.id"],
            name=op.f("fk_news_ai_model_calls_batch_id_news_ai_batches"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_news_ai_model_calls")),
        comment="新闻 AI 模型调用的提示词、请求参数与原始响应审计记录",
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )
    op.create_index(
        "ix_news_ai_model_calls_batch",
        "news_ai_model_calls",
        ["batch_id", "created_at"],
        unique=False,
    )
    op.create_table(
        "news_ai_model_call_items",
        sa.Column("call_id", sa.BigInteger(), nullable=False),
        sa.Column("news_id", sa.String(length=255), nullable=False),
        sa.ForeignKeyConstraint(
            ["call_id"],
            ["news_ai_model_calls.id"],
            name=op.f("fk_news_ai_model_call_items_call_id_news_ai_model_calls"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["news_id"],
            ["news.id"],
            name=op.f("fk_news_ai_model_call_items_news_id_news"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "call_id", "news_id", name=op.f("pk_news_ai_model_call_items")
        ),
        comment="模型调用与新闻记录的可查询关联",
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )
    op.create_index(
        "ix_news_ai_model_call_items_news",
        "news_ai_model_call_items",
        ["news_id", "call_id"],
        unique=False,
    )
    op.add_column(
        "ai_monitor_opportunities",
        sa.Column(
            "news_ai_batch_ids_json",
            sa.JSON(),
            nullable=True,
            comment="机会首次生成时冻结的本租户新闻 AI 批次 ID",
        ),
    )
    op.add_column(
        "ai_monitor_opportunities",
        sa.Column(
            "news_ai_model_call_ids_json",
            sa.JSON(),
            nullable=True,
            comment="机会首次生成时冻结的本租户模型调用审计 ID",
        ),
    )


def downgrade() -> None:
    _require_mysql()
    op.drop_column("ai_monitor_opportunities", "news_ai_model_call_ids_json")
    op.drop_column("ai_monitor_opportunities", "news_ai_batch_ids_json")
    op.drop_index(
        "ix_news_ai_model_call_items_news", table_name="news_ai_model_call_items"
    )
    op.drop_table("news_ai_model_call_items")
    op.drop_index("ix_news_ai_model_calls_batch", table_name="news_ai_model_calls")
    op.drop_table("news_ai_model_calls")
