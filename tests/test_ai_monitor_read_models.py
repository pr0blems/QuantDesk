from __future__ import annotations

from datetime import UTC, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import BigInteger, create_engine, func, select
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session

from quantdesk_v2.ai_monitor import historical_opportunity_fact_analytics
from quantdesk_v2.ai_monitor_read_models import (
    reconcile_ai_monitor_read_models,
    refresh_ai_monitor_read_models,
)
from quantdesk_v2.application.ai_monitor import OpportunityProjectionLagging
from quantdesk_v2.interfaces.api.ai_monitor import _current_opportunity_projection_page
from quantdesk_v2.models import (
    AiMonitorOpportunity,
    AiMonitorOpportunityCurrent,
    AiMonitorPrediction,
    AiMonitorPredictionFact,
    AiMonitorScoreHistory,
    Base,
    OpportunityGateDecision,
    OpportunityMarketSnapshot,
    utcnow,
)


@compiles(BigInteger, "sqlite")
def _compile_big_integer_as_sqlite_integer(
    _type: BigInteger, _compiler: object, **_: object
) -> str:
    return "INTEGER"


def _projection_session() -> Session:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    names = (
        "ai_monitor_configs",
        "ai_monitor_opportunities",
        "ai_monitor_predictions",
        "opportunity_market_snapshots",
        "opportunity_gate_decisions",
        "ai_monitor_prediction_facts",
        "ai_monitor_opportunity_current",
        "ai_monitor_score_history",
    )
    Base.metadata.create_all(engine, tables=[Base.metadata.tables[name] for name in names])
    return Session(engine, expire_on_commit=False)


