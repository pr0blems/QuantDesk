"""Add worker leases and controlled execution domain foundation.

Revision ID: 0018_execution_foundation
Revises: 0017_opportunity_states
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0018_execution_foundation"
down_revision: str | None = "0017_opportunity_states"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _require_mysql() -> None:
    if op.get_bind().dialect.name not in {"mysql", "mariadb"}:
        raise RuntimeError("QuantDesk migrations require MySQL or MariaDB")


def upgrade() -> None:
    _require_mysql()
    op.create_table(
        "worker_leases",
        sa.Column("worker_key", sa.String(100), primary_key=True, comment="Worker 角色键"),
        sa.Column("owner_id", sa.String(36), nullable=False, comment="本次进程租约 UUID"),
        sa.Column("metadata_json", sa.JSON(), nullable=False, comment="主机和进程元数据"),
        sa.Column("acquired_at", sa.DateTime(), nullable=False, comment="取得租约时间"),
        sa.Column("heartbeat_at", sa.DateTime(), nullable=False, comment="最后心跳时间"),
        sa.Column("expires_at", sa.DateTime(), nullable=False, comment="租约失效时间"),
        comment="后台 Worker 角色的可续租单实例运行权",
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )
    op.create_index("ix_worker_leases_expires_at", "worker_leases", ["expires_at"])

    op.create_table(
        "exchange_accounts",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("public_id", sa.String(36), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("venue", sa.String(32), nullable=False, server_default="binance"),
        sa.Column("account_type", sa.String(32), nullable=False, server_default="portfolio_margin"),
        sa.Column("environment", sa.String(16), nullable=False, server_default="demo"),
        sa.Column("status", sa.String(16), nullable=False, server_default="disabled"),
        sa.Column("credential_fingerprint", sa.String(32)),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "environment IN ('demo', 'live')", name="ck_exchange_accounts_valid_environment"
        ),
        sa.CheckConstraint(
            "status IN ('disabled', 'read_only', 'shadow', 'canary', 'enabled')",
            name="ck_exchange_accounts_valid_status",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name="fk_exchange_accounts_user", ondelete="CASCADE"
        ),
        sa.UniqueConstraint("public_id", name="uq_exchange_accounts_public_id"),
        sa.UniqueConstraint("id", "user_id", name="uq_exchange_accounts_id_user_id"),
        sa.UniqueConstraint("user_id", "name", name="uq_exchange_accounts_user_name"),
        comment="用户隔离的交易所账户与分阶段执行门禁",
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )
    op.create_index("ix_exchange_accounts_user_status", "exchange_accounts", ["user_id", "status"])

    op.create_table(
        "risk_decisions",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("public_id", sa.String(36), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("exchange_account_id", sa.BigInteger(), nullable=False),
        sa.Column("subject_type", sa.String(32), nullable=False),
        sa.Column("subject_id", sa.String(64), nullable=False),
        sa.Column("decision", sa.String(16), nullable=False),
        sa.Column("reason_codes_json", sa.JSON(), nullable=False),
        sa.Column("limits_json", sa.JSON(), nullable=False),
        sa.Column("exposure_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "decision IN ('approved', 'rejected')", name="ck_risk_decisions_valid_decision"
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name="fk_risk_decisions_user", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["exchange_account_id"],
            ["exchange_accounts.id"],
            name="fk_risk_decisions_account",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("public_id", name="uq_risk_decisions_public_id"),
        comment="不可变的交易前风控审批证据",
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )
    op.create_index("ix_risk_decisions_user_created", "risk_decisions", ["user_id", "created_at"])

    op.create_table(
        "order_intents",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("public_id", sa.String(36), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("exchange_account_id", sa.BigInteger(), nullable=False),
        sa.Column("deployment_id", sa.BigInteger()),
        sa.Column("signal_id", sa.BigInteger()),
        sa.Column("risk_decision_id", sa.BigInteger()),
        sa.Column("idempotency_key", sa.String(191), nullable=False),
        sa.Column("symbol", sa.String(32), nullable=False),
        sa.Column("side", sa.String(8), nullable=False),
        sa.Column("position_side", sa.String(8), nullable=False, server_default="BOTH"),
        sa.Column("order_type", sa.String(24), nullable=False),
        sa.Column("time_in_force", sa.String(8)),
        sa.Column("quantity", sa.Numeric(48, 18), nullable=False),
        sa.Column("price", sa.Numeric(30, 12)),
        sa.Column("stop_price", sa.Numeric(30, 12)),
        sa.Column("leverage", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("reduce_only", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("state", sa.String(24), nullable=False, server_default="created"),
        sa.Column("expires_at", sa.DateTime()),
        sa.Column("error_code", sa.String(64)),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint("side IN ('buy', 'sell')", name="ck_order_intents_valid_side"),
        sa.CheckConstraint("quantity > 0", name="ck_order_intents_positive_quantity"),
        sa.ForeignKeyConstraint(
            ["exchange_account_id", "user_id"],
            ["exchange_accounts.id", "exchange_accounts.user_id"],
            name="fk_order_intents_account_tenant",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["deployment_id"],
            ["strategy_deployments.id"],
            name="fk_order_intents_deployment",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["signal_id"],
            ["strategy_signals.id"],
            name="fk_order_intents_signal",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["risk_decision_id"],
            ["risk_decisions.id"],
            name="fk_order_intents_risk_decision",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("public_id", name="uq_order_intents_public_id"),
        sa.UniqueConstraint(
            "exchange_account_id",
            "idempotency_key",
            name="uq_order_intents_account_idempotency",
        ),
        comment="通过风控前后均可审计的幂等订单意图",
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )
    op.create_index(
        "ix_order_intents_user_state_created",
        "order_intents",
        ["user_id", "state", "created_at"],
    )

    op.create_table(
        "exchange_orders",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("intent_id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("exchange_account_id", sa.BigInteger(), nullable=False),
        sa.Column("exchange_order_id", sa.String(64)),
        sa.Column("client_order_id", sa.String(64), nullable=False),
        sa.Column("state", sa.String(24), nullable=False),
        sa.Column("requested_quantity", sa.Numeric(48, 18), nullable=False),
        sa.Column("filled_quantity", sa.Numeric(48, 18), nullable=False, server_default="0"),
        sa.Column("average_price", sa.Numeric(30, 12)),
        sa.Column("raw_json", sa.JSON()),
        sa.Column("submitted_at", sa.DateTime()),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["intent_id"],
            ["order_intents.id"],
            name="fk_exchange_orders_intent",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name="fk_exchange_orders_user", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["exchange_account_id"],
            ["exchange_accounts.id"],
            name="fk_exchange_orders_account",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("intent_id", name="uq_exchange_orders_intent_id"),
        sa.UniqueConstraint(
            "exchange_account_id", "exchange_order_id", name="uq_exchange_orders_account_order"
        ),
        sa.UniqueConstraint(
            "exchange_account_id", "client_order_id", name="uq_exchange_orders_account_client"
        ),
        comment="交易所确认后的订单当前状态快照",
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )
    op.create_index(
        "ix_exchange_orders_account_state", "exchange_orders", ["exchange_account_id", "state"]
    )

    op.create_table(
        "order_events",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("exchange_order_id", sa.BigInteger(), nullable=False),
        sa.Column("event_key", sa.String(191), nullable=False),
        sa.Column("event_type", sa.String(32), nullable=False),
        sa.Column("previous_state", sa.String(24)),
        sa.Column("next_state", sa.String(24), nullable=False),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.Column("exchange_ts", sa.DateTime()),
        sa.Column("received_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["exchange_order_id"],
            ["exchange_orders.id"],
            name="fk_order_events_order",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("event_key", name="uq_order_events_event_key"),
        comment="只追加的订单状态与交易所回报事件",
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )
    op.create_index(
        "ix_order_events_order_exchange_ts", "order_events", ["exchange_order_id", "exchange_ts"]
    )

    op.create_table(
        "fills",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("exchange_order_id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("exchange_account_id", sa.BigInteger(), nullable=False),
        sa.Column("exchange_trade_id", sa.String(64), nullable=False),
        sa.Column("price", sa.Numeric(30, 12), nullable=False),
        sa.Column("quantity", sa.Numeric(48, 18), nullable=False),
        sa.Column("commission", sa.Numeric(30, 12), nullable=False, server_default="0"),
        sa.Column("commission_asset", sa.String(16)),
        sa.Column("realized_pnl", sa.Numeric(30, 12)),
        sa.Column("filled_at", sa.DateTime(), nullable=False),
        sa.Column("raw_json", sa.JSON()),
        sa.CheckConstraint("quantity > 0", name="ck_fills_positive_quantity"),
        sa.CheckConstraint("price > 0", name="ck_fills_positive_price"),
        sa.ForeignKeyConstraint(
            ["exchange_order_id"],
            ["exchange_orders.id"],
            name="fk_fills_order",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name="fk_fills_user", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["exchange_account_id"],
            ["exchange_accounts.id"],
            name="fk_fills_account",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint(
            "exchange_account_id", "exchange_trade_id", name="uq_fills_account_trade"
        ),
        comment="交易所逐笔成交事实，不允许覆盖历史成交",
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )
    op.create_index("ix_fills_user_filled_at", "fills", ["user_id", "filled_at"])

    op.create_table(
        "outbox_events",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("event_key", sa.String(191), nullable=False),
        sa.Column("aggregate_type", sa.String(64), nullable=False),
        sa.Column("aggregate_id", sa.String(64), nullable=False),
        sa.Column("event_type", sa.String(64), nullable=False),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("available_at", sa.DateTime(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("published_at", sa.DateTime()),
        sa.Column("last_error_code", sa.String(64)),
        sa.CheckConstraint(
            "status IN ('pending', 'publishing', 'published', 'failed')",
            name="ck_outbox_events_valid_status",
        ),
        sa.UniqueConstraint("event_key", name="uq_outbox_events_event_key"),
        comment="业务事务与异步消息可靠衔接的 Outbox 事件",
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )
    op.create_index(
        "ix_outbox_events_status_available", "outbox_events", ["status", "available_at"]
    )


def downgrade() -> None:
    _require_mysql()
    for table in (
        "outbox_events",
        "fills",
        "order_events",
        "exchange_orders",
        "order_intents",
        "risk_decisions",
        "exchange_accounts",
        "worker_leases",
    ):
        op.drop_table(table)
