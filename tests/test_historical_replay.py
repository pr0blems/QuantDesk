from __future__ import annotations

import csv
import io
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from sqlalchemy import ForeignKeyConstraint, UniqueConstraint

from quantdesk_v2 import ai_monitor, historical_replay
from quantdesk_v2.historical_replay import (
    CONSERVATIVE_COST_MODEL,
    _download,
    _historical_exit_decision,
    _historical_indicator_snapshot,
    _historical_news_snapshot,
    _historical_score_observation,
    _months,
    _parse_archive,
    _replay_symbol,
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


def test_historical_exit_requires_two_consecutive_low_closed_bars() -> None:
    start = 1_800_000_000_000
    interval = 900_000
    candles = [
        {
            "open_time": start + index * interval,
            "open": 100 + index,
            "high": 102 + index,
            "low": 99 + index,
            "close": 101 + index,
        }
        for index in range(4)
    ]
    risk_plan = {"stop_loss_price": 50, "take_profit_price": 150}
    one_low = _historical_exit_decision(
        candles[:1],
        [
            {
                "price_time_ms": start + interval,
                "combined": 64,
                "direction": "long",
            }
        ],
        entry_price=100,
        direction="long",
        risk_plan=risk_plan,
        start_ms=start,
        due_ms=start + interval,
        timeframe_ms=interval,
        exit_threshold=65,
    )
    confirmed = _historical_exit_decision(
        candles,
        [
            {
                "price_time_ms": start + interval,
                "combined": 64,
                "direction": "long",
            },
            {
                "price_time_ms": start + interval * 2,
                "combined": 70,
                "direction": "long",
            },
            {
                "price_time_ms": start + interval * 3,
                "combined": 64,
                "direction": "long",
            },
            {
                "price_time_ms": start + interval * 4,
                "combined": 63,
                "direction": "long",
            },
        ],
        entry_price=100,
        direction="long",
        risk_plan=risk_plan,
        start_ms=start,
        due_ms=start + interval * 4,
        timeframe_ms=interval,
        exit_threshold=65,
    )

    assert one_low is not None and one_low["reason"] == "max_holding_time"
    assert confirmed is not None and confirmed["reason"] == "score_breakdown"
    assert confirmed["confirmation_points"] == 2
    assert confirmed["price_time_ms"] == start + interval * 4
    assert confirmed["price"] == 104


def test_historical_exit_reversal_is_immediate_and_precedes_future_barrier() -> None:
    start = 1_800_000_000_000
    interval = 900_000
    decision = _historical_exit_decision(
        [
            {
                "open_time": start,
                "open": 100,
                "high": 101,
                "low": 99,
                "close": 100.5,
            },
            {
                "open_time": start + interval,
                "open": 100.5,
                "high": 106,
                "low": 100,
                "close": 105,
            },
        ],
        [
            {
                "price_time_ms": start + interval,
                "combined": 80,
                "direction": "short",
            }
        ],
        entry_price=100,
        direction="long",
        risk_plan={"stop_loss_price": 98, "take_profit_price": 104},
        start_ms=start,
        due_ms=start + interval * 2,
        timeframe_ms=interval,
        exit_threshold=65,
    )

    assert decision is not None and decision["reason"] == "score_reversal"
    assert decision["confirmation_points"] == 1
    assert decision["price_time_ms"] == start + interval
    assert decision["price"] == 100.5
    assert decision["observed_bar_count"] == 1


def test_historical_score_observation_marks_only_confirmed_opposite_direction() -> None:
    observation = _historical_score_observation(
        [
            {"id": "long", "ts": 90, "direction": "long", "score": 80},
            {"id": "short", "ts": 95, "direction": "short", "score": 90},
            {"id": "future", "ts": 101, "direction": "short", "score": 100},
        ],
        {
            "items": [
                {
                    "key": "moving_average_bull",
                    "bullish_strength": 10,
                    "bearish_strength": 95,
                    "bullish_triggered": False,
                    "bearish_triggered": True,
                }
            ]
        },
        held_direction="long",
        observed_at_seconds=100,
        configured_keys=["moving_average_bull"],
        minimum_news_score=60,
        minimum_news_mentions=1,
        news_lookback_seconds=20,
        minimum_indicator_score=65,
        minimum_combined_score=70,
        news_weight=45,
        technical_weight=35,
    )

    assert observation == {
        "price_time_ms": 100_000,
        "direction": "short",
        "news": 90.0,
        "technical": 95.0,
        "combined": 92.1875,
    }


def test_historical_exit_barrier_wins_tie_and_does_not_read_due_open_bar() -> None:
    start = 1_800_000_000_000
    interval = 900_000
    barrier = _historical_exit_decision(
        [
            {
                "open_time": start,
                "open": 100,
                "high": 105,
                "low": 99,
                "close": 100.5,
            }
        ],
        [
            {
                "price_time_ms": start + interval,
                "combined": 80,
                "direction": "short",
            }
        ],
        entry_price=100,
        direction="long",
        risk_plan={"stop_loss_price": 98, "take_profit_price": 104},
        start_ms=start,
        due_ms=start + interval,
        timeframe_ms=interval,
        exit_threshold=65,
    )
    capped = _historical_exit_decision(
        [
            {
                "open_time": start,
                "open": 100,
                "high": 101,
                "low": 99,
                "close": 100.5,
            },
            {
                "open_time": start + interval,
                "open": 100.5,
                "high": 120,
                "low": 80,
                "close": 90,
            },
        ],
        [
            {
                "price_time_ms": start + interval,
                "combined": 70,
                "direction": "long",
            }
        ],
        entry_price=100,
        direction="long",
        risk_plan={"stop_loss_price": 98, "take_profit_price": 104},
        start_ms=start,
        due_ms=start + interval,
        timeframe_ms=interval,
        exit_threshold=65,
    )

    assert barrier is not None and barrier["reason"] == "take_profit"
    assert capped is not None and capped["reason"] == "max_holding_time"
    assert capped["price_time_ms"] == start + interval
    assert capped["price"] == 100.5
    assert capped["observed_bar_count"] == 1


def test_one_hour_replay_reaches_two_low_score_exit_on_closed_15m_bars(
    monkeypatch,
) -> None:
    hour_ms = 3_600_000
    quarter_hour_ms = 900_000
    start = 1_800_000_000_000
    signal_index = 119
    signal_open_ms = start + signal_index * hour_ms
    entry_at_ms = signal_open_ms + hour_ms
    candles = [
        {
            "open_time": start + index * hour_ms,
            "open": 100,
            "high": 101,
            "low": 99,
            "close": 100,
            "volume": 1_000,
        }
        for index in range(122)
    ]
    exit_candles = [
        {
            "open_time": entry_at_ms + index * quarter_hour_ms,
            "open": 100 + index / 10,
            "high": 100.5,
            "low": 99.5,
            "close": 100.1 + index / 10,
            "volume": 250,
        }
        for index in range(4)
    ]
    news = [
        {
            "id": "entry",
            "ts": signal_open_ms // 1_000 + 1,
            "direction": "long",
            "score": 100,
        },
        {
            "id": "weakening",
            "ts": entry_at_ms // 1_000 + 60,
            "direction": "long",
            "score": 60,
        },
    ]

    evaluated_signal_ends: list[int] = []

    def fake_evaluate(items, _timeframe):  # noqa: ANN001
        evaluated_signal_ends.append(int(items[-1]["open_time"]))
        return {
            "items": [
                {
                    "key": "moving_average_bull",
                    "bullish_strength": 80,
                    "bearish_strength": 0,
                    "bullish_triggered": True,
                    "bearish_triggered": False,
                }
            ]
        }

    monkeypatch.setattr(
        historical_replay,
        "evaluate_directional_strategy_indicators",
        fake_evaluate,
    )
    monkeypatch.setattr(
        historical_replay.ai_monitor,
        "virtual_risk_plan_snapshot",
        lambda **_kwargs: {"stop_loss_price": 50, "take_profit_price": 150},
    )

    class Database:
        def __init__(self) -> None:
            self.added = []

        def scalar(self, _statement):
            return (
                1
                if any(isinstance(item, AiMonitorReplaySignal) for item in self.added)
                else None
            )

        def add(self, item) -> None:  # noqa: ANN001
            self.added.append(item)

        def flush(self) -> None:
            for item in self.added:
                if isinstance(item, AiMonitorReplaySignal) and item.id is None:
                    item.id = 1

    database = Database()
    run = SimpleNamespace(
        id=7,
        user_id=9,
        timeframe="1h",
        out_of_sample_start_at=datetime(2100, 1, 1),
        config_snapshot_json={
            "minimum_news_confidence": 0.6,
            "minimum_news_mentions": 1,
            "news_lookback_hours": 24,
            "minimum_indicator_score": 65,
            "minimum_combined_score": 90,
            "indicator_keys": ["moving_average_bull"],
            "news_score_weight": 50,
            "technical_score_weight": 50,
        },
    )

    generated = _replay_symbol(
        database,
        run,
        "AAPL",
        "AAPLUSDT",
        news,
        candles,
        exit_candles,
    )

    signal = next(
        item for item in database.added if isinstance(item, AiMonitorReplaySignal)
    )
    outcome = next(
        item for item in database.added if isinstance(item, AiMonitorReplayOutcome)
    )
    assert generated == 1
    assert signal.entry_at == datetime.fromtimestamp(
        entry_at_ms / 1_000, UTC
    ).replace(tzinfo=None)
    assert signal.due_at == datetime.fromtimestamp(
        (entry_at_ms + hour_ms) / 1_000, UTC
    ).replace(tzinfo=None)
    assert outcome.settlement_json["exit_reason"] == "score_breakdown"
    assert outcome.settlement_json["score_at_exit"]["confirmation_points"] == 2
    assert outcome.exit_at == datetime.fromtimestamp(
        (entry_at_ms + quarter_hour_ms * 2) / 1_000, UTC
    ).replace(tzinfo=None)
    assert round(float(outcome.exit_price), 4) == 100.2
    # Both 15m score observations before the exit use only the previously
    # closed 1h signal bar, never the still-open entry bar.
    assert evaluated_signal_ends[:3] == [signal_open_ms] * 3


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


def test_prediction_exit_migration_follows_replay_and_backfills_legacy_rows() -> None:
    migration = (
        ROOT / "migrations/versions/0051_ai_prediction_exit_lifecycle.py"
    ).read_text(encoding="utf-8")

    assert 'down_revision: str | None = "0050_ai_historical_replay"' in migration
    assert '"exit_at"' in migration
    assert '"exit_reason"' in migration
    assert "legacy_horizon_close" in migration


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
