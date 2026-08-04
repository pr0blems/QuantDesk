"""Add the administrator control-plane tables.

Revision ID: 0015_admin_control_plane
Revises: 0014_strategy_runtime
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0015_admin_control_plane"
down_revision: str | None = "0014_strategy_runtime"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE_OPTIONS = {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"}


def _require_mysql() -> None:
    if op.get_bind().dialect.name not in {"mysql", "mariadb"}:
        raise RuntimeError("QuantDesk migrations require MySQL or MariaDB")


def upgrade() -> None:
    _require_mysql()
    op.create_table(
        "admin_settings",
        sa.Column("key", sa.String(64), nullable=False, comment="配置稳定键"),
        sa.Column("value_json", sa.JSON(), nullable=False, comment="配置 JSON"),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("updated_by", sa.BigInteger(), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["updated_by"],
            ["users.id"],
            name="fk_admin_settings_updated_by_users",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("key", name="pk_admin_settings"),
        comment="管理员发布的系统运行配置",
        **TABLE_OPTIONS,
    )
    op.create_table(
        "news_source_settings",
        sa.Column("name", sa.String(80), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("lang", sa.String(16), server_default="en", nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("1"), nullable=False),
        sa.Column("slow", sa.Boolean(), server_default=sa.text("0"), nullable=False),
        sa.Column("weight", sa.Integer(), server_default="100", nullable=False),
        sa.Column("hourly_limit", sa.Integer(), server_default="600", nullable=False),
        sa.Column("last_success_at", sa.BigInteger(), nullable=True),
        sa.Column("last_error_at", sa.BigInteger(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("fetched_items", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("inserted_items", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("updated_by", sa.BigInteger(), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["updated_by"],
            ["users.id"],
            name="fk_news_source_settings_updated_by_users",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("name", name="pk_news_source_settings"),
        comment="可由管理员维护的新闻来源与健康状态",
        **TABLE_OPTIONS,
    )
    op.create_index("ix_news_source_settings_enabled", "news_source_settings", ["enabled"])
    op.create_table(
        "collector_status",
        sa.Column("name", sa.String(32), nullable=False),
        sa.Column("heartbeat_at", sa.BigInteger(), nullable=False),
        sa.Column("last_success_at", sa.BigInteger(), nullable=True),
        sa.Column("last_error_at", sa.BigInteger(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("cycles", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("items", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("details_json", sa.JSON(), nullable=True),
        sa.PrimaryKeyConstraint("name", name="pk_collector_status"),
        comment="后台采集器心跳与最近运行结果",
        **TABLE_OPTIONS,
    )


def downgrade() -> None:
    _require_mysql()
    op.drop_table("collector_status")
    op.drop_index("ix_news_source_settings_enabled", table_name="news_source_settings")
    op.drop_table("news_source_settings")
    op.drop_table("admin_settings")
