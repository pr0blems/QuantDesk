"""Add independent worker heartbeat records.

Revision ID: 0069_worker_heartbeats
Revises: 0068_strategy_promotion_reviews
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision: str = "0069_worker_heartbeats"
down_revision: str | None = "0068_strategy_promotion_reviews"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE_OPTIONS = {
    "mysql_engine": "InnoDB",
    "mysql_charset": "utf8mb4",
    "mysql_collate": "utf8mb4_bin",
}


def _require_mysql() -> None:
    if op.get_bind().dialect.name not in {"mysql", "mariadb"}:
        raise RuntimeError("QuantDesk migrations require MySQL or MariaDB")


def upgrade() -> None:
    _require_mysql()
    op.create_table(
        "worker_heartbeats",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("worker_type", sa.String(24), nullable=False),
        sa.Column("instance_key", sa.String(128), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("pid", sa.Integer(), nullable=False),
        sa.Column("host", sa.String(128), nullable=False),
        sa.Column("release_version", sa.String(64), nullable=False),
        sa.Column("details_json", sa.JSON(), nullable=True),
        sa.Column("started_at", mysql.DATETIME(fsp=6), nullable=False),
        sa.Column("last_seen_at", mysql.DATETIME(fsp=6), nullable=False),
        sa.Column("stopped_at", mysql.DATETIME(fsp=6), nullable=True),
        sa.Column(
            "updated_at",
            mysql.DATETIME(fsp=6),
            server_default=sa.text("CURRENT_TIMESTAMP(6)"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "worker_type IN ('market', 'shadow', 'paper', 'live', 'ai', 'settlement')",
            name="ck_worker_heartbeats_valid_worker_type",
        ),
        sa.CheckConstraint(
            "status IN ('starting', 'running', 'degraded', 'stopped', 'error')",
            name="ck_worker_heartbeats_valid_status",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_worker_heartbeats"),
        sa.UniqueConstraint(
            "worker_type",
            "instance_key",
            name="uq_worker_heartbeats_identity",
        ),
        comment="Independent worker liveness and release identity",
        **TABLE_OPTIONS,
    )
    op.create_index(
        "ix_worker_heartbeats_type_seen",
        "worker_heartbeats",
        ["worker_type", "last_seen_at"],
    )


def downgrade() -> None:
    _require_mysql()
    op.drop_index(
        "ix_worker_heartbeats_type_seen",
        table_name="worker_heartbeats",
    )
    op.drop_table("worker_heartbeats")
