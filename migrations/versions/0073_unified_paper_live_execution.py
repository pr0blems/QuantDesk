"""Persist paper fills and trusted Binance wallet bindings.

Revision ID: 0073_unified_execution
Revises: 0072_opportunity_expiry_index
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision: str = "0073_unified_execution"
down_revision: str | None = "0072_opportunity_expiry_index"
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
    op.add_column(
        "users",
        sa.Column("binance_physical_account_id", sa.String(191), nullable=True),
    )
    op.execute(
        """
        UPDATE users
        SET binance_physical_account_id = CONCAT('binance-usdm:', REPLACE(UUID(), '-', ''))
        WHERE binance_api_key_encrypted IS NOT NULL
          AND binance_physical_account_id IS NULL
        """
    )
    op.create_unique_constraint(
        "uq_users_binance_physical_account_id",
        "users",
        ["binance_physical_account_id"],
    )
    op.create_table(
        "paper_order_executions",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("public_id", sa.String(36), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("paper_account_id", sa.BigInteger(), nullable=False),
        sa.Column("deployment_id", sa.BigInteger(), nullable=False),
        sa.Column("intent_id", sa.String(191), nullable=False),
        sa.Column("client_order_id", sa.String(36), nullable=False),
        sa.Column("symbol", sa.String(32), nullable=False),
        sa.Column("action", sa.String(16), nullable=False),
        sa.Column("side", sa.String(4), nullable=False),
        sa.Column("position_side", sa.String(8), nullable=False),
        sa.Column("quantity", sa.Numeric(30, 18), nullable=False),
        sa.Column("executed_quantity", sa.Numeric(30, 18), nullable=False),
        sa.Column("average_price", sa.Numeric(30, 18), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("response_json", sa.JSON(), nullable=True),
        sa.Column("created_at", mysql.DATETIME(fsp=6), server_default=sa.text("CURRENT_TIMESTAMP(6)"), nullable=False),
        sa.Column("updated_at", mysql.DATETIME(fsp=6), server_default=sa.text("CURRENT_TIMESTAMP(6)"), nullable=False),
        sa.CheckConstraint("action IN ('open', 'close')", name="ck_paper_order_action"),
        sa.CheckConstraint("side IN ('BUY', 'SELL')", name="ck_paper_order_side"),
        sa.CheckConstraint("position_side IN ('BOTH', 'LONG', 'SHORT')", name="ck_paper_order_position_side"),
        sa.CheckConstraint("status IN ('FILLED', 'REJECTED', 'UNKNOWN')", name="ck_paper_order_status"),
        sa.ForeignKeyConstraint(
            ["paper_account_id", "user_id"],
            ["paper_accounts.id", "paper_accounts.user_id"],
            name="fk_paper_order_account_tenant",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["deployment_id", "user_id"],
            ["strategy_deployments.id", "strategy_deployments.user_id"],
            name="fk_paper_order_deployment_tenant",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_paper_order_executions"),
        sa.UniqueConstraint("public_id", name="uq_paper_order_public_id"),
        sa.UniqueConstraint("intent_id", name="uq_paper_order_intent_id"),
        sa.UniqueConstraint("client_order_id", name="uq_paper_order_client_order_id"),
        comment="Durable simulated broker fills used for restart-safe reconciliation",
        **TABLE_OPTIONS,
    )
    op.create_index(
        "ix_paper_order_account_created",
        "paper_order_executions",
        ["paper_account_id", "created_at"],
    )


def downgrade() -> None:
    _require_mysql()
    op.drop_index("ix_paper_order_account_created", table_name="paper_order_executions")
    op.drop_table("paper_order_executions")
    op.drop_constraint("uq_users_binance_physical_account_id", "users", type_="unique")
    op.drop_column("users", "binance_physical_account_id")

