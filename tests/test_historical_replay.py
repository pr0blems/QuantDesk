from __future__ import annotations

import csv
import io
import zipfile
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import ForeignKeyConstraint, UniqueConstraint

from quantdesk_v2 import ai_monitor, historical_replay
from quantdesk_v2.historical_replay import (
    CONSERVATIVE_COST_MODEL,
    _download,
    _historical_indicator_snapshot,
    _historical_news_snapshot,
    _months,
    _parse_archive,
    _sample_split,
    _select_replay_symbols,
)
from quantdesk_v2.models import (
    AiMonitorReplayOutcome,
    AiMonitorReplayRun,
    AiMonitorReplaySignal,
)

ROOT = Path(__file__).resolve().parents[1]


def test_archive_download_retries_transient_network_errors(monkeypatch) -> None:
    attempts = 0

    def fake_urlopen(_request, timeout):  # noqa: ANN001, ARG001
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise historical_replay.urllib.error.URLError("temporary")
        return io.BytesIO(b"verified archive")

    monkeypatch.setattr(historical_replay.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(historical_replay.time, "sleep", lambda _seconds: None)

    assert (
        _download("https://data.binance.vision/example.zip", max_bytes=100)
        == b"verified archive"
    )
    assert attempts == 3


def test_binance_archive_parser_skips_header_and_keeps_ohlcv() -> None:
    csv_buffer = io.StringIO(newline="")
    writer = csv.writer(csv_buffer)
    writer.writerow(["open_time", "open", "high", "low", "close", "volume"])
    writer.writerow(["1700000000000", "10", "12", "9", "11", "100"])
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w") as bundle:
        bundle.writestr("AAPLUSDT-1h-2026-07.csv", csv_buffer.getvalue())

    assert _parse_archive(payload.getvalue()) == [
        (1700000000000, 10.0, 12.0, 9.0, 11.0, 100.0)
    ]


def test_sample_split_has_two_bar_embargo() -> None:
    oos_start = datetime(2026, 7, 1, 0, 0)

    assert _sample_split(oos_start, oos_start, "1h") == "oos"
    assert _sample_split(oos_start - timedelta(hours=1), oos_start, "1h") == "embargo"
    assert _sample_split(oos_start - timedelta(hours=3), oos_start, "1h") == "train"


def test_historical_indicator_snapshot_never_proxies_missing_prediction_feature() -> None:
    scan = {
        "items": [
            {
                "key": "moving_average_bull",
                "bullish_strength": 88,
                "bearish_strength": 22,
                "bullish_triggered": True,
                "bearish_triggered": False,
            }
        ]
    }
    snapshot = _historical_indicator_snapshot(
        scan, ["moving_average_bull", "prediction_trend"], "long"
    )

    assert snapshot["score"] == 88
    assert snapshot["policy_passed"] is True
    assert snapshot["available_keys"] == ["moving_average_bull"]
    assert snapshot["unavailable_keys"] == ["prediction_trend"]
    assert snapshot["prediction_feature_proxy_used"] is False


def test_historical_indicator_snapshot_requires_the_live_grouped_policy() -> None:
    scan = {
        "items": [
            {
                "key": "moving_average_bull",
                "bullish_strength": 99,
                "bearish_strength": 1,
                "bullish_triggered": False,
                "bearish_triggered": False,
            }
        ]
    }

    snapshot = _historical_indicator_snapshot(scan, ["moving_average_bull"], "long")

    assert snapshot["score"] == 99
    assert snapshot["policy_passed"] is False


def test_historical_news_snapshot_enforces_mentions_lookback_and_point_in_time() -> None:
    news = [
        {"id": "old", "ts": 10, "direction": "long", "score": 90},
        {"id": "one", "ts": 90, "direction": "long", "score": 80},
        {"id": "two", "ts": 100, "direction": "long", "score": 70},
        {"id": "future", "ts": 101, "direction": "long", "score": 99},
        {"id": "short", "ts": 100, "direction": "short", "score": 99},
    ]

    snapshot = _historical_news_snapshot(
        news,
        direction="long",
        signal_at_seconds=100,
        minimum_score=60,
        minimum_mentions=2,
        lookback_seconds=20,
    )

    assert [item["id"] for item in snapshot] == ["one", "two"]
    assert (
        _historical_news_snapshot(
            news,
            direction="long",
            signal_at_seconds=100,
            minimum_score=60,
            minimum_mentions=3,
            lookback_seconds=20,
        )
        == []
    )


def test_replay_symbol_selection_prefers_explicit_then_tenant_config() -> None:
    catalog = [
        {"symbol": "AAPL", "contract_symbol": "AAPLUSDT"},
        {"symbol": "NVDA", "contract_symbol": "NVDAUSDT"},
    ]

    configured = _select_replay_symbols(
        catalog, requested=[], configured=["NVDAUSDT"]
    )
    explicit = _select_replay_symbols(
        catalog, requested=["AAPL"], configured=["NVDAUSDT"]
    )
    unrestricted = _select_replay_symbols(catalog, requested=[], configured=[])

    assert [item["symbol"] for item in configured] == ["NVDA"]
    assert [item["symbol"] for item in explicit] == ["AAPL"]
    assert unrestricted == catalog


def test_replay_schema_enforces_active_run_and_exact_signal_run_tenant() -> None:
    run_uniques = {
        constraint.name: tuple(column.name for column in constraint.columns)
        for constraint in AiMonitorReplayRun.__table__.constraints
        if isinstance(constraint, UniqueConstraint)
    }
    signal_uniques = {
        constraint.name: tuple(column.name for column in constraint.columns)
        for constraint in AiMonitorReplaySignal.__table__.constraints
        if isinstance(constraint, UniqueConstraint)
    }
    outcome_foreign_keys = {
        constraint.name: (
            tuple(column.name for column in constraint.columns),
            tuple(element.target_fullname for element in constraint.elements),
        )
        for constraint in AiMonitorReplayOutcome.__table__.constraints
        if isinstance(constraint, ForeignKeyConstraint)
    }

    assert run_uniques["uq_ai_monitor_replay_runs_active_user"] == (
        "active_user_id",
    )
    assert signal_uniques["uq_ai_monitor_replay_signals_id_run_user"] == (
        "id",
        "run_id",
        "user_id",
    )
    assert outcome_foreign_keys["fk_ai_replay_outcome_signal_run_user"] == (
        ("signal_id", "run_id", "user_id"),
        (
            "ai_monitor_replay_signals.id",
            "ai_monitor_replay_signals.run_id",
            "ai_monitor_replay_signals.user_id",
        ),
    )


def test_replay_migration_follows_score_weights_and_keeps_integrity_guards() -> None:
    migration = (
        ROOT / "migrations/versions/0050_ai_monitor_historical_replay.py"
    ).read_text(encoding="utf-8")

    assert 'down_revision: str | None = "0049_ai_score_weights"' in migration
    assert '"uq_ai_monitor_replay_runs_active_user"' in migration
    assert '"fk_ai_replay_outcome_signal_run_user"' in migration


def test_readiness_cost_model_cannot_be_disabled() -> None:
    disabled = {
        "prediction_fee_enabled": False,
        "prediction_fee_bps_per_side": 0,
        "prediction_slippage_enabled": False,
        "prediction_slippage_bps_per_side": 0,
        "prediction_funding_enabled": False,
        "prediction_funding_bps_per_8h": 0,
    }
    forced = ai_monitor.readiness_cost_config(disabled)
    breakdown = ai_monitor.prediction_cost_breakdown(
        datetime(2026, 1, 1), datetime(2026, 1, 1, 1), forced
    )

    assert forced["prediction_fee_enabled"] is True
    assert forced["prediction_slippage_enabled"] is True
    assert forced["prediction_funding_enabled"] is True
    assert breakdown["total_cost_bps"] == 16.125
    assert CONSERVATIVE_COST_MODEL["forced_for_readiness"] is True


def test_month_range_is_inclusive() -> None:
    assert _months(datetime(2025, 12, 31), datetime(2026, 2, 1)) == [
        (2025, 12),
        (2026, 1),
        (2026, 2),
    ]
