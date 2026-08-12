"""Add user-configurable AI news analysis system prompt.

Revision ID: 0052_ai_news_prompt
Revises: 0051_ai_prediction_exit
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0052_ai_news_prompt"
down_revision: str | None = "0051_ai_prediction_exit"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _require_mysql() -> None:
    if op.get_bind().dialect.name not in {"mysql", "mariadb"}:
        raise RuntimeError("QuantDesk migrations require MySQL or MariaDB")


def upgrade() -> None:
    _require_mysql()
    op.add_column(
        "ai_monitor_configs",
        sa.Column(
            "news_system_prompt",
            sa.Text(),
            nullable=True,
            comment="User-configured system prompt for AI news analysis",
        ),
    )


def downgrade() -> None:
    _require_mysql()
    op.drop_column("ai_monitor_configs", "news_system_prompt")
