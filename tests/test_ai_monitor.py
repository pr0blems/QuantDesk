from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy.dialects import mysql, sqlite

from quantdesk_v2 import news_ai
from quantdesk_v2.ai_monitor import (
    NEWS_BATCH_SIZE,
    RUN_STALE_SECONDS,
    _news_model_call_audit_index,
    _take_ingested_news,
    adaptive_exit_precedes,
    aggregate_news_candidates,
    annotate_event_cluster_selection,
    append_score_history,
    backfill_prediction_path_metrics,
    configured_indicator_policy,
    edge_calibration_summary,
    effective_opportunity_score_weights,
    enqueue_news_analysis,
    filter_monitored_candidates,
    historical_closed_settlement_price,
    historical_settlement_price,
    indicator_catalog,
    indicator_conflicts,
    indicator_templates,
    market_flow_history_snapshot,
    market_flow_snapshot,
    match_configured_indicators,
    merged_opportunity_expiration,
    news_actionability_snapshot,
    opportunity_score_weights,
    prediction_actionability_gate_summary,
    prediction_adaptive_path_exit,
    prediction_cost_breakdown,
    prediction_estimated_cost_bps,
    prediction_live_score_snapshot,
    prediction_net_outcome,
    prediction_outcome,
    prediction_path_metrics,
    prediction_price_barrier_exit,
    prediction_score_exit_price,
    prediction_score_exit_signal,
    prediction_settlement_cost_config,
    prediction_soft_exit_policy,
    refresh_pending_prediction_scores,
    reopen_legacy_prediction_settlements,
    settle_due_predictions,
    settleable_historical_outcomes,
    settlement_exit_subreason,
    signal_readiness_snapshot,
    stable_gate_summary,
    strongest_candidate_per_symbol,
    summarize_historical_opportunities,
    virtual_entry_gate_snapshot,
    virtual_position_snapshot,
    virtual_risk_plan_snapshot,
    weighted_opportunity_score,
)
from quantdesk_v2.interfaces.api.ai_monitor import (
    _ai_monitor_revisions,
    _changed_revision_scopes,
    _local_date_utc_window,
    _prediction_settlement_out,
    _price_comparison_out,
    _revision_event_id,
    _safe_market_data_health,
    _sse_message,
    _stable_opportunity_contract,
    _utc_out,
    _websocket_access_token,
    ai_monitor_events,
)
from quantdesk_v2.market_microstructure import order_book_gate_snapshot
from quantdesk_v2.models import (
    AiMonitorOpportunity,
    AiMonitorPrediction,
    AiMonitorRun,
    News,
    NewsAiBatch,
    NewsAiModelCall,
    NewsAiModelCallItem,
)
from quantdesk_v2.schemas import (
    AiMonitorConfigUpdate,
    AiMonitorCostConfigUpdate,
    AiMonitorNewsAnalyzeRequest,
)

ROOT = Path(__file__).resolve().parents[1]


def test_price_comparison_uses_binance_as_execution_and_cash_feeds_as_reference() -> None:
    now = datetime.now(UTC)
    now_ms = int(now.timestamp() * 1000)

    result = _price_comparison_out(
        {"price": 101.0, "ts": now_ms},
        {
            "price": 100.0,
            "source_timestamp": int(now.timestamp()),
            "fetched_at": now,
            "available": True,
            "stale": False,
            "live": True,
        },
        {
            "captured_at": now.isoformat(),
            "quote": {
                "bid": 99.9,
                "ask": 100.1,
                "quote_received_at_ms": now_ms,
            },
        },
    )

    assert result["execution_source"] == "binance"
    assert result["sources"]["binance"]["fresh"] is True
    assert result["sources"]["finnhub"]["fresh"] is True
    assert result["sources"]["unusual_whales"]["fresh"] is True
    assert result["reference_price"] == pytest.approx(100.0)
    assert result["basis_bps"] == pytest.approx(100.0)
    assert result["state"] == "spread_watch"
    assert result["pair_direction"] == "short_binance_long_spot"
    assert result["actionable"] is False


def test_price_comparison_uses_stale_cash_snapshot_for_opening_gap_forecast() -> None:
    now = datetime.now(UTC)
    stale_at = now - timedelta(hours=2)

    result = _price_comparison_out(
        {"price": 95.0, "ts": int(now.timestamp() * 1000)},
        {
            "price": 100.0,
            "previous_close": 99.0,
            "source_timestamp": int(stale_at.timestamp()),
            "fetched_at": stale_at,
            "available": True,
            "stale": True,
            "live": False,
        },
        {
            "captured_at": stale_at.isoformat(),
            "quote": {
                "bid": 99.9,
                "ask": 100.1,
                "quote_received_at_ms": int(stale_at.timestamp() * 1000),
            },
        },
        direction="short",
        news_score=85.0,
        news_count=4,
        new_news_count=1,
        reused_news_count=3,
        memory_window_hours=168,
    )

    assert result["comparable"] is False
    assert result["reference_price"] is None
    assert result["basis_bps"] is None
    assert result["state"] == "opening_gap_watch"
    assert result["snapshot_reference_price"] == pytest.approx(100.0)
    assert result["snapshot_gap_bps"] == pytest.approx(-500.0)
    assert result["previous_close_gap_bps"] == pytest.approx(-404.0404)
    assert result["provider_divergence_bps"] == pytest.approx(0.0)
    assert result["provider_divergence_mode"] == "snapshot"
    assert result["pair_direction"] is None
    forecast = result["opening_forecast"]
    assert forecast["available"] is True
    assert forecast["label"] == "bearish_open"
    assert forecast["direction"] == "down"
    assert forecast["confidence"] == pytest.approx(96.0)
    assert forecast["news_score"] == pytest.approx(85.0)
    assert forecast["related_news_count"] == 4
    assert forecast["new_news_count"] == 1
    assert forecast["reused_news_count"] == 3
    assert forecast["memory_window_hours"] == 168
    assert forecast["gap_bps"] == pytest.approx(-500.0)
    assert forecast["gap_aligned"] is True
    assert forecast["reference_mode"] == "latest_cash_snapshot"
    assert forecast["research_only"] is True
    assert result["actionable"] is False


def test_ai_monitor_api_serializes_naive_database_datetimes_as_utc() -> None:
    naive = datetime(2026, 8, 10, 7, 14, 27)

    serialized = _utc_out(naive)

    assert serialized is not None
    assert serialized.tzinfo is UTC
    assert serialized.isoformat() == "2026-08-10T07:14:27+00:00"
    assert _utc_out(None) is None


def test_ai_monitor_local_date_filter_uses_browser_calendar_boundaries() -> None:
    start, end = _local_date_utc_window(
        datetime(2026, 8, 17).date(),
        datetime(2026, 8, 17).date(),
        480,
    )

    assert start == datetime(2026, 8, 16, 16, 0)
    assert end == datetime(2026, 8, 17, 16, 0)


def test_ai_monitor_local_date_filter_supports_one_sided_ranges() -> None:
    start, end = _local_date_utc_window(
        datetime(2026, 8, 17).date(),
        None,
        -300,
    )
    assert start == datetime(2026, 8, 17, 5, 0)
    assert end is None

    start, end = _local_date_utc_window(
        None,
        datetime(2026, 8, 17).date(),
        -300,
    )
    assert start is None
    assert end == datetime(2026, 8, 18, 5, 0)


def test_ai_monitor_revision_snapshot_scopes_private_rows_to_user() -> None:
    statements = []
    values = iter(
        (
            (datetime(2026, 8, 16, 10, 0), 41),
            (datetime(2026, 8, 16, 10, 1), 42),
            (1_777_000_000, datetime(2026, 8, 16, 10, 2)),
            (datetime(2026, 8, 16, 10, 3), 43),
        )
    )

    class Result:
        def __init__(self, value) -> None:
            self.value = value

        def one(self):
            return self.value

    class Database:
        def execute(self, statement):
            statements.append(statement)
            return Result(next(values))

    revisions = _ai_monitor_revisions(Database(), 73)  # type: ignore[arg-type]

    opportunity_sql = str(
        statements[0].compile(dialect=mysql.dialect(), compile_kwargs={"literal_binds": True})
    )
    run_sql = str(
        statements[1].compile(dialect=mysql.dialect(), compile_kwargs={"literal_binds": True})
    )
    assert "ai_monitor_opportunities.user_id = 73" in opportunity_sql
    assert "ai_monitor_runs.user_id = 73" in run_sql
    assert revisions["opportunities"]["cursor"] == 41
    assert revisions["news"]["latest_ts"] == 1_777_000_000
    assert revisions["market"]["captured_at"] == "2026-08-16T10:03:00+00:00"


def test_ai_monitor_sse_revision_protocol_is_resumable_and_scope_incremental() -> None:
    initial = {
        "opportunities": {"cursor": 4},
        "runs": {"cursor": 3},
        "news": {"latest_ts": 2},
        "market": {"cursor": 1},
    }
    changed = {
        **initial,
        "opportunities": {"cursor": 5},
        "market": {"cursor": 2},
    }

    event_id = _revision_event_id(initial)
    assert event_id == _revision_event_id(dict(reversed(list(initial.items()))))
    assert _changed_revision_scopes(initial, changed) == ["opportunities", "market"]

    message = _sse_message(
        event="update",
        event_id=event_id,
        data={"scopes": ["opportunities", "market"]},
    )
    assert f"id: {event_id}\n" in message
    assert "event: update\n" in message
    assert "retry: 3000\n" in message
    assert 'data: {"scopes":["opportunities","market"]}\n\n' in message
    assert "token" not in message.lower()


def test_ai_monitor_sse_flushes_connection_before_revision_queries() -> None:
    request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(database_engine=object())),
        headers={},
    )
    response = ai_monitor_events(request, user_id=73)

    async def read_first_chunk() -> str:
        chunk = await anext(response.body_iterator)
        await response.body_iterator.aclose()
        return chunk

    assert asyncio.run(read_first_chunk()) == ": connected\n\n"


def test_ai_monitor_websocket_reads_bearer_from_subprotocol_without_query_string() -> None:
    websocket = SimpleNamespace(
        scope={
            "subprotocols": [
                "quantdesk.ai-monitor.v1",
                "quantdesk.auth.header.payload.signature",
            ]
        }
    )

    assert _websocket_access_token(websocket) == "header.payload.signature"
    assert _websocket_access_token(SimpleNamespace(scope={"subprotocols": []})) is None


def test_ai_monitor_market_data_health_is_operational_and_secret_free() -> None:
    runtime = SimpleNamespace(
        health_snapshot=lambda: {
            "status": "connected",
            "connected": True,
            "last_event_at_ms": 1_776_336_000_000,
            "rest": {"status": "ready", "last_poll_at_ms": 1_776_336_000_100},
            "leadership": {"status": "leader", "is_leader": True},
            "writer": {
                "queue_utilization": 0.05,
                "events_per_minute": 120,
                "write_latency_ms": {"p50": 8.0, "p95": 20.0, "p99": 30.0},
            },
            "retention": {"status": "ready", "last_run_at_ms": 1_776_336_000_200},
            "api_key": "must-not-leak",
        },
        channel_health_snapshot=lambda: {"price": {"age_ms": 420, "status": "live"}},
    )
    request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(unusual_whales_runtime=runtime))
    )

    health = _safe_market_data_health(request)  # type: ignore[arg-type]

    assert health["websocket_connected"] is True
    assert health["rest_healthy"] is True
    assert health["quote"] == {"age_ms": 420, "status": "live"}
    assert health["leadership"] == {"status": "leader", "is_leader": True}
    assert "must-not-leak" not in str(health)


def test_stable_opportunity_contract_normalizes_legacy_gate_check_list() -> None:
    contract = _stable_opportunity_contract(
        {
            "virtual_entry_gate": {
                "entry_ready": False,
                "checks": [
                    {"key": "news_score", "passed": True},
                    {"key": "entry_price", "passed": False},
                ],
            }
        },
        None,
    )

    assert contract["gate_summary"]["passed"] is False
    assert contract["gate_summary"]["checks"] == {
        "news_score": True,
        "entry_price": False,
    }
    assert "REFERENCE_QUOTE_UNAVAILABLE" in contract["gate_summary"]["warnings"]
    assert contract["data_quality"] == {
        "quote_available": False,
        "quote_status": "unavailable",
        "reject_reason": "REFERENCE_QUOTE_UNAVAILABLE",
    }


def test_current_opportunity_recomputes_cash_reference_failures_as_observations() -> None:
    evidence = {
        "market_quality": {
            "quote_available": False,
            "data_status": "degraded",
            "checks": {
                "price_available": True,
                "ticker_fresh": True,
                "kline_fresh": True,
                "feature_quality": True,
                "reference_quote_available": False,
                "quote_fresh": False,
                "spread_acceptable": False,
            },
        },
        "market_flow": {"hard_conflict": False, "legacy_hard_conflict": False},
        "unusual_whales_policy": {"effective_mode": "record"},
        "gate_summary": {
            "passed": False,
            "blocking_reasons": ["REFERENCE_QUOTE_UNAVAILABLE"],
        },
    }

    current = _stable_opportunity_contract(
        evidence,
        None,
        recompute_current_gate=True,
    )
    frozen = _stable_opportunity_contract(evidence, None)

    assert current["gate_summary"]["passed"] is True
    assert current["gate_summary"]["execution_price_source"] == "binance"
    assert current["gate_summary"]["decision_checks"] == {
        "price_available": True,
        "ticker_fresh": True,
        "kline_fresh": True,
        "feature_quality": True,
        "directional_conflict_clear": True,
    }
    assert "OBSERVED_ONLY:REFERENCE_QUOTE_UNAVAILABLE" in current["gate_summary"]["warnings"]
    assert frozen["gate_summary"]["passed"] is False


def test_prediction_settlement_metadata_explains_pending_market_retry() -> None:
    now = datetime.now(UTC).replace(tzinfo=None)
    item = SimpleNamespace(
        status="pending",
        predicted_at=now - timedelta(hours=2),
        due_at=now - timedelta(hours=1),
        updated_at=now - timedelta(minutes=1),
    )

    metadata = _prediction_settlement_out(item)

    assert metadata is not None
    assert metadata["phase"] == "awaiting_market_data"
    assert metadata["retry_interval_minutes"] == 5
    assert metadata["grace_hours"] == 6
    assert metadata["poll_interval_seconds"] == 20
    assert metadata["next_retry_at"] > _utc_out(now)


def test_prediction_settlement_metadata_monitors_exit_before_time_cap() -> None:
    now = datetime.now(UTC).replace(tzinfo=None)
    item = SimpleNamespace(
        status="pending",
        predicted_at=now - timedelta(minutes=10),
        due_at=now + timedelta(minutes=50),
        updated_at=now - timedelta(minutes=4),
    )

    metadata = _prediction_settlement_out(item)

    assert metadata is not None
    assert metadata["phase"] == "monitoring_exit"
    assert metadata["next_retry_at"] < metadata["due_at"]


def test_legacy_prediction_is_reopened_before_new_lifecycle_statistics() -> None:
    original_evidence = {
        "risk_plan": {"version": "legacy_risk_guard"},
        "settlement": {"version": "path_cost_v2"},
    }
    item = SimpleNamespace(
        status="completed",
        result="win",
        exit_price=Decimal("102"),
        exit_at=datetime(2026, 8, 10, 9, 0),
        exit_reason="legacy_horizon_close",
        raw_return_bps=Decimal("200"),
        directional_return_bps=Decimal("200"),
        net_directional_return_bps=Decimal("184"),
        net_result="win",
        max_favorable_bps=Decimal("250"),
        max_adverse_bps=Decimal("-30"),
        completed_at=datetime(2026, 8, 10, 9, 1),
        settlement_version="path_cost_v2",
        evidence_json=original_evidence,
        updated_at=datetime(2026, 8, 10, 9, 1),
    )

    class Scalars:
        def all(self):
            return [item]

    class Database:
        flushed = False
        statement = None

        def scalars(self, statement):
            self.statement = statement
            return Scalars()

        def flush(self):
            self.flushed = True

    database = Database()
    repaired = reopen_legacy_prediction_settlements(database)
    mysql_compiled = database.statement.compile(dialect=mysql.dialect())
    mysql_sql = str(mysql_compiled)
    sqlite_sql = str(database.statement.compile(dialect=sqlite.dialect()))

    assert repaired == 1
    assert item.status == "pending"
    assert item.result is None
    assert item.exit_price is None
    assert item.exit_reason is None
    assert item.settlement_version == "repair_pending_v4"
    assert item.evidence_json["risk_plan"]["settlement_version"] == "cost_consistent_exit_v7"
    assert item.evidence_json["settlement_repair"]["status"] == "pending_recalculation"
    assert database.flushed is True
    assert "FOR UPDATE SKIP LOCKED" in mysql_sql
    assert "FOR UPDATE" not in sqlite_sql
    assert "ai_monitor_predictions.status = %s" in mysql_sql
    assert "ai_monitor_predictions.exit_reason = %s" in mysql_sql
    assert {"completed", "legacy_horizon_close"}.issubset(set(mysql_compiled.params.values()))
    assert "cost_consistent_exit_v8" not in set(mysql_compiled.params.values())
    assert database.statement.get_execution_options()["populate_existing"] is True


