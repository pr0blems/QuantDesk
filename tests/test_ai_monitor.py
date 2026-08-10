from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

from sqlalchemy.dialects import mysql

from quantdesk_v2 import news_ai
from quantdesk_v2.ai_monitor import (
    NEWS_BATCH_SIZE,
    RUN_STALE_SECONDS,
    _take_ingested_news,
    aggregate_news_candidates,
    enqueue_news_analysis,
    filter_monitored_candidates,
    historical_settlement_price,
    indicator_catalog,
    indicator_conflicts,
    indicator_templates,
    match_configured_indicators,
    prediction_outcome,
    settle_due_predictions,
    settleable_historical_outcomes,
    strongest_candidate_per_symbol,
    summarize_historical_opportunities,
)
from quantdesk_v2.interfaces.api.ai_monitor import _utc_out
from quantdesk_v2.models import (
    AiMonitorOpportunity,
    AiMonitorPrediction,
    AiMonitorRun,
    News,
    NewsAiBatch,
)
from quantdesk_v2.schemas import AiMonitorConfigUpdate, AiMonitorNewsAnalyzeRequest

ROOT = Path(__file__).resolve().parents[1]


def test_ai_monitor_api_serializes_naive_database_datetimes_as_utc() -> None:
    naive = datetime(2026, 8, 10, 7, 14, 27)

    serialized = _utc_out(naive)

    assert serialized is not None
    assert serialized.tzinfo is UTC
    assert serialized.isoformat() == "2026-08-10T07:14:27+00:00"
    assert _utc_out(None) is None


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


def test_configured_indicators_use_all_selected_policy() -> None:
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
    assert rejected is False
    assert [item["matched"] for item in rejected_evidence] == [True, False]


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
            {"result": "win", "directional_return_bps": 100, "technical_confirmed": True},
            {"result": "loss", "directional_return_bps": -50, "technical_confirmed": False},
            {"result": "flat", "directional_return_bps": 0, "technical_confirmed": False},
            {"result": "unavailable", "directional_return_bps": None, "technical_confirmed": False},
        ]
    )

    assert summary["historical_count"] == 4
    assert summary["settled_count"] == 3
    assert summary["win_count"] == 1
    assert summary["hit_rate"] == 50.0
    assert summary["coverage_rate"] == 75.0
    assert summary["average_directional_return_bps"] == 16.6667


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

    class Scalars:
        def all(self):
            return [settled, missing]

    class Database:
        flushed = False

        def scalars(self, _statement):
            return Scalars()

        def flush(self):
            self.flushed = True

    class Repository:
        calls: list[tuple[str, str, int, int]] = []

        def kline_range(self, symbol, timeframe, start_ms, end_ms):
            self.calls.append((symbol, timeframe, start_ms, end_ms))
            if symbol == "AAPLUSDT":
                return [{"open_time": due_ms, "open": 102, "close": 102}]
            return []

        def _query(self, *_args, **_kwargs):
            raise AssertionError("current ticker must not be used for due settlement")

    database = Database()
    repository = Repository()

    result = settle_due_predictions(database, repository)

    assert result == {"completed": 1, "unavailable": 1}
    assert settled.status == "completed"
    assert settled.exit_price == Decimal("102.0")
    assert settled.result == "win"
    assert missing.status == "unavailable"
    assert missing.exit_price is None
    assert database.flushed is True
    assert {call[0] for call in repository.calls} == {"AAPLUSDT", "MSFTUSDT"}


def test_ai_monitor_config_normalizes_symbol_allowlist() -> None:
    config = AiMonitorConfigUpdate(monitor_symbols=[" nvdausdt ", "AAPLUSDT", "NVDAUSDT"])

    assert config.monitor_symbols == ["NVDAUSDT", "AAPLUSDT"]


def test_ai_monitor_prediction_table_is_separate_from_trading_orders() -> None:
    table = AiMonitorPrediction.__table__

    assert table.name == "ai_monitor_predictions"
    assert {"opportunity_id", "entry_price", "exit_price", "result", "due_at"} <= {
        column.name for column in table.columns
    }
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