def test_read_models_project_prediction_current_state_and_scores() -> None:
    db = _projection_session()
    now = utcnow()
    opportunity = AiMonitorOpportunity(
        id=101,
        public_id="opportunity-101",
        user_id=7,
        analysis_run_id=11,
        symbol="AAPL",
        contract_symbol="AAPLUSDT",
        direction="long",
        status="discovered",
        timeframe="1h",
        news_score=Decimal("82"),
        indicator_score=Decimal("88"),
        combined_score=Decimal("84.5"),
        matched_indicator_keys_json=["trend"],
        news_ids_json=["news-1"],
        evidence_json={
            "signal_scores": {
                "news": 82,
                "indicator": 88,
                "market_flow": 73,
                "combined": 84.5,
            },
            "market_quality": {
                "market_session": "regular",
                "data_status": "passed",
                "data_coverage": 0.8,
                "quote": {"price": 999.99},
            },
            "market": {"price": 191.25},
            "gate_summary": {"passed": True, "blocking_reasons": []},
            "risk_events": [
                {
                    "event_name": "Chicago PMI",
                    "title": "Chicago PMI",
                    "risk_level": "medium",
                    "scheduled_at": "2026-08-28T13:45:00+00:00",
                    "minutes_until_event": 60,
                    "blocking_active": False,
                }
            ],
            "event_gate": {
                "status": "warning",
                "event_name": "Chicago PMI",
                "minutes_until_event": 60,
                "blocking": False,
            },
            "unusual_whales_policy": {
                "enabled": True,
                "channels": {"option_trades": False, "gex": True},
            },
            "version": {
                "feature": "features-v2",
                "weights": "weights-v3",
                "decision": "decision-v4",
            },
        },
        dedup_key="dedup-101",
        discovered_at=now,
        expires_at=now + timedelta(hours=4),
        created_at=now,
        updated_at=now,
    )
    prediction = AiMonitorPrediction(
        id=201,
        public_id="prediction-201",
        user_id=7,
        opportunity_id=101,
        symbol="AAPL",
        contract_symbol="AAPLUSDT",
        direction="long",
        timeframe="1h",
        status="pending",
        confidence_score=Decimal("84.5"),
        entry_price=Decimal("190"),
        signal_news_score=Decimal("82"),
        signal_indicator_score=Decimal("88"),
        estimated_cost_bps=Decimal("16"),
        settlement_version="cost_consistent_exit_v8",
        readiness_status="research_only",
        calibration_sample_count=0,
        evidence_json=opportunity.evidence_json,
        predicted_at=now,
        due_at=now + timedelta(hours=1),
        created_at=now,
        updated_at=now,
    )
    snapshot = OpportunityMarketSnapshot(
        id=301,
        opportunity_id=101,
        user_id=7,
        captured_at=now,
        quote_snapshot_json={
            "source": "unusual_whales",
            "last_price": 190,
            "bid": 189.99,
            "ask": 190.01,
            "quote_received_at_ms": int(
                (now - timedelta(hours=8)).replace(tzinfo=UTC).timestamp() * 1000
            ),
            "quote_age_ms": 1200,
        },
        option_flow_snapshot_json={},
        gex_snapshot_json={"available": True, "fresh": True},
        institutional_flow_snapshot_json={"available": True, "fresh": True},
        macro_snapshot_json={},
        risk_gate_snapshot_json={},
        score_components_json={},
        data_quality_json={"coverage": 0.8},
        weights_version="weights-v3",
        feature_version="features-v2",
        decision_version="decision-v4",
    )
    decision = OpportunityGateDecision(
        id=401,
        public_id="decision-401",
        opportunity_id=101,
        user_id=7,
        analysis_run_id=11,
        symbol="AAPL",
        contract_symbol="AAPLUSDT",
        direction="long",
        gate_status="passed",
        selected=True,
        decision_at=now,
        blocking_reasons_json=[],
        warnings_json=[],
        risk_gate_snapshot_json={},
        quote_snapshot_json={},
        market_flow_snapshot_json={"score": 73},
        score_components_json={
            "news": 82,
            "technical": 88,
            "market_flow": 73,
            "combined": 84.5,
        },
        data_quality_json={"coverage": 0.8},
        feature_version="features-v2",
        weights_version="weights-v3",
        decision_version="decision-v4",
        dedup_key="gate-dedup-401",
        created_at=now,
    )
    db.add_all([opportunity, prediction, snapshot, decision])
    db.commit()

    result = refresh_ai_monitor_read_models(
        db,
        user_id=7,
        force_availability_check=True,
    )

    assert result == {
        "available": True,
        "prediction_facts": 1,
        "current_opportunities": 1,
        "score_history": 1,
    }
    fact = db.scalar(select(AiMonitorPredictionFact))
    assert fact is not None
    assert fact.snapshot_complete is True
    assert fact.market_session in {"premarket", "regular", "postmarket", "closed"}
    assert fact.quote_quality == "passed"
    assert fact.quote_source == "unusual_whales"
    assert fact.quote_age_ms == 1200
    assert fact.option_flow_status == "channel_disabled"
    assert fact.gex_status == "available"
    assert fact.institutional_flow_status == "available"
    assert fact.projection_version == "signal_features_v4"
    assert fact.price_source == "binance"
    assert fact.news_score == Decimal("82.0000")
    assert fact.combined_score == Decimal("84.5000")
    current = db.scalar(select(AiMonitorOpportunityCurrent))
    assert current is not None
    assert current.lifecycle_status == "triggered"
    assert current.price_source == "binance"
    assert current.current_price == Decimal("191.250000000000")
    history = db.scalar(select(AiMonitorScoreHistory))
    assert history is not None
    assert history.sampled_at.minute % 5 == 0
    assert history.combined_score == Decimal("84.5000")
    reconciliation = reconcile_ai_monitor_read_models(db, user_id=7)
    assert reconciliation["ready"] is True
    assert reconciliation["price_source_violations"] == 0

    projected_page = _current_opportunity_projection_page(
        db,
        user_id=7,
        limit=20,
        page=1,
        now=now,
    )
    assert projected_page is not None
    assert projected_page["pagination"]["total"] == 1
    assert projected_page["rows"][0][0].contract_symbol == "AAPLUSDT"

    second = refresh_ai_monitor_read_models(
        db,
        user_id=7,
        force_availability_check=True,
    )
    assert second["available"] is True
    assert db.scalar(select(func.count()).select_from(AiMonitorPredictionFact)) == 1
    assert db.scalar(select(func.count()).select_from(AiMonitorScoreHistory)) == 1

    prediction.status = "completed"
    prediction.result = "win"
    prediction.exit_price = Decimal("192")
    prediction.raw_return_bps = Decimal("105.2632")
    prediction.directional_return_bps = Decimal("105.2632")
    prediction.max_favorable_bps = Decimal("150")
    prediction.max_adverse_bps = Decimal("-20")
    prediction.exit_reason = "take_profit"
    prediction.exit_subreason = "profit_lock"
    prediction.peak_favorable_bps_at_exit = Decimal("150")
    prediction.protected_bps_at_exit = Decimal("45")
    prediction.exit_at = now + timedelta(minutes=30)
    prediction.completed_at = prediction.exit_at
    prediction.updated_at = now + timedelta(minutes=31)
    db.commit()
    refresh_ai_monitor_read_models(db, user_id=7)

    analytics = historical_opportunity_fact_analytics(
        db,
        7,
        limit=20,
        page=1,
        date_from=None,
        date_to=None,
        timezone_offset_minutes=0,
        symbol="",
        news_score_min=0,
        indicator_score_min=0,
        combined_score_min=0,
        option_flow_score_min=0,
        gex_score_min=0,
        min_data_coverage=0,
        feature_version="",
        decision_version="all",
        settlement_version="current",
        direction="all",
        market_session="all",
        quote_quality="all",
        event_risk="all",
        exit_reason="all",
        include_readiness=False,
    )
    assert analytics is not None
    assert analytics["query_mode"] == "prediction_fact_read_model"
    assert analytics["pagination"]["total"] == 1
    assert analytics["summary"]["total_prediction_count"] == 1
    assert "excluded_legacy_settlement_count" not in analytics["summary"]
    assert analytics["items"][0]["price_source"] == "binance"
    assert analytics["items"][0]["entry_price"] == 190.0
    assert analytics["items"][0]["exit_detail"] == "profit_lock"
    assert analytics["items"][0]["exit_protection"] == {
        "peak_favorable_bps": 150.0,
        "protected_bps": 45.0,
    }
    assert analytics["items"][0]["event_title"] == "Chicago PMI"
    assert analytics["items"][0]["event_gate"]["status"] == "warning"
    assert analytics["items"][0]["risk_events"][0]["minutes_until_event"] == 60
    assert analytics["filters"]["settlement_version"] == "cost_consistent_exit_v8"
    assert analytics["settlement_versions"][0]["current"] is True

    prediction.settlement_version = "cost_consistent_exit_v6"
    prediction.updated_at = now + timedelta(minutes=32)
    db.commit()
    refresh_ai_monitor_read_models(db, user_id=7)
    legacy = historical_opportunity_fact_analytics(
        db,
        7,
        limit=20,
        page=1,
        date_from=None,
        date_to=None,
        timezone_offset_minutes=0,
        symbol="",
        news_score_min=0,
        indicator_score_min=0,
        combined_score_min=0,
        option_flow_score_min=0,
        gex_score_min=0,
        min_data_coverage=0,
        feature_version="",
        decision_version="all",
        settlement_version="cost_consistent_exit_v6",
        direction="all",
        market_session="all",
        quote_quality="all",
        event_risk="all",
        exit_reason="profit_lock",
        include_readiness=False,
    )
    assert legacy is not None
    assert legacy["pagination"]["total"] == 1
    assert legacy["summary"]["exit_reason_counts"] == {"profit_lock": 1}
    assert legacy["filters"]["settlement_version"] == "cost_consistent_exit_v6"


