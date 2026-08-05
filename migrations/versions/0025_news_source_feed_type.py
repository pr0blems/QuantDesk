"""Add explicit news source feed formats.

Revision ID: 0025_news_source_feed_type
Revises: 0024_prediction_history_index
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0025_news_source_feed_type"
down_revision: str | None = "0024_prediction_history_index"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "news_source_settings",
        sa.Column(
            "feed_type",
            sa.String(length=32),
            nullable=False,
            server_default="rss",
            comment="来源格式：rss 或 taoz_flash",
        ),
    )


def downgrade() -> None:
    op.drop_column("news_source_settings", "feed_type")
