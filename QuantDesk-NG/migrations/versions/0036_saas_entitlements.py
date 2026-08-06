"""Add tenant SaaS plans, overrides, and quota counters.

Revision ID: 0036_saas_entitlements
Revises: 0035_optimization_governance
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0036_saas_entitlements"
down_revision: str | None = "0035_optimization_governance"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _require_mysql() -> None:
    if op.get_bind().dialect.name not in {"mysql", "mariadb"}:
        raise RuntimeError("QuantDesk migrations require MySQL or MariaDB")


def upgrade() -> None:
    _require_mysql()
    op.create_table(
        "saas_entitlements",
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("plan_code", sa.String(length=32), nullable=False, server_default="free"),
        sa.Column("overrides_json", sa.JSON(), nullable=False),
        sa.Column("starts_at", sa.DateTime(), nullable=False),
        sa.Column("ends_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "plan_code IN ('free', 'pro', 'enterprise')", name="ck_saas_entitlements_plan_code"
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("user_id"),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )
    op.create_table(
        "saas_usage_counters",
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("metric", sa.String(length=64), nullable=False),
        sa.Column("period_start", sa.Date(), nullable=False),
        sa.Column("amount", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint("amount >= 0", name="ck_saas_usage_counters_nonnegative_amount"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("user_id", "metric", "period_start"),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )
    op.create_index(
        "ix_saas_usage_counters_period",
        "saas_usage_counters",
        ["period_start", "metric"],
    )


def downgrade() -> None:
    _require_mysql()
    op.drop_index("ix_saas_usage_counters_period", table_name="saas_usage_counters")
    op.drop_table("saas_usage_counters")
    op.drop_table("saas_entitlements")
