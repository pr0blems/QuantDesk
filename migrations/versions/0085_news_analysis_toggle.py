"""Add an independent scheduled news-analysis switch.

Revision ID: 0085_news_analysis_toggle
Revises: 0084_strategy_parameter_profiles
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0085_news_analysis_toggle"
down_revision: str | None = "0084_strategy_parameter_profiles"
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
            "news_analysis_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("0"),
            comment="是否启用定时 AI 新闻分析",
        ),
    )


def downgrade() -> None:
    _require_mysql()
    op.drop_column("ai_monitor_configs", "news_analysis_enabled")
