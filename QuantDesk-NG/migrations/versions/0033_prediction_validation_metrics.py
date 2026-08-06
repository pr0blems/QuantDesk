"""Add replayable leakage-safe prediction validation metrics.

Revision ID: 0033_prediction_validation
Revises: 0032_underlying_aligned_windows
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0033_prediction_validation"
down_revision: str | None = "0032_underlying_aligned_windows"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _require_mysql() -> None:
    if op.get_bind().dialect.name not in {"mysql", "mariadb"}:
        raise RuntimeError("QuantDesk migrations require MySQL or MariaDB")


def upgrade() -> None:
    _require_mysql()
    op.create_table(
        "prediction_validation_metrics",
        sa.Column("model_key", sa.String(64), nullable=False),
        sa.Column("model_version", sa.Integer(), nullable=False),
        sa.Column("horizon_seconds", sa.Integer(), nullable=False),
        sa.Column("window_start_ms", sa.BigInteger(), nullable=False),
        sa.Column("evaluated_until_ms", sa.BigInteger(), nullable=False),
        sa.Column("completed_outcomes", sa.Integer(), nullable=False),
        sa.Column("directional_predictions", sa.Integer(), nullable=False),
        sa.Column("correct_directional", sa.Integer(), nullable=False),
        sa.Column("coverage_ratio", sa.Numeric(10, 8), nullable=False),
        sa.Column("directional_accuracy", sa.Numeric(10, 8)),
        sa.Column("brier_score", sa.Numeric(12, 10)),
        sa.Column("calibration_gap", sa.Numeric(12, 10)),
        sa.Column("mean_net_return_bps", sa.Numeric(20, 8)),
        sa.Column("last_completed_at_ms", sa.BigInteger()),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("model_key", "model_version", "horizon_seconds", "window_start_ms"),
        sa.CheckConstraint("status IN ('collecting','validated')", name="ck_prediction_validation_status"),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )
    op.create_index(
        "ix_prediction_validation_latest",
        "prediction_validation_metrics",
        ["horizon_seconds", "evaluated_until_ms"],
    )


def downgrade() -> None:
    _require_mysql()
    op.drop_index("ix_prediction_validation_latest", table_name="prediction_validation_metrics")
    op.drop_table("prediction_validation_metrics")
