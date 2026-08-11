"""Add rolling market-flow metrics to the latest depth snapshot.

Revision ID: 0048_market_flow_metrics
Revises: 0047_ai_prediction_costs
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0048_market_flow_metrics"
down_revision: str | None = "0047_ai_prediction_costs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _require_mysql() -> None:
    if op.get_bind().dialect.name not in {"mysql", "mariadb"}:
        raise RuntimeError("QuantDesk migrations require MySQL or MariaDB")


def upgrade() -> None:
    _require_mysql()
    columns = (
        sa.Column(
            "bid_depth_notional_5",
            sa.Numeric(30, 12),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "ask_depth_notional_5",
            sa.Numeric(30, 12),
            nullable=False,
            server_default="0",
        ),
        sa.Column("bid_level_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("ask_level_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("spread_bps", sa.Numeric(20, 8), nullable=True),
        sa.Column("bid_depth_change_5s_pct", sa.Numeric(20, 8), nullable=True),
        sa.Column("ask_depth_change_5s_pct", sa.Numeric(20, 8), nullable=True),
        sa.Column("bid_depth_change_30s_pct", sa.Numeric(20, 8), nullable=True),
        sa.Column("ask_depth_change_30s_pct", sa.Numeric(20, 8), nullable=True),
        sa.Column("imbalance_change_5s", sa.Numeric(20, 10), nullable=True),
    )
    for column in columns:
        op.add_column("market_microstructure", column)
    checks = (
        ("bid_notional_5_nonnegative", "bid_depth_notional_5 >= 0"),
        ("ask_notional_5_nonnegative", "ask_depth_notional_5 >= 0"),
        ("bid_level_count", "bid_level_count BETWEEN 0 AND 100"),
        ("ask_level_count", "ask_level_count BETWEEN 0 AND 100"),
        ("spread_bps", "spread_bps IS NULL OR spread_bps >= 0"),
        (
            "imbalance_change_5s",
            "imbalance_change_5s IS NULL OR imbalance_change_5s BETWEEN -2 AND 2",
        ),
    )
    for name, condition in checks:
        op.create_check_constraint(name, "market_microstructure", condition)


def downgrade() -> None:
    _require_mysql()
    for name in (
        "imbalance_change_5s",
        "spread_bps",
        "ask_level_count",
        "bid_level_count",
        "ask_notional_5_nonnegative",
        "bid_notional_5_nonnegative",
    ):
        op.drop_constraint(name, "market_microstructure", type_="check")
    for column in (
        "imbalance_change_5s",
        "ask_depth_change_30s_pct",
        "bid_depth_change_30s_pct",
        "ask_depth_change_5s_pct",
        "bid_depth_change_5s_pct",
        "spread_bps",
        "ask_level_count",
        "bid_level_count",
        "ask_depth_notional_5",
        "bid_depth_notional_5",
    ):
        op.drop_column("market_microstructure", column)