def test_ai_monitor_prediction_migration_follows_workspace_revision() -> None:
    migration = (ROOT / "migrations/versions/0038_ai_monitor_predictions.py").read_text(
        encoding="utf-8"
    )

    assert 'down_revision: str | None = "0037_ai_monitor_workspace"' in migration
    assert '"ai_monitor_predictions"' in migration
    assert '"monitor_symbols_json"' in migration


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
    migration = (
        ROOT / "migrations/versions/0041_ai_monitor_pipeline_optimization.py"
    ).read_text(encoding="utf-8")

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
    assert 'status="discovered" if matched else "candidate"' in monitor
    assert 'AiMonitorOpportunity.status.in_(("candidate", "discovered"))' in api
    assert 'down_revision: str | None = "0039_news_ai_industries"' in migration
    assert "status IN ('candidate', 'discovered', 'expired', 'dismissed')" in migration


def test_opportunity_related_news_endpoint_is_tenant_scoped() -> None:
    api = (ROOT / "src/quantdesk_v2/interfaces/api/ai_monitor.py").read_text(encoding="utf-8")
    endpoint = api[api.index('@router.get("/opportunities/{opportunity_id}/news")') :]

    assert "AiMonitorOpportunity.public_id == opportunity_id" in endpoint
    assert "AiMonitorOpportunity.user_id == user.id" in endpoint
    assert "opportunity.news_ids_json" in endpoint
    assert "News.id.in_(news_ids)" in endpoint
    assert 'raise HTTPException(status_code=404, detail="opportunity not found")' in endpoint


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

    assert app.index('item.key === "monitor"') < app.index('key: "ai-monitor"')
    assert 'tag="ai-monitor-dashboard"' in app
    assert '"/assets/ai-monitor.js?v=20260810-26"' in entrypoint
    assert '"/assets/monitor.js?v=20260810-forecast-2"' in entrypoint
    assert '"ai-monitor": "发现机会"' in app
    assert '{ key: "ai-monitor", icon: "机", label: "发现机会" }' in app
    assert legacy_index.index('data-panel-target="monitor"') < legacy_index.index(
        'data-panel-target="ai-monitor"'
    )
    assert 'href="/ai-monitor" data-panel-target="ai-monitor"' in legacy_index
    assert 'data-panel="ai-monitor"' in legacy_index
    assert '<ai-monitor-dashboard id="ai-monitor-dashboard"></ai-monitor-dashboard>' in legacy_index
    assert 'src="/assets/ai-monitor.js?v=20260810-26"' in legacy_index
    assert '"ai-monitor": "/ai-monitor"' in legacy_app
    assert 'selected === "ai-monitor" && typeof aiMonitor.start === "function"' in legacy_app
    assert 'selected !== "ai-monitor" && typeof aiMonitor.pause === "function"' in legacy_app
    assert 'classList.toggle("ai-monitor-mode", selected === "ai-monitor")' in legacy_app
    assert ".workspace-content.ai-monitor-mode" in legacy_styles
    for asset in (
        "/assets/style.css?v=20260810-font1_6x-1",
        "/assets/strategies.css?v=20260810-font1_6x-1",
        "/assets/terminal.css?v=20260810-font1_6x-1",
        "/assets/paper.js?v=20260809-paper-combo-1",
        "/assets/live.js?v=20260809-font1_6x-1",
        "/assets/backtest.js?v=20260809-font1_6x-1",
        "/assets/app.js?v=20260810-ai-monitor-1",
    ):
        assert asset in legacy_index
    assert 'view: "opportunities"' in component
    assert 'class="ai-nav-root active"' in component
    assert 'class="ai-subnav-group"' in component
    assert "<p><strong>发现机会</strong><small>二级菜单</small></p>" not in component
    assert component.index('class="ai-nav-root active"') < component.index(
        'class="ai-subnav-group"'
    )
    assert component.index('class="ai-subnav-group"') < component.index("<strong>新闻列表</strong>")
    assert ".ai-subnav-group .ai-nav-child" in stylesheet
    for label in ("新闻列表", "分析记录", "指标配置", "发现机会", "预测统计分析"):
        assert label in component
    assert component.index('id="run-opportunity"') < component.index('id="open-config"')
    assert component.index('id="open-config"') < component.index('id="ai-refresh"')
    assert (
        'this.q("#open-config").addEventListener("click", () => this.openConfig("indicators"));'
        in component
    )
    assert 'id="open-news-config"' in component
    assert 'this.openConfig("news")' in component
    assert 'data-analyze-news="${this.escape(item.id)}"' in component
    assert 'this.api("/news/analyze"' in component
    assert "await this.waitForRun(run.id)" in component
    assert ".news-analyze-action" in stylesheet
    assert 'data-ai-view="config"' not in component
    assert "所选指标全部满足后" in component
    assert "监控品种" in component
    assert "每 15 分钟从数据库读取最新 10 条未分析新闻" in component
    assert "related_industries" in component
    assert "等待全部指标确认" in component
    assert "暂无技术行情" in component
    assert 'data-opportunity-tab="current"' in component
    assert 'data-opportunity-tab="history"' in component
    assert "当前机会" in component
    assert "历史机会" in component
    assert 'id="include-expired"' not in component
    assert 'tab === "history" ? !active : active' in component
    assert "this.parseDate(item.expires_at).getTime() > now" in component
    assert "`${raw}Z`" in component
    assert "if (!unique.has(instrument)) unique.set(instrument, item)" in component
    assert 'id="current-direction-counts"' in component
    assert "opportunityDirectionCounts" in component
    assert "做多 ${counts.long} 个，做空 ${counts.short} 个" in component
    assert ".opportunity-tabs .direction-counts .long" in stylesheet
    assert ".opportunity-tabs .direction-counts .short" in stylesheet
    assert "已剔除行情不足" in component
    assert ".opportunity-tabs" in stylesheet
    for label in ("信号时间", "信号有效期间", "买入方向", "买入价格"):
        assert label in component
    assert 'id="opportunity-research" research-only' in component
    assert 'data-open-contract="${this.escape(item.contract_symbol)}"' in component
    assert 'data-opportunity-id="${this.escape(item.id)}"' in component
    assert "research.openResearch(button.dataset.openContract" in component
    assert 'data-ai-conclusion="${this.escape(item.id)}"' in component
    assert component.index('${symbolControl}<small>') < component.index('${conclusionControl}</div>')
    assert "AI分析结论" in component
    assert 'id="ai-conclusion-modal"' in component
    assert "openAiConclusion(opportunityId, trigger)" in component
    assert "closeAiConclusion()" in component
    assert "AI 新闻研判" in component
    assert "技术指标验证" in component
    assert "仅作虚拟预测研究，不会触发实盘交易" in component
    assert 'data-conclusion-view="news"' in component
    assert 'data-conclusion-view="analysis"' in component
    assert 'data-conclusion-view="fundamentals"' in component
    assert component.index('data-conclusion-view="fundamentals"') < component.index(
        'data-conclusion-view="news"'
    )
    assert "基本面信息" in component
    assert "相关新闻列表" in component
    assert 'this.api(`/opportunities/${encodeURIComponent(item.id)}/news`)' in component
    assert 'this.api(`/opportunities/${encodeURIComponent(item.id)}/fundamentals`)' in component
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
    assert ".ai-conclusion-dialog" in stylesheet
    assert "combined_score: opportunity.combined_score" in component
    assert "technical_confirmed: evidence.confirmed === true" in component
    assert "outcome_result: opportunity.outcome?.result" in component
    assert 'tab === "history"' in component
    assert 'class="history-result ${this.escape(outcomeResult)}"' in component
    assert "是否命中" in component
    assert ".opportunity-metrics.with-result" in stylesheet
    assert ".history-result.win" in stylesheet
    assert 'this.api("/opportunity-analytics?limit=300")' in component
    assert 'this.api("/prediction-records?limit=200")' not in component
    for label in ("历史机会", "有效样本", "命中次数", "命中概率", "平均方向收益"):
        assert label in component
    assert "仅作虚拟统计" in (ROOT / "src/quantdesk_v2/ai_monitor.py").read_text(encoding="utf-8")
    assert '@router.get("/opportunity-analytics")' in (
        ROOT / "src/quantdesk_v2/interfaces/api/ai_monitor.py"
    ).read_text(encoding="utf-8")
    assert ".analytics-summary" in stylesheet
    assert ".ai-module-nav" in stylesheet