def test_current_projection_refuses_source_table_fallback_when_lagging() -> None:
    db = _projection_session()
    now = utcnow()
    db.add(
        AiMonitorOpportunity(
            id=501,
            public_id="opportunity-501",
            user_id=7,
            analysis_run_id=51,
            symbol="NVDA",
            contract_symbol="NVDAUSDT",
            direction="short",
            status="discovered",
            timeframe="1h",
            news_score=Decimal("80"),
            indicator_score=Decimal("85"),
            combined_score=Decimal("82"),
            matched_indicator_keys_json=["trend"],
            news_ids_json=["news-501"],
            evidence_json={},
            dedup_key="dedup-501",
            discovered_at=now,
            expires_at=now + timedelta(hours=1),
            created_at=now,
            updated_at=now,
        )
    )
    db.commit()

    with pytest.raises(OpportunityProjectionLagging):
        _current_opportunity_projection_page(
            db,
            user_id=7,
            limit=20,
            page=1,
            now=now,
        )


def test_read_model_migration_follows_latest_revision() -> None:
    source = (
        __import__("pathlib").Path(__file__).parents[1]
        / "migrations"
        / "versions"
        / "0059_ai_monitor_read_models.py"
    ).read_text(encoding="utf-8")
    assert 'down_revision: str | None = "0058_finnhub_quote_snapshots"' in source
    for table in (
        "ai_monitor_prediction_facts",
        "ai_monitor_opportunity_current",
        "ai_monitor_score_history",
    ):
        assert f'"{table}"' in source

    feature_status_source = (
        __import__("pathlib").Path(__file__).parents[1]
        / "migrations"
        / "versions"
        / "0063_prediction_fact_feature_status.py"
    ).read_text(encoding="utf-8")
    assert 'down_revision: str | None = "0062_ai_projection_outbox"' in feature_status_source
    for column in (
        "quote_source",
        "quote_age_ms",
        "quote_spread_bps",
        "option_flow_status",
        "gex_status",
        "institutional_flow_status",
        "projection_version",
    ):
        assert f'"{column}"' in feature_status_source

    exit_semantics_source = (
        __import__("pathlib").Path(__file__).parents[1]
        / "migrations"
        / "versions"
        / "0065_prediction_exit_semantics.py"
    ).read_text(encoding="utf-8")
    assert 'down_revision: str | None = "0064_strategy_source_runtime"' in exit_semantics_source
    for column in (
        "exit_subreason",
        "peak_favorable_bps_at_exit",
        "protected_bps_at_exit",
    ):
        assert f'"{column}"' in exit_semantics_source


def test_ai_monitor_frontend_cancels_stale_opportunity_requests() -> None:
    source = (
        __import__("pathlib").Path(__file__).parents[1]
        / "src"
        / "quantdesk_v2"
        / "static"
        / "ai-monitor.js"
    ).read_text(encoding="utf-8")
    assert "this.opportunitiesAbortController?.abort()" in source
    assert "{ signal: controller.signal }" in source
    assert 'error?.name === "AbortError"' in source
    assert "this.startNewsAutoScroll()" in source
    assert "requestAnimationFrame(step)" in source
    assert "window.setInterval(() => this.autoScrollNews(), 80)" not in source
