"""Add current underlying-market quotes for contract comparison rows.

Revision ID: 0034_underlying_quotes
Revises: 0033_legacy_paper_mode
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0034_underlying_quotes"
down_revision: str | None = "0033_legacy_paper_mode"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _require_mysql() -> None:
    if op.get_bind().dialect.name not in {"mysql", "mariadb"}:
        raise RuntimeError("QuantDesk migrations require MySQL or MariaDB")


def upgrade() -> None:
    _require_mysql()
    op.create_table(
        "underlying_market_quotes",
        sa.Column("contract_symbol", sa.String(32), nullable=False),
        sa.Column("quote_symbol", sa.String(32)),
        sa.Column("relation", sa.String(24), nullable=False),
        sa.Column("instrument_type", sa.String(32), nullable=False),
        sa.Column("display_name", sa.String(191)),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("market_state", sa.String(24), nullable=False),
        sa.Column("currency", sa.String(12)),
        sa.Column("exchange_name", sa.String(64)),
        sa.Column("price", sa.Numeric(30, 12)),
        sa.Column("previous_close", sa.Numeric(30, 12)),
        sa.Column("change_pct", sa.Numeric(20, 8)),
        sa.Column("regular_market_price", sa.Numeric(30, 12)),
        sa.Column("day_open", sa.Numeric(30, 12)),
        sa.Column("day_high", sa.Numeric(30, 12)),
        sa.Column("day_low", sa.Numeric(30, 12)),
        sa.Column("volume", sa.Numeric(30, 4)),
        sa.Column("market_time_ms", sa.BigInteger()),
        sa.Column("received_at_ms", sa.BigInteger(), nullable=False),
        sa.Column("quality_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("contract_symbol"),
        sa.CheckConstraint(
            "status IN ('ok','stale','unavailable','unsupported')",
            name="ck_underlying_market_quotes_status",
        ),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
        comment="合约对应股票、ETF、商品或原生市场标的的当前行情",
    )
    op.create_index(
        "ix_underlying_market_quotes_time",
        "underlying_market_quotes",
        ["market_time_ms"],
    )


def downgrade() -> None:
    _require_mysql()
    op.drop_index("ix_underlying_market_quotes_time", table_name="underlying_market_quotes")
    op.drop_table("underlying_market_quotes")
