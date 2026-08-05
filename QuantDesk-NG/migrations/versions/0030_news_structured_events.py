"""Add structured macro fields and source-quality counters to news events.

Revision ID: 0030_news_structured_events
Revises: 0029_binance_derivatives_events
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0030_news_structured_events"
down_revision: str | None = "0029_binance_derivatives_events"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _require_mysql() -> None:
    if op.get_bind().dialect.name not in {"mysql", "mariadb"}:
        raise RuntimeError("QuantDesk migrations require MySQL or MariaDB")


def upgrade() -> None:
    _require_mysql()
    op.add_column("news_event_clusters", sa.Column("scheduled_at", sa.BigInteger(), nullable=True))
    op.add_column("news_event_clusters", sa.Column("actual_at", sa.BigInteger(), nullable=True))
    op.add_column("news_event_clusters", sa.Column("actual_value", sa.Numeric(30, 10), nullable=True))
    op.add_column("news_event_clusters", sa.Column("consensus_value", sa.Numeric(30, 10), nullable=True))
    op.add_column("news_event_clusters", sa.Column("surprise_value", sa.Numeric(30, 10), nullable=True))
    op.add_column("news_event_clusters", sa.Column("event_unit", sa.String(16), nullable=True))
    op.add_column("news_event_clusters", sa.Column("affected_symbols_json", sa.JSON(), nullable=True))
    op.add_column(
        "news_event_clusters",
        sa.Column("source_quality_score", sa.Numeric(10, 8), nullable=False, server_default="0"),
    )
    op.add_column(
        "news_event_clusters",
        sa.Column("confirmation_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.create_index(
        "ix_news_event_clusters_scheduled_at", "news_event_clusters", ["scheduled_at"]
    )


def downgrade() -> None:
    _require_mysql()
    op.drop_index("ix_news_event_clusters_scheduled_at", table_name="news_event_clusters")
    for name in (
        "confirmation_count",
        "source_quality_score",
        "affected_symbols_json",
        "event_unit",
        "surprise_value",
        "consensus_value",
        "actual_value",
        "actual_at",
        "scheduled_at",
    ):
        op.drop_column("news_event_clusters", name)
