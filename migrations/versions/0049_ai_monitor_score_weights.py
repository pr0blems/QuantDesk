"""Add configurable AI opportunity score weights.

Revision ID: 0049_ai_score_weights
Revises: 0048_market_flow_metrics
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0049_ai_score_weights"
down_revision: str | None = "0048_market_flow_metrics"
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
            "minimum_market_flow_quality",
            sa.Numeric(5, 4),
            nullable=False,
            server_default="0.5000",
        ),
    )
    op.add_column(
        "ai_monitor_configs",
        sa.Column(
            "news_score_weight", sa.Numeric(5, 2), nullable=False, server_default="45.00"
        ),
    )
    op.add_column(
        "ai_monitor_configs",
        sa.Column(
            "technical_score_weight", sa.Numeric(5, 2), nullable=False, server_default="35.00"
        ),
    )
    op.add_column(
        "ai_monitor_configs",
        sa.Column(
            "market_flow_score_weight", sa.Numeric(5, 2), nullable=False, server_default="20.00"
        ),
    )
    op.create_check_constraint(
        "valid_minimum_market_flow_quality",
        "ai_monitor_configs",
        "minimum_market_flow_quality BETWEEN 0 AND 1",
    )
    op.create_check_constraint(
        "valid_news_score_weight",
        "ai_monitor_configs",
        "news_score_weight BETWEEN 0 AND 100",
    )
    op.create_check_constraint(
        "valid_technical_score_weight",
        "ai_monitor_configs",
        "technical_score_weight BETWEEN 0 AND 100",
    )
    op.create_check_constraint(
        "valid_market_flow_score_weight",
        "ai_monitor_configs",
        "market_flow_score_weight BETWEEN 0 AND 100",
    )
    op.create_check_constraint(
        "valid_score_weight_total",
        "ai_monitor_configs",
        "news_score_weight + technical_score_weight + market_flow_score_weight = 100",
    )


def downgrade() -> None:
    _require_mysql()
    for name in (
        "valid_score_weight_total",
        "valid_market_flow_score_weight",
        "valid_technical_score_weight",
        "valid_news_score_weight",
        "valid_minimum_market_flow_quality",
    ):
        op.drop_constraint(name, "ai_monitor_configs", type_="check")
    for column in (
        "market_flow_score_weight",
        "technical_score_weight",
        "news_score_weight",
    ):
        op.drop_column("ai_monitor_configs", column)
    op.drop_column("ai_monitor_configs", "minimum_market_flow_quality")
