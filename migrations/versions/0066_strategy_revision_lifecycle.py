"""Add revision-scoped strategy lifecycle controls.

Revision ID: 0066_strategy_revision_lifecycle
Revises: 0065_prediction_exit_semantics
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0066_strategy_revision_lifecycle"
down_revision: str | None = "0065_prediction_exit_semantics"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_LIFECYCLE_CHECK = (
    "lifecycle_status IN ('draft', 'validated', 'backtested', 'shadow', "
    "'paper', 'micro_live', 'live', 'published', 'retired')"
)


def _require_mysql() -> None:
    if op.get_bind().dialect.name not in {"mysql", "mariadb"}:
        raise RuntimeError("QuantDesk migrations require MySQL or MariaDB")


def upgrade() -> None:
    _require_mysql()
    op.drop_constraint(
        "ck_user_strategies_valid_lifecycle_status",
        "user_strategies",
        type_="check",
    )
    op.create_check_constraint(
        "ck_user_strategies_valid_lifecycle_status",
        "user_strategies",
        _LIFECYCLE_CHECK,
    )
    op.add_column(
        "strategy_revisions",
        sa.Column(
            "lifecycle_status",
            sa.String(16),
            nullable=False,
            server_default="draft",
        ),
    )
    op.execute(
        """
        UPDATE strategy_revisions AS revision_row
        JOIN user_strategies AS strategy_row
          ON strategy_row.id = revision_row.user_strategy_id
         AND strategy_row.user_id = revision_row.user_id
        SET revision_row.lifecycle_status = CASE
            WHEN revision_row.version = strategy_row.version
                THEN strategy_row.lifecycle_status
            ELSE 'retired'
        END
        """
    )
    op.alter_column(
        "strategy_revisions",
        "lifecycle_status",
        existing_type=sa.String(16),
        nullable=False,
        server_default=None,
    )
    op.create_check_constraint(
        "ck_strategy_revisions_valid_lifecycle_status",
        "strategy_revisions",
        _LIFECYCLE_CHECK,
    )
    op.create_index(
        "ix_strategy_revisions_strategy_lifecycle",
        "strategy_revisions",
        ["user_strategy_id", "lifecycle_status"],
    )


def downgrade() -> None:
    _require_mysql()
    op.drop_index(
        "ix_strategy_revisions_strategy_lifecycle",
        table_name="strategy_revisions",
    )
    op.drop_constraint(
        "ck_strategy_revisions_valid_lifecycle_status",
        "strategy_revisions",
        type_="check",
    )
    op.drop_column("strategy_revisions", "lifecycle_status")
    op.execute(
        """
        UPDATE user_strategies
        SET lifecycle_status = CASE
            WHEN lifecycle_status = 'retired' THEN 'retired'
            WHEN lifecycle_status = 'draft' THEN 'draft'
            ELSE 'published'
        END
        """
    )
    op.drop_constraint(
        "ck_user_strategies_valid_lifecycle_status",
        "user_strategies",
        type_="check",
    )
    op.create_check_constraint(
        "ck_user_strategies_valid_lifecycle_status",
        "user_strategies",
        "lifecycle_status IN ('draft', 'published', 'retired')",
    )
