"""Add forward outcomes for news decision calibration.

Revision ID: 0025_news_outcomes
Revises: 0024_news_intelligence
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0025_news_outcomes"
down_revision: str | None = "0024_news_intelligence"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _require_mysql() -> None:
    if op.get_bind().dialect.name not in {"mysql", "mariadb"}:
        raise RuntimeError("QuantDesk migrations require MySQL or MariaDB")


def upgrade() -> None:
    _require_mysql()
    op.create_table(
        "news_event_outcomes",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("decision_id", sa.BigInteger(), nullable=False),
        sa.Column("event_id", sa.BigInteger(), nullable=False),
        sa.Column("symbol", sa.String(32), nullable=False),
        sa.Column("horizon_seconds", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("predicted_direction", sa.String(16), nullable=False),
        sa.Column("entry_price", sa.Numeric(30, 12), nullable=False),
        sa.Column("exit_price", sa.Numeric(30, 12)),
        sa.Column("return_bps", sa.Numeric(20, 8)),
        sa.Column("signed_return_bps", sa.Numeric(20, 8)),
        sa.Column("directional_hit", sa.Boolean()),
        sa.Column("due_at", sa.BigInteger(), nullable=False),
        sa.Column("completed_at", sa.BigInteger()),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["decision_id"],
            ["news_decisions.id"],
            name="fk_news_event_outcomes_decision",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["event_id"],
            ["news_event_clusters.id"],
            name="fk_news_event_outcomes_event",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint(
            "decision_id", "horizon_seconds", name="uq_news_event_outcomes_horizon"
        ),
        sa.CheckConstraint(
            "status IN ('pending','completed','unavailable')",
            name="ck_news_event_outcomes_status",
        ),
        comment="新闻裁决的前向收益标签，用于走步校准和基准率验证",
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )
    op.create_index(
        "ix_news_event_outcomes_status_due", "news_event_outcomes", ["status", "due_at"]
    )
    op.create_index(
        "ix_news_event_outcomes_symbol_horizon",
        "news_event_outcomes",
        ["symbol", "horizon_seconds", "status"],
    )


def downgrade() -> None:
    _require_mysql()
    op.drop_index("ix_news_event_outcomes_symbol_horizon", table_name="news_event_outcomes")
    op.drop_index("ix_news_event_outcomes_status_due", table_name="news_event_outcomes")
    op.drop_table("news_event_outcomes")
