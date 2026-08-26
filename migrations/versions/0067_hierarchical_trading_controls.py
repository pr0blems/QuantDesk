"""Add hierarchical fail-closed trading controls.

Revision ID: 0067_hierarchical_trading_controls
Revises: 0066_strategy_revision_lifecycle
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision: str = "0067_hierarchical_trading_controls"
down_revision: str | None = "0066_strategy_revision_lifecycle"
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
        "trading_control_latches",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("public_id", sa.String(36), nullable=False),
        sa.Column("owner_user_id", sa.BigInteger(), nullable=True),
        sa.Column("owner_scope", sa.String(64), nullable=False),
        sa.Column("scope_type", sa.String(32), nullable=False),
        sa.Column("scope_key", sa.String(191), nullable=False),
        sa.Column("engaged", sa.Boolean(), server_default=sa.text("0"), nullable=False),
        sa.Column("reason_code", sa.String(64), nullable=True),
        sa.Column("reason_text", sa.String(500), nullable=True),
        sa.Column("version", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("changed_by_user_id", sa.BigInteger(), nullable=True),
        sa.Column(
            "changed_at",
            mysql.DATETIME(fsp=6),
            server_default=sa.text("CURRENT_TIMESTAMP(6)"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            mysql.DATETIME(fsp=6),
            server_default=sa.text("CURRENT_TIMESTAMP(6)"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "scope_type IN ('global', 'account', 'strategy_revision', 'symbol', "
            "'data_source', 'broker_connection')",
            name="ck_trading_control_latches_valid_scope_type",
        ),
        sa.CheckConstraint(
            "version >= 0",
            name="ck_trading_control_latches_non_negative_version",
        ),
        sa.ForeignKeyConstraint(
            ["owner_user_id"],
            ["users.id"],
            name="fk_trading_control_latches_owner_user_id_users",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["changed_by_user_id"],
            ["users.id"],
            name="fk_trading_control_latches_changed_by_user_id_users",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_trading_control_latches"),
        sa.UniqueConstraint("public_id", name="uq_trading_control_latches_public_id"),
        sa.UniqueConstraint(
            "owner_scope",
            "scope_type",
            "scope_key",
            name="uq_trading_control_latches_scope",
        ),
        comment="Persisted global/account/strategy/symbol/data/broker trading latches",
        **TABLE_OPTIONS,
    )
    op.create_index(
        "ix_trading_control_latches_active",
        "trading_control_latches",
        ["engaged", "scope_type"],
        unique=False,
    )
    op.create_index(
        "ix_trading_control_latches_owner",
        "trading_control_latches",
        ["owner_scope", "changed_at"],
        unique=False,
    )

    op.create_table(
        "trading_control_events",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("public_id", sa.String(36), nullable=False),
        sa.Column("command_id", sa.String(36), nullable=False),
        sa.Column("latch_id", sa.BigInteger(), nullable=False),
        sa.Column("actor_user_id", sa.BigInteger(), nullable=False),
        sa.Column("action", sa.String(16), nullable=False),
        sa.Column("expected_version", sa.Integer(), nullable=False),
        sa.Column("resulting_version", sa.Integer(), nullable=False),
        sa.Column("reason_code", sa.String(64), nullable=False),
        sa.Column("reason_text", sa.String(500), nullable=False),
        sa.Column("metadata_json", sa.JSON(), nullable=True),
        sa.Column(
            "created_at",
            mysql.DATETIME(fsp=6),
            server_default=sa.text("CURRENT_TIMESTAMP(6)"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "action IN ('engage', 'release')",
            name="ck_trading_control_events_valid_action",
        ),
        sa.CheckConstraint(
            "expected_version >= 0 AND resulting_version > expected_version",
            name="ck_trading_control_events_valid_version_transition",
        ),
        sa.ForeignKeyConstraint(
            ["latch_id"],
            ["trading_control_latches.id"],
            name="fk_trading_control_events_latch_id_trading_control_latches",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["actor_user_id"],
            ["users.id"],
            name="fk_trading_control_events_actor_user_id_users",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_trading_control_events"),
        sa.UniqueConstraint("public_id", name="uq_trading_control_events_public_id"),
        sa.UniqueConstraint("command_id", name="uq_trading_control_events_command_id"),
        comment="Append-only audit evidence for hierarchical trading controls",
        **TABLE_OPTIONS,
    )
    op.create_index(
        "ix_trading_control_events_latch_created",
        "trading_control_events",
        ["latch_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_trading_control_events_actor_created",
        "trading_control_events",
        ["actor_user_id", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    _require_mysql()
    op.drop_index(
        "ix_trading_control_events_actor_created",
        table_name="trading_control_events",
    )
    op.drop_index(
        "ix_trading_control_events_latch_created",
        table_name="trading_control_events",
    )
    op.drop_table("trading_control_events")
    op.drop_index(
        "ix_trading_control_latches_owner",
        table_name="trading_control_latches",
    )
    op.drop_index(
        "ix_trading_control_latches_active",
        table_name="trading_control_latches",
    )
    op.drop_table("trading_control_latches")
