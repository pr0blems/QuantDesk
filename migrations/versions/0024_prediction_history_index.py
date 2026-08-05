"""Add the battle prediction history paging index.

Revision ID: 0024_prediction_history_index
Revises: 0023_repair_prediction_outcomes
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0024_prediction_history_index"
down_revision: str | None = "0023_repair_prediction_outcomes"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "ix_battle_predictions_history",
        "battle_predictions",
        ["predicted_at_ms", "id"],
    )


def downgrade() -> None:
    op.drop_index("ix_battle_predictions_history", table_name="battle_predictions")
