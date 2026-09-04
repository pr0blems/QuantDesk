"""Add default and symbol-specific strategy execution parameter profiles.

Revision ID: 0084_strategy_parameter_profiles
Revises: 0083_ai_monitor_technical_primary
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0084_strategy_parameter_profiles"
down_revision: str | None = "0083_ai_technical_primary"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _require_mysql() -> None:
    if op.get_bind().dialect.name not in {"mysql", "mariadb"}:
        raise RuntimeError("QuantDesk migrations require MySQL or MariaDB")


def upgrade() -> None:
    _require_mysql()
    op.create_table(
        "strategy_parameter_profiles",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("public_id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("strategy_id", sa.BigInteger(), nullable=False),
        sa.Column("scope_key", sa.String(length=32), nullable=False),
        sa.Column("strategy_version", sa.Integer(), nullable=False),
        sa.Column("parameters_json", sa.JSON(), nullable=False),
        sa.Column("execution_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "scope_key = '*' OR scope_key <> ''",
            name="ck_strategy_parameter_profiles_valid_scope_key",
        ),
        sa.CheckConstraint(
            "strategy_version > 0", name="ck_strategy_parameter_profiles_positive_strategy_version"
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["strategy_id", "user_id"],
            ["user_strategies.id", "user_strategies.user_id"],
            name="fk_strategy_parameter_profiles_strategy_tenant",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("public_id", name="uq_strategy_parameter_profiles_public_id"),
        sa.UniqueConstraint(
            "user_id",
            "strategy_id",
            "scope_key",
            name="uq_strategy_parameter_profiles_scope",
        ),
        comment="策略默认或品种专属的可变运行参数；不修改不可变策略修订",
        mysql_charset="utf8mb4",
        mysql_engine="InnoDB",
    )
    op.create_index(
        "ix_strategy_parameter_profiles_user_strategy_updated",
        "strategy_parameter_profiles",
        ["user_id", "strategy_id", "updated_at"],
        unique=False,
    )


def downgrade() -> None:
    _require_mysql()
    op.drop_index(
        "ix_strategy_parameter_profiles_user_strategy_updated",
        table_name="strategy_parameter_profiles",
    )
    op.drop_table("strategy_parameter_profiles")
