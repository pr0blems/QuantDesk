"""Add durable live canary observation windows.

Revision ID: 0077_live_canary_observations
Revises: 0076_disable_legacy_paper
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0077_live_canary_observations"
down_revision: str | None = "0076_disable_legacy_paper"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _require_mysql() -> None:
    if op.get_bind().dialect.name not in {"mysql", "mariadb"}:
        raise RuntimeError("QuantDesk migrations require MySQL or MariaDB")


def upgrade() -> None:
    _require_mysql()
    op.create_table(
        "live_canary_runs",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("public_id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("live_account_id", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("window_seconds", sa.Integer(), nullable=False),
        sa.Column("minimum_open_fills", sa.Integer(), nullable=False),
        sa.Column("sample_count", sa.Integer(), nullable=False),
        sa.Column("violation_count", sa.Integer(), nullable=False),
        sa.Column("failure_codes_json", sa.JSON(), nullable=False),
        sa.Column("metrics_json", sa.JSON(), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("due_at", sa.DateTime(), nullable=False),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "status IN ('running', 'passed', 'failed', 'canceled')",
            name="ck_live_canary_runs_valid_status",
        ),
        sa.CheckConstraint(
            "window_seconds >= 900",
            name="ck_live_canary_runs_minimum_window",
        ),
        sa.CheckConstraint(
            "minimum_open_fills >= 0",
            name="ck_live_canary_runs_valid_minimum_open_fills",
        ),
        sa.ForeignKeyConstraint(
            ["live_account_id", "user_id"],
            ["live_trading_accounts.id", "live_trading_accounts.user_id"],
            name="fk_live_canary_runs_account_tenant",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_live_canary_runs"),
        sa.UniqueConstraint("public_id", name="uq_live_canary_runs_public_id"),
        comment="Durable production canary acceptance window; observation only",
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )
    op.create_index(
        "ix_live_canary_runs_account_status_due",
        "live_canary_runs",
        ["live_account_id", "status", "due_at"],
    )
    op.create_table(
        "live_canary_samples",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("run_id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("live_account_id", sa.BigInteger(), nullable=False),
        sa.Column("passed", sa.Boolean(), nullable=False),
        sa.Column("failure_codes_json", sa.JSON(), nullable=False),
        sa.Column("metrics_json", sa.JSON(), nullable=False),
        sa.Column("sampled_at", sa.DateTime(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["live_canary_runs.id"],
            name="fk_live_canary_samples_run",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["live_account_id", "user_id"],
            ["live_trading_accounts.id", "live_trading_accounts.user_id"],
            name="fk_live_canary_samples_account_tenant",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_live_canary_samples"),
        comment="Append-only production canary safety observations",
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )
    op.create_index(
        "ix_live_canary_samples_run_sampled",
        "live_canary_samples",
        ["run_id", "sampled_at"],
    )


def downgrade() -> None:
    _require_mysql()
    op.drop_index(
        "ix_live_canary_samples_run_sampled",
        table_name="live_canary_samples",
    )
    op.drop_table("live_canary_samples")
    op.drop_index(
        "ix_live_canary_runs_account_status_due",
        table_name="live_canary_runs",
    )
    op.drop_table("live_canary_runs")
