"""Store rolling contract price-direction counts.

Revision ID: 0022_contract_price_move_counts
Revises: 0021_binance_environment_parity
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0022_contract_price_move_counts"
down_revision: str | None = "0021_binance_environment_parity"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    if op.get_bind().dialect.name not in {"mysql", "mariadb"}:
        raise RuntimeError("QuantDesk migrations require MySQL or MariaDB")
    op.create_table(
        "contract_price_move_buckets",
        sa.Column("symbol", sa.String(32), nullable=False),
        sa.Column("bucket_ts", sa.BigInteger(), nullable=False),
        sa.Column("up_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("down_count", sa.Integer(), server_default="0", nullable=False),
        sa.PrimaryKeyConstraint("symbol", "bucket_ts"),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )
    op.create_index(
        "ix_contract_price_move_buckets_time",
        "contract_price_move_buckets",
        ["bucket_ts"],
    )


def downgrade() -> None:
    if op.get_bind().dialect.name not in {"mysql", "mariadb"}:
        raise RuntimeError("QuantDesk migrations require MySQL or MariaDB")
    op.drop_index(
        "ix_contract_price_move_buckets_time",
        table_name="contract_price_move_buckets",
    )
    op.drop_table("contract_price_move_buckets")
