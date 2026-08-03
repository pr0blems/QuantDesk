"""Add per-user contract monitor preferences.

Revision ID: 0003_add_monitor_preferences
Revises: 0002_add_schema_comments
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003_add_monitor_preferences"
down_revision: str | None = "0002_add_schema_comments"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column(
            "monitor_watchlist",
            sa.JSON(),
            nullable=True,
            comment="当前用户的合约监控自选列表",
        ),
    )
    op.add_column(
        "users",
        sa.Column(
            "monitor_last_read_alert_id",
            sa.BigInteger(),
            server_default=sa.text("0"),
            nullable=False,
            comment="当前用户已读的最新监控信号 ID",
        ),
    )


def downgrade() -> None:
    op.drop_column("users", "monitor_last_read_alert_id")
    op.drop_column("users", "monitor_watchlist")