def test_path_metric_backfill_preserves_frozen_settlement_version() -> None:
    predicted_at = datetime.now(UTC).replace(tzinfo=None) - timedelta(hours=2)
    item = SimpleNamespace(
        contract_symbol="AAPLUSDT",
        predicted_at=predicted_at,
        due_at=predicted_at + timedelta(hours=1),
        entry_price=Decimal("100"),
        direction="long",
        settlement_version="horizon_aligned_exit_v5",
        max_favorable_bps=None,
        max_adverse_bps=None,
        updated_at=predicted_at,
    )

    class Scalars:
        def all(self):
            return [item]

    class Database:
        flushed = False

        def scalars(self, _statement):
            return Scalars()

        def flush(self):
            self.flushed = True

    class Repository:
        def kline_range(self, _symbol, _timeframe, start_ms, _end_ms):
            return [
                {
                    "open_time": start_ms + 15 * 60 * 1_000,
                    "open": 100,
                    "high": 102,
                    "low": 99,
                    "close": 101,
                }
            ]

    database = Database()
    result = backfill_prediction_path_metrics(database, Repository())

    assert result == {"scanned": 1, "completed": 1, "unavailable": 0}
    assert item.max_favorable_bps == Decimal("200.0")
    assert item.max_adverse_bps == Decimal("-100.0")
    assert item.settlement_version == "horizon_aligned_exit_v5"
    assert database.flushed is True


def test_ai_monitor_catalog_reuses_all_contract_research_indicators() -> None:
    items = indicator_catalog("1h")

    assert len(items) == 20
    assert len({item["key"] for item in items}) == 20
    assert {item["source"] for item in items} == {"kline", "prediction"}
    assert "moving_average_bull" in {item["key"] for item in items}
    assert "prediction_trend" in {item["key"] for item in items}
    assert NEWS_BATCH_SIZE == 10


def test_news_monitor_persists_batch_before_foreign_key_run() -> None:
    source = (ROOT / "src/quantdesk_v2/ai_monitor.py").read_text(encoding="utf-8")
    batch_add = source.index("NewsAiBatch(")
    run_add = source.index("run = AiMonitorRun(")

    assert batch_add < source.index("db.flush()", batch_add) < run_add


def test_ai_monitor_run_endpoint_rolls_back_database_failures() -> None:
    source = (ROOT / "src/quantdesk_v2/interfaces/api/ai_monitor.py").read_text(encoding="utf-8")
    endpoint = source[source.index('@router.post("/runs"') :]

    assert "except IntegrityError:" in endpoint
    assert "except SQLAlchemyError:" in endpoint
    assert endpoint.count("db.rollback()") >= 3
    assert "status_code=503" in endpoint


def test_single_news_analysis_targets_only_the_selected_record() -> None:
    api = (ROOT / "src/quantdesk_v2/interfaces/api/ai_monitor.py").read_text(encoding="utf-8")
    monitor = (ROOT / "src/quantdesk_v2/ai_monitor.py").read_text(encoding="utf-8")
    analyzer = (ROOT / "src/quantdesk_v2/news_ai.py").read_text(encoding="utf-8")
    payload = AiMonitorNewsAnalyzeRequest(news_id="news-selected")

    assert payload.news_id == "news-selected"
    assert '@router.post("/news/analyze", status_code=202)' in api
    assert "create_single_news_run(db, user.id, payload.news_id)" in api
    assert "[payload.news_id]" in api
    assert "def create_single_news_run(" in monitor
    assert "news_ids: Sequence[str] | None = None" in analyzer
    assert "News.id.in_(selected_news_ids)" in analyzer


def test_startup_only_fails_stale_news_batches_and_monitor_runs() -> None:
    source = (ROOT / "src/quantdesk_v2/admin.py").read_text(encoding="utf-8")

    assert "stale_cutoff = now - timedelta(minutes=10)" in source
    assert "NewsAiBatch.updated_at < stale_cutoff" in source
    assert "AiMonitorRun.updated_at < stale_cutoff" in source
    assert AiMonitorRun.__table__.c.status.name == "status"


def test_ai_monitor_worker_recovers_abandoned_runs_continuously() -> None:
    source = (ROOT / "src/quantdesk_v2/ai_monitor.py").read_text(encoding="utf-8")

    assert RUN_STALE_SECONDS == 300
    assert "def recover_stale_runs(" in source
    assert "recover_stale_runs(db)" in source
    assert 'latest_news_run.status in {"completed", "partial"}' in source
    assert "pending >= NEWS_CATCH_UP_THRESHOLD and catch_up_allowed" in source
    assert 'name="ai-news-immediate"' in source
    assert "def _ingest_worker_loop(" in source
    assert "def _enqueue_failed_legacy_news(" in source
    assert "trigger_opportunity=True" in source
    assert "symbols_config=symbols_config" in source


def test_collector_news_queue_is_ordered_and_deduplicated() -> None:
    _take_ingested_news(10_000)

    assert enqueue_news_analysis(["news-1", "news-1", "news-2"]) == 2
    assert enqueue_news_analysis(["news-2", "news-3"]) == 1
    assert _take_ingested_news(2) == ["news-1", "news-2"]
    assert _take_ingested_news(2) == ["news-3"]


def test_news_candidates_group_by_direction_and_require_confidence() -> None:
    rows = [
        {
            "id": "news-1",
            "ts": 100,
            "source": "feed",
            "title": "Apple update",
            "title_zh": "苹果公司更新",
            "ai_confidence": 0.9,
            "ai_reason": "利好产品需求",
            "related_us_stocks": [
                {"symbol": "AAPL", "relevance": 0.8, "direction": "bull"},
                {"symbol": "MSFT", "relevance": 0.9, "direction": "bear"},
            ],
        },
        {
            "id": "news-2",
            "ts": 101,
            "source": "feed",
            "title": "Apple follow-up",
            "ai_confidence": 0.8,
            "related_us_stocks": [{"symbol": "AAPL", "relevance": 0.75, "direction": "bull"}],
        },
    ]

    candidates = aggregate_news_candidates(
        rows,
        {"AAPL": "AAPLUSDT", "MSFT": "MSFTUSDT"},
        minimum_confidence=0.6,
        minimum_mentions=2,
    )

    assert len(candidates) == 1
    assert candidates[0]["symbol"] == "AAPL"
    assert candidates[0]["contract_symbol"] == "AAPLUSDT"
    assert candidates[0]["direction"] == "long"
    assert candidates[0]["news_score"] == 66.0
    assert {item["id"] for item in candidates[0]["news"]} == {"news-1", "news-2"}


def test_news_candidates_include_bearish_stocks_as_short_opportunities() -> None:
    candidates = aggregate_news_candidates(
        [
            {
                "id": "bear-news",
                "ts": 100,
                "ai_confidence": 0.9,
                "related_us_stocks": [{"symbol": "TSLA", "relevance": 0.8, "direction": "bear"}],
            },
            {
                "id": "bull-news",
                "ts": 101,
                "ai_confidence": 0.85,
                "related_us_stocks": [{"symbol": "NVDA", "relevance": 0.9, "direction": "bullish"}],
            },
        ],
        {"TSLA": "TSLAUSDT", "NVDA": "NVDAUSDT"},
        minimum_confidence=0.6,
        minimum_mentions=1,
    )

    assert {(item["symbol"], item["direction"]) for item in candidates} == {
        ("TSLA", "short"),
        ("NVDA", "long"),
    }
    short = next(item for item in candidates if item["direction"] == "short")
    assert short["news"][0]["direction"] == "short"


def test_news_candidates_reject_generic_crypto_mstr_association() -> None:
    candidates = aggregate_news_candidates(
        [
            {
                "id": "eth-news",
                "ts": 100,
                "title": "Ethereum and Solana staking update",
                "summary": "The report only discusses altcoin supply.",
                "ai_confidence": 0.9,
                "related_us_stocks": [{"symbol": "MSTR", "relevance": 0.9, "direction": "bull"}],
            },
            {
                "id": "bitcoin-news",
                "ts": 101,
                "title": "Bitcoin falls as institutional demand weakens",
                "summary": "BTC volatility rises.",
                "ai_confidence": 0.9,
                "related_us_stocks": [{"symbol": "MSTR", "relevance": 0.8, "direction": "bear"}],
            },
        ],
        {"MSTR": "MSTRUSDT"},
        minimum_confidence=0.6,
        minimum_mentions=1,
    )

    assert len(candidates) == 1
    assert candidates[0]["symbol"] == "MSTR"
    assert candidates[0]["direction"] == "short"
    assert [item["id"] for item in candidates[0]["news"]] == ["bitcoin-news"]


def test_ai_monitor_keeps_only_the_strongest_direction_per_symbol() -> None:
    candidates = strongest_candidate_per_symbol(
        [
            {
                "symbol": "LLY",
                "contract_symbol": "LLYUSDT",
                "direction": "long",
                "news_score": 80,
                "news": [{"ts": 100}],
            },
            {
                "symbol": "LLY",
                "contract_symbol": "LLYUSDT",
                "direction": "short",
                "news_score": 85,
                "news": [{"ts": 101}],
            },
            {
                "symbol": "AMD",
                "contract_symbol": "AMDUSDT",
                "direction": "short",
                "news_score": 75,
                "news": [{"ts": 102}],
            },
        ]
    )

    assert [(item["symbol"], item["direction"]) for item in candidates] == [
        ("LLY", "short"),
        ("AMD", "short"),
    ]


def test_news_candidates_keep_unmapped_us_stocks_visible() -> None:
    candidates = aggregate_news_candidates(
        [
            {
                "id": "shop-news",
                "ts": 100,
                "title_zh": "Shopify AI 商务增长",
                "ai_confidence": 0.8,
                "related_us_stocks": [{"symbol": "SHOP", "relevance": 0.9, "direction": "bull"}],
            }
        ],
        {},
        minimum_confidence=0.6,
        minimum_mentions=1,
    )

    assert len(candidates) == 1
    assert candidates[0]["symbol"] == "SHOP"
    assert candidates[0]["contract_symbol"] == ""
    assert candidates[0]["market_available"] is False
    assert candidates[0]["direction"] == "long"


def test_configured_indicators_use_grouped_core_policy() -> None:
    scan = {
        "items": [
            {
                "key": "moving_average_bull",
                "name": "均线多头",
                "triggered": True,
                "status": "triggered",
                "summary": "已满足",
            },
            {
                "key": "price_volume_rise",
                "name": "量价齐升",
                "triggered": False,
                "status": "not_triggered",
                "summary": "未满足",
            },
        ],
        "prediction_features": {
            "items": [
                {
                    "key": "prediction_trend",
                    "name": "周期趋势",
                    "direction": "bullish",
                    "status": "bullish",
                    "summary": "偏多",
                }
            ]
        },
    }

    matched, evidence = match_configured_indicators(
        scan, ["moving_average_bull", "prediction_trend"]
    )
    rejected, rejected_evidence = match_configured_indicators(
        scan, ["moving_average_bull", "price_volume_rise"]
    )

    assert matched is True
    assert all(item["matched"] for item in evidence)
    assert all(item["strength"] == 100 for item in evidence)
    assert rejected is False
    assert [item["matched"] for item in rejected_evidence] == [True, False]


def test_configured_indicators_ignore_unavailable_observation_inputs() -> None:
    scan = {
        "items": [
            {
                "key": "moving_average_bull",
                "name": "均线多头",
                "bullish_triggered": True,
                "status": "triggered",
                "bullish_strength": 82,
            }
        ],
        "prediction_features": {
            "items": [
                {
                    "key": "prediction_trend",
                    "name": "周期趋势",
                    "direction": "bullish",
                    "status": "bullish",
                    "available": True,
                    "bullish_strength": 78,
                },
                {
                    "key": "prediction_aggressive_flow",
                    "name": "主动成交",
                    "direction": None,
                    "status": "insufficient",
                    "available": False,
                },
                {
                    "key": "prediction_velocity",
                    "name": "价格速度",
                    "direction": None,
                    "status": "insufficient",
                    "available": False,
                },
            ]
        },
    }

    matched, evidence = match_configured_indicators(
        scan,
        [
            "moving_average_bull",
            "prediction_trend",
            "prediction_aggressive_flow",
            "prediction_velocity",
        ],
    )
    policy = configured_indicator_policy(evidence)

    assert matched is True
    assert policy["passed_groups"] == ["trend"]
    assert policy["core_matched_count"] == 2
    assert policy["technical_score"] == 80
    assert [item["available"] for item in evidence[-2:]] == [False, False]


def test_configured_indicators_accept_one_coherent_setup_group() -> None:
    scan = {
        "items": [
            {
                "key": "trend_breakout",
                "name": "趋势突破",
                "bullish_triggered": True,
                "status": "triggered",
                "bullish_strength": 88,
            },
            {
                "key": "price_volume_rise",
                "name": "量价齐升",
                "bullish_triggered": True,
                "status": "triggered",
                "bullish_strength": 84,
            },
            {
                "key": "new_low_reversal",
                "name": "新低反转",
                "bullish_triggered": False,
                "status": "not_triggered",
                "bullish_strength": 30,
            },
            {
                "key": "oversold_bounce",
                "name": "超跌反弹",
                "bullish_triggered": False,
                "status": "not_triggered",
                "bullish_strength": 35,
            },
        ],
    }

    matched, evidence = match_configured_indicators(
        scan,
        [
            "trend_breakout",
            "price_volume_rise",
            "new_low_reversal",
            "oversold_bounce",
        ],
    )
    policy = configured_indicator_policy(evidence)

    assert matched is True
    assert policy["passed_groups"] == ["breakout"]
    assert policy["technical_score"] == 86


def test_live_rescoring_does_not_slide_signal_expiration() -> None:
    current = datetime(2026, 8, 12, 10, 0, tzinfo=UTC)
    proposed = datetime(2026, 8, 12, 11, 0, tzinfo=UTC)

    assert (
        merged_opportunity_expiration(
            current,
            proposed,
            has_prediction=False,
            has_new_material_news=False,
            newly_confirmed=False,
        )
        == current
    )
    assert (
        merged_opportunity_expiration(
            current,
            proposed,
            has_prediction=False,
            has_new_material_news=True,
            newly_confirmed=False,
        )
        == proposed
    )
    assert (
        merged_opportunity_expiration(
            current,
            proposed,
            has_prediction=True,
            has_new_material_news=True,
            newly_confirmed=False,
        )
        == current
    )


def test_live_score_history_seeds_legacy_snapshot_and_is_bounded() -> None:
    first = {
        "news": 70.0,
        "technical": 65.0,
        "market_flow": 50.0,
        "combined": 64.25,
        "calculated_at": "2026-08-12T10:00:00",
    }
    second = {**first, "combined": 68.5, "calculated_at": "2026-08-12T10:15:00"}

    history = append_score_history({"score_snapshot": first}, second)

    assert history == [first, second]
    frozen = {**first, "combined": 61.0, "calculated_at": "2026-08-12T09:45:00"}
    history = append_score_history(
        {"score_history": [second]},
        {**second, "combined": 69.0, "calculated_at": "2026-08-12T10:30:00"},
        seed_snapshots=[frozen],
    )
    assert [item["combined"] for item in history] == [61.0, 68.5, 69.0]
    for index in range(100):
        history = append_score_history(
            {"score_history": history},
            {**second, "combined": index, "calculated_at": f"point-{index}"},
        )
    assert len(history) == 96
    assert history[-1]["combined"] == 99


def test_market_flow_history_snapshot_keeps_trend_inputs_only() -> None:
    snapshot = market_flow_history_snapshot(
        {
            "score": 67,
            "main_force_ratio": 0.81,
            "active_buy_ratio": 0.63,
            "bid_depth_notional": 1_250_000,
            "ask_depth_notional": 980_000,
            "bid_depth_change_30s_pct": 12.5,
            "ask_depth_change_30s_pct": -4.5,
            "ignored_payload": "not persisted",
        }
    )

    assert snapshot == {
        "score": 67.0,
        "main_force_ratio": 0.81,
        "active_buy_ratio": 0.63,
        "bid_depth_notional": 1_250_000.0,
        "ask_depth_notional": 980_000.0,
        "bid_depth_change_30s_pct": 12.5,
        "ask_depth_change_30s_pct": -4.5,
    }


