"""Add evidence-based indexes for production query paths.

Revision ID: 0060_query_path_indexes
Revises: 0059_ai_monitor_read_models
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0060_query_path_indexes"
down_revision: str | None = "0059_ai_monitor_read_models"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _require_mysql() -> None:
    if op.get_bind().dialect.name not in {"mysql", "mariadb"}:
        raise RuntimeError("QuantDesk migrations require MySQL or MariaDB")


def upgrade() -> None:
    _require_mysql()
    op.create_index("ix_news_source_ts", "news", ["source", "ts"])
    op.create_index(
        "ix_news_translation_pending",
        "news",
        ["lang", "title_zh", "ts"],
        mysql_length={"title_zh": 1},
    )
    op.create_index(
        "ix_news_ai_analysis_records_user_time",
        "news_ai_analysis_records",
        ["user_id", "analyzed_at", "id"],
    )
    op.create_index(
        "ix_ai_monitor_runs_recovery",
        "ai_monitor_runs",
        ["status", "updated_at"],
    )
    op.create_index(
        "ix_ai_monitor_opportunities_user_discovered",
        "ai_monitor_opportunities",
        ["user_id", "discovered_at", "id"],
    )
    op.create_index(
        "ix_ai_monitor_predictions_user_updated",
        "ai_monitor_predictions",
        ["user_id", "updated_at", "id"],
    )
    op.create_index(
        "ix_battle_predictions_metrics_cover",
        "battle_predictions",
        ["model_key", "horizon_seconds", "id", "result"],
    )
    op.create_index(
        "ix_prediction_outcomes_metrics_cover",
        "prediction_outcomes",
        [
            "prediction_id",
            "status",
            "directional_return_bps",
            "max_favorable_bps",
            "max_adverse_bps",
        ],
    )


def downgrade() -> None:
    _require_mysql()
    op.drop_index(
        "ix_prediction_outcomes_metrics_cover",
        table_name="prediction_outcomes",
    )
    op.drop_index(
        "ix_battle_predictions_metrics_cover",
        table_name="battle_predictions",
    )
    op.drop_index(
        "ix_ai_monitor_predictions_user_updated",
        table_name="ai_monitor_predictions",
    )
    op.drop_index(
        "ix_ai_monitor_opportunities_user_discovered",
        table_name="ai_monitor_opportunities",
    )
    op.drop_index("ix_ai_monitor_runs_recovery", table_name="ai_monitor_runs")
    op.drop_index(
        "ix_news_ai_analysis_records_user_time",
        table_name="news_ai_analysis_records",
    )
    op.drop_index("ix_news_translation_pending", table_name="news")
    op.drop_index("ix_news_source_ts", table_name="news")
