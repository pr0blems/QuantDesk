"""Persist partial-depth liquidity pools for contract strength analysis.

Revision ID: 0027_order_depth_pool
Revises: 0026_price_snapshots
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0027_order_depth_pool"
down_revision: str | None = "0026_price_snapshots"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _require_mysql() -> None:
    if op.get_bind().dialect.name not in {"mysql", "mariadb"}:
        raise RuntimeError("QuantDesk migrations require MySQL or MariaDB")


def upgrade() -> None:
    _require_mysql()
    op.add_column("market_microstructure", sa.Column("bid_depth_qty", sa.Numeric(30, 8)))
    op.add_column("market_microstructure", sa.Column("ask_depth_qty", sa.Numeric(30, 8)))
    op.add_column("market_microstructure", sa.Column("bid_depth_notional", sa.Numeric(30, 8)))
    op.add_column("market_microstructure", sa.Column("ask_depth_notional", sa.Numeric(30, 8)))
    op.add_column("market_microstructure", sa.Column("book_imbalance_5", sa.Numeric(16, 8)))
    op.add_column(
        "market_microstructure",
        sa.Column("depth_levels", sa.Integer(), nullable=False, server_default="0"),
    )
    op.drop_constraint(
        "uq_prediction_feature_symbol_time",
        "prediction_feature_snapshots",
        type_="unique",
    )
    op.create_unique_constraint(
        "uq_prediction_feature_symbol_time_schema",
        "prediction_feature_snapshots",
        ["symbol", "as_of_ms", "feature_schema_version"],
    )


def downgrade() -> None:
    _require_mysql()
    op.execute(
        """DELETE prediction FROM battle_predictions prediction
           JOIN prediction_feature_snapshots feature
             ON feature.id=prediction.feature_snapshot_id
           WHERE feature.feature_schema_version=3"""
    )
    op.execute("DELETE FROM prediction_feature_snapshots WHERE feature_schema_version=3")
    op.drop_constraint(
        "uq_prediction_feature_symbol_time_schema",
        "prediction_feature_snapshots",
        type_="unique",
    )
    op.create_unique_constraint(
        "uq_prediction_feature_symbol_time",
        "prediction_feature_snapshots",
        ["symbol", "as_of_ms"],
    )
    op.drop_column("market_microstructure", "depth_levels")
    op.drop_column("market_microstructure", "book_imbalance_5")
    op.drop_column("market_microstructure", "ask_depth_notional")
    op.drop_column("market_microstructure", "bid_depth_notional")
    op.drop_column("market_microstructure", "ask_depth_qty")
    op.drop_column("market_microstructure", "bid_depth_qty")