def test_pending_prediction_rescore_uses_its_frozen_timeframe_and_weights() -> None:
    now = datetime(2026, 8, 12, 8, 0, tzinfo=UTC)
    prediction = SimpleNamespace(
        symbol="TEST",
        contract_symbol="TESTUSDT",
        timeframe="4h",
        direction="long",
        evidence_json={
            "configured_indicator_keys": ["moving_average_bull", "trend_breakout"],
            "score_weights": {"news": 1.0, "technical": 0.0, "market_flow": 0.0},
            "live_readiness": {"minimum_combined_score": 88.0},
        },
    )

    class Repository:
        calls: list[tuple[str, str]] = []

        def strategy_indicators(self, symbol, timeframe):
            self.calls.append((symbol, timeframe))
            return {
                "items": [
                    {
                        "key": "moving_average_bull",
                        "available": True,
                        "status": "matched",
                        "bullish_triggered": True,
                        "bullish_strength": 80,
                    },
                    {
                        "key": "trend_breakout",
                        "available": True,
                        "status": "matched",
                        "bullish_triggered": True,
                        "bullish_strength": 70,
                    },
                ],
                "prediction_features": {"items": []},
            }

    repository = Repository()
    snapshot = prediction_live_score_snapshot(
        prediction,
        {"direction": "long", "news_score": 81.0},
        repository,
        {
            "ticker": {
                "TESTUSDT": {
                    "price": 123.5,
                    "ts": int(now.timestamp()),
                }
            }
        },
        now,
    )

    assert snapshot is not None
    assert repository.calls == [("TESTUSDT", "4h")]
    assert snapshot["combined"] == 81.0
    assert snapshot["strategy_identity"] == {
        "timeframe": "4h",
        "indicator_keys": ["moving_average_bull", "trend_breakout"],
        "score_weights": {"news": 1.0, "technical": 0.0, "market_flow": 0.0},
        "effective_score_weights": {"news": 1.0, "technical": 0.0, "market_flow": 0.0},
        "minimum_indicator_score": 65.0,
        "minimum_combined_score": 88.0,
    }
    assert snapshot["reference_price"] == 123.5
    assert snapshot["reference_price_time_ms"] == int(now.timestamp() * 1000)


def test_pending_rescore_aggregates_raw_news_with_frozen_threshold_and_side() -> None:
    now = datetime(2026, 8, 12, 8, 0, tzinfo=UTC)
    prediction = SimpleNamespace(
        id=7,
        user_id=3,
        symbol="TEST",
        contract_symbol="TESTUSDT",
        timeframe="1h",
        direction="long",
        signal_news_score=Decimal("85"),
        signal_indicator_score=Decimal("75"),
        confidence_score=Decimal("80"),
        predicted_at=now - timedelta(minutes=15),
        evidence_json={
            "minimum_news_score": 80.0,
            "minimum_news_mentions": 2,
            "configured_indicator_keys": ["moving_average_bull", "trend_breakout"],
            "score_weights": {"news": 1.0, "technical": 0.0, "market_flow": 0.0},
            "live_readiness": {
                "minimum_indicator_score": 65.0,
                "minimum_combined_score": 70.0,
            },
            "signal_scores": {"market_flow": 50.0},
        },
    )

    class Scalars:
        def all(self):
            return [prediction]

    class Database:
        statement = None

        def scalars(self, statement):
            self.statement = statement
            return Scalars()

    class Repository:
        def strategy_indicators(self, _symbol, timeframe):
            assert timeframe == "1h"
            return {
                "items": [
                    {
                        "key": "moving_average_bull",
                        "available": True,
                        "status": "matched",
                        "bearish_triggered": True,
                        "bearish_strength": 90,
                    },
                    {
                        "key": "trend_breakout",
                        "available": True,
                        "status": "matched",
                        "bearish_triggered": True,
                        "bearish_strength": 90,
                    },
                ],
                "prediction_features": {"items": []},
            }

    news_rows = [
        SimpleNamespace(
            id=str(index),
            ts=int(now.timestamp()) - index,
            source="test",
            title=f"short-{index}",
            title_zh=None,
            ai_reason="frozen policy candidate",
            ai_confidence=0.9,
            related_us_stocks=[{"symbol": "TEST", "direction": "short", "relevance": 1.0}],
        )
        for index in (1, 2)
    ]
    database = Database()

    updated = refresh_pending_prediction_scores(
        database,
        user_id=3,
        news_rows=news_rows,
        symbol_map={"TEST": "TESTUSDT"},
        repository=Repository(),
        market_flow_inputs={"ticker": {"TESTUSDT": {"price": 101.0, "ts": int(now.timestamp())}}},
        now=now,
    )

    assert updated == 1
    latest = prediction.evidence_json["latest_live_score"]
    assert latest["direction"] == "short"
    assert latest["combined"] == 90.0
    assert latest["entry_confirmed"] is True
    assert prediction.evidence_json["score_exit_policy"]["news_policy"] == {
        "minimum_confidence": 0.8,
        "minimum_mentions": 2,
        "lookback_hours": 24,
    }


def test_two_empty_frozen_news_scans_confirm_score_breakdown() -> None:
    start = datetime(2026, 8, 12, 8, 0, tzinfo=UTC)
    prediction = SimpleNamespace(
        id=8,
        user_id=3,
        symbol="TEST",
        contract_symbol="TESTUSDT",
        timeframe="1h",
        direction="long",
        signal_news_score=Decimal("90"),
        signal_indicator_score=Decimal("80"),
        confidence_score=Decimal("90"),
        predicted_at=start - timedelta(minutes=15),
        evidence_json={
            "minimum_news_score": 80.0,
            "minimum_news_mentions": 2,
            "news_lookback_hours": 1,
            "configured_indicator_keys": ["moving_average_bull", "trend_breakout"],
            "score_weights": {"news": 1.0, "technical": 0.0, "market_flow": 0.0},
            "live_readiness": {
                "minimum_indicator_score": 65.0,
                "minimum_combined_score": 70.0,
            },
            "signal_scores": {"market_flow": 50.0},
        },
    )

    class Scalars:
        def all(self):
            return [prediction]

    class Database:
        def scalars(self, _statement):
            return Scalars()

    class Repository:
        def strategy_indicators(self, _symbol, _timeframe):
            return {
                "items": [
                    {
                        "key": key,
                        "available": True,
                        "status": "matched",
                        "bullish_triggered": True,
                        "bullish_strength": 90,
                    }
                    for key in ("moving_average_bull", "trend_breakout")
                ],
                "prediction_features": {"items": []},
            }

    market_inputs = {"ticker": {"TESTUSDT": {"price": 101.0, "ts": int(start.timestamp())}}}
    expired_opposite_news = [
        SimpleNamespace(
            id=f"old-{index}",
            ts=int((start - timedelta(hours=2)).timestamp()) - index,
            source="test",
            title="expired opposite news",
            title_zh=None,
            ai_reason="outside frozen lookback",
            ai_confidence=0.99,
            related_us_stocks=[{"symbol": "TEST", "direction": "short", "relevance": 1.0}],
        )
        for index in (1, 2)
    ]
    for observed_at in (start, start + timedelta(minutes=15)):
        market_inputs["ticker"]["TESTUSDT"]["ts"] = int(observed_at.timestamp())
        assert (
            refresh_pending_prediction_scores(
                Database(),
                user_id=3,
                news_rows=expired_opposite_news,
                symbol_map={"TEST": "TESTUSDT"},
                repository=Repository(),
                market_flow_inputs=market_inputs,
                now=observed_at,
            )
            == 1
        )

    signal = prediction_score_exit_signal(
        prediction.evidence_json,
        "long",
        start_ms=int((start - timedelta(minutes=15)).timestamp() * 1000),
        end_ms=int((start + timedelta(minutes=15)).timestamp() * 1000),
    )
    assert signal is not None
    assert signal["reason"] == "score_breakdown"
    assert signal["confirmation_points"] == 2


def test_virtual_position_uses_directional_pnl_and_frozen_risk_levels() -> None:
    risk_plan = virtual_risk_plan_snapshot(
        entry_price=100,
        direction="long",
        timeframe="1h",
        atr_pct=2,
    )
    assert risk_plan["stop_loss_price"] == 97
    assert risk_plan["take_profit_price"] == 106
    assert risk_plan["risk_reward_ratio"] == 2

    prediction = SimpleNamespace(
        evidence_json={"risk_plan": risk_plan},
        entry_price=Decimal("100"),
        exit_price=None,
        status="pending",
        direction="long",
        timeframe="1h",
        estimated_cost_bps=Decimal("10"),
        due_at=datetime(2026, 8, 12, 12, 0),
    )
    snapshot = virtual_position_snapshot(
        prediction,
        {"price": 105, "ts": int(datetime(2026, 8, 12, 11, 30, tzinfo=UTC).timestamp())},
    )
    assert snapshot["available"] is True
    assert snapshot["gross_return_bps"] == 500
    assert snapshot["net_return_bps"] == 490
    assert snapshot["net_pnl_per_unit"] == 4.9
    assert snapshot["net_pnl_per_10000"] == 490
    assert snapshot["profit_state"] == "profit"

    short_prediction = SimpleNamespace(**{**prediction.__dict__, "direction": "short"})
    short_snapshot = virtual_position_snapshot(
        short_prediction,
        {"price": 95, "ts": int(datetime(2026, 8, 12, 11, 30, tzinfo=UTC).timestamp())},
    )
    assert short_snapshot["gross_return_bps"] == 500
    assert short_snapshot["net_pnl_per_unit"] == 4.9


def test_virtual_entry_gate_requires_every_signal_condition_and_a_real_price() -> None:
    ready = virtual_entry_gate_snapshot(
        direction="long",
        news_score=76.0,
        news_mention_count=2,
        minimum_news_score=60.0,
        minimum_news_mentions=1,
        indicator_policy_passed=True,
        indicator_score=72.0,
        minimum_indicator_score=65.0,
        combined_score=74.0,
        minimum_combined_score=70.0,
        market_flow_hard_conflict=False,
        entry_price=123.45,
        checked_at="2026-08-12T12:00:00+00:00",
    )

    assert ready["entry_ready"] is True
    assert ready["status"] == "ready"
    assert ready["real_order_enabled"] is False
    assert all(item["passed"] for item in ready["checks"])

    unavailable = virtual_entry_gate_snapshot(
        direction="short",
        news_score=76.0,
        news_mention_count=2,
        minimum_news_score=60.0,
        minimum_news_mentions=1,
        indicator_policy_passed=True,
        indicator_score=72.0,
        minimum_indicator_score=65.0,
        combined_score=74.0,
        minimum_combined_score=70.0,
        market_flow_hard_conflict=False,
        entry_price=0.0,
        checked_at="2026-08-12T12:00:00+00:00",
    )

    assert unavailable["signal_confirmed"] is True
    assert unavailable["entry_ready"] is False
    assert unavailable["status"] == "price_unavailable"
    assert unavailable["checks"][-1]["passed"] is False

    quality_blocked = virtual_entry_gate_snapshot(
        direction="long",
        news_score=80,
        news_mention_count=1,
        minimum_news_score=60,
        minimum_news_mentions=1,
        indicator_policy_passed=True,
        indicator_score=80,
        minimum_indicator_score=65,
        combined_score=80,
        minimum_combined_score=75,
        market_flow_hard_conflict=False,
        entry_price=100,
        checked_at="2026-08-12T12:00:00+00:00",
        has_new_trigger_news=False,
        require_new_trigger_news=True,
        market_quality_passed=False,
        require_market_quality=True,
    )
    assert quality_blocked["signal_confirmed"] is False
    assert quality_blocked["entry_ready"] is False
    assert {item["key"] for item in quality_blocked["checks"] if not item["passed"]} == {
        "new_news_trigger",
        "market_quality",
    }


def test_virtual_entry_gate_requires_order_book_direction_confirmation() -> None:
    blocked = virtual_entry_gate_snapshot(
        direction="long",
        news_score=80,
        news_mention_count=1,
        minimum_news_score=60,
        minimum_news_mentions=1,
        indicator_policy_passed=True,
        indicator_score=80,
        minimum_indicator_score=65,
        combined_score=80,
        minimum_combined_score=75,
        market_flow_hard_conflict=False,
        entry_price=100,
        checked_at="2026-08-26T15:00:00+00:00",
        order_book_gate={
            "quality_passed": True,
            "direction_clear": True,
            "confirms_direction": False,
            "directional_pressure": 0.05,
        },
    )

    assert blocked["signal_confirmed"] is False
    assert (
        next(item for item in blocked["checks"] if item["key"] == "order_book_direction")["passed"]
        is False
    )


def test_prediction_actionability_gate_blocks_non_actionable_correlated_entry() -> None:
    summary = prediction_actionability_gate_summary(
        {
            "status": "passed",
            "passed": True,
            "decision_checks": {},
            "blocking_reasons": [],
            "warnings": [],
        },
        market_quality={"passed": False},
        market_environment={
            "resonance": "divergent",
            "market_session": {
                "key": "postmarket",
                "allows_new_entries": False,
            },
        },
        news_trigger={
            "has_actionable_new_news": False,
            "event_cluster": {"selected": False},
        },
        order_book_gate={
            "quality_passed": True,
            "confirms_direction": False,
        },
        require_new_news=True,
    )

    assert summary["status"] == "blocked"
    assert summary["passed"] is False
    assert set(summary["blocking_reasons"]) == {
        "RAW_MARKET_QUALITY_BLOCKED",
        "NON_REGULAR_US_SESSION",
        "MACRO_DIRECTION_DIVERGENT",
        "NEWS_NOT_ACTIONABLE",
        "CORRELATED_EVENT_ALREADY_SELECTED",
        "ORDER_BOOK_DIRECTION_NOT_CONFIRMED",
    }


def test_closing_recap_news_is_evidence_but_not_a_new_catalyst() -> None:
    recap = news_actionability_snapshot(
        {"title": "美股收盘：AMD 收涨 5%，芯片股走强", "reason": "收盘复盘"}
    )
    catalyst = news_actionability_snapshot(
        {"title": "AMD 发布超预期财报并上调全年指引", "reason": "盈利预期上修"}
    )

    assert recap["actionable"] is False
    assert recap["reason_code"] == "CLOSING_RECAP_NOT_A_CATALYST"
    assert catalyst["actionable"] is True


def test_shared_news_event_only_selects_the_strongest_symbol() -> None:
    candidates = [
        {
            "symbol": "MSFT",
            "direction": "long",
            "news_score": 78,
            "news_trigger": {"actionable_new_news_ids": ["cloud-capex"]},
        },
        {
            "symbol": "META",
            "direction": "long",
            "news_score": 84,
            "news_trigger": {"actionable_new_news_ids": ["cloud-capex"]},
        },
        {
            "symbol": "AMZN",
            "direction": "short",
            "news_score": 75,
            "news_trigger": {"actionable_new_news_ids": ["cloud-capex"]},
        },
    ]

    annotated = annotate_event_cluster_selection(candidates)
    selected = {
        item["symbol"]: item["news_trigger"]["event_cluster"]["selected"] for item in annotated
    }

    assert selected == {"MSFT": False, "META": True, "AMZN": True}
    assert candidates[0]["news_trigger"]["event_cluster"]["selected_symbol"] == "META"


def test_configured_indicators_match_the_requested_short_direction() -> None:
    scan = {
        "items": [
            {
                "key": "moving_average_bull",
                "name": "均线多头",
                "bearish_name": "均线空头",
                "triggered": False,
                "bullish_triggered": False,
                "bearish_triggered": True,
                "status": "not_triggered",
                "bearish_status": "triggered",
                "summary": "多头未满足",
                "bearish_summary": "均线空头反向条件已满足。",
            }
        ],
        "prediction_features": {
            "items": [
                {
                    "key": "prediction_trend",
                    "name": "周期趋势",
                    "direction": "bearish",
                    "status": "bearish",
                    "summary": "偏空",
                }
            ]
        },
    }

    matched, evidence = match_configured_indicators(
        scan, ["moving_average_bull", "prediction_trend"], "short"
    )

    assert matched is True
    assert [item["name"] for item in evidence] == ["均线空头", "周期趋势"]
    assert all(item["direction"] == "short" for item in evidence)


def test_opportunity_scan_persists_candidate_direction_and_deduplicates_per_side() -> None:
    source = (ROOT / "src/quantdesk_v2/ai_monitor.py").read_text(encoding="utf-8")

    assert 'candidate["direction"],' in source
    assert source.count('direction=candidate["direction"]') == 2
    assert 'direction="long"' not in source
    assert '"short_candidate_count"' in source


def test_monitor_symbols_limit_news_candidates_by_contract() -> None:
    candidates = [
        {"symbol": "AAPL", "contract_symbol": "AAPLUSDT"},
        {"symbol": "NVDA", "contract_symbol": "NVDAUSDT"},
        {"symbol": "SHOP", "contract_symbol": ""},
    ]

    assert filter_monitored_candidates(candidates, []) == candidates[:2]
    assert filter_monitored_candidates(candidates, ["NVDAUSDT"]) == [candidates[1]]


def test_ai_monitor_indicator_templates_are_and_compatible() -> None:
    templates = indicator_templates()

    assert {item["key"] for item in templates} == {
        "trend_confirmation",
        "breakout_confirmation",
        "pullback_confirmation",
        "reversal_confirmation",
    }
    assert all(not indicator_conflicts(item["indicator_keys"]) for item in templates)
    conflicts = indicator_conflicts(["low_volume_pullback", "price_volume_rise"])
    assert [(item["left"], item["right"]) for item in conflicts] == [
        ("low_volume_pullback", "price_volume_rise")
    ]


