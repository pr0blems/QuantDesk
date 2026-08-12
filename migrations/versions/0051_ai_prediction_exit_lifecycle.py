"""Add auditable virtual prediction exit lifecycle fields.

Revision ID: 0051_ai_prediction_exit
Revises: 0050_ai_historical_replay
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0051_ai_prediction_exit"
down_revision: str | None = "0050_ai_historical_replay"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _require_mysql() -> None:
    if op.get_bind().dialect.name not in {"mysql", "mariadb"}:
        raise RuntimeError("QuantDesk migrations require MySQL or MariaDB")


def upgrade() -> None:
    _require_mysql()
    op.add_column(
        "ai_monitor_predictions",
        sa.Column(
            "exit_at",
            sa.DateTime(),
            nullable=True,
            comment="Virtual position exit time (UTC)",
        ),
    )
    op.add_column(
        "ai_monitor_predictions",
        sa.Column(
            "exit_reason",
            sa.String(32),
            nullable=True,
            comment="Virtual exit trigger",
        ),
    )
    op.create_check_constraint(
        "valid_exit_reason",
        "ai_monitor_predictions",
        "exit_reason IS NULL OR exit_reason IN "
        "('take_profit', 'stop_loss', 'score_breakdown', 'score_reversal', "
        "'max_holding_time', 'legacy_horizon_close')",
    )
    op.execute(
        sa.text(
            """
            UPDATE ai_monitor_predictions
            SET exit_at=COALESCE(completed_at, due_at),
                exit_reason='legacy_horizon_close'
            WHERE status='completed' AND exit_price IS NOT NULL
              AND exit_reason IS NULL
            """
        )
    )


def downgrade() -> None:
    _require_mysql()
    op.drop_constraint(
        "valid_exit_reason",
        "ai_monitor_predictions",
        type_="check",
    )
    op.drop_column("ai_monitor_predictions", "exit_reason")
    op.drop_column("ai_monitor_predictions", "exit_at")
