"""Persist Binance public derivatives risk snapshots and liquidation events.

Revision ID: 0029_binance_derivatives_events
Revises: 0028_underlying_quotes
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0029_binance_derivatives_events"
down_revision: str | None = "0028_underlying_quotes"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _require_mysql() -> None:
    if op.get_bind().dialect.name not in {"mysql", "mariadb"}:
        raise RuntimeError("QuantDesk migrations require MySQL or MariaDB")


def upgrade() -> None:
    _require_mysql()
    op.create_table(
        "binance_derivative_snapshots",
        sa.Column("symbol", sa.String(32), nullable=False),
        sa.Column("event_time", sa.BigInteger(), nullable=False),
        sa.Column("received_at", sa.BigInteger(), nullable=False),
        sa.Column("basis", sa.Numeric(30, 12)),
        sa.Column("basis_rate", sa.Numeric(30, 12)),
        sa.Column("adl_json", sa.JSON(), nullable=False),
        sa.Column("raw_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("symbol", "event_time"),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
        comment="Binance Basis/ADL public derivative risk snapshots",
    )
    op.create_index(
        "ix_binance_derivative_snapshots_time",
        "binance_derivative_snapshots",
        ["event_time"],
    )
    op.create_table(
        "binance_liquidation_events",
        sa.Column("event_id", sa.String(128), nullable=False),
        sa.Column("symbol", sa.String(32), nullable=False),
        sa.Column("event_time", sa.BigInteger(), nullable=False),
        sa.Column("trade_time", sa.BigInteger()),
        sa.Column("side", sa.String(8)),
        sa.Column("order_type", sa.String(24)),
        sa.Column("time_in_force", sa.String(8)),
        sa.Column("price", sa.Numeric(30, 12)),
        sa.Column("average_price", sa.Numeric(30, 12)),
        sa.Column("quantity", sa.Numeric(30, 12)),
        sa.Column("notional", sa.Numeric(30, 12)),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("event_id"),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
        comment="Binance public liquidation events from websocket/REST",
    )
    op.create_index(
        "ix_binance_liquidation_events_symbol_time",
        "binance_liquidation_events",
        ["symbol", "event_time"],
    )


def downgrade() -> None:
    _require_mysql()
    op.drop_index(
        "ix_binance_liquidation_events_symbol_time",
        table_name="binance_liquidation_events",
    )
    op.drop_table("binance_liquidation_events")
    op.drop_index(
        "ix_binance_derivative_snapshots_time",
        table_name="binance_derivative_snapshots",
    )
    op.drop_table("binance_derivative_snapshots")