def test_ai_monitor_prediction_outcome_is_direction_aware() -> None:
    long_result = prediction_outcome(100, 102, "long")
    short_result = prediction_outcome(100, 102, "short")

    assert long_result == {
        "raw_return_bps": 200.0,
        "directional_return_bps": 200.0,
        "result": "win",
    }
    assert short_result["directional_return_bps"] == -200.0
    assert short_result["result"] == "loss"


def test_ai_monitor_prediction_cost_and_path_metrics_are_direction_aware() -> None:
    predicted_at = datetime(2026, 8, 11, 8, 0)
    due_at = predicted_at + timedelta(hours=1)
    cost = prediction_estimated_cost_bps(predicted_at, due_at)
    net = prediction_net_outcome(20.0, cost)
    candles = [
        {
            "open_time": int(predicted_at.replace(tzinfo=UTC).timestamp() * 1_000),
            "open": 100,
            "high": 103,
            "low": 98,
            "close": 101,
        }
    ]

    assert cost == 16.125
    assert net == {
        "estimated_cost_bps": 16.125,
        "net_directional_return_bps": 3.875,
        "net_result": "win",
    }
    assert prediction_path_metrics(
        candles,
        100,
        "long",
        int(predicted_at.replace(tzinfo=UTC).timestamp() * 1_000),
        int(due_at.replace(tzinfo=UTC).timestamp() * 1_000),
    ) == {"max_favorable_bps": 300.0, "max_adverse_bps": -200.0}
    assert prediction_path_metrics(
        candles,
        100,
        "short",
        int(predicted_at.replace(tzinfo=UTC).timestamp() * 1_000),
        int(due_at.replace(tzinfo=UTC).timestamp() * 1_000),
    ) == {"max_favorable_bps": 200.0, "max_adverse_bps": -300.0}


def test_prediction_exit_uses_first_barrier_and_same_bar_is_conservative() -> None:
    start = 1_800_000_000_000
    risk_plan = {
        "stop_loss_price": 98,
        "take_profit_price": 104,
    }
    target = prediction_price_barrier_exit(
        [{"open_time": start, "open": 100, "high": 104.5, "low": 99}],
        100,
        "long",
        risk_plan,
        start,
        start + 900_000,
    )
    conflict = prediction_price_barrier_exit(
        [{"open_time": start, "open": 100, "high": 105, "low": 97}],
        100,
        "long",
        risk_plan,
        start,
        start + 900_000,
    )

    assert target is not None and target["reason"] == "take_profit"
    assert target["exit_subreason"] == "hard_target"
    assert target["price"] == 104
    assert conflict is not None and conflict["reason"] == "stop_loss"
    assert conflict["same_bar_conflict"] is True


def test_prediction_exit_uses_the_barrier_hit_candles_own_close_time() -> None:
    start = 1_800_000_000_000
    interval = 900_000
    result = prediction_price_barrier_exit(
        [
            {"open_time": start, "open": 100, "high": 104.5, "low": 99},
            {
                "open_time": start + interval,
                "open": 101,
                "high": 102,
                "low": 100,
            },
        ],
        100,
        "long",
        {"stop_loss_price": 98, "take_profit_price": 104},
        start,
        start + interval * 2,
        timeframe_ms=interval,
    )

    assert result is not None
    assert result["reason"] == "take_profit"
    assert result["price_time_ms"] == start + interval


def test_prediction_exit_ignores_a_candle_that_has_not_closed_at_boundary() -> None:
    start = 1_800_000_000_000
    interval = 900_000
    risk_plan = {"stop_loss_price": 98, "take_profit_price": 104}

    result = prediction_price_barrier_exit(
        [
            {"open_time": start, "open": 100, "high": 103, "low": 99},
            {
                "open_time": start + interval,
                "open": 100,
                "high": 105,
                "low": 99,
            },
        ],
        100,
        "long",
        risk_plan,
        start,
        start + interval,
        timeframe_ms=interval,
    )

    assert result is None


def test_adaptive_exit_activates_profit_lock_only_on_the_next_closed_bar() -> None:
    start = 1_800_000_000_000
    interval = 900_000
    result = prediction_adaptive_path_exit(
        [
            {
                "open_time": start,
                "open": 100,
                "high": 100.4,
                "low": 99.9,
                "close": 100.3,
            },
            {
                "open_time": start + interval,
                "open": 100.3,
                "high": 100.35,
                "low": 100.15,
                "close": 100.2,
            },
        ],
        100,
        "long",
        start,
        start + interval * 2,
        estimated_cost_bps=16,
    )

    assert result is not None
    assert result["reason"] == "take_profit"
    assert result["exit_subreason"] == "profit_lock"
    assert result["price"] == pytest.approx(100.2)
    assert result["price_time_ms"] == start + interval * 2


def test_new_risk_plan_uses_r_normalized_profit_protection() -> None:
    plan = virtual_risk_plan_snapshot(
        entry_price=100,
        direction="long",
        timeframe="1h",
        atr_pct=1,
    )

    assert plan["version"] == "atr_risk_reward_guard_v6"
    assert plan["settlement_version"] == "cost_consistent_exit_v8"
    assert plan["execution_policy"] == "cost_consistent_risk_guard_v9"
    assert plan["stop_loss_pct"] == 1.5
    assert plan["take_profit_pct"] == 3.0
    assert plan["profit_protection"] == {
        "version": "risk_unit_profit_guard_v2",
        "mode": "risk_unit",
        "risk_bps": 150.0,
        "activation_r": 0.5,
        "minimum_net_protected_r": 0.25,
        "early_maximum_giveback_r": 0.35,
        "trailing_activation_r": 1.0,
        "maximum_giveback_r": 0.5,
        "activation_boundary": "prior_closed_15m_bar",
        "minimum_hold_policy": "immediate_after_activation",
    }
    assert plan["failed_follow_through"]["maximum_favorable_r"] == 0.5
    assert plan["failed_follow_through"]["maximum_favorable_bps"] == 75
    assert plan["failed_follow_through"]["maximum_adverse_r"] == 0.2
    assert plan["failed_follow_through"]["minimum_adverse_bps"] == 15
    assert plan["failed_follow_through"]["confirmation_closes"] == 2
    assert plan["failed_follow_through"]["minimum_hold_policy"] == "horizon_aligned"


def test_risk_unit_profit_guard_is_not_delayed_by_signal_soft_exit_window() -> None:
    start = 1_800_000_000_000
    interval = 900_000
    result = prediction_adaptive_path_exit(
        [
            {
                "open_time": start,
                "open": 100,
                "high": 100.7,
                "low": 99.9,
                "close": 100.6,
            },
            {
                "open_time": start + interval,
                "open": 100.6,
                "high": 100.65,
                "low": 100.1,
                "close": 100.2,
            },
        ],
        100,
        "long",
        start,
        start + interval * 2,
        estimated_cost_bps=16,
        minimum_soft_exit_ms=2 * 60 * 60 * 1000,
        minimum_profit_protection_ms=0,
        minimum_failed_follow_through_ms=2 * 60 * 60 * 1000,
        profit_protection={
            "mode": "risk_unit",
            "risk_bps": 100,
            "activation_r": 0.5,
            "minimum_protected_r": 0,
            "trailing_activation_r": 1,
            "maximum_giveback_r": 0.5,
        },
    )

    assert result is not None
    assert result["reason"] == "take_profit"
    assert result["exit_subreason"] == "profit_lock"
    assert result["protection_mode"] == "risk_unit"
    assert result["activation_bps"] == 50
    assert result["price"] == pytest.approx(100.18)
    assert result["price_time_ms"] == start + interval * 2


def test_v7_profit_lock_guarantees_quarter_r_after_cost_before_arming() -> None:
    start = 1_800_000_000_000
    interval = 900_000
    result = prediction_adaptive_path_exit(
        [
            {
                "open_time": start,
                "open": 100,
                "high": 100.30,
                "low": 99.95,
                "close": 100.29,
            },
            {
                "open_time": start + interval,
                "open": 100.29,
                "high": 100.31,
                "low": 100.27,
                "close": 100.28,
            },
        ],
        100,
        "long",
        start,
        start + interval * 2,
        estimated_cost_bps=16,
        minimum_profit_protection_ms=0,
        profit_protection={
            "mode": "risk_unit",
            "risk_bps": 50,
            "activation_r": 0.5,
            "minimum_net_protected_r": 0.25,
            "early_maximum_giveback_r": 0.35,
            "trailing_activation_r": 1,
            "maximum_giveback_r": 0.5,
        },
    )

    assert result is not None
    assert result["exit_subreason"] == "profit_lock"
    assert result["effective_activation_bps"] == pytest.approx(28.5)
    assert result["protected_bps"] == pytest.approx(28.5)
    assert result["protected_bps"] - 16 == pytest.approx(12.5)


def test_v7_profit_protection_floor_is_monotonic_across_r_regimes() -> None:
    start = 1_800_000_000_000
    interval = 900_000
    result = prediction_adaptive_path_exit(
        [
            {
                "open_time": start,
                "open": 100,
                "high": 100.8,
                "low": 99.9,
                "close": 100.7,
            },
            {
                "open_time": start + interval,
                "open": 100.7,
                "high": 101.1,
                "low": 100.65,
                "close": 101.0,
            },
            {
                "open_time": start + interval * 2,
                "open": 101.0,
                "high": 101.05,
                "low": 100.5,
                "close": 100.7,
            },
        ],
        100,
        "long",
        start,
        start + interval * 3,
        estimated_cost_bps=16,
        minimum_profit_protection_ms=0,
        profit_protection={
            "mode": "risk_unit",
            "risk_bps": 100,
            "activation_r": 0.5,
            "minimum_net_protected_r": 0.25,
            "early_maximum_giveback_r": 0.35,
            "trailing_activation_r": 1,
            "maximum_giveback_r": 0.5,
        },
    )

    assert result is not None
    assert result["exit_subreason"] == "trailing_profit"
    assert result["peak_favorable_bps"] == pytest.approx(110)
    assert result["protected_bps"] == pytest.approx(60)


def test_risk_unit_guard_keeps_failed_follow_through_horizon_aligned() -> None:
    start = 1_800_000_000_000
    interval = 900_000
    result = prediction_adaptive_path_exit(
        [
            {
                "open_time": start + index * interval,
                "open": 100 - index * 0.05,
                "high": 100.1,
                "low": 99.7 - index * 0.1,
                "close": 99.9 - index * 0.1,
            }
            for index in range(3)
        ],
        100,
        "long",
        start,
        start + interval * 3,
        minimum_profit_protection_ms=0,
        minimum_failed_follow_through_ms=2 * 60 * 60 * 1000,
        profit_protection={
            "mode": "risk_unit",
            "risk_bps": 100,
        },
    )

    assert result is None


def test_adaptive_exit_cuts_failed_follow_through_after_three_closed_bars() -> None:
    start = 1_800_000_000_000
    interval = 900_000
    result = prediction_adaptive_path_exit(
        [
            {
                "open_time": start + index * interval,
                "open": 100 - index * 0.05,
                "high": 100.1,
                "low": 99.7 - index * 0.1,
                "close": 99.9 - index * 0.1,
            }
            for index in range(3)
        ],
        100,
        "long",
        start,
        start + interval * 3,
    )

    assert result is not None
    assert result["reason"] == "score_breakdown"
    assert result["exit_subreason"] == "failed_follow_through"
    assert result["observed_bar_count"] == 3
    assert result["price_time_ms"] == start + interval * 3


def test_v8_failed_follow_through_uses_r_threshold_and_two_closed_bars() -> None:
    start = 1_800_000_000_000
    interval = 900_000
    candles = [
        {
            "open_time": start + index * interval,
            "open": 100,
            "high": 100.1,
            "low": 99.6,
            "close": close,
        }
        for index, close in enumerate((99.95, 99.75, 99.74, 99.70))
    ]
    failed_policy = {
        "closed_bars": 3,
        "maximum_adverse_r": 0.2,
        "minimum_adverse_bps": 15,
        "confirmation_closes": 2,
        "confirmation_unit": "closed_15m_bar",
    }
    first_confirmation_only = prediction_adaptive_path_exit(
        candles[:3],
        100,
        "long",
        start,
        start + interval * 3,
        profit_protection={"mode": "risk_unit", "risk_bps": 100},
        failed_follow_through=failed_policy,
    )
    result = prediction_adaptive_path_exit(
        candles,
        100,
        "long",
        start,
        start + interval * 4,
        profit_protection={"mode": "risk_unit", "risk_bps": 100},
        failed_follow_through=failed_policy,
    )

    assert first_confirmation_only is None
    assert result is not None
    assert result["exit_subreason"] == "failed_follow_through"
    assert result["failed_threshold_mode"] == "risk_unit"
    assert result["failed_loss_threshold_bps"] == -20
    assert result["confirmation_points"] == 2
    assert result["price_time_ms"] == start + interval * 4


def test_gap_loss_is_not_classified_as_successful_profit_protection() -> None:
    assert (
        settlement_exit_subreason(
            {
                "reason": "take_profit",
                "exit_subreason": "profit_lock",
                "gap_execution": True,
            },
            net_result="loss",
        )
        == "profit_lock_gap_loss"
    )
    assert (
        settlement_exit_subreason(
            {
                "reason": "take_profit",
                "exit_subreason": "profit_lock",
                "gap_execution": False,
            },
            net_result="win",
        )
        == "profit_lock"
    )


def test_settlement_keeps_stop_loss_when_profit_guard_ties_on_same_bar() -> None:
    adaptive = {
        "reason": "take_profit",
        "exit_subreason": "profit_lock",
        "price": 100.2,
        "price_time_ms": 1_800_001_800_000,
    }
    barrier = {
        "reason": "stop_loss",
        "price": 98.0,
        "price_time_ms": 1_800_001_800_000,
    }

    assert adaptive_exit_precedes(barrier, adaptive) is False
    assert adaptive_exit_precedes({**barrier, "reason": "take_profit"}, adaptive) is True
    assert adaptive_exit_precedes(None, adaptive) is True


def test_score_exit_price_is_never_before_the_score_signal() -> None:
    signal_ms = 1_800_000_600_000
    frozen = prediction_score_exit_price(
        [],
        {
            "price_time_ms": signal_ms,
            "reference_price": 101.5,
            "reference_price_time_ms": signal_ms - 10_000,
        },
        end_ms=signal_ms,
    )
    legacy = prediction_score_exit_price(
        [
            {
                "open_time": signal_ms - 900_000,
                "open": 98,
                "close": 99,
            },
            {"open_time": signal_ms + 300_000, "open": 102, "close": 103},
        ],
        {"price_time_ms": signal_ms},
        end_ms=signal_ms + 900_000,
    )

    assert frozen == {
        "price": 101.5,
        "price_time_ms": signal_ms,
        "price_source": "frozen_score_reference",
        "reference_price_time_ms": signal_ms - 10_000,
    }
    assert legacy is not None
    assert legacy["price"] == 102
    assert legacy["price_time_ms"] == signal_ms + 300_000
    assert legacy["price_time_ms"] >= signal_ms


def test_hard_cap_uses_first_executable_open_at_or_after_cap() -> None:
    cap_ms = 1_800_000_900_000
    interval = 900_000

    result = historical_closed_settlement_price(
        [
            {
                "open_time": cap_ms - interval,
                "open": 100,
                "close": 101,
            },
            {"open_time": cap_ms, "open": 102, "close": 999},
            {"open_time": cap_ms + interval, "open": 103, "close": 104},
        ],
        cap_ms,
        timeframe_ms=interval,
        not_before_ms=cap_ms - interval // 2,
    )

    assert result == {
        "price": 102.0,
        "price_time_ms": cap_ms,
        "price_source": "first_executable_open_at_or_after_cap",
    }


def test_hard_cap_never_reuses_a_pre_entry_candle() -> None:
    cap_ms = 1_800_000_900_000
    interval = 900_000

    result = historical_closed_settlement_price(
        [
            {
                "open_time": cap_ms - interval,
                "open": 100,
                "close": 101,
            }
        ],
        cap_ms,
        timeframe_ms=interval,
        not_before_ms=cap_ms - interval // 2,
    )

    assert result is None


