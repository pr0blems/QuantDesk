"""Add encrypted Clash-style proxy management state.

Revision ID: 0034_proxy_management
Revises: 0033_prediction_validation
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0034_proxy_management"
down_revision: str | None = "0033_prediction_validation"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _require_mysql() -> None:
    if op.get_bind().dialect.name not in {"mysql", "mariadb"}:
        raise RuntimeError("QuantDesk migrations require MySQL or MariaDB")


def upgrade() -> None:
    _require_mysql()
    op.create_table(
        "proxy_subscriptions",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("public_id", sa.String(length=36), nullable=False),
        sa.Column("name", sa.String(length=96), nullable=False),
        sa.Column("endpoint", sa.Text()),
        sa.Column("auth_encrypted", sa.Text()),
        sa.Column("auth_fingerprint", sa.String(length=16)),
        sa.Column("source_format", sa.String(length=16), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("refresh_interval_minutes", sa.Integer(), nullable=False),
        sa.Column("last_imported_at", sa.DateTime()),
        sa.Column("last_error", sa.String(length=240)),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("public_id", name="uq_proxy_subscriptions_public_id"),
        mysql_charset="utf8mb4",
        mysql_engine="InnoDB",
    )
    op.create_index("ix_proxy_subscriptions_enabled", "proxy_subscriptions", ["enabled"])
    op.create_table(
        "proxy_nodes",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("subscription_id", sa.BigInteger()),
        sa.Column("name", sa.String(length=160), nullable=False),
        sa.Column("protocol", sa.String(length=16), nullable=False),
        sa.Column("host", sa.String(length=255), nullable=False),
        sa.Column("port", sa.Integer(), nullable=False),
        sa.Column("username", sa.String(length=128)),
        sa.Column("password_encrypted", sa.Text()),
        sa.Column("credential_fingerprint", sa.String(length=16)),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("health_status", sa.String(length=16), nullable=False),
        sa.Column("last_latency_ms", sa.Integer()),
        sa.Column("last_tested_at", sa.DateTime()),
        sa.Column("last_error", sa.String(length=240)),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint("protocol IN ('http','socks5')", name="ck_proxy_nodes_valid_protocol"),
        sa.CheckConstraint("port BETWEEN 1 AND 65535", name="ck_proxy_nodes_valid_port"),
        sa.CheckConstraint(
            "health_status IN ('unknown','healthy','unhealthy')",
            name="ck_proxy_nodes_valid_health_status",
        ),
        sa.ForeignKeyConstraint(["subscription_id"], ["proxy_subscriptions.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "subscription_id", "protocol", "host", "port", name="uq_proxy_nodes_endpoint"
        ),
        mysql_charset="utf8mb4",
        mysql_engine="InnoDB",
    )
    op.create_index(
        "ix_proxy_nodes_enabled_health",
        "proxy_nodes",
        ["enabled", "health_status", "last_latency_ms"],
    )
    op.create_table(
        "proxy_runtime_settings",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("selection_mode", sa.String(length=16), nullable=False),
        sa.Column("active_node_id", sa.BigInteger()),
        sa.Column("fallback_state", sa.String(length=32), nullable=False),
        sa.Column("fallback_reason", sa.String(length=240)),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "selection_mode IN ('direct','manual','auto')",
            name="ck_proxy_runtime_settings_valid_selection_mode",
        ),
        sa.ForeignKeyConstraint(["active_node_id"], ["proxy_nodes.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        mysql_charset="utf8mb4",
        mysql_engine="InnoDB",
    )
    op.execute(
        "INSERT INTO proxy_runtime_settings "
        "(id, enabled, selection_mode, fallback_state, updated_at) "
        "VALUES (1, 0, 'direct', 'direct', UTC_TIMESTAMP())"
    )


def downgrade() -> None:
    _require_mysql()
    op.drop_table("proxy_runtime_settings")
    op.drop_index("ix_proxy_nodes_enabled_health", table_name="proxy_nodes")
    op.drop_table("proxy_nodes")
    op.drop_index("ix_proxy_subscriptions_enabled", table_name="proxy_subscriptions")
    op.drop_table("proxy_subscriptions")
