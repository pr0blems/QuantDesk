"""Store the immutable algorithm configuration used by each prediction.

Revision ID: 0036_prediction_algo_snapshot
Revises: 0035_battle_kline_features
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0036_prediction_algo_snapshot"
down_revision: str | None = "0035_battle_kline_features"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _require_mysql() -> None:
    if op.get_bind().dialect.name not in {"mysql", "mariadb"}:
        raise RuntimeError("QuantDesk migrations require MySQL or MariaDB")


def upgrade() -> None:
    _require_mysql()
    # Keep the append-only snapshot separate from the hot prediction table.
    # Creating a child table avoids rewriting battle_predictions. Its foreign
    # key can take a brief metadata lock while being created, then guarantees
    # that retention cleanup cannot leave orphaned algorithm snapshots.
    if sa.inspect(op.get_bind()).has_table("prediction_algorithm_snapshots"):
        return
    op.create_table(
        "prediction_algorithm_snapshots",
        sa.Column("prediction_id", sa.BigInteger(), primary_key=True, autoincrement=False),
        sa.Column("algorithm_config_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["prediction_id"],
            ["battle_predictions.id"],
            name="fk_prediction_algorithm_snapshots_prediction",
            ondelete="CASCADE",
        ),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )


def downgrade() -> None:
    _require_mysql()
    op.drop_table("prediction_algorithm_snapshots")