def test_prediction_score_exit_requires_distinct_closed_bars_for_breakdown_and_reversal() -> None:
    start = datetime(2026, 8, 12, 8, 0, tzinfo=UTC)
    evidence = {
        "live_readiness": {"minimum_combined_score": 70},
        "live_score_history": [
            {"calculated_at": start.isoformat(), "combined": 72, "direction": "long"},
            {
                "calculated_at": (start + timedelta(minutes=15)).isoformat(),
                "combined": 64,
                "direction": "long",
            },
            {
                "calculated_at": (start + timedelta(minutes=30)).isoformat(),
                "combined": 63,
                "direction": "long",
            },
        ],
    }
    result = prediction_score_exit_signal(
        evidence,
        "long",
        start_ms=int(start.timestamp() * 1000),
        end_ms=int((start + timedelta(minutes=30)).timestamp() * 1000),
    )
    reversal = prediction_score_exit_signal(
        {
            "live_score_history": [
                {
                    "calculated_at": (start + timedelta(minutes=15)).isoformat(),
                    "combined": 80,
                    "direction": "short",
                },
                {
                    "calculated_at": (start + timedelta(minutes=30)).isoformat(),
                    "combined": 80,
                    "direction": "short",
                },
            ]
        },
        "long",
        start_ms=int(start.timestamp() * 1000),
        end_ms=int((start + timedelta(minutes=30)).timestamp() * 1000),
    )

    assert result is not None and result["reason"] == "score_breakdown"
    assert result["confirmation_points"] == 2
    assert result["confirmation_unit"] == "closed_15m_bar"
    assert result["confirmation_scores"] == [64.0, 63.0]
    assert result["confirmation_bar_times_ms"] == [
        int((start + timedelta(minutes=15)).timestamp() * 1000),
        int((start + timedelta(minutes=30)).timestamp() * 1000),
    ]
    assert reversal is not None and reversal["reason"] == "score_reversal"
    assert reversal["confirmation_points"] == 2

    recovered_later = prediction_score_exit_signal(
        {
            "live_readiness": {"minimum_combined_score": 70},
            "live_score_history": [
                {
                    "calculated_at": (start + timedelta(minutes=15)).isoformat(),
                    "combined": 64,
                    "direction": "long",
                },
                {
                    "calculated_at": (start + timedelta(minutes=30)).isoformat(),
                    "combined": 63,
                    "direction": "long",
                },
                {
                    "calculated_at": (start + timedelta(minutes=45)).isoformat(),
                    "combined": 80,
                    "direction": "long",
                },
            ],
        },
        "long",
        start_ms=int(start.timestamp() * 1000),
        end_ms=int((start + timedelta(minutes=45)).timestamp() * 1000),
    )
    assert recovered_later is not None
    assert recovered_later["reason"] == "score_breakdown"
    assert recovered_later["price_time_ms"] == int(
        (start + timedelta(minutes=30)).timestamp() * 1000
    )


def test_prediction_score_exit_ignores_repeated_scans_in_one_closed_bar() -> None:
    start = datetime(2026, 8, 12, 8, 0, tzinfo=UTC)
    evidence = {
        "live_readiness": {"minimum_combined_score": 70},
        "live_score_history": [
            {
                "calculated_at": (start + timedelta(minutes=30, seconds=5)).isoformat(),
                "combined": 61.4318,
                "direction": "long",
            },
            {
                "calculated_at": (start + timedelta(minutes=30, seconds=25)).isoformat(),
                "combined": 61.4318,
                "direction": "long",
            },
            {
                "calculated_at": (start + timedelta(minutes=31)).isoformat(),
                "combined": 60.0,
                "direction": "long",
            },
        ],
    }

    result = prediction_score_exit_signal(
        evidence,
        "long",
        start_ms=int(start.timestamp() * 1000),
        end_ms=int((start + timedelta(minutes=45)).timestamp() * 1000),
    )

    assert result is None


def test_prediction_score_exit_waits_for_minimum_hold_after_two_closed_bars() -> None:
    start = datetime(2026, 8, 12, 8, 7, tzinfo=UTC)
    observations = [
        {
            "calculated_at": (start + timedelta(minutes=8)).isoformat(),
            "combined": 64,
            "direction": "long",
        },
        {
            "calculated_at": (start + timedelta(minutes=23)).isoformat(),
            "combined": 63,
            "direction": "long",
        },
    ]
    before_warmup = prediction_score_exit_signal(
        {
            "live_readiness": {"minimum_combined_score": 70},
            "live_score_history": observations,
        },
        "long",
        start_ms=int(start.timestamp() * 1000),
        end_ms=int((start + timedelta(minutes=24)).timestamp() * 1000),
    )
    after_warmup = prediction_score_exit_signal(
        {
            "live_readiness": {"minimum_combined_score": 70},
            "live_score_history": [
                *observations,
                {
                    "calculated_at": (start + timedelta(minutes=31)).isoformat(),
                    "combined": 62,
                    "direction": "long",
                },
            ],
        },
        "long",
        start_ms=int(start.timestamp() * 1000),
        end_ms=int((start + timedelta(minutes=31)).timestamp() * 1000),
    )

    assert before_warmup is None
    assert after_warmup is not None
    assert after_warmup["reason"] == "score_breakdown"
    assert after_warmup["price_time_ms"] == int((start + timedelta(minutes=31)).timestamp() * 1000)


def test_hourly_soft_exit_policy_waits_for_half_of_four_hour_horizon() -> None:
    start = datetime(2026, 8, 21, 8, 0, tzinfo=UTC)
    start_ms = int(start.timestamp() * 1000)
    due_ms = int((start + timedelta(hours=4)).timestamp() * 1000)
    policy = prediction_soft_exit_policy("1h", start_ms=start_ms, due_ms=due_ms)

    assert policy["bar_ms"] == 60 * 60 * 1000
    assert policy["minimum_hold_ms"] == 2 * 60 * 60 * 1000
    assert policy["confirmation_unit"] == "closed_1h_bar"

    evidence = {
        "live_readiness": {"minimum_combined_score": 70},
        "live_score_history": [
            {
                "calculated_at": (start + timedelta(hours=1)).isoformat(),
                "combined": 64,
                "direction": "short",
            },
            {
                "calculated_at": (start + timedelta(hours=2)).isoformat(),
                "combined": 63,
                "direction": "short",
            },
        ],
    }
    before_minimum = prediction_score_exit_signal(
        evidence,
        "long",
        start_ms=start_ms,
        end_ms=int((start + timedelta(hours=1)).timestamp() * 1000),
        confirmation_bar_ms=policy["bar_ms"],
        minimum_hold_ms=policy["minimum_hold_ms"],
        confirmation_bars=policy["confirmation_bars"],
        confirmation_unit=policy["confirmation_unit"],
    )
    confirmed = prediction_score_exit_signal(
        evidence,
        "long",
        start_ms=start_ms,
        end_ms=int((start + timedelta(hours=2)).timestamp() * 1000),
        confirmation_bar_ms=policy["bar_ms"],
        minimum_hold_ms=policy["minimum_hold_ms"],
        confirmation_bars=policy["confirmation_bars"],
        confirmation_unit=policy["confirmation_unit"],
    )

    assert before_minimum is None
    assert confirmed is not None
    assert confirmed["reason"] == "score_reversal"
    assert confirmed["confirmation_points"] == 2
    assert confirmed["minimum_hold_ms"] == 2 * 60 * 60 * 1000


def test_ai_monitor_prediction_cost_components_are_optional_and_configurable() -> None:
    predicted_at = datetime(2026, 8, 11, 8, 0)
    due_at = predicted_at + timedelta(hours=4)
    config = {
        "prediction_fee_enabled": False,
        "prediction_fee_bps_per_side": 9,
        "prediction_slippage_enabled": True,
        "prediction_slippage_bps_per_side": 2.5,
        "prediction_funding_enabled": True,
        "prediction_funding_bps_per_8h": 4,
    }

    breakdown = prediction_cost_breakdown(predicted_at, due_at, config)

    assert breakdown["fee_cost_bps"] == 0
    assert breakdown["slippage_cost_bps"] == 5
    assert breakdown["funding_cost_bps"] == 2
    assert breakdown["total_cost_bps"] == 7
    assert prediction_estimated_cost_bps(predicted_at, due_at, config) == 7


def test_current_settlement_policy_cannot_disable_execution_costs() -> None:
    disabled = {
        "fee_enabled": False,
        "fee_bps_per_side": 0,
        "slippage_enabled": False,
        "slippage_bps_per_side": 0,
        "funding_enabled": False,
        "funding_bps_per_8h": 0,
    }

    previous = prediction_settlement_cost_config(
        disabled,
        settlement_version="cost_consistent_exit_v7",
    )
    current = prediction_settlement_cost_config(
        disabled,
        settlement_version="cost_consistent_exit_v8",
    )
    legacy = prediction_settlement_cost_config(
        disabled,
        settlement_version="horizon_aligned_exit_v5",
    )

    assert current["prediction_fee_enabled"] is True
    assert current["prediction_fee_bps_per_side"] == 5
    assert current["prediction_slippage_enabled"] is True
    assert current["prediction_slippage_bps_per_side"] == 3
    assert current["prediction_funding_enabled"] is True
    assert current["prediction_funding_bps_per_8h"] == 1
    assert previous == current
    assert legacy["prediction_fee_enabled"] is False
    assert legacy["prediction_slippage_enabled"] is False
    assert legacy["prediction_funding_enabled"] is False


def test_historical_opportunity_analytics_uses_expiry_price_and_summarizes_hits() -> None:
    expiry = 1_800_000_000_000
    settlement = historical_settlement_price(
        [
            {"open_time": expiry - 900_000, "open": 99, "close": 100},
            {"open_time": expiry, "open": 101, "close": 102},
        ],
        expiry,
    )

    assert settlement == {"price": 101.0, "price_time_ms": expiry}
    assert (
        historical_settlement_price(
            [{"open_time": expiry - 4_000_000, "open": 99, "close": 100}],
            expiry,
        )
        is None
    )

    summary = summarize_historical_opportunities(
        [
            {
                "result": "win",
                "direction": "long",
                "directional_return_bps": 100,
                "technical_confirmed": True,
            },
            {
                "result": "loss",
                "direction": "short",
                "directional_return_bps": -50,
                "technical_confirmed": False,
            },
            {
                "result": "flat",
                "direction": "long",
                "directional_return_bps": 0,
                "technical_confirmed": False,
            },
            {
                "result": "unavailable",
                "direction": "short",
                "directional_return_bps": None,
                "technical_confirmed": False,
            },
        ]
    )

    assert summary["historical_count"] == 4
    assert summary["settled_count"] == 3
    assert summary["win_count"] == 1
    assert summary["hit_rate"] == 50.0
    assert summary["coverage_rate"] == 75.0
    assert summary["average_directional_return_bps"] == 16.6667
    assert summary["long_count"] == 2
    assert summary["short_count"] == 2


def test_historical_statistics_exclude_insufficient_expiry_market_data() -> None:
    outcomes = settleable_historical_outcomes(
        [
            {"id": "settled", "result": "win"},
            {"id": "missing-market", "result": "unavailable"},
        ]
    )

    assert outcomes == [{"id": "settled", "result": "win"}]


def test_due_predictions_use_historical_due_price_without_ticker_fallback() -> None:
    due_at = datetime.now(UTC).replace(tzinfo=None) - timedelta(minutes=1)
    old_due_at = datetime.now(UTC).replace(tzinfo=None) - timedelta(hours=7)
    due_ms = int(due_at.replace(tzinfo=UTC).timestamp() * 1_000)
    settled = SimpleNamespace(
        id=1,
        contract_symbol="AAPLUSDT",
        due_at=due_at,
        entry_price=Decimal("100"),
        direction="long",
        status="pending",
        result=None,
        exit_price=None,
        raw_return_bps=None,
        directional_return_bps=None,
        completed_at=None,
    )
    missing = SimpleNamespace(
        id=2,
        contract_symbol="MSFTUSDT",
        due_at=due_at,
        entry_price=Decimal("100"),
        direction="long",
        status="pending",
        result=None,
        exit_price=None,
        raw_return_bps=None,
        directional_return_bps=None,
        completed_at=None,
    )
    retry = SimpleNamespace(
        id=3,
        contract_symbol="TSLAUSDT",
        due_at=due_at,
        entry_price=Decimal("100"),
        direction="short",
        status="unavailable",
        result=None,
        exit_price=None,
        raw_return_bps=None,
        directional_return_bps=None,
        completed_at=due_at,
    )
    expired_missing = SimpleNamespace(
        id=4,
        contract_symbol="GOOGLUSDT",
        due_at=old_due_at,
        entry_price=Decimal("100"),
        direction="long",
        status="pending",
        result=None,
        exit_price=None,
        raw_return_bps=None,
        directional_return_bps=None,
        completed_at=None,
    )

    class Scalars:
        def all(self):
            return [settled, missing, retry, expired_missing]

    class Database:
        flushed = False
        statement = None

        def scalars(self, statement):
            self.statement = statement
            return Scalars()

        def flush(self):
            self.flushed = True

    class Repository:
        calls: list[tuple[str, str, int, int]] = []

        def kline_range(self, symbol, timeframe, start_ms, end_ms):
            self.calls.append((symbol, timeframe, start_ms, end_ms))
            if symbol in {"AAPLUSDT", "TSLAUSDT"}:
                return [
                    {
                        "open_time": due_ms,
                        "open": 102,
                        "close": 102,
                    }
                ]
            return []

        def _query(self, *_args, **_kwargs):
            raise AssertionError("current ticker must not be used for due settlement")

    database = Database()
    repository = Repository()

    result = settle_due_predictions(database, repository)
    mysql_compiled = database.statement.compile(dialect=mysql.dialect())
    mysql_sql = str(mysql_compiled)
    sqlite_sql = str(database.statement.compile(dialect=sqlite.dialect()))

    assert result == {
        "completed": 2,
        "recovered": 1,
        "deferred": 1,
        "unavailable": 1,
        "take_profit": 0,
        "stop_loss": 0,
        "score_exit": 0,
        "max_holding": 2,
        "profit_protection": 0,
        "failed_follow_through": 0,
    }
    assert settled.status == "completed"
    assert settled.exit_price == Decimal("102.0")
    assert settled.result == "win"
    assert settled.net_result == "win"
    assert float(settled.net_directional_return_bps) == pytest.approx(183.96875)
    assert settled.max_favorable_bps == Decimal("200.0")
    assert settled.max_adverse_bps == Decimal("0.0")
    assert settled.settlement_version == "cost_consistent_exit_v7"
    assert (
        settled.evidence_json["settlement"]["score_exit_policy_version"]
        == "horizon_aligned_closed_bar_v3"
    )
    assert settled.exit_reason == "max_holding_time"
    assert settled.exit_at.timestamp() == pytest.approx(due_at.timestamp(), abs=0.001)
    assert missing.status == "pending"
    assert missing.exit_price is None
    assert retry.status == "completed"
    assert retry.exit_price == Decimal("102.0")
    assert retry.result == "loss"
    assert expired_missing.status == "unavailable"
    assert database.flushed is True
    assert "FOR UPDATE SKIP LOCKED" in mysql_sql
    assert "FOR UPDATE" not in sqlite_sql
    assert mysql_sql.count("ai_monitor_predictions.status = %s") == 2
    assert (
        "ai_monitor_predictions.status = %s OR "
        "ai_monitor_predictions.status = %s AND "
        "ai_monitor_predictions.updated_at <= %s"
    ) in mysql_sql
    assert {"pending", "unavailable"}.issubset(set(mysql_compiled.params.values()))
    assert database.statement.get_execution_options()["populate_existing"] is True
    assert {call[0] for call in repository.calls} == {
        "AAPLUSDT",
        "MSFTUSDT",
        "TSLAUSDT",
        "GOOGLUSDT",
    }


def test_ai_monitor_config_normalizes_symbol_allowlist() -> None:
    config = AiMonitorConfigUpdate(monitor_symbols=[" nvdausdt ", "AAPLUSDT", "NVDAUSDT"])

    assert config.monitor_symbols == ["NVDAUSDT", "AAPLUSDT"]
    assert config.minimum_indicator_score == 65
    assert config.minimum_combined_score == 75
    assert config.news_lookback_hours == 168
    assert config.prediction_max_holding_bars == 4
    assert config.minimum_calibration_samples == 1000
    assert config.news_score_weight == 45
    assert config.technical_score_weight == 35
    assert config.market_flow_score_weight == 20

    with pytest.raises(ValueError):
        AiMonitorConfigUpdate(prediction_max_holding_bars=0)

    costs = AiMonitorCostConfigUpdate(
        prediction_fee_enabled=False,
        prediction_fee_bps_per_side=4.5,
        prediction_slippage_enabled=True,
        prediction_slippage_bps_per_side=2,
        prediction_funding_enabled=False,
        prediction_funding_bps_per_8h=0.8,
    )
    assert costs.prediction_fee_enabled is False
    assert costs.prediction_slippage_bps_per_side == 2


def test_ai_monitor_score_weights_are_validated_and_applied() -> None:
    config = AiMonitorConfigUpdate(
        news_score_weight=20,
        technical_score_weight=30,
        market_flow_score_weight=50,
    )

    assert opportunity_score_weights(config.model_dump()) == {
        "news": 0.2,
        "technical": 0.3,
        "market_flow": 0.5,
    }
    assert weighted_opportunity_score(80, 60, 40, config.model_dump()) == 54

    missing_flow = {
        "directional_data_available": False,
        "fresh": False,
        "data_quality": 0,
    }
    assert effective_opportunity_score_weights(config.model_dump(), missing_flow) == {
        "news": 0.4,
        "technical": 0.6,
        "market_flow": 0.0,
    }
    assert weighted_opportunity_score(80, 60, 50, config.model_dump(), missing_flow) == 68

    with pytest.raises(ValueError, match="权重合计必须为 100%"):
        AiMonitorConfigUpdate(
            news_score_weight=50,
            technical_score_weight=40,
            market_flow_score_weight=20,
        )
    rounded = AiMonitorConfigUpdate(
        news_score_weight=45.004,
        technical_score_weight=34.996,
        market_flow_score_weight=20,
    )
    assert (
        rounded.news_score_weight,
        rounded.technical_score_weight,
        rounded.market_flow_score_weight,
    ) == (45.0, 35.0, 20.0)
    with pytest.raises(ValueError, match="权重合计必须为 100%"):
        AiMonitorConfigUpdate(
            news_score_weight=33.334,
            technical_score_weight=33.333,
            market_flow_score_weight=33.333,
        )


