"""Repair prediction outcome observation columns.

Revision ID: 0023_repair_prediction_outcomes
Revises: 0022_battle_prediction
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0023_repair_prediction_outcomes"
down_revision: str | None = "0022_battle_prediction"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    columns = {
        column["name"]
        for column in sa.inspect(op.get_bind()).get_columns("prediction_outcomes")
    }
    if "last_observed_price" not in columns:
        op.add_column(
            "prediction_outcomes",
            sa.Column("last_observed_price", sa.Numeric(30, 12)),
        )
    if "last_observed_at_ms" not in columns:
        op.add_column(
            "prediction_outcomes",
            sa.Column("last_observed_at_ms", sa.BigInteger()),
        )


def downgrade() -> None:
    # This is an idempotent repair for columns owned by revision 0022.
    # Keeping them during a one-step downgrade avoids removing valid 0022 schema.
    pass
