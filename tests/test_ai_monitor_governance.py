from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from quantdesk_v2.application.ai_monitor import (
    AiMonitorAuthority,
    OpportunityGenerationService,
    PredictionSettlementService,
)

ROOT = Path(__file__).parents[1]


def test_opportunity_generation_is_deterministic_and_refreshes_projection() -> None:
    calls: list[str] = []

    def scan(*_args: object) -> dict[str, object]:
        calls.append("scan")
        return {"failed_symbols": [], "created": 2}

    def refresh(*_args: object, **_kwargs: object) -> dict[str, object]:
        calls.append("projection")
        return {"available": True}

    result = OpportunityGenerationService(
        scan=scan,
        refresh_projection=refresh,
        version="decision-v1",
    ).execute(
        SimpleNamespace(),
        SimpleNamespace(),
        SimpleNamespace(user_id=7),
        {},
        Path("symbols.json"),
    )

    assert calls == ["scan", "projection"]
    assert result.authority is AiMonitorAuthority.DETERMINISTIC
    assert result.payload["read_models"] == {"available": True}


def test_prediction_settlement_service_keeps_ai_out_of_fact_settlement() -> None:
    calls: list[str] = []

    def reopen(_db: object) -> int:
        calls.append("reopen")
        return 1

    def settle(_db: object, _repository: object) -> dict[str, int]:
        calls.append("settle")
        return {"completed": 1, "unavailable": 0}

    def refresh(*_args: object, **_kwargs: object) -> dict[str, bool]:
        calls.append("projection")
        return {"available": True}

    result = PredictionSettlementService(
        settle=settle,
        reopen_legacy=reopen,
        refresh_projection=refresh,
        version="settlement-v1",
    ).execute_cycle(SimpleNamespace(), SimpleNamespace())

    assert calls == ["reopen", "settle", "projection"]
    assert result.authority is AiMonitorAuthority.DETERMINISTIC


def test_current_opportunity_api_has_no_source_fallback() -> None:
    source = (
        ROOT / "src/quantdesk_v2/interfaces/api/ai_monitor.py"
    ).read_text(encoding="utf-8")
    current_query = source[source.index("def opportunities(") : source.index(
        "def opportunity_order_book("
    )]
    assert "source_fallback" not in current_query
    assert '"current_read_model"' in current_query
    assert "OpportunityProjectionError" in current_query


def test_ai_monitor_orchestration_uses_all_governed_domain_services() -> None:
    source = (ROOT / "src/quantdesk_v2/ai_monitor.py").read_text(encoding="utf-8")
    for service in (
        "OpportunityGenerationService",
        "PredictionSettlementService",
        "NewsScoringService",
        "MarketFeatureService",
        "MacroRegimeService",
        "EventGateService",
    ):
        assert service in source


def test_market_feature_production_paths_do_not_call_ai_monitor_private_storage() -> None:
    api_source = (
        ROOT / "src/quantdesk_v2/interfaces/api/ai_monitor.py"
    ).read_text(encoding="utf-8")
    orchestration_source = (
        ROOT / "src/quantdesk_v2/ai_monitor.py"
    ).read_text(encoding="utf-8")
    persistence_source = (
        ROOT
        / "src/quantdesk_v2/infrastructure/persistence/ai_monitor_market_features.py"
    ).read_text(encoding="utf-8")

    assert "ai_monitor._market_flow_input_maps" not in api_source
    assert "ai_monitor.latest_realtime_feature_snapshots" not in api_source
    assert "ai_monitor.realtime_feature_payload" not in api_source
    assert "market_flow_inputs = load_market_flow_input_maps" in orchestration_source
    assert "repository._query" not in persistence_source


def test_ai_monitor_no_longer_defines_market_feature_compatibility_facades() -> None:
    orchestration_source = (
        ROOT / "src/quantdesk_v2/ai_monitor.py"
    ).read_text(encoding="utf-8")

    assert "def realtime_feature_payload(" not in orchestration_source
    assert "def latest_realtime_feature_snapshots(" not in orchestration_source
    assert "def _market_flow_input_maps(" not in orchestration_source
    assert "normalize_realtime_feature_payload(" in orchestration_source
    assert "load_latest_realtime_feature_snapshots" in orchestration_source
    assert "load_market_flow_input_maps" in orchestration_source


def test_opportunity_candidate_policies_live_in_the_application_boundary() -> None:
    orchestration_source = (
        ROOT / "src/quantdesk_v2/ai_monitor.py"
    ).read_text(encoding="utf-8")
    opportunity_source = (
        ROOT / "src/quantdesk_v2/application/ai_monitor/opportunity_generation.py"
    ).read_text(encoding="utf-8")
    news_source = (
        ROOT / "src/quantdesk_v2/application/ai_monitor/news_scoring.py"
    ).read_text(encoding="utf-8")

    for name in (
        "filter_monitored_candidates",
        "annotate_event_cluster_selection",
        "fresh_candidate_news_ids",
        "strongest_candidate_per_symbol",
        "news_event_bursts",
    ):
        assert f"def {name}(" not in orchestration_source
    assert "def filter_monitored_candidates(" in opportunity_source
    assert "def annotate_event_cluster_selection(" in opportunity_source
    assert "def fresh_candidate_news_ids(" in opportunity_source
    assert "def strongest_candidate_per_symbol(" in opportunity_source
    assert "def news_event_bursts(" in news_source


