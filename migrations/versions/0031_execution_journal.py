"""Add the durable, tenant-scoped execution idempotency journal.

Revision ID: 0031_execution_journal
Revises: 0030_ai_decision_ledger
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision: str = "0031_execution_journal"
down_revision: str | None = "0030_ai_decision_ledger"
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
        "execution_idempotency_records",
        sa.Column("scope_hash", sa.String(64), nullable=False),
        sa.Column("tenant_scope", sa.String(191), nullable=False),
        sa.Column("user_scope", sa.String(191), nullable=False),
        sa.Column("account_scope", sa.String(191), nullable=False),
        sa.Column("deployment_scope", sa.String(191), nullable=False),
        sa.Column("idempotency_key", sa.String(191), nullable=False),
        sa.Column("intent_id", sa.String(191), nullable=False),
        sa.Column("intent_hash", sa.String(64), nullable=False),
        sa.Column("intent_json", mysql.LONGTEXT(), nullable=False),
        sa.Column("claim_status", sa.String(16), nullable=False),
        sa.Column("claim_token", sa.String(64), nullable=False),
        sa.Column("lease_expires_at", mysql.DATETIME(fsp=6), nullable=False),
        sa.Column("recovery_token", sa.String(64), nullable=True),
        sa.Column("recovery_lease_expires_at", mysql.DATETIME(fsp=6), nullable=True),
        sa.Column("recovery_started_at", mysql.DATETIME(fsp=6), nullable=True),
        sa.Column(
            "recovery_epoch",
            sa.BigInteger(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column("checkpoint_hash", sa.String(64), nullable=True),
        sa.Column("checkpoint_json", mysql.LONGTEXT(), nullable=True),
        sa.Column("broker_name", sa.String(64), nullable=True),
        sa.Column("client_order_id", sa.String(36), nullable=True),
        sa.Column("checkpointed_at", mysql.DATETIME(fsp=6), nullable=True),
        sa.Column("execution_state", sa.String(32), nullable=True),
        sa.Column("result_hash", sa.String(64), nullable=True),
        sa.Column("result_json", mysql.LONGTEXT(), nullable=True),
        sa.Column(
            "claimed_at",
            mysql.DATETIME(fsp=6),
            server_default=sa.text("CURRENT_TIMESTAMP(6)"),
            nullable=False,
        ),
        sa.Column("completed_at", mysql.DATETIME(fsp=6), nullable=True),
        sa.Column(
            "updated_at",
            mysql.DATETIME(fsp=6),
            server_default=sa.text("CURRENT_TIMESTAMP(6)"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "claim_status IN ('in_progress', 'completed')",
            name="ck_execution_idempotency_claim_status",
        ),
        sa.CheckConstraint(
            "(claim_status = 'in_progress' AND execution_state IS NULL "
            "AND result_hash IS NULL AND result_json IS NULL AND completed_at IS NULL) "
            "OR (claim_status = 'completed' AND execution_state IS NOT NULL "
            "AND result_hash IS NOT NULL AND result_json IS NOT NULL "
            "AND completed_at IS NOT NULL)",
            name="ck_execution_idempotency_result_lifecycle",
        ),
        sa.CheckConstraint(
            "(checkpoint_hash IS NULL AND checkpoint_json IS NULL "
            "AND broker_name IS NULL AND client_order_id IS NULL "
            "AND checkpointed_at IS NULL) OR "
            "(checkpoint_hash IS NOT NULL AND checkpoint_json IS NOT NULL "
            "AND broker_name IS NOT NULL AND client_order_id IS NOT NULL "
            "AND checkpointed_at IS NOT NULL)",
            name="ck_execution_idempotency_checkpoint_lifecycle",
        ),
        sa.CheckConstraint(
            "(recovery_token IS NULL AND recovery_lease_expires_at IS NULL) OR "
            "(recovery_token IS NOT NULL AND recovery_lease_expires_at IS NOT NULL)",
            name="ck_execution_idempotency_recovery_lease",
        ),
        sa.CheckConstraint(
            "(recovery_started_at IS NULL AND recovery_epoch = 0 "
            "AND recovery_token IS NULL) OR "
            "(recovery_started_at IS NOT NULL AND recovery_epoch >= 1)",
            name="ck_execution_idempotency_recovery_epoch",
        ),
        sa.PrimaryKeyConstraint("scope_hash", name="pk_execution_idempotency_records"),
        comment=(
            "Fail-closed execution reservation and recoverable result per complete tenant scope"
        ),
        **TABLE_OPTIONS,
    )
    op.create_index(
        "ix_execution_idempotency_account_updated",
        "execution_idempotency_records",
        ["tenant_scope", "user_scope", "account_scope", "updated_at"],
        unique=False,
    )
    op.create_index(
        "ix_execution_idempotency_recovery",
        "execution_idempotency_records",
        [
            "claim_status",
            "execution_state",
            "recovery_lease_expires_at",
            "updated_at",
        ],
        unique=False,
    )


def downgrade() -> None:
    _require_mysql()
    op.drop_index(
        "ix_execution_idempotency_recovery",
        table_name="execution_idempotency_records",
    )
    op.drop_index(
        "ix_execution_idempotency_account_updated",
        table_name="execution_idempotency_records",
    )
    op.drop_table("execution_idempotency_records")
