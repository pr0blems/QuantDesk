"""Add tenant-owned opportunity watch and ignore state.

Revision ID: 0017_opportunity_states
Revises: 0016_ai_model_configs
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0017_opportunity_states"
down_revision: str | None = "0016_ai_model_configs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _require_mysql() -> None:
    if op.get_bind().dialect.name not in {"mysql", "mariadb"}:
        raise RuntimeError("QuantDesk migrations require MySQL or MariaDB")


def upgrade() -> None:
    _require_mysql()
    op.create_table(
        "user_opportunity_states",
        sa.Column("user_id", sa.BigInteger(), nullable=False, comment="所属用户 ID，用于租户隔离"),
        sa.Column(
            "opportunity_id",
            sa.BigInteger(),
            nullable=False,
            comment="用户关注或忽略的公共市场机会 ID",
        ),
        sa.Column(
            "state",
            sa.String(16),
            nullable=False,
            comment="用户状态：watching 关注或 ignored 忽略",
        ),
        sa.Column(
            "notify_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
            comment="该机会状态更新时是否生成用户提醒",
        ),
        sa.Column("last_viewed_at", sa.DateTime(), comment="用户最后查看该机会证据的时间（UTC）"),
        sa.Column(
            "created_at", sa.DateTime(), nullable=False, comment="用户机会状态创建时间（UTC）"
        ),
        sa.Column(
            "updated_at", sa.DateTime(), nullable=False, comment="用户机会状态最后更新时间（UTC）"
        ),
        sa.CheckConstraint(
            "state IN ('watching', 'ignored')",
            name="ck_user_opportunity_states_valid_state",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_user_opportunity_states_user",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["opportunity_id"],
            ["market_opportunities.id"],
            name="fk_user_opportunity_states_opportunity",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("user_id", "opportunity_id", name="pk_user_opportunity_states"),
        comment="用户对公共市场机会的关注、忽略和提醒偏好",
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )
    op.create_index(
        "ix_user_opportunity_states_user_state",
        "user_opportunity_states",
        ["user_id", "state", "updated_at"],
    )


def downgrade() -> None:
    _require_mysql()
    op.drop_index(
        "ix_user_opportunity_states_user_state",
        table_name="user_opportunity_states",
    )
    op.drop_table("user_opportunity_states")
