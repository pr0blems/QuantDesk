"""Add configurable cost assumptions for AI prediction statistics.

Revision ID: 0047_ai_prediction_costs
Revises: 0046_ai_live_readiness
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0047_ai_prediction_costs"
down_revision: str | None = "0046_ai_live_readiness"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _require_mysql() -> None:
    if op.get_bind().dialect.name not in {"mysql", "mariadb"}:
        raise RuntimeError("QuantDesk migrations require MySQL or MariaDB")


def upgrade() -> None:
    _require_mysql()
    columns = (
        sa.Column(
            "prediction_fee_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
        sa.Column(
            "prediction_fee_bps_per_side",
            sa.Numeric(8, 4),
            nullable=False,
            server_default="5.0000",
        ),
        sa.Column(
            "prediction_slippage_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
        sa.Column(
            "prediction_slippage_bps_per_side",
            sa.Numeric(8, 4),
            nullable=False,
            server_default="3.0000",
        ),
        sa.Column(
            "prediction_funding_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
        sa.Column(
            "prediction_funding_bps_per_8h",
            sa.Numeric(8, 4),
            nullable=False,
            server_default="1.0000",
        ),
    )
    for column in columns:
        op.add_column("ai_monitor_configs", column)
    checks = (
        ("valid_prediction_fee_bps", "prediction_fee_bps_per_side BETWEEN 0 AND 500"),
        (
            "valid_prediction_slippage_bps",
            "prediction_slippage_bps_per_side BETWEEN 0 AND 500",
        ),
        (
            "valid_prediction_funding_bps",
            "prediction_funding_bps_per_8h BETWEEN 0 AND 500",
        ),
    )
    for name, condition in checks:
        op.create_check_constraint(name, "ai_monitor_configs", condition)


def downgrade() -> None:
    _require_mysql()
    for name in (
        "valid_prediction_funding_bps",
        "valid_prediction_slippage_bps",
        "valid_prediction_fee_bps",
    ):
        op.drop_constraint(name, "ai_monitor_configs", type_="check")
    for column in (
        "prediction_funding_bps_per_8h",
        "prediction_funding_enabled",
        "prediction_slippage_bps_per_side",
        "prediction_slippage_enabled",
        "prediction_fee_bps_per_side",
        "prediction_fee_enabled",
    ):
        op.drop_column("ai_monitor_configs", column)