def test_model_call_audit_index_keeps_only_current_tenant_batch_links() -> None:
    class Database:
        def __init__(self) -> None:
            self.statement = None

        def execute(self, statement):
            self.statement = statement
            return SimpleNamespace(
                all=lambda: [
                    ("news-1", 11, "batch-current"),
                    ("news-1", 12, "batch-old"),
                ]
            )

    database = Database()
    result = _news_model_call_audit_index(
        database,
        7,
        [SimpleNamespace(id="news-1", ai_batch_id="batch-current")],
    )
    sql = str(database.statement.compile(dialect=mysql.dialect()))

    assert result == {"news-1": {"batch_id": "batch-current", "call_ids": [11]}}
    assert "news_ai_batches.started_by" in sql
    assert "news_ai_model_calls.batch_id IN" in sql
    assert "news_ai_model_call_items.news_id IN" in sql


def test_live_readiness_requires_samples_quality_and_cost_stress_edge() -> None:
    calibration = edge_calibration_summary([40.0] * 30, 30)
    readiness = signal_readiness_snapshot(
        matched=True,
        indicator_score=82,
        combined_score=78,
        estimated_cost_bps=16,
        market_quality={"passed": True},
        calibration=calibration,
        minimum_indicator_score=65,
        minimum_combined_score=70,
        safety_margin_bps=10,
        market_flow={
            "directional_data_available": True,
            "fresh": True,
            "data_quality": 0.8,
        },
        market_flow_weight=0.2,
        minimum_market_flow_quality=0.5,
    )

    assert calibration["lower_bound_bps"] == 40.0
    assert readiness["status"] == "shadow_ready"
    assert all(readiness["checks"].values())
    blocked = signal_readiness_snapshot(
        matched=True,
        indicator_score=82,
        combined_score=78,
        estimated_cost_bps=16,
        market_quality={"passed": False},
        calibration=edge_calibration_summary([40.0] * 29, 30),
        minimum_indicator_score=65,
        minimum_combined_score=70,
        safety_margin_bps=10,
        market_flow={
            "directional_data_available": True,
            "fresh": True,
            "data_quality": 0.8,
        },
        market_flow_weight=0.2,
        minimum_market_flow_quality=0.5,
    )
    assert blocked["status"] == "research_only"
    assert "历史校准样本不足" in blocked["failed_reasons"]

    missing_flow = signal_readiness_snapshot(
        matched=True,
        indicator_score=82,
        combined_score=78,
        estimated_cost_bps=16,
        market_quality={"passed": True},
        calibration=calibration,
        minimum_indicator_score=65,
        minimum_combined_score=70,
        safety_margin_bps=10,
        market_flow={
            "directional_data_available": False,
            "fresh": False,
            "data_quality": 0,
        },
        market_flow_weight=0.2,
        minimum_market_flow_quality=0.5,
    )
    assert missing_flow["status"] == "research_only"
    assert missing_flow["checks"]["market_flow_available"] is False
    assert missing_flow["checks"]["market_flow_freshness"] is False
    assert missing_flow["checks"]["market_flow_quality"] is False

    flow_disabled = signal_readiness_snapshot(
        matched=True,
        indicator_score=82,
        combined_score=78,
        estimated_cost_bps=16,
        market_quality={"passed": True},
        calibration=calibration,
        minimum_indicator_score=65,
        minimum_combined_score=70,
        safety_margin_bps=10,
        market_flow={},
        market_flow_weight=0,
        minimum_market_flow_quality=0.5,
    )
    assert flow_disabled["status"] == "shadow_ready"
    assert flow_disabled["market_flow_quality"]["required"] is False


def test_strategy_readiness_uses_the_latest_bounded_prediction_window() -> None:
    source = (ROOT / "src/quantdesk_v2/ai_monitor.py").read_text(encoding="utf-8")
    readiness = source[
        source.index("def strategy_readiness_report(") : source.index(
            "def historical_opportunity_analytics("
        )
    ]

    assert "AiMonitorPrediction.predicted_at.desc()" in readiness
    assert ".limit(5000)" in readiness
    assert "predictions.reverse()" in readiness
    assert "settlement_version == PREDICTION_SETTLEMENT_VERSION" in readiness
    assert "item.exit_at or item.due_at" in readiness


def test_prediction_surfaces_only_expose_the_current_settlement_policy() -> None:
    analytics_source = (ROOT / "src/quantdesk_v2/ai_monitor.py").read_text(encoding="utf-8")
    api_source = (ROOT / "src/quantdesk_v2/interfaces/api/ai_monitor.py").read_text(
        encoding="utf-8"
    )
    frontend = (ROOT / "src/quantdesk_v2/static/ai-monitor.js").read_text(encoding="utf-8")
    fact_analytics = analytics_source[
        analytics_source.index(
            "def historical_opportunity_fact_analytics("
        ) : analytics_source.index("def historical_opportunity_analytics(")
    ]
    historical_analytics = analytics_source[
        analytics_source.index("def historical_opportunity_analytics(") : analytics_source.index(
            "def settle_due_predictions("
        )
    ]
    overview = api_source[
        api_source.index("def overview(") : api_source.index("def ai_monitor_events(")
    ]
    prediction_records = api_source[
        api_source.index("def prediction_records(") : api_source.index("def opportunity_analytics(")
    ]

    assert "AiMonitorPredictionFact.settlement_version" in fact_analytics
    assert "AiMonitorPrediction.settlement_version" in historical_analytics
    assert "excluded_legacy_settlement_count" not in fact_analytics
    assert "excluded_legacy_settlement_count" not in historical_analytics
    assert "AiMonitorPrediction.settlement_version" in overview
    assert "AiMonitorPrediction.settlement_version" in prediction_records
    assert "excluded_legacy_settlement_count" not in frontend
    assert "当前策略 ${this.escape(summary.settlement_policy_version" in frontend


def test_market_flow_snapshot_combines_real_inputs_and_blocks_opposite_direction() -> None:
    now = datetime(2026, 8, 11, 8, 0, tzinfo=UTC)
    now_seconds = int(now.timestamp())
    inputs = {
        "depth": {
            "TESTUSDT": {
                "ts": now_seconds,
                "bid_depth_notional": 1_400_000,
                "ask_depth_notional": 600_000,
                "bid_depth_notional_5": 300_000,
                "ask_depth_notional_5": 100_000,
                "book_imbalance": 0.4,
                "book_imbalance_5": 0.5,
                "bid_level_count": 100,
                "ask_level_count": 96,
                "spread_bps": 2.5,
                "bid_depth_change_5s_pct": 12,
                "ask_depth_change_5s_pct": -4,
                "bid_depth_change_30s_pct": 20,
                "ask_depth_change_30s_pct": 3,
                "imbalance_change_5s": 0.08,
            }
        },
        "positioning": {
            "TESTUSDT": {
                "snapshot_at_ms": now_seconds * 1000,
                "taker_buy_sell_ratio": 4,
                "taker_buy_volume": 800,
                "taker_sell_volume": 200,
            }
        },
        "ticker": {"TESTUSDT": {"quote_volume": 50_000_000, "pct_24h": 1.2, "ts": now_seconds}},
        "underlying": {"TESTUSDT": {"volume": 1_000_000}},
        "profile": {
            "TEST": {
                "shares_outstanding": 100,
                "market_cap": 10_000,
                "source": "finnhub",
            }
        },
    }

    bullish = market_flow_snapshot(
        inputs,
        symbol="TEST",
        contract_symbol="TESTUSDT",
        direction="long",
        now=now,
    )
    bearish = market_flow_snapshot(
        inputs,
        symbol="TEST",
        contract_symbol="TESTUSDT",
        direction="short",
        now=now,
    )

    assert bullish["turnover_rate_pct"] == 1
    assert bullish["main_force_ratio"] > 0.75
    assert bullish["confirms_direction"] is True
    assert bullish["hard_conflict"] is False
    assert bullish["directional_data_available"] is True
    assert bullish["fresh"] is True
    assert bullish["depth_status"] == "fresh"
    assert bullish["depth_age_seconds"] == 0
    assert bullish["depth_unavailable_reason"] is None
    assert bullish["order_book_gate"]["passed"] is True
    assert bullish["order_book_confirms_direction"] is True
    assert bullish["data_quality"] >= 0.5
    assert bullish["bid_level_count"] == 100
    assert bullish["sources"]["order_count"] == "visible_price_levels_proxy"
    assert bearish["hard_conflict"] is True
    assert bearish["order_book_gate"]["direction_conflict"] is True


def test_order_book_gate_tolerates_bounded_clock_skew_and_only_vetoes_conflicts() -> None:
    now_seconds = 1_800_000_000
    depth = {
        "ts": now_seconds + 8,
        "bid_depth_notional": 1_000,
        "ask_depth_notional": 900,
        "book_imbalance": 0.05263158,
        "book_imbalance_5": 0.08,
        "bid_level_count": 100,
        "ask_level_count": 100,
        "spread_bps": 2.5,
        "imbalance_change_5s": 0.01,
    }

    neutral = order_book_gate_snapshot(
        depth,
        direction="long",
        now_seconds=now_seconds,
    )
    conflict = order_book_gate_snapshot(
        {**depth, "book_imbalance": -0.4, "book_imbalance_5": -0.5},
        direction="long",
        now_seconds=now_seconds,
    )
    one_sided_wall = order_book_gate_snapshot(
        {**depth, "book_imbalance": 0.1, "book_imbalance_5": -0.7},
        direction="long",
        now_seconds=now_seconds,
    )

    assert neutral["snapshot_status"] == "fresh"
    assert neutral["age_seconds"] == 0
    assert neutral["clock_skew_seconds"] == 8
    assert neutral["passed"] is True
    assert neutral["confirms_direction"] is False
    assert conflict["quality_passed"] is True
    assert conflict["direction_conflict"] is True
    assert conflict["passed"] is False
    assert conflict["reason"] == "BINANCE_ORDER_BOOK_DIRECTION_CONFLICT"
    assert one_sided_wall["directional_pressure"] < -0.2
    assert one_sided_wall["direction_conflict"] is False
    assert one_sided_wall["passed"] is True


def test_market_flow_snapshot_keeps_depth_with_bounded_future_clock_skew() -> None:
    now = datetime(2026, 8, 11, 8, 0, tzinfo=UTC)
    snapshot = market_flow_snapshot(
        {
            "depth": {
                "TESTUSDT": {
                    "ts": int(now.timestamp()) + 8,
                    "bid_depth_notional": 120,
                    "ask_depth_notional": 100,
                    "book_imbalance": 0.09,
                    "book_imbalance_5": 0.12,
                    "bid_level_count": 100,
                    "ask_level_count": 100,
                    "spread_bps": 1.2,
                }
            }
        },
        symbol="TEST",
        contract_symbol="TESTUSDT",
        direction="long",
        now=now,
    )

    assert snapshot["depth_status"] == "fresh"
    assert snapshot["depth_age_seconds"] == 0
    assert snapshot["bid_depth_notional"] == 120
    assert snapshot["order_book_quality_passed"] is True


def test_stable_gate_blocks_an_unusable_execution_order_book() -> None:
    now = datetime(2026, 8, 11, 8, 0, tzinfo=UTC)
    result = stable_gate_summary(
        {
            "checks": {
                "price_available": True,
                "ticker_fresh": True,
                "kline_fresh": True,
                "feature_quality": True,
            },
            "data_status": "live",
        },
        {
            "hard_conflict": False,
            "order_book_gate": {
                "quality_passed": False,
                "direction_clear": False,
                "reason": "BINANCE_ORDER_BOOK_STALE",
            },
        },
        evaluated_at=now,
        policy_mode="record",
    )

    assert result["passed"] is False
    assert result["decision_checks"]["order_book_usable"] is False
    assert "BINANCE_ORDER_BOOK_NOT_USABLE" in result["blocking_reasons"]
    assert result["execution_safety_gate_applied"] is True


def test_market_flow_snapshot_explains_stale_binance_depth() -> None:
    now = datetime(2026, 8, 11, 8, 0, tzinfo=UTC)
    snapshot = market_flow_snapshot(
        {
            "depth": {
                "TESTUSDT": {
                    "ts": int(now.timestamp()) - 31,
                    "bid_depth_notional": 100,
                    "ask_depth_notional": 90,
                }
            }
        },
        symbol="TEST",
        contract_symbol="TESTUSDT",
        direction="long",
        now=now,
    )

    assert snapshot["freshness"]["depth"] is False
    assert snapshot["depth_status"] == "stale"
    assert snapshot["depth_age_seconds"] == 31
    assert snapshot["depth_unavailable_reason"] == "BINANCE_DEPTH_SNAPSHOT_STALE"
    assert snapshot["bid_depth_notional"] is None


def test_ai_monitor_prediction_table_is_separate_from_trading_orders() -> None:
    table = AiMonitorPrediction.__table__

    assert table.name == "ai_monitor_predictions"
    assert {
        "opportunity_id",
        "entry_price",
        "exit_price",
        "result",
        "due_at",
        "signal_news_score",
        "signal_indicator_score",
        "estimated_cost_bps",
        "net_directional_return_bps",
        "max_favorable_bps",
        "max_adverse_bps",
    } <= {column.name for column in table.columns}
    assert {foreign_key.target_fullname for foreign_key in table.foreign_keys} == {
        "users.id",
        "ai_monitor_opportunities.id",
        "ai_monitor_opportunities.user_id",
    }


def test_ai_monitor_concurrency_guards_are_database_backed() -> None:
    run_constraints = {
        constraint.name: tuple(column.name for column in constraint.columns)
        for constraint in AiMonitorRun.__table__.constraints
        if hasattr(constraint, "columns")
    }
    opportunity_constraints = {
        constraint.name: tuple(column.name for column in constraint.columns)
        for constraint in AiMonitorOpportunity.__table__.constraints
        if hasattr(constraint, "columns")
    }
    claim_targets = {
        foreign_key.target_fullname
        for foreign_key in News.__table__.c.ai_claim_batch_id.foreign_keys
    }
    statement = news_ai._claimable_news_statement(
        "batch-1",
        selected_news_ids=[],
        only_unanalyzed=True,
        minimum_news_ts=1_700_000_000,
        stale_cutoff=datetime(2026, 8, 10),
    )
    sql = str(statement.compile(dialect=mysql.dialect()))
    migration = (ROOT / "migrations/versions/0043_ai_monitor_concurrency.py").read_text(
        encoding="utf-8"
    )

    assert run_constraints["uq_ai_monitor_runs_active_user_type"] == (
        "active_user_id",
        "run_type",
    )
    assert run_constraints["uq_ai_monitor_runs_id_user_id"] == ("id", "user_id")
    assert opportunity_constraints["uq_ai_monitor_opportunities_id_user_id"] == (
        "id",
        "user_id",
    )
    assert claim_targets == {"news_ai_batches.id"}
    assert "FOR UPDATE SKIP LOCKED" in sql
    assert "news.ai_claim_batch_id IS NULL" in sql
    assert "news.ai_analyzed_at IS NULL" in sql
    assert 'down_revision: str | None = "0042_security_financials"' in migration
    assert '"uq_ai_monitor_runs_active_user_type"' in migration
    assert '"fk_ai_monitor_predictions_opportunity_user"' in migration


def test_news_ai_model_call_audit_schema_follows_concurrency_revision() -> None:
    migration = (ROOT / "migrations/versions/0044_news_ai_model_call_audit.py").read_text(
        encoding="utf-8"
    )

    assert NewsAiModelCall.__tablename__ == "news_ai_model_calls"
    assert NewsAiModelCallItem.__tablename__ == "news_ai_model_call_items"
    assert NewsAiModelCall.__table__.c.request_json.nullable is False
    assert NewsAiModelCall.__table__.c.response_text.nullable is True
    assert NewsAiModelCallItem.__table__.c.news_id.primary_key is True
    assert AiMonitorOpportunity.__table__.c.news_ai_batch_ids_json.nullable is True
    assert AiMonitorOpportunity.__table__.c.news_ai_model_call_ids_json.nullable is True
    assert 'down_revision: str | None = "0043_ai_monitor_claims"' in migration
    assert "mysql.LONGTEXT()" in migration
    assert '"news_ai_model_call_items"' in migration
    assert '"news_ai_batch_ids_json"' in migration
    assert '"news_ai_model_call_ids_json"' in migration


