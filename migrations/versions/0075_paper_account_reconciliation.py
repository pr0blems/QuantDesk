"""Add paper balance ledger and reconciliation status.

Revision ID: 0075_paper_reconciliation
Revises: 0074_paper_projections
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision: str = "0075_paper_reconciliation"
down_revision: str | None = "0074_paper_projections"
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
        "paper_account_balance_checkpoints",
        sa.Column("paper_account_id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("baseline_balance", sa.Numeric(30, 8), nullable=False),
        sa.Column("baseline_execution_id", sa.BigInteger(), nullable=True),
        sa.Column("expected_balance", sa.Numeric(30, 8), nullable=False),
        sa.Column("last_execution_id", sa.BigInteger(), nullable=True),
        sa.Column(
            "checksum_version",
            sa.String(32),
            server_default="paper_balance_v1",
            nullable=False,
        ),
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
        sa.ForeignKeyConstraint(
            ["paper_account_id", "user_id"],
            ["paper_accounts.id", "paper_accounts.user_id"],
            name="fk_paper_balance_checkpoint_account",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "paper_account_id", name="pk_paper_account_balance_checkpoints"
        ),
        sa.UniqueConstraint(
            "paper_account_id",
            "user_id",
            name="uq_paper_balance_checkpoint_tenant",
        ),
        **TABLE_OPTIONS,
    )
    op.execute(
        """
        INSERT INTO paper_account_balance_checkpoints(
            paper_account_id,user_id,baseline_balance,baseline_execution_id,
            expected_balance,last_execution_id
        )
        SELECT a.id,a.user_id,a.balance,MAX(e.id),a.balance,MAX(e.id)
        FROM paper_accounts a
        LEFT JOIN paper_order_executions e
          ON e.paper_account_id=a.id AND e.user_id=a.user_id
        GROUP BY a.id,a.user_id,a.balance
        """
    )
    op.create_table(
        "paper_account_ledger_entries",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("public_id", sa.String(36), nullable=False),
        sa.Column("paper_account_id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("source_execution_id", sa.BigInteger(), nullable=False),
        sa.Column("entry_type", sa.String(16), nullable=False),
        sa.Column("amount", sa.Numeric(30, 8), nullable=False),
        sa.Column("balance_after", sa.Numeric(30, 8), nullable=False),
        sa.Column(
            "created_at",
            mysql.DATETIME(fsp=6),
            server_default=sa.text("CURRENT_TIMESTAMP(6)"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "entry_type IN ('open_debit', 'close_credit')",
            name="ck_paper_ledger_entry_type",
        ),
        sa.ForeignKeyConstraint(
            ["paper_account_id", "user_id"],
            ["paper_accounts.id", "paper_accounts.user_id"],
            name="fk_paper_ledger_account",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["source_execution_id"],
            ["paper_order_executions.id"],
            name="fk_paper_ledger_execution",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_paper_account_ledger_entries"),
        sa.UniqueConstraint("public_id", name="uq_paper_ledger_public_id"),
        sa.UniqueConstraint(
            "source_execution_id", name="uq_paper_ledger_source_execution"
        ),
        **TABLE_OPTIONS,
    )
    op.create_index(
        "ix_paper_ledger_account_created",
        "paper_account_ledger_entries",
        ["paper_account_id", "created_at"],
    )
    op.create_table(
        "paper_account_reconciliation_status",
        sa.Column("paper_account_id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("pending_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("drift_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("warning_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("details_json", sa.JSON(), nullable=True),
        sa.Column("last_success_at", mysql.DATETIME(fsp=6), nullable=True),
        sa.Column("last_error_at", mysql.DATETIME(fsp=6), nullable=True),
        sa.Column(
            "checked_at",
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
            "status IN ('healthy', 'warning', 'blocked')",
            name="ck_paper_reconciliation_status",
        ),
        sa.ForeignKeyConstraint(
            ["paper_account_id", "user_id"],
            ["paper_accounts.id", "paper_accounts.user_id"],
            name="fk_paper_reconciliation_account",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "paper_account_id", name="pk_paper_account_reconciliation_status"
        ),
        sa.UniqueConstraint(
            "paper_account_id",
            "user_id",
            name="uq_paper_reconciliation_tenant",
        ),
        **TABLE_OPTIONS,
    )


def downgrade() -> None:
    _require_mysql()
    op.drop_table("paper_account_reconciliation_status")
    op.drop_index(
        "ix_paper_ledger_account_created",
        table_name="paper_account_ledger_entries",
    )
    op.drop_table("paper_account_ledger_entries")
    op.drop_table("paper_account_balance_checkpoints")
