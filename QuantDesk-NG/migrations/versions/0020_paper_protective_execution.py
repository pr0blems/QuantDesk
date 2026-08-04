"""Make paper protective exits resistant to transient price crossings.

Revision ID: 0020_paper_protective_execution
Revises: 0019_opportunity_intelligence
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0020_paper_protective_execution"
down_revision: str | None = "0019_opportunity_intelligence"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _require_mysql() -> None:
    if op.get_bind().dialect.name not in {"mysql", "mariadb"}:
        raise RuntimeError("QuantDesk migrations require MySQL or MariaDB")


def upgrade() -> None:
    _require_mysql()
    op.add_column(
        "market_microstructure",
        sa.Column("window_low_price", sa.Numeric(30, 12), nullable=True),
    )
    op.add_column(
        "market_microstructure",
        sa.Column("window_low_event_time", sa.BigInteger(), nullable=True),
    )
    op.add_column(
        "market_microstructure",
        sa.Column("window_high_price", sa.Numeric(30, 12), nullable=True),
    )
    op.add_column(
        "market_microstructure",
        sa.Column("window_high_event_time", sa.BigInteger(), nullable=True),
    )
    op.add_column(
        "paper_positions",
        sa.Column("protection_started_at_ms", sa.BigInteger(), nullable=True),
    )
    op.execute(
        "UPDATE paper_positions SET protection_started_at_ms=opened_ts*1000 "
        "WHERE protection_started_at_ms IS NULL"
    )
    op.alter_column(
        "paper_positions",
        "protection_started_at_ms",
        existing_type=sa.BigInteger(),
        nullable=False,
    )


def downgrade() -> None:
    _require_mysql()
    op.drop_column("paper_positions", "protection_started_at_ms")
    op.drop_column("market_microstructure", "window_high_event_time")
    op.drop_column("market_microstructure", "window_high_price")
    op.drop_column("market_microstructure", "window_low_event_time")
    op.drop_column("market_microstructure", "window_low_price")