def test_ai_monitor_prediction_migration_follows_workspace_revision() -> None:
    migration = (ROOT / "migrations/versions/0038_ai_monitor_predictions.py").read_text(
        encoding="utf-8"
    )

    assert 'down_revision: str | None = "0037_ai_monitor_workspace"' in migration
    assert '"ai_monitor_predictions"' in migration
    assert '"monitor_symbols_json"' in migration


def test_ai_monitor_execution_metrics_migration_follows_model_call_audit() -> None:
    migration = (ROOT / "migrations/versions/0045_ai_prediction_execution_metrics.py").read_text(
        encoding="utf-8"
    )

    assert 'down_revision: str | None = "0044_news_ai_call_audit"' in migration
    for column in (
        "signal_news_score",
        "signal_indicator_score",
        "estimated_cost_bps",
        "net_directional_return_bps",
        "max_favorable_bps",
        "max_adverse_bps",
        "settlement_version",
    ):
        assert f'"{column}"' in migration


def test_ai_monitor_live_readiness_migration_follows_execution_metrics() -> None:
    migration = (ROOT / "migrations/versions/0046_ai_live_readiness_gates.py").read_text(
        encoding="utf-8"
    )

    assert 'down_revision: str | None = "0045_ai_prediction_metrics"' in migration
    for column in (
        "minimum_indicator_score",
        "minimum_combined_score",
        "maximum_market_age_seconds",
        "minimum_feature_quality",
        "minimum_calibration_samples",
        "live_safety_margin_bps",
        "readiness_status",
        "expected_edge_lower_bound_bps",
    ):
        assert f'"{column}"' in migration


def test_ai_monitor_cost_config_migration_follows_live_readiness() -> None:
    migration = (ROOT / "migrations/versions/0047_ai_prediction_cost_config.py").read_text(
        encoding="utf-8"
    )

    assert 'down_revision: str | None = "0046_ai_live_readiness"' in migration
    for column in (
        "prediction_fee_enabled",
        "prediction_fee_bps_per_side",
        "prediction_slippage_enabled",
        "prediction_slippage_bps_per_side",
        "prediction_funding_enabled",
        "prediction_funding_bps_per_8h",
    ):
        assert f'"{column}"' in migration


def test_market_flow_migration_follows_prediction_cost_config() -> None:
    migration = (ROOT / "migrations/versions/0048_market_flow_metrics.py").read_text(
        encoding="utf-8"
    )

    assert 'down_revision: str | None = "0047_ai_prediction_costs"' in migration
    for column in (
        "bid_depth_notional_5",
        "ask_depth_notional_5",
        "bid_level_count",
        "ask_level_count",
        "spread_bps",
        "bid_depth_change_5s_pct",
        "ask_depth_change_30s_pct",
        "imbalance_change_5s",
    ):
        assert f'"{column}"' in migration
    assert '"bid_notional_5_nonnegative"' in migration
    assert '"ck_market_microstructure_bid_notional_5_nonnegative"' not in migration


def test_ai_monitor_score_weight_migration_follows_market_flow_metrics() -> None:
    migration = (ROOT / "migrations/versions/0049_ai_monitor_score_weights.py").read_text(
        encoding="utf-8"
    )

    assert 'down_revision: str | None = "0048_market_flow_metrics"' in migration
    for column in (
        "minimum_market_flow_quality",
        "news_score_weight",
        "technical_score_weight",
        "market_flow_score_weight",
    ):
        assert f'"{column}"' in migration
    assert (
        '"news_score_weight + technical_score_weight + market_flow_score_weight = 100"' in migration
    )
    assert '"valid_news_score_weight"' in migration
    assert '"ck_ai_monitor_configs_news_score_weight"' not in migration


def test_news_ai_industry_migration_follows_prediction_revision() -> None:
    migration = (ROOT / "migrations/versions/0039_news_ai_industries.py").read_text(
        encoding="utf-8"
    )
    constraints = {
        constraint.name: str(constraint.sqltext)
        for constraint in NewsAiBatch.__table__.constraints
        if hasattr(constraint, "sqltext")
    }

    assert News.__table__.c.related_industries.type.python_type is dict
    assert any("10" in sqltext and "300" in sqltext for sqltext in constraints.values())
    assert "UPDATE ai_monitor_configs SET news_interval_minutes=15" not in migration
    assert 'down_revision: str | None = "0038_ai_monitor_predictions"' in migration
    assert '"related_industries"' in migration


def test_ai_monitor_pipeline_migration_adds_pending_news_index() -> None:
    migration = (ROOT / "migrations/versions/0041_ai_monitor_pipeline_optimization.py").read_text(
        encoding="utf-8"
    )

    assert 'down_revision: str | None = "0040_ai_monitor_candidates"' in migration
    assert '"ix_news_ai_pending_ts"' in migration


def test_news_candidates_are_visible_before_prediction_confirmation() -> None:
    monitor = (ROOT / "src/quantdesk_v2/ai_monitor.py").read_text(encoding="utf-8")
    api = (ROOT / "src/quantdesk_v2/interfaces/api/ai_monitor.py").read_text(encoding="utf-8")
    migration = (ROOT / "migrations/versions/0040_ai_monitor_opportunity_candidates.py").read_text(
        encoding="utf-8"
    )
    opportunity_status = next(
        constraint
        for constraint in AiMonitorOpportunity.__table__.constraints
        if constraint.name == "ck_ai_monitor_opportunities_valid_status"
    )

    assert "candidate" in str(opportunity_status.sqltext)
    assert 'status="discovered" if signal_confirmed else "candidate"' in monitor
    assert 'AiMonitorOpportunity.status.in_(("candidate", "discovered"))' in api
    assert 'down_revision: str | None = "0039_news_ai_industries"' in migration
    assert "status IN ('candidate', 'discovered', 'expired', 'dismissed')" in migration


def test_opportunity_scan_cleans_only_unpredicted_transient_rows() -> None:
    monitor = (ROOT / "src/quantdesk_v2/ai_monitor.py").read_text(encoding="utf-8")

    assert "def cleanup_unpredicted_opportunities" in monitor
    assert "if row.id in prediction_ids:" in monitor
    assert 'if row.status == "expired"' in monitor
    assert "policy != INDICATOR_MATCH_POLICY" in monitor
    assert "cleanup = cleanup_unpredicted_opportunities(db, run.user_id)" in monitor
    assert '"opportunity_cleanup": cleanup' in monitor


def test_opportunity_related_news_endpoint_is_tenant_scoped() -> None:
    api = (ROOT / "src/quantdesk_v2/interfaces/api/ai_monitor.py").read_text(encoding="utf-8")
    endpoint = api[api.index('@router.get("/opportunities/{opportunity_id}/news")') :]

    assert "AiMonitorOpportunity.public_id == opportunity_id" in endpoint
    assert "AiMonitorOpportunity.user_id == user.id" in endpoint
    assert "opportunity.news_ids_json" in endpoint
    assert "News.id.in_(news_ids)" in endpoint


def test_opportunity_live_order_book_endpoint_is_tenant_scoped() -> None:
    api = (ROOT / "src/quantdesk_v2/interfaces/api/ai_monitor.py").read_text(encoding="utf-8")
    endpoint = api[
        api.index('@router.get("/opportunities/{opportunity_id}/order-book")') : api.index(
            '@router.get("/opportunities/{opportunity_id}/news")'
        )
    ]

    assert "AiMonitorOpportunity.public_id == opportunity_id" in endpoint
    assert "AiMonitorOpportunity.user_id == user.id" in endpoint
    assert "ws_depth.order_book_snapshot(contract_symbol, limit)" in endpoint
    assert "except ws_depth.OrderBookUnavailableError as exc" in endpoint
    assert "status_code=503" in endpoint
    assert 'raise HTTPException(status_code=404, detail="opportunity not found")' in endpoint


def test_opportunities_are_ordered_by_signal_time_descending() -> None:
    api = (ROOT / "src/quantdesk_v2/interfaces/api/ai_monitor.py").read_text(encoding="utf-8")
    endpoint = api[
        api.index('@router.get("/opportunities")') : api.index(
            '@router.get("/opportunities/{opportunity_id}/news")'
        )
    ]

    assert "AiMonitorOpportunity.discovered_at.desc()" in endpoint
    assert "AiMonitorOpportunity.id.desc()" in endpoint
    assert "AiMonitorOpportunity.combined_score.desc()" not in endpoint
    assert "AiMonitorOpportunity.updated_at.desc()" not in endpoint
    assert "prediction_by_opportunity_id" in endpoint
    assert "AiMonitorPrediction.opportunity_id == AiMonitorOpportunity.id" in endpoint
    assert 'scope: Literal["legacy", "current", "history"]' in endpoint
    assert '"pagination"' in endpoint
    assert 'live_tickers.get((item.contract_symbol or "").upper())' in endpoint
    assert '"prediction_entry_price"' in api
    assert '"prediction_combined_score"' in api
    assert '"prediction_market_flow_score"' in api
    assert '"virtual_position"' in api
    assert "ai_monitor.virtual_position_snapshot(prediction, live_market)" in api
    assert '"prediction_entry_gate"' in api
    assert "ai_monitor.prediction_entry_gate_snapshot(prediction)" in api
    assert '"updated_at": _utc_out(item.updated_at)' in api


def test_opportunity_model_call_endpoint_is_tenant_scoped_and_returns_raw_audit() -> None:
    api = (ROOT / "src/quantdesk_v2/interfaces/api/ai_monitor.py").read_text(encoding="utf-8")
    endpoint = api[api.index('@router.get("/opportunities/{opportunity_id}/model-calls")') :]

    assert "AiMonitorOpportunity.user_id == user.id" in endpoint
    assert "NewsAiBatch.started_by == user.id" in endpoint
    assert "opportunity.news_ai_batch_ids_json" in endpoint
    assert "opportunity.news_ai_model_call_ids_json" in endpoint
    assert "NewsAiModelCall.batch_id.in_(batch_ids)" in endpoint
    assert "NewsAiModelCall.id.in_(call_ids)" in endpoint
    assert "NewsAiModelCallItem.news_id.in_(news_ids)" not in endpoint
    assert '"request_json": dict(call.request_json or {})' in endpoint
    assert '"response_text": call.response_text' in endpoint
    assert '"response_envelope": call.response_envelope' in endpoint
    assert "系统不会用后续重跑结果替代" in endpoint


def test_opportunity_fundamentals_endpoint_reads_existing_research_tables() -> None:
    api = (ROOT / "src/quantdesk_v2/interfaces/api/ai_monitor.py").read_text(encoding="utf-8")
    endpoint = api[api.index('@router.get("/opportunities/{opportunity_id}/fundamentals")') :]

    assert "AiMonitorOpportunity.user_id == user.id" in endpoint
    assert "Security.symbol == opportunity.symbol" in endpoint
    assert "db.get(CompanyProfile, security.id)" in endpoint
    assert "select(SecurityFundamentalAnalysis)" in endpoint
    assert "select(SecurityFinancialSnapshot)" in endpoint
    assert '"business_summary": analysis.business_summary' in endpoint
    assert '"revenue_ttm"' in endpoint
    assert '"operating_cash_flow_ttm"' in endpoint
    assert '"total_debt"' in endpoint
    assert '"pe_ratio"' in endpoint
    assert '"financial_health_score"' in endpoint
    assert '"financial_metrics_complete"' not in endpoint


