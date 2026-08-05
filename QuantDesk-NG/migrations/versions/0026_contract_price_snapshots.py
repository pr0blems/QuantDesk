"""Persist minute price snapshots for the contract monitor's five-minute change.

Revision ID: 0026_price_snapshots
Revises: 0025_news_outcomes
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0026_price_snapshots"
down_revision: str | None = "0025_news_outcomes"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _require_mysql() -> None:
    if op.get_bind().dialect.name not in {"mysql", "mariadb"}:
        raise RuntimeError("QuantDesk migrations require MySQL or MariaDB")


def upgrade() -> None:
    _require_mysql()
    op.create_table(
        "contract_price_snapshots",
        sa.Column("symbol", sa.String(32), nullable=False),
        sa.Column("bucket_ts", sa.BigInteger(), nullable=False),
        sa.Column("price", sa.Double(), nullable=False),
        sa.PrimaryKeyConstraint("symbol", "bucket_ts"),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
        comment="合约监控的分钟价格快照，用于计算近五分钟涨跌幅",
    )
    op.create_index(
        "ix_contract_price_snapshots_time",
        "contract_price_snapshots",
        ["bucket_ts"],
    )


def downgrade() -> None:
    _require_mysql()
    op.drop_index("ix_contract_price_snapshots_time", table_name="contract_price_snapshots")
    op.drop_table("contract_price_snapshots")
