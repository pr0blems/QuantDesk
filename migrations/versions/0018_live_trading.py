"""Add isolated Binance live strategy deployments and order intents.

Revision ID: 0018_live_trading
Revises: 0017_opportunity_states
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0018_live_trading"
down_revision: str | None = "0017_opportunity_states"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _require_mysql() -> None:
    if op.get_bind().dialect.name not in {"mysql", "mariadb"}:
        raise RuntimeError("QuantDesk migrations require MySQL or MariaDB")


def upgrade() -> None:
    _require_mysql()
    op.create_table(
        "live_trading_accounts",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("public_id", sa.String(36), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("strategy_id", sa.BigInteger(), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="paused"),
        sa.Column("config_json", sa.JSON(), nullable=False),
        sa.Column("strategy_snapshot_json", sa.JSON(), nullable=False),
        sa.Column("credential_version", sa.Integer(), nullable=False),
        sa.Column("armed_at", sa.DateTime(), nullable=True),
        sa.Column("last_tick_at", sa.DateTime(), nullable=True),
        sa.Column("last_error_code", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint(
            "status IN ('paused', 'active', 'archived', 'error')",
            name="ck_live_trading_accounts_valid_status",
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["strategy_id", "user_id"],
            ["user_strategies.id", "user_strategies.user_id"],
            name="fk_live_accounts_strategy_tenant",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("public_id", name="uq_live_accounts_public_id"),
        sa.UniqueConstraint("id", "user_id", name="uq_live_accounts_id_user_id"),
        sa.UniqueConstraint("user_id", "name", name="uq_live_accounts_user_name"),
        comment="用户隔离的 Binance 实盘策略部署；资金与仓位以交易所为准",
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )
    op.create_index(
        "ix_live_accounts_user_status_updated",
        "live_trading_accounts",
        ["user_id", "status", "updated_at"],
    )

    op.create_table(
        "live_order_intents",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("public_id", sa.String(36), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("live_account_id", sa.BigInteger(), nullable=False),
        sa.Column("deployment_id", sa.BigInteger(), nullable=False),
        sa.Column("signal_key", sa.String(191), nullable=False),
        sa.Column("client_order_id", sa.String(36), nullable=False),
        sa.Column("binance_order_id", sa.String(64), nullable=True),
        sa.Column("symbol", sa.String(32), nullable=False),
        sa.Column("action", sa.String(16), nullable=False),
        sa.Column("side", sa.String(4), nullable=False),
        sa.Column("order_type", sa.String(32), nullable=False),
        sa.Column("quantity", sa.Numeric(30, 18), nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="created"),
        sa.Column("request_json", sa.JSON(), nullable=False),
        sa.Column("response_json", sa.JSON(), nullable=True),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.Column("submitted_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint(
            "action IN ('open', 'close', 'stop', 'take_profit')",
            name="ck_live_order_intents_valid_action",
        ),
        sa.CheckConstraint("side IN ('BUY', 'SELL')", name="ck_live_order_intents_valid_side"),
        sa.CheckConstraint(
            "status IN ('created', 'submitted', 'filled', 'canceled', 'rejected', 'unknown')",
            name="ck_live_order_intents_valid_status",
        ),
        sa.ForeignKeyConstraint(
            ["live_account_id", "user_id"],
            ["live_trading_accounts.id", "live_trading_accounts.user_id"],
            name="fk_live_order_intents_account_tenant",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["deployment_id", "user_id"],
            ["strategy_deployments.id", "strategy_deployments.user_id"],
            name="fk_live_order_intents_deployment_tenant",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("public_id", name="uq_live_order_intents_public_id"),
        sa.UniqueConstraint("signal_key", name="uq_live_order_intents_signal_key"),
        sa.UniqueConstraint("client_order_id", name="uq_live_order_intents_client_order_id"),
        comment="Binance 实盘订单的幂等意图、脱敏响应与审计状态",
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )
    op.create_index(
        "ix_live_order_intents_user_created",
        "live_order_intents",
        ["user_id", "created_at"],
    )
    op.create_index(
        "ix_live_order_intents_account_symbol",
        "live_order_intents",
        ["live_account_id", "symbol", "status"],
    )


def downgrade() -> None:
    _require_mysql()
    op.drop_table("live_order_intents")
    op.drop_table("live_trading_accounts")
