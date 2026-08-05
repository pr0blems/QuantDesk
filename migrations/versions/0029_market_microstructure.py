"""Add the latest per-symbol Binance order-book metrics.

Revision ID: 0029_market_microstructure
Revises: 0028_stock_library_zh
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0029_market_microstructure"
down_revision: str | None = "0028_stock_library_zh"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE_OPTIONS = {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"}


def _require_mysql() -> None:
    if op.get_bind().dialect.name not in {"mysql", "mariadb"}:
        raise RuntimeError("QuantDesk migrations require MySQL or MariaDB")


def upgrade() -> None:
    _require_mysql()
    op.create_table(
        "market_microstructure",
        sa.Column("symbol", sa.String(32), nullable=False),
        sa.Column("bid_depth_notional", sa.Numeric(30, 12), nullable=False),
        sa.Column("ask_depth_notional", sa.Numeric(30, 12), nullable=False),
        sa.Column("book_imbalance", sa.Numeric(12, 10), nullable=False),
        sa.Column("book_imbalance_5", sa.Numeric(12, 10), nullable=False),
        sa.Column("depth_levels", sa.Integer(), nullable=False),
        sa.Column("ts", sa.BigInteger(), nullable=False),
        sa.CheckConstraint(
            "bid_depth_notional >= 0",
            name="ck_market_microstructure_bid_notional_nonnegative",
        ),
        sa.CheckConstraint(
            "ask_depth_notional >= 0",
            name="ck_market_microstructure_ask_notional_nonnegative",
        ),
        sa.CheckConstraint(
            "book_imbalance BETWEEN -1 AND 1",
            name="ck_market_microstructure_book_imbalance_range",
        ),
        sa.CheckConstraint(
            "book_imbalance_5 BETWEEN -1 AND 1",
            name="ck_market_microstructure_book_imbalance_5_range",
        ),
        sa.CheckConstraint(
            "depth_levels BETWEEN 0 AND 100",
            name="ck_market_microstructure_depth_levels_range",
        ),
        sa.PrimaryKeyConstraint("symbol", name="pk_market_microstructure"),
        **TABLE_OPTIONS,
    )
    op.create_index(
        "ix_market_microstructure_ts",
        "market_microstructure",
        ["ts"],
        unique=False,
    )


def downgrade() -> None:
    _require_mysql()
    op.drop_index("ix_market_microstructure_ts", table_name="market_microstructure")
    op.drop_table("market_microstructure")
