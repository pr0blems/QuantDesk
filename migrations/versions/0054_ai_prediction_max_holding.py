"""Decouple prediction maximum holding time from the indicator timeframe.

Revision ID: 0054_ai_prediction_max_holding
Revises: 0053_news_ai_memory
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0054_ai_prediction_max_holding"
down_revision: str | None = "0053_news_ai_memory"
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
            "prediction_max_holding_bars",
            sa.Integer(),
            server_default=sa.text("4"),
            nullable=False,
            comment="Maximum number of indicator-timeframe bars held by a prediction",
        ),
    )
    op.create_check_constraint(
        "ck_ai_monitor_configs_valid_prediction_max_holding_bars",
        "ai_monitor_configs",
        "prediction_max_holding_bars BETWEEN 1 AND 24",
    )


def downgrade() -> None:
    _require_mysql()
    op.drop_constraint(
        "ck_ai_monitor_configs_valid_prediction_max_holding_bars",
        "ai_monitor_configs",
        type_="check",
    )
    op.drop_column("ai_monitor_configs", "prediction_max_holding_bars")