def test_ai_monitor_frontend_is_registered_beside_contract_monitor() -> None:
    app = (ROOT / "web/src/App.tsx").read_text(encoding="utf-8")
    entrypoint = (ROOT / "web/src/main.tsx").read_text(encoding="utf-8")
    legacy_index = (ROOT / "src/quantdesk_v2/static/index.html").read_text(encoding="utf-8")
    legacy_app = (ROOT / "src/quantdesk_v2/static/app.js").read_text(encoding="utf-8")
    legacy_styles = (ROOT / "src/quantdesk_v2/static/style.css").read_text(encoding="utf-8")
    component = (ROOT / "src/quantdesk_v2/static/ai-monitor.js").read_text(encoding="utf-8")
    stylesheet = (ROOT / "src/quantdesk_v2/static/ai-monitor.css").read_text(encoding="utf-8")

    assert app.index('{ key: "monitor"') < app.index('{ key: "ai-monitor"')
    assert 'tag="ai-monitor-dashboard"' in app
    assert '"/assets/ai-monitor.js?v=20260826-ws1"' in entrypoint
    assert '"/assets/monitor.js?v=20260810-forecast-2"' in entrypoint
    assert '"ai-monitor": "发现机会"' in app
    assert '{ key: "ai-monitor", icon: "机", label: "发现机会" }' in app
    assert legacy_index.index('data-panel-target="monitor"') < legacy_index.index(
        'data-panel-target="ai-monitor"'
    )
    assert 'href="/ai-monitor" data-panel-target="ai-monitor"' in legacy_index
    assert 'data-panel="ai-monitor"' in legacy_index
    assert '<ai-monitor-dashboard id="ai-monitor-dashboard"></ai-monitor-dashboard>' in legacy_index
    assert 'src="/assets/ai-monitor.js?v=20260826-ws1"' in legacy_index
    assert 'href="/assets/ai-monitor.css?v=20260825-52"' in component
    assert '"ai-monitor": "/ai-monitor"' in legacy_app
    assert 'selected === "ai-monitor" && typeof aiMonitor.start === "function"' in legacy_app
    assert 'selected !== "ai-monitor" && typeof aiMonitor.pause === "function"' in legacy_app
    assert 'classList.toggle("ai-monitor-mode", selected === "ai-monitor")' in legacy_app
    assert ".workspace-content.ai-monitor-mode" in legacy_styles
    for asset in (
        "/assets/style.css?v=20260810-font1_6x-1",
        "/assets/strategies.css?v=20260825-strategyi18n1",
        "/assets/terminal.css?v=20260810-font1_6x-1",
        "/assets/paper.js?v=20260809-paper-combo-1",
        "/assets/live.js?v=20260809-font1_6x-1",
        "/assets/backtest.js?v=20260809-font1_6x-1",
        "/assets/app.js?v=20260826-nav-1",
    ):
        assert asset in legacy_index
    assert 'view: "opportunities"' in component
    assert 'class="ai-nav-root active"' in component
    assert 'class="ai-subnav-group"' in component
    assert "<p><strong>发现机会</strong><small>二级菜单</small></p>" not in component
    assert 'basisState === "opening_gap_watch"' in component
    assert '"跨时段预判"' in component
    assert '"开盘预判"' in component
    assert "本轮没有新增新闻" in component
    assert "当前机会关联" in component
    assert "AI 记忆用于新新闻回溯" in component
    assert "新相关新闻分析完成后自动刷新" in component
    assert "用最近现货快照、当前 BN 价格和" not in component
    assert 'value != null && value !== ""' in component
    assert "这是开盘概率预判，不是无风险套利" in component
    assert ".cross-venue-basis.opening_gap_watch" in stylesheet
    assert component.index('class="ai-nav-root active"') < component.index(
        'class="ai-subnav-group"'
    )
    assert component.index('class="ai-subnav-group"') < component.index("<strong>新闻列表</strong>")
    assert ".ai-subnav-group .ai-nav-child" in stylesheet
    for label in ("新闻列表", "分析记录", "指标配置", "发现机会", "预测统计分析"):
        assert label in component
    assert component.index('id="run-opportunity"') < component.index('id="open-config"')
    assert component.index('id="run-opportunity"') < component.index('id="open-weight-config"')
    assert component.index('id="open-weight-config"') < component.index('id="open-config"')
    assert component.index('id="open-config"') < component.index('id="ai-refresh"')
    assert (
        'this.q("#open-config").addEventListener("click", () => this.openConfig("indicators"));'
        in component
    )
    assert 'id="open-news-config"' in component
    assert 'this.openConfig("news")' in component
    assert 'id="open-weight-config"' in component
    assert 'this.openConfig("weights")' in component
    for field in (
        "config-news-weight",
        "config-technical-weight",
        "config-market-flow-weight",
        "config-market-flow-quality",
        "weight-total-state",
    ):
        assert f'id="{field}"' in component
    assert "Number(config.minimum_market_flow_quality ?? 0.5) * 100" in component
    assert (
        'minimum_market_flow_quality: Number(this.q("#config-market-flow-quality").value) / 100'
        in component
    )
    assert "scoreWeightSummary" in component
    assert ".weight-preview" in stylesheet
    assert 'data-analyze-news="${this.escape(item.id)}"' in component
    assert 'this.api("/news/analyze"' in component
    assert "await this.waitForRun(run.id)" in component
    assert ".news-analyze-action" in stylesheet
    assert 'data-ai-view="config"' not in component
    assert "趋势、突破、回踩、反转按策略组择一确认" in component
    assert "监控品种" in component
    assert "每 15 分钟从数据库读取最新 10 条未分析新闻" in component
    assert "related_industries" in component
    assert "等待策略组与评分确认" in component
    assert "暂无技术行情" in component
    assert 'data-opportunity-tab="current"' in component
    assert 'data-opportunity-tab="history"' in component
    assert 'id="opportunity-status-tabs"' in component
    for status in ("all", "triggered", "ready", "waiting", "failed"):
        assert f'data-opportunity-status="{status}"' in component
        assert f'id="opportunity-status-{status}-count"' in component
    assert "setOpportunityStatusFilter(status)" in component
    assert "renderOpportunityStatusCounts()" in component
    assert "status === this.state.opportunityStatusFilter" in component
    assert "this.virtualEntryState(item, this.virtualEntryGate(item)).tone" in component
    assert "当前机会" in component
    assert "历史机会" in component
    assert (
        'this.state.updateStreamStatus = this.state.lastSuccessfulRefreshAt ? "polling" : "connecting";'
        in component
    )
    assert "const pipelineInitializing = !this.state.lastSuccessfulRefreshAt" in component
    assert (
        'const pipelineReconnecting = this.state.updateStreamStatus === "reconnecting"' in component
    )
    assert (
        "const restPollingHealthy = hasRecentRestSuccess && !this.state.lastRefreshError;"
        in component
    )
    assert "const restHealthy = this.firstValue(" not in component
    assert "最后刷新 ${this.formatDate(transportUpdatedAt)}" in component
    assert '"正在连接数据"' in component
    assert '"增量推送重连中"' in component
    assert 'id="include-expired"' not in component
    assert "const bySignalTimeDesc = (left, right) =>" in component
    assert (
        'const isAwaitingSettlement = (item) => ["pending", "unavailable"].includes(String(item.prediction_status || ""))'
        in component
    )
    assert (
        'items.filter((item) => isActive(item) && item.prediction_status !== "completed").sort(bySignalTimeDesc)'
        in component
    )
    assert "items.filter(isAwaitingSettlement).sort(bySignalTimeDesc)" in component
    assert "this.parseDate(item.expires_at).getTime() > now" in component
    assert "`${raw}Z`" in component
    assert "if (!unique.has(instrument)) unique.set(instrument, item)" in component
    assert 'id="current-direction-counts"' in component
    assert 'id="history-direction-counts"' in component
    assert "opportunityDirectionCounts" in component
    assert "historyOpportunityDirectionCounts" in component
    assert 'this.api("/opportunities?limit=300&include_expired=true")' in component
    assert "做多 ${counts.long} 次，做空 ${counts.short} 次" in component
    assert ".opportunity-tabs .direction-counts .long" in stylesheet
    assert ".opportunity-tabs .direction-counts .short" in stylesheet
    assert ".opportunity-tabs .direction-counts .pending" in stylesheet
    assert "已剔除行情不足" in component
    assert ".opportunity-tabs" in stylesheet
    assert ".opportunity-status-tabs" in stylesheet
    assert '.opportunity-status-tabs button[data-opportunity-status="triggered"]' in stylesheet
    for label in ("信号时间", "信号有效期间", "候选方向", "触发进度"):
        assert label in component
    assert 'id="opportunity-research" research-only' in component
    assert 'data-open-contract="${this.escape(item.contract_symbol)}"' in component
    assert 'data-opportunity-id="${this.escape(item.id)}"' in component
    assert "research.openResearch(button.dataset.openContract" in component
    assert 'data-ai-conclusion="${this.escape(item.id)}"' in component
    assert 'class="ai-settings-menu"' in component
    assert 'data-toggle-opportunity-details="${this.escape(item.id)}"' in component
    assert "expandedOpportunityIds: new Set()" in component
    assert (
        'event.composedPath().find((node) => node?.matches?.("[data-toggle-opportunity-details]"))'
        in component
    )
    assert 'const nextExpanded = !card.classList.contains("is-expanded")' in component
    assert (
        'key === "header" && currentSection.querySelector("[data-toggle-opportunity-details]")'
        in component
    )
    assert (
        'data-layout-state="${this.escape(entryState.tone)}:${this.escape(entryState.label)}:'
        in component
    )
    assert ".opportunity-item:not(.is-expanded) .opportunity-feature-grid" in stylesheet
    assert 'id="score-trend-modal"' in component
    assert 'data-score-trend="${this.escape(item.id)}"' in component
    assert "opportunityScoreHistory(item)" in component
    assert "item?.prediction_market_flow_score" in component
    assert "const unique = new Map()" in component
    assert "renderScoreTrendChart(item, history)" in component
    assert "openScoreTrend(opportunityId, trigger)" in component
    assert 'class="market-depth-control ${marketFlowTrend.direction}"' in component
    assert 'data-market-flow-trend="${this.escape(item.id)}"' not in component
    assert "openMarketFlowTrend(opportunityId, trigger)" in component
    assert 'return this.openOrderBook(opportunityId, trigger, "flow")' in component
    assert "renderMarketFlowTrendChart(item, history)" in component
    assert "盘口与资金分析" in component
    assert 'data-order-book-view="depth"' in component
    assert 'data-order-book-view="flow"' in component
    assert 'id="order-book-flow-body"' in component
    assert "renderOrderBookFlow()" in component
    assert "买卖盘名义资金量变化" in component
    assert "可见挂单与资金盘口评分用于研究" in component
    assert ".market-depth-control" in stylesheet
    assert ".order-book-view-tabs" in stylesheet
    assert ".market-flow-score" not in stylesheet
    assert "evidence.score_history" in component
    assert 'class="score-trend-chart"' in component
    assert 'class="score-line ${definition.key}"' in component
    assert 'style="--series:${definition.color}"' not in component
    assert "virtualEntryGate(item)" in component
    assert "virtualEntryState(item, gate)" in component
    assert "virtualPositionSnapshot(item)" in component
    assert "当前 ${this.state.displayLeverage}x 仓位ROE" in component
    assert "保证金口径，不代表账户总收益" in component
    assert "冻结入场价" in component
    assert "实时价格" in component
    assert "参考止损价" in component
    assert "参考止盈价" in component
    assert "越过参考止损·待K线确认" in component
    assert "已按止损结算" in component
    assert "net_pnl_per_10000" in component
    assert ".virtual-position-metrics" in stylesheet
    assert 'check.key !== "entry_price"' in component
    assert "候选方向" in component
    assert "触发进度" in component
    assert "仅为研判方向，尚未买入" in component
    assert "尚未入场" in component
    assert ".opportunity-signal .trigger-progress" in stylesheet
    assert (
        'class="virtual-entry-gate ${entryState.tone} ${triggeredPosition ? "position-active" : ""}"'
        in component
    )
    assert "ENTRY GATE" in component
    assert "冻结触发价格" in component
    assert "真实订单关闭" in component
    assert component.index("${symbolControl}<small>") < component.index(
        "${conclusionControl}</div>"
    )
    assert "AI分析结论" in component
    assert 'id="ai-conclusion-modal"' in component
    assert "openAiConclusion(opportunityId, trigger)" in component
    assert "closeAiConclusion()" in component
    assert "AI 新闻研判" in component
    assert "技术指标验证" in component
    assert "仅作预测研究，不会触发实盘交易" in component
    assert "虚拟回放" in component
    assert "虚拟持仓" in component
    assert "VIRTUAL" not in component
    assert 'id="open-news-logic"' in component
    assert "新闻分析逻辑" in component
    assert "openNewsAnalysisLogic(trigger)" in component
    assert "renderNewsAnalysisLogic()" in component
    assert "this.api(`/opportunities/${encodeURIComponent(item.id)}/model-calls`)" in component
    assert "System 提示词" in component
    assert "User 提示词 / 新闻输入" in component
    assert "模型返回原始文本" in component
    assert "服务商完整响应包" in component
    assert "API Key 从未写入记录" in component
    assert 'data-conclusion-view="news"' in component
    assert 'data-conclusion-view="analysis"' in component
    assert 'data-conclusion-view="fundamentals"' in component
    assert 'data-conclusion-view="market"' in component
    assert component.index('data-conclusion-view="fundamentals"') < component.index(
        'data-conclusion-view="news"'
    )
    assert component.index('data-conclusion-view="news"') < component.index(
        'data-conclusion-view="market"'
    )
    assert component.index('data-conclusion-view="market"') < component.index(
        'data-conclusion-view="analysis"'
    )
    assert "基本面信息" in component
    assert "相关新闻列表" in component
    assert "资金盘口指标" in component
    assert "主力量比" in component
    assert "5秒挂单增速" in component
    assert "this.api(`/opportunities/${encodeURIComponent(item.id)}/news`)" in component
    assert "this.api(`/opportunities/${encodeURIComponent(item.id)}/fundamentals`)" in component
    assert "renderAiRelatedNewsList(items, opportunity" in component
    assert "renderAiFundamentals(data, opportunity)" in component
    assert "完整基本面" in component
    assert "TTM 营收" in component
    assert "经营现金流" in component
    assert "自由现金流" in component
    assert "总债务" in component
    assert "EV/EBITDA" in component
    assert ".ai-fundamental-financials" in stylesheet
    assert ".ai-conclusion-nav" in stylesheet
    assert ".ai-related-news-card" in stylesheet
    assert ".ai-fundamental-panel" in stylesheet
    assert ".ai-conclusion-trigger" in stylesheet
    assert ".opportunity-score.up" in stylesheet
    assert ".opportunity-score.down" in stylesheet
    assert ".score-trend-modal" in stylesheet
    assert ".score-trend-chart" in stylesheet
    assert 'id="order-book-modal"' in component
    assert 'data-order-book="${this.escape(item.id)}"' in component
    assert "/order-book?limit=${this.orderBookLimit}" in component
    assert 'String(snapshot.transport || "").startsWith("websocket")' in component
    assert '"REST 快照"' in component
    assert "跨进程时由短缓存 REST 快照补齐" in component
    assert "买卖盘口梯形表" in component
    assert ".order-book-ladder" in stylesheet
    assert ".order-book-chart" in stylesheet
    assert ".score-line.combined" in stylesheet
    assert ".score-line.market_flow" in stylesheet
    assert ".virtual-entry-gate" in stylesheet
    assert ".virtual-entry-checks" in stylesheet
    assert ".virtual-entry-price" in stylesheet
    assert ".ai-conclusion-dialog" in stylesheet
    assert ".ai-market-flow-grid" in stylesheet
    assert ".news-logic-trigger" in stylesheet
    assert 'id="open-news-system-prompt"' in component
    assert 'id="open-historical-judgment"' in component
    assert 'id="historical-judgment-modal"' in component
    assert "historical_analysis_memory" in component
    assert 'id="news-system-prompt-modal"' in component
    assert 'data-conclusion-view="memory"' in component
    assert (
        "this.api(`/opportunities/${encodeURIComponent(item.id)}/news-analysis-records`)"
        in component
    )
    assert "一周新闻研判追踪" in component
    assert "7天记忆" in component
    assert "当前机会关联" in component
    assert "已过滤历史污染" in component
    assert "判断依据与过程" in component
    assert "事实输入" in component
    assert "反向证据" in component
    assert ".ai-memory-reasoning-steps" in stylesheet
    assert ".ai-memory-evidence-groups" in stylesheet
    assert 'this.api("/news-system-prompt"' in component
    assert ".news-system-prompt-trigger" in stylesheet
    assert ".news-system-prompt-dialog" in stylesheet
    assert ".historical-judgment-trigger" in stylesheet
    assert ".historical-judgment-dialog" in stylesheet
    assert ".ai-memory-timeline" in stylesheet
    assert ".news-logic-dialog" in stylesheet
    assert ".news-model-call-tabs" in stylesheet
    assert ".news-model-raw-block" in stylesheet
    assert "combined_score: opportunity.combined_score" in component
    assert "technical_confirmed: evidence.confirmed === true" in component
    assert "outcome_result: opportunity.outcome?.result" in component
    assert 'tab === "history"' in component
    assert 'class="history-result ${this.escape(outcomeResult)}"' in component
    assert "是否命中" in component
    assert 'item.prediction_status || ""' in component
    assert 'predictionStatus === "pending"' in component
    assert 'predictionStatus === "unavailable"' in component
    assert "技术未确认" in component
    assert "未生成预测" in component
    assert "退出行情宽限期内未取得行情" in component
    assert 'monitoring_exit: "正在监控退出条件"' in component
    assert "止盈、止损与评分转弱/反转" in component
    assert "due_at 仅是强制退出上限，价格或评分条件可提前退出" in component
    assert "等待观察周期结束，或等待结算时点附近" not in component
    assert "到期结算价" not in component
    assert "预测结算 ${this.formatDate(settlementState.dueAt)}" not in component
    assert ".opportunity-metrics.with-result" in stylesheet
    assert ".history-result.win" in stylesheet
    assert ".history-result.not_created" in stylesheet
    assert ".history-result.pending" in stylesheet
    assert (
        'const data = await this.api("/opportunities?limit=300&include_expired=true")' in component
    )
    assert 'this.api("/opportunity-analytics?limit=300")' not in component
    assert 'id="historical-replay"' not in component
    assert 'id="replay-form"' not in component
    assert 'this.api("/replays?limit=5")' not in component
    assert 'id="prediction-filter-form"' in component
    assert 'id="prediction-news-score-min"' in component
    assert 'id="prediction-indicator-score-min"' in component
    assert 'id="prediction-direction"' in component
    assert 'id="prediction-cost-form"' in component
    assert 'id="prediction-fee-enabled"' in component
    assert 'id="prediction-slippage-enabled"' in component
    assert 'id="prediction-funding-enabled"' in component
    assert 'this.api("/cost-config"' in component
    assert "news_score_min: String(filters.newsScoreMin)" in component
    assert "indicator_score_min: String(filters.indicatorScoreMin)" in component
    assert "combined_score_min: String(filters.combinedScoreMin)" in component
    assert "option_flow_score_min: String(filters.optionFlowScoreMin)" in component
    assert "gex_score_min: String(filters.gexScoreMin)" in component
    assert "direction: filters.direction" in component
    assert "market_session: filters.marketSession" in component
    assert "quote_quality: filters.quoteQuality" in component
    assert "event_risk: filters.eventRisk" in component
    assert "exit_reason: filters.exitReason" in component
    assert "item?.lifecycle_status" in component
    assert "item?.gate_summary" in component
    assert "item?.score_components" in component
    assert 'item?.flow && typeof item.flow === "object"' in component
    assert "stableFlow.option_flow" in component
    assert "stableFlow.institutional_flow" in component
    assert "item?.data_quality" in component
    assert "item?.api_version" in component
    assert "item?.version?.api" in component
    assert "item?.signal_snapshot" in component
    assert "无数据：尚未完成行情与风险门控评估" in component
    assert "无数据 · 未参与门控" in component
    assert "flow.score ?? 50" not in component
    assert ".virtual-entry-check.missing" in stylesheet
    assert "筛选样本" in component
    assert "多 / 空方向" in component
    assert 'this.api("/prediction-records?limit=200")' not in component
    for label in ("历史机会", "筛选样本", "命中次数", "命中概率", "x 仓位ROE"):
        assert label in component
    for label in ("x 成本后 ROE", "毛利润率", "MFE / MAE", "成本后结果", "成本计算"):
        assert label in component
    assert (
        "${this.formatLeveragedReturnFromBps(item.net_directional_return_bps)}<small>毛利润率"
        in component
    )
    assert (
        "${this.formatLeveragedReturnFromBps(item.gross_directional_return_bps)}<small>净仓位ROE"
        not in component
    )
    assert "RISK UNIT EXIT GUARD V7" in component
    assert 'id="prediction-settlement-version"' in component
    assert "settlement_version: filters.settlementVersion" in component
    for label in (
        "最低技术强度",
        "最低组合评分",
        "最大实时行情延迟",
        "历史校准样本门槛",
        "成本安全边际",
        "实盘准备门槛",
        "影子候选",
    ):
        assert label in component
    assert 'id="strategy-readiness"' in component
    assert 'readiness.status === "shadow_ready"' in component
    assert ".strategy-readiness" in stylesheet
    assert "不会执行任何交易" in (ROOT / "src/quantdesk_v2/ai_monitor.py").read_text(
        encoding="utf-8"
    )
    analytics_source = (ROOT / "src/quantdesk_v2/ai_monitor.py").read_text(encoding="utf-8")
    assert "OpportunityMarketSnapshot," in analytics_source
    assert ".outerjoin(\n            OpportunityMarketSnapshot," in analytics_source
    assert "AiMonitorOpportunity.id == AiMonitorPrediction.opportunity_id" in analytics_source
    assert 'AiMonitorPrediction.status == "completed"' in analytics_source
    assert "直接统计已经完成结算的预测" in analytics_source
    assert '@router.get("/opportunity-analytics")' in (
        ROOT / "src/quantdesk_v2/interfaces/api/ai_monitor.py"
    ).read_text(encoding="utf-8")
    api_source = (ROOT / "src/quantdesk_v2/interfaces/api/ai_monitor.py").read_text(
        encoding="utf-8"
    )
    for parameter in ("news_score_min", "indicator_score_min", "direction"):
        assert parameter in api_source
    assert '@router.get("/opportunity-readiness")' in api_source
    assert "include_readiness: bool = Query(default=False)" in api_source
    assert ".analytics-summary" in stylesheet
    assert ".analytics-filters" in stylesheet
    assert ".analytics-cost-config" in stylesheet
    assert ".ai-module-nav" in stylesheet