def test_news_candidate_authority_lives_in_the_application_boundary() -> None:
    orchestration_source = (
        ROOT / "src/quantdesk_v2/ai_monitor.py"
    ).read_text(encoding="utf-8")
    news_source = (
        ROOT / "src/quantdesk_v2/application/ai_monitor/news_scoring.py"
    ).read_text(encoding="utf-8")

    assert "aggregate=score_news_candidates" in orchestration_source
    assert "actionability=classify_news_actionability" in orchestration_source
    assert "resolve_news_event_cluster_id(" in orchestration_source
    for name in (
        "aggregate_news_candidates",
        "configured_indicator_policy",
        "indicator_group",
        "match_configured_indicators",
        "multi_timeframe_technical_snapshot",
        "news_actionability_snapshot",
        "news_event_cluster_id",
        "resolved_news_event_cluster_id",
        "select_directional_candidates_with_technical_context",
    ):
        assert f"def {name}(" not in orchestration_source
        assert f"def {name}(" in news_source


def test_opportunity_admission_queries_live_in_the_persistence_boundary() -> None:
    orchestration_source = (
        ROOT / "src/quantdesk_v2/ai_monitor.py"
    ).read_text(encoding="utf-8")
    persistence_source = (
        ROOT
        / "src/quantdesk_v2/infrastructure/persistence/ai_monitor_opportunity_generation.py"
    ).read_text(encoding="utf-8")

    assert "load_consumed_news_ids_by_direction(" in orchestration_source
    assert "load_active_candidate_keys(" in orchestration_source
    assert "load_candidate_persistence_state(" in orchestration_source
    assert "create_opportunity_prediction(" in orchestration_source
    assert "consumed_rows = db.execute(" not in orchestration_source
    assert "active_for_symbol = db.scalars(" not in orchestration_source
    assert "AiMonitorPrediction(" not in orchestration_source
    assert "def consumed_news_ids_by_direction(" in persistence_source
    assert "def active_candidate_keys(" in persistence_source
    assert "def candidate_persistence_state(" in persistence_source
    assert "def create_opportunity_prediction(" in persistence_source


def test_event_visibility_logic_and_queries_have_owned_boundaries() -> None:
    orchestration_source = (
        ROOT / "src/quantdesk_v2/ai_monitor.py"
    ).read_text(encoding="utf-8")
    application_source = (
        ROOT / "src/quantdesk_v2/application/ai_monitor/event_gate.py"
    ).read_text(encoding="utf-8")
    persistence_source = (
        ROOT
        / "src/quantdesk_v2/infrastructure/persistence/ai_monitor_event_gate.py"
    ).read_text(encoding="utf-8")

    for name in (
        "market_risk_event_snapshot",
        "market_risk_event_gate_snapshot",
    ):
        assert f"def {name}(" not in orchestration_source
        assert f"def {name}(" in application_source
    for name in (
        "market_risk_event_contexts",
        "active_market_risk_events",
        "backfill_prediction_event_contexts",
    ):
        assert f"def {name}(" not in orchestration_source
        assert f"def {name}(" in persistence_source


def test_prediction_settlement_policy_and_persistence_have_owned_boundaries() -> None:
    orchestration_source = (
        ROOT / "src/quantdesk_v2/ai_monitor.py"
    ).read_text(encoding="utf-8")
    policy_source = (
        ROOT / "src/quantdesk_v2/application/ai_monitor/prediction_settlement.py"
    ).read_text(encoding="utf-8")
    persistence_source = (
        ROOT
        / "src/quantdesk_v2/infrastructure/persistence/ai_monitor_prediction_settlement.py"
    ).read_text(encoding="utf-8")

    for name in (
        "prediction_outcome",
        "prediction_cost_breakdown",
        "prediction_path_metrics",
        "prediction_price_barrier_exit",
        "prediction_adaptive_path_exit",
        "prediction_score_exit_signal",
        "prediction_score_exit_price",
        "virtual_risk_plan_snapshot",
    ):
        assert f"def {name}(" not in orchestration_source
        assert f"def {name}(" in policy_source
    for name in (
        "settle_due_predictions",
        "reopen_legacy_prediction_settlements",
        "backfill_prediction_path_metrics",
    ):
        assert f"def {name}(" not in orchestration_source
        assert f"def {name}(" in persistence_source


def test_read_model_projection_is_owned_by_the_persistence_boundary() -> None:
    compatibility_source = (
        ROOT / "src/quantdesk_v2/ai_monitor_read_models.py"
    ).read_text(encoding="utf-8")
    persistence_source = (
        ROOT
        / "src/quantdesk_v2/infrastructure/persistence/ai_monitor_read_models.py"
    ).read_text(encoding="utf-8")
    reader_source = (
        ROOT
        / "src/quantdesk_v2/infrastructure/persistence/ai_monitor_projection.py"
    ).read_text(encoding="utf-8")

    for name in (
        "read_models_available",
        "refresh_prediction_facts",
        "refresh_current_opportunities",
        "refresh_score_history",
        "refresh_ai_monitor_read_models",
        "reconcile_ai_monitor_read_models",
    ):
        assert f"def {name}(" not in compatibility_source
        assert f"def {name}(" in persistence_source
    assert "from .ai_monitor_read_models import read_models_available" in reader_source
    assert "from ...ai_monitor_read_models" not in reader_source


def test_historical_replay_uses_no_private_ai_monitor_symbols() -> None:
    replay_source = (
        ROOT / "src/quantdesk_v2/historical_replay.py"
    ).read_text(encoding="utf-8")

    assert "ai_monitor._" not in replay_source
    assert "classify_ablation_signal_state" in replay_source
