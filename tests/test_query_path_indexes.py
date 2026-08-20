from pathlib import Path

from quantdesk_v2.models import (
    AiMonitorOpportunity,
    AiMonitorPrediction,
    AiMonitorRun,
    News,
    NewsAiAnalysisRecord,
)

ROOT = Path(__file__).resolve().parents[1]


def _index_columns(model: type, name: str) -> tuple[str, ...]:
    index = next(item for item in model.__table__.indexes if item.name == name)
    return tuple(column.name for column in index.columns)


def test_models_declare_production_query_path_indexes() -> None:
    assert _index_columns(News, "ix_news_source_ts") == ("source", "ts")
    assert _index_columns(News, "ix_news_translation_pending") == (
        "lang",
        "title_zh",
        "ts",
    )
    assert _index_columns(
        NewsAiAnalysisRecord,
        "ix_news_ai_analysis_records_user_time",
    ) == ("user_id", "analyzed_at", "id")
    assert _index_columns(AiMonitorRun, "ix_ai_monitor_runs_recovery") == (
        "status",
        "updated_at",
    )
    assert _index_columns(
        AiMonitorOpportunity,
        "ix_ai_monitor_opportunities_user_discovered",
    ) == ("user_id", "discovered_at", "id")
    assert _index_columns(
        AiMonitorPrediction,
        "ix_ai_monitor_predictions_user_updated",
    ) == ("user_id", "updated_at", "id")


def test_translation_index_uses_a_bounded_mysql_text_prefix() -> None:
    index = next(
        item for item in News.__table__.indexes if item.name == "ix_news_translation_pending"
    )
    assert index.dialect_options["mysql"]["length"] == {"title_zh": 1}


def test_query_path_index_migration_follows_latest_revision() -> None:
    source = (ROOT / "migrations/versions/0060_query_path_indexes.py").read_text(
        encoding="utf-8"
    )
    assert 'revision: str = "0060_query_path_indexes"' in source
    assert 'down_revision: str | None = "0059_ai_monitor_read_models"' in source
    for name in (
        "ix_news_source_ts",
        "ix_news_translation_pending",
        "ix_news_ai_analysis_records_user_time",
        "ix_ai_monitor_runs_recovery",
        "ix_ai_monitor_opportunities_user_discovered",
        "ix_ai_monitor_predictions_user_updated",
        "ix_battle_predictions_metrics_cover",
        "ix_prediction_outcomes_metrics_cover",
    ):
        assert name in source
