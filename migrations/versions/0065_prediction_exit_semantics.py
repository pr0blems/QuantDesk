"""Persist precise prediction exit semantics and protection state.

Revision ID: 0065_prediction_exit_semantics
Revises: 0064_strategy_source_runtime
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0065_prediction_exit_semantics"
down_revision: str | None = "0064_strategy_source_runtime"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _require_mysql() -> None:
    if op.get_bind().dialect.name not in {"mysql", "mariadb"}:
        raise RuntimeError("QuantDesk migrations require MySQL or MariaDB")


def _add_exit_columns(table_name: str) -> None:
    existing = {
        column["name"]
        for column in sa.inspect(op.get_bind()).get_columns(table_name)
    }
    if "exit_subreason" not in existing:
        op.add_column(
            table_name,
            sa.Column("exit_subreason", sa.String(length=32), nullable=True),
        )
    if "peak_favorable_bps_at_exit" not in existing:
        op.add_column(
            table_name,
            sa.Column(
                "peak_favorable_bps_at_exit",
                sa.Numeric(20, 8),
                nullable=True,
            ),
        )
    if "protected_bps_at_exit" not in existing:
        op.add_column(
            table_name,
            sa.Column("protected_bps_at_exit", sa.Numeric(20, 8), nullable=True),
        )


def upgrade() -> None:
    _require_mysql()
    _add_exit_columns("ai_monitor_predictions")
    _add_exit_columns("ai_monitor_prediction_facts")
    prediction_indexes = {
        index["name"]
        for index in sa.inspect(op.get_bind()).get_indexes(
            "ai_monitor_predictions"
        )
    }
    if "ix_ai_monitor_predictions_user_settlement_predicted" not in prediction_indexes:
        op.create_index(
            "ix_ai_monitor_predictions_user_settlement_predicted",
            "ai_monitor_predictions",
            ["user_id", "settlement_version", "predicted_at"],
        )
    fact_indexes = {
        index["name"]
        for index in sa.inspect(op.get_bind()).get_indexes(
            "ai_monitor_prediction_facts"
        )
    }
    if "ix_ai_monitor_prediction_facts_user_settlement_signal" not in fact_indexes:
        op.create_index(
            "ix_ai_monitor_prediction_facts_user_settlement_signal",
            "ai_monitor_prediction_facts",
            ["user_id", "settlement_version", "signal_at"],
        )

    op.execute(
        sa.text(
            """
            UPDATE ai_monitor_predictions
            SET
              exit_subreason = COALESCE(
                NULLIF(
                  JSON_UNQUOTE(JSON_EXTRACT(evidence_json, '$.settlement.exit_subreason')),
                  'null'
                ),
                IF(exit_reason = 'take_profit', 'hard_target', NULL)
              ),
              peak_favorable_bps_at_exit = COALESCE(
                CAST(NULLIF(
                  JSON_UNQUOTE(JSON_EXTRACT(
                    evidence_json,
                    '$.settlement.peak_favorable_bps_at_decision'
                  )),
                  'null'
                ) AS DECIMAL(20, 8)),
                max_favorable_bps
              ),
              protected_bps_at_exit = CAST(NULLIF(
                JSON_UNQUOTE(JSON_EXTRACT(evidence_json, '$.settlement.protected_bps')),
                'null'
              ) AS DECIMAL(20, 8))
            """
        )
    )
    op.execute(
        sa.text(
            """
            UPDATE ai_monitor_prediction_facts AS fact
            INNER JOIN ai_monitor_predictions AS prediction
              ON prediction.id = fact.prediction_id
            SET
              fact.exit_subreason = prediction.exit_subreason,
              fact.peak_favorable_bps_at_exit = prediction.peak_favorable_bps_at_exit,
              fact.protected_bps_at_exit = prediction.protected_bps_at_exit
            """
        )
    )


def downgrade() -> None:
    _require_mysql()
    op.drop_index(
        "ix_ai_monitor_prediction_facts_user_settlement_signal",
        table_name="ai_monitor_prediction_facts",
    )
    op.drop_index(
        "ix_ai_monitor_predictions_user_settlement_predicted",
        table_name="ai_monitor_predictions",
    )
    for table_name in (
        "ai_monitor_prediction_facts",
        "ai_monitor_predictions",
    ):
        existing = {
            column["name"]
            for column in sa.inspect(op.get_bind()).get_columns(table_name)
        }
        for name in (
            "protected_bps_at_exit",
            "peak_favorable_bps_at_exit",
            "exit_subreason",
        ):
            if name in existing:
                op.drop_column(table_name, name)
