"""Persist latest minute-bucket Finnhub US cash-equity quotes.

Revision ID: 0058_finnhub_quote_snapshots
Revises: 0057_opportunity_gate_decisions
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0058_finnhub_quote_snapshots"
down_revision: str | None = "0057_opportunity_gate_decisions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _require_mysql() -> None:
    if op.get_bind().dialect.name not in {"mysql", "mariadb"}:
        raise RuntimeError("QuantDesk migrations require MySQL or MariaDB")


def upgrade() -> None:
    _require_mysql()
    op.create_table(
        "finnhub_quote_snapshots",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("symbol", sa.String(length=32), nullable=False),
        sa.Column("bucket_at", sa.DateTime(), nullable=False),
        sa.Column("price", sa.Numeric(30, 12), nullable=False),
        sa.Column("change", sa.Numeric(30, 12), nullable=True),
        sa.Column("change_percent", sa.Numeric(20, 8), nullable=True),
        sa.Column("day_high", sa.Numeric(30, 12), nullable=True),
        sa.Column("day_low", sa.Numeric(30, 12), nullable=True),
        sa.Column("day_open", sa.Numeric(30, 12), nullable=True),
        sa.Column("previous_close", sa.Numeric(30, 12), nullable=True),
        sa.Column("volume", sa.Numeric(30, 8), nullable=True),
        sa.Column("source_timestamp", sa.BigInteger(), nullable=False),
        sa.Column("fetched_at", sa.DateTime(), nullable=False),
        sa.Column("live", sa.Boolean(), nullable=False),
        sa.Column("captured_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint("price > 0", name="ck_finnhub_quote_snapshots_positive_price"),
        sa.PrimaryKeyConstraint("id", name="pk_finnhub_quote_snapshots"),
        sa.UniqueConstraint(
            "symbol",
            "bucket_at",
            name="uq_finnhub_quote_snapshots_symbol_bucket",
        ),
        comment="Minute-bucket latest Finnhub US cash-equity quotes",
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )
    op.create_index(
        "ix_finnhub_quote_snapshots_symbol_source_time",
        "finnhub_quote_snapshots",
        ["symbol", "source_timestamp"],
    )


def downgrade() -> None:
    _require_mysql()
    op.drop_index(
        "ix_finnhub_quote_snapshots_symbol_source_time",
        table_name="finnhub_quote_snapshots",
    )
    op.drop_table("finnhub_quote_snapshots")
