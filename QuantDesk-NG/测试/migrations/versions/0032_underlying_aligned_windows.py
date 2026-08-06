"""Store rolling underlying returns on the same monitor windows as contracts.

Revision ID: 0032_underlying_aligned_windows
Revises: 0031_data_quality_archives
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0032_underlying_aligned_windows"
down_revision: str | None = "0031_data_quality_archives"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _require_mysql() -> None:
    if op.get_bind().dialect.name not in {"mysql", "mariadb"}:
        raise RuntimeError("QuantDesk migrations require MySQL or MariaDB")


def upgrade() -> None:
    _require_mysql()
    with op.batch_alter_table("underlying_market_quotes") as batch:
        batch.add_column(sa.Column("pct_2m", sa.Numeric(20, 8)))
        batch.add_column(sa.Column("pct_5m", sa.Numeric(20, 8)))
        batch.add_column(sa.Column("pct_10m", sa.Numeric(20, 8)))
        batch.add_column(sa.Column("pct_24h", sa.Numeric(20, 8)))


def downgrade() -> None:
    _require_mysql()
    with op.batch_alter_table("underlying_market_quotes") as batch:
        batch.drop_column("pct_24h")
        batch.drop_column("pct_10m")
        batch.drop_column("pct_5m")
        batch.drop_column("pct_2m")
