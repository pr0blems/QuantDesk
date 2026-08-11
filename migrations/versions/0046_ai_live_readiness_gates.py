"""Add continuous-score and shadow-readiness gates to AI monitoring.

Revision ID: 0046_ai_live_readiness
Revises: 0045_ai_prediction_metrics
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0046_ai_live_readiness"
down_revision: str | None = "0045_ai_prediction_metrics"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _require_mysql() -> None:
    if op.get_bind().dialect.name not in {"mysql", "mariadb"}:
        raise RuntimeError("QuantDesk migrations require MySQL or MariaDB")


def upgrade() -> None:
    _require_mysql()
    config_columns = (
        sa.Column(
            "minimum_indicator_score",
            sa.Numeric(5, 2),
            nullable=False,
            server_default="65.00",
        ),
        sa.Column(
            "minimum_combined_score",
            sa.Numeric(5, 2),
            nullable=False,
            server_default="70.00",
        ),
        sa.Column(
            "maximum_market_age_seconds",
            sa.Integer(),
            nullable=False,
            server_default="120",
        ),
        sa.Column(
            "minimum_feature_quality",
            sa.Numeric(5, 4),
            nullable=False,
            server_default="0.7000",
        ),
        sa.Column(
            "minimum_calibration_samples",
            sa.Integer(),
            nullable=False,
            server_default="1000",
        ),
        sa.Column(
            "live_safety_margin_bps",
            sa.Numeric(8, 4),
            nullable=False,
            server_default="10.0000",
        ),
    )
    for column in config_columns:
        op.add_column("ai_monitor_configs", column)
    config_checks = (
        ("valid_minimum_indicator_score", "minimum_indicator_score BETWEEN 0 AND 100"),
        ("valid_minimum_combined_score", "minimum_combined_score BETWEEN 0 AND 100"),
        ("valid_maximum_market_age", "maximum_market_age_seconds BETWEEN 5 AND 3600"),
        ("valid_minimum_feature_quality", "minimum_feature_quality BETWEEN 0 AND 1"),
        (
            "valid_minimum_calibration_samples",
            "minimum_calibration_samples BETWEEN 30 AND 5000",
        ),
        ("valid_live_safety_margin", "live_safety_margin_bps BETWEEN 0 AND 500"),
    )
    for name, condition in config_checks:
        op.create_check_constraint(name, "ai_monitor_configs", condition)

    op.add_column(
        "ai_monitor_predictions",
        sa.Column(
            "readiness_status",
            sa.String(32),
            nullable=False,
            server_default="research_only",
        ),
    )
    op.add_column(
        "ai_monitor_predictions",
        sa.Column(
            "calibration_sample_count",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )
    op.add_column(
        "ai_monitor_predictions",
        sa.Column("expected_gross_edge_bps", sa.Numeric(20, 8), nullable=True),
    )
    op.add_column(
        "ai_monitor_predictions",
        sa.Column("expected_edge_lower_bound_bps", sa.Numeric(20, 8), nullable=True),
    )
    op.create_check_constraint(
        "valid_readiness_status",
        "ai_monitor_predictions",
        "readiness_status IN ('research_only', 'shadow_ready')",
    )


def downgrade() -> None:
    _require_mysql()
    op.drop_constraint("valid_readiness_status", "ai_monitor_predictions", type_="check")
    for column in (
        "expected_edge_lower_bound_bps",
        "expected_gross_edge_bps",
        "calibration_sample_count",
        "readiness_status",
    ):
        op.drop_column("ai_monitor_predictions", column)
    for name in (
        "valid_live_safety_margin",
        "valid_minimum_calibration_samples",
        "valid_minimum_feature_quality",
        "valid_maximum_market_age",
        "valid_minimum_combined_score",
        "valid_minimum_indicator_score",
    ):
        op.drop_constraint(name, "ai_monitor_configs", type_="check")
    for column in (
        "live_safety_margin_bps",
        "minimum_calibration_samples",
        "minimum_feature_quality",
        "maximum_market_age_seconds",
        "minimum_combined_score",
        "minimum_indicator_score",
    ):
        op.drop_column("ai_monitor_configs", column)
