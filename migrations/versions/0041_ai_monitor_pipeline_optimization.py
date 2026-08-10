"""Optimize pending-news selection for the AI monitor pipeline.

Revision ID: 0041_ai_monitor_pipeline
Revises: 0040_ai_monitor_candidates
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0041_ai_monitor_pipeline"
down_revision: str | None = "0040_ai_monitor_candidates"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _require_mysql() -> None:
    if op.get_bind().dialect.name not in {"mysql", "mariadb"}:
        raise RuntimeError("QuantDesk migrations require MySQL or MariaDB")


def upgrade() -> None:
    _require_mysql()
    indexes = {item["name"] for item in sa.inspect(op.get_bind()).get_indexes("news")}
    if "ix_news_ai_pending_ts" not in indexes:
        op.create_index(
            "ix_news_ai_pending_ts",
            "news",
            ["ai_analyzed_at", "ts"],
            unique=False,
        )


def downgrade() -> None:
    _require_mysql()
    indexes = {item["name"] for item in sa.inspect(op.get_bind()).get_indexes("news")}
    if "ix_news_ai_pending_ts" in indexes:
        op.drop_index("ix_news_ai_pending_ts", table_name="news")
