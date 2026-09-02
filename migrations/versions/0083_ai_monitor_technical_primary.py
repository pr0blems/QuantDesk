"""Make technical and market-flow evidence the primary AI Monitor inputs.

Revision ID: 0083_ai_technical_primary
Revises: 0082_martingale_tp4_foundation
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0083_ai_technical_primary"
down_revision: str | None = "0082_martingale_tp4_foundation"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _require_mysql() -> None:
    if op.get_bind().dialect.name not in {"mysql", "mariadb"}:
        raise RuntimeError("QuantDesk migrations require MySQL or MariaDB")


def upgrade() -> None:
    _require_mysql()
    op.execute(
        sa.text(
            """
            UPDATE ai_monitor_configs
               SET news_score_weight = 20.00,
                   technical_score_weight = 50.00,
                   market_flow_score_weight = 30.00
             WHERE news_score_weight = 45.00
               AND technical_score_weight = 35.00
               AND market_flow_score_weight = 20.00
            """
        )
    )
    op.alter_column(
        "ai_monitor_configs",
        "news_score_weight",
        existing_type=sa.Numeric(5, 2),
        nullable=False,
        server_default="20.00",
    )
    op.alter_column(
        "ai_monitor_configs",
        "technical_score_weight",
        existing_type=sa.Numeric(5, 2),
        nullable=False,
        server_default="50.00",
    )
    op.alter_column(
        "ai_monitor_configs",
        "market_flow_score_weight",
        existing_type=sa.Numeric(5, 2),
        nullable=False,
        server_default="30.00",
    )


def downgrade() -> None:
    _require_mysql()
    op.alter_column(
        "ai_monitor_configs",
        "news_score_weight",
        existing_type=sa.Numeric(5, 2),
        nullable=False,
        server_default="45.00",
    )
    op.alter_column(
        "ai_monitor_configs",
        "technical_score_weight",
        existing_type=sa.Numeric(5, 2),
        nullable=False,
        server_default="35.00",
    )
    op.alter_column(
        "ai_monitor_configs",
        "market_flow_score_weight",
        existing_type=sa.Numeric(5, 2),
        nullable=False,
        server_default="20.00",
    )
