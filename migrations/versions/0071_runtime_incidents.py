"""Add operational incidents and the independent operations worker.

Revision ID: 0071_runtime_incidents
Revises: 0070_strategy_artifacts_manifests
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision: str = "0071_runtime_incidents"
down_revision: str | None = "0070_strategy_artifacts_manifests"
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
    op.drop_constraint(
        "ck_worker_heartbeats_valid_worker_type",
        "worker_heartbeats",
        type_="check",
    )
    op.create_check_constraint(
        "ck_worker_heartbeats_valid_worker_type",
        "worker_heartbeats",
        "worker_type IN ('market', 'shadow', 'paper', 'live', 'ai', 'ops', 'settlement')",
    )
    op.create_table(
        "runtime_incidents",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("public_id", sa.String(36), nullable=False),
        sa.Column("dedup_key", sa.String(191), nullable=False),
        sa.Column("severity", sa.String(4), nullable=False),
        sa.Column("category", sa.String(64), nullable=False),
        sa.Column("source_type", sa.String(32), nullable=False),
        sa.Column("source_key", sa.String(191), nullable=False),
        sa.Column("title", sa.String(191), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("details_json", sa.JSON(), nullable=False),
        sa.Column("occurrence_count", sa.Integer(), nullable=False),
        sa.Column("first_seen_at", mysql.DATETIME(fsp=6), nullable=False),
        sa.Column("last_seen_at", mysql.DATETIME(fsp=6), nullable=False),
        sa.Column("acknowledged_by_user_id", sa.BigInteger(), nullable=True),
        sa.Column("acknowledged_at", mysql.DATETIME(fsp=6), nullable=True),
        sa.Column("resolved_by_user_id", sa.BigInteger(), nullable=True),
        sa.Column("resolved_at", mysql.DATETIME(fsp=6), nullable=True),
        sa.Column("resolution_note", sa.String(500), nullable=True),
        sa.Column(
            "created_at",
            mysql.DATETIME(fsp=6),
            server_default=sa.text("CURRENT_TIMESTAMP(6)"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            mysql.DATETIME(fsp=6),
            server_default=sa.text("CURRENT_TIMESTAMP(6)"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "severity IN ('P0', 'P1', 'P2')", name="ck_runtime_incidents_valid_severity"
        ),
        sa.CheckConstraint(
            "status IN ('open', 'acknowledged', 'resolved')",
            name="ck_runtime_incidents_valid_status",
        ),
        sa.CheckConstraint(
            "occurrence_count > 0",
            name="ck_runtime_incidents_positive_occurrence_count",
        ),
        sa.ForeignKeyConstraint(
            ["acknowledged_by_user_id"],
            ["users.id"],
            name="fk_runtime_incidents_ack_user",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["resolved_by_user_id"],
            ["users.id"],
            name="fk_runtime_incidents_resolve_user",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_runtime_incidents"),
        sa.UniqueConstraint("public_id", name="uq_runtime_incidents_public_id"),
        sa.UniqueConstraint("dedup_key", name="uq_runtime_incidents_dedup_key"),
        comment="Deduplicated operational and trading safety incidents",
        **TABLE_OPTIONS,
    )
    op.create_index(
        "ix_runtime_incidents_status_severity",
        "runtime_incidents",
        ["status", "severity", "last_seen_at"],
    )
    op.create_index(
        "ix_runtime_incidents_source",
        "runtime_incidents",
        ["source_type", "source_key"],
    )


def downgrade() -> None:
    _require_mysql()
    op.drop_index("ix_runtime_incidents_source", table_name="runtime_incidents")
    op.drop_index("ix_runtime_incidents_status_severity", table_name="runtime_incidents")
    op.drop_table("runtime_incidents")
    op.drop_constraint(
        "ck_worker_heartbeats_valid_worker_type",
        "worker_heartbeats",
        type_="check",
    )
    op.create_check_constraint(
        "ck_worker_heartbeats_valid_worker_type",
        "worker_heartbeats",
        "worker_type IN ('market', 'shadow', 'paper', 'live', 'ai', 'settlement')",
    )
