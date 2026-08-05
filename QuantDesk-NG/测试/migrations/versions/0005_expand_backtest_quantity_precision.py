"""Expand backtest trade quantity precision for very small positions.

Revision ID: 0005_backtest_qty_precision
Revises: 0004_add_backtest_tables
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005_backtest_qty_precision"
down_revision: str | None = "0004_add_backtest_tables"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column(
        "backtest_trades",
        "quantity",
        existing_type=sa.Numeric(precision=30, scale=12),
        type_=sa.Numeric(precision=48, scale=18),
        existing_nullable=False,
        existing_comment="成交标的数量",
        comment="成交标的数量，保留极小仓位精度",
    )


def downgrade() -> None:
    op.alter_column(
        "backtest_trades",
        "quantity",
        existing_type=sa.Numeric(precision=48, scale=18),
        type_=sa.Numeric(precision=30, scale=12),
        existing_nullable=False,
        existing_comment="成交标的数量，保留极小仓位精度",
        comment="成交标的数量",
    )
