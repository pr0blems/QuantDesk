from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import ForeignKeyConstraint, MetaData, Table, UniqueConstraint

from quantdesk_v2 import battle
from quantdesk_v2.schemas import PredictionAlgorithmUpdate


def _features(direction: float = 1.0) -> dict[str, float]:
    return {
        "data_quality": 1.0,
        "micro_age_ms": 1_000,
        "positioning_age_ms": 60_000,
        "aggressive_flow": 0.8 * direction,
        "book_imbalance": 0.7 * direction,
        "book_imbalance_5": 0.6 * direction,
        "depth_levels": 100,
        "velocity": 0.6 * direction,
        "flash_imbalance": 0.5 * direction,
        "taker_flow": 0.7 * direction,
        "price_oi_impulse": 0.5 * direction,
        "trend_15m": 0.7 * direction,
        "trend_1h": 0.6 * direction,
        "trend_4h": 0.5 * direction,
        "account_crowding": 0.0,
        "funding_crowding": 0.0,
        "realized_volatility_bps": 12.0,
    }


def _with_kline_strategies(
    features: dict[str, object], timeframe: str = "15m"
) -> dict[str, object]:
    features["kline_strategies"] = {
        timeframe: {
            "triggered_count": len(battle.KLINE_STRATEGY_FEATURES),
            "available_count": len(battle.KLINE_STRATEGY_FEATURES),
            "values": {name: 1.0 for name in battle.KLINE_STRATEGY_FEATURES},
        }
    }
    return features


@pytest.mark.parametrize("horizon", battle.HORIZONS)
def test_battle_prediction_is_three_way_and_explicitly_heuristic(horizon: int) -> None:
    bullish = battle.predict(_features(), horizon)
    bearish = battle.predict(_features(-1), horizon)

    assert bullish["result"] == "long"
    assert bearish["result"] == "short"
    assert bullish["prediction_state"] == "heuristic"
    assert bullish["confidence_label"] in {"low", "medium"}
    assert sum(
        bullish[name] for name in ("long_probability", "short_probability", "neutral_probability")
    ) == pytest.approx(1.0)
    assert bullish["long_probability"] > bullish["short_probability"]
    assert bearish["short_probability"] > bearish["long_probability"]


def test_battle_prediction_abstains_when_market_data_is_stale() -> None:
    features = _features()
    features["micro_age_ms"] = battle.MAX_MARKET_AGE_MS + 1

    result = battle.predict(features, 900)

    assert result["prediction_state"] == "data_insufficient"
    assert result["result"] == "neutral"
    assert result["neutral_probability"] == pytest.approx(0.8)
    assert "DATA_INSUFFICIENT" in result["reason_codes"]


def test_custom_direction_threshold_changes_new_prediction_only() -> None:
    features = _with_kline_strategies(_features(0.3))
    config = battle.default_algorithm_config()
    config["direction_threshold"] = 0.5
    config["config_version"] = 7

    assert battle.predict(features, 900)["result"] == "long"
    adjusted = battle.predict(features, 900, config)

    assert adjusted["result"] == "neutral"
    assert adjusted["components"]["algorithm_config_version"] == 7
    assert adjusted["components"]["direction_threshold"] == 0.5


def test_prediction_algorithm_schema_rejects_invalid_weight_total() -> None:
    config = battle.default_algorithm_config()
    config["weights"]["5m"]["trend"] = 0.5

    with pytest.raises(ValueError, match="weights must sum to 1"):
        PredictionAlgorithmUpdate.model_validate(config)


def test_default_algorithm_has_eight_market_and_twelve_kline_features() -> None:
    config = battle.default_algorithm_config()

    assert len(battle.MARKET_ALGORITHM_FEATURES) == 8
    assert len(battle.KLINE_STRATEGY_FEATURES) == 12
    assert len(battle.ALGORITHM_FEATURES) == 20
    for weights in config["weights"].values():
        assert sum(weights.values()) == pytest.approx(1.0)
        assert sum(weights[name] for name in battle.MARKET_ALGORITHM_FEATURES) == pytest.approx(
            0.76
        )
        assert sum(weights[name] for name in battle.KLINE_STRATEGY_FEATURES) == pytest.approx(0.24)


def test_legacy_eight_feature_config_is_migrated_without_losing_relative_weights() -> None:
    legacy = battle.default_algorithm_config()
    for horizon in legacy["weights"]:
        legacy["weights"][horizon] = {
            name: battle.DEFAULT_ALGORITHM_CONFIG["weights"][horizon][name] / 0.76
            for name in battle.MARKET_ALGORITHM_FEATURES
        }

    migrated = battle.normalize_algorithm_config(legacy)

    for weights in migrated["weights"].values():
        assert sum(weights.values()) == pytest.approx(1.0)
        assert all(weights[name] == pytest.approx(0.02) for name in battle.KLINE_STRATEGY_FEATURES)


def test_triggered_kline_strategies_contribute_to_prediction_score() -> None:
    plain = battle.predict(_features(0.1), 900)
    enhanced = battle.predict(_with_kline_strategies(_features(0.1)), 900)

    assert enhanced["battle_score"] > plain["battle_score"]
    assert enhanced["components"]["kline_strategy_triggered_count"] == 12
    assert enhanced["components"]["kline_bollinger_breakout"] == pytest.approx(0.02)


def test_kline_strategy_scan_maps_only_complete_triggers_to_one() -> None:
    candles = [
        {
            "open_time": index * 900_000,
            "open": 100.0,
            "high": 101.0,
            "low": 99.0,
            "close": 100.0,
            "volume": 100.0,
        }
        for index in range(79)
    ]
    candles.append(
        {
            "open_time": 79 * 900_000,
            "open": 120.0,
            "high": 132.0,
            "low": 119.0,
            "close": 130.0,
            "volume": 200.0,
        }
    )

    result = battle.evaluate_kline_strategy_features(candles, "15m")

    assert result["candle_count"] == 80
    assert result["values"]["kline_bollinger_breakout"] == 1.0
    assert result["values"]["kline_trend_breakout"] == 1.0
    assert result["values"]["kline_new_low_reversal"] == 0.0


def test_runtime_algorithm_config_reads_persisted_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = battle.default_algorithm_config()
    config["direction_threshold"] = 0.27
    monkeypatch.setattr(
        battle.store,
        "query",
        lambda *_args, **_kwargs: [{"value_json": config, "version": 4}],
    )
    battle.invalidate_algorithm_config_cache()

    loaded = battle.current_algorithm_config()

    assert loaded["direction_threshold"] == 0.27
    assert loaded["config_version"] == 4
    battle.invalidate_algorithm_config_cache()


def test_feature_vector_normalizes_flashes_and_price_open_interest() -> None:
    now_ms = 1_800_000
    current = {
        "snapshot_at_ms": now_ms,
        "open_interest": 110,
        "mark_price": 101,
        "global_long_short_ratio": 1.2,
        "taker_buy_sell_ratio": 1.5,
        "quality_json": {
            "open_interest": True,
            "account_ratio": True,
            "taker": True,
        },
    }
    previous = {"open_interest": 100, "mark_price": 100}
    micro = {
        "received_at": now_ms - 1_000,
        "book_imbalance": 0.2,
        "book_imbalance_5": 0.3,
        "depth_levels": 100,
        "aggressive_buy_ratio": 0.6,
        "price_velocity_bps_60s": 4,
        "realized_volatility_60s": 8,
        "spread_bps": 1.5,
    }

    features, quality = battle.build_feature_vector(
        positioning=current,
        previous_positioning=previous,
        microstructure=micro,
        scores={"15m": 50, "1h": 40, "4h": 20},
        up_count=90,
        down_count=30,
        now_ms=now_ms,
    )

    assert quality == 1.0
    assert features["flash_imbalance"] == pytest.approx(0.5)
    assert features["price_oi_impulse"] > 0
    assert features["taker_flow"] > 0


def test_binance_positioning_client_uses_public_tradfi_endpoints(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def fake_get(url: str, **_: object):
        calls.append(url)
        return [] if "/futures/data/" in url else {}

    monkeypatch.setattr(battle.binance_client, "_get", fake_get)

    battle.binance_client.fetch_open_interest("aaplusdt")
    battle.binance_client.fetch_global_long_short_ratio("aaplusdt")
    battle.binance_client.fetch_taker_buy_sell_ratio("aaplusdt")

    assert any("/fapi/v1/openInterest?" in url and "AAPLUSDT" in url for url in calls)
    assert any("/futures/data/globalLongShortAccountRatio?" in url for url in calls)
    assert any("/futures/data/takerlongshortRatio?" in url for url in calls)


def test_local_microstructure_abstains_without_real_depth_or_positioning() -> None:
    now_ms = 1_800_000
    micro = battle._local_microstructure(
        {"15m": 60, "1h": 40, "4h": 20},
        {"price": 100, "pct_24h": 3.5, "ts": now_ms // 1_000},
        now_ms,
    )
    features, quality = battle.build_feature_vector(
        positioning={
            "snapshot_at_ms": now_ms,
            "mark_price": 100,
            "quality_json": {},
        },
        previous_positioning=None,
        microstructure=micro,
        scores={"15m": 60, "1h": 40, "4h": 20},
        up_count=60,
        down_count=0,
        now_ms=now_ms,
    )

    assert micro["depth_levels"] == 0
    assert micro["book_imbalance"] == 0
    assert quality == pytest.approx(0.6)
    result = battle.predict(features, 900)
    assert result["prediction_state"] == "data_insufficient"
    assert result["result"] == "neutral"


def test_local_microstructure_does_not_invent_freshness_without_ticker() -> None:
    micro = battle._local_microstructure(
        {"15m": 100, "1h": 100},
        {"price": None, "pct_24h": None, "ts": None},
        1_800_000,
    )

    assert micro["received_at"] == 0
    assert micro["depth_levels"] == 0
    assert micro["aggressive_buy_ratio"] == pytest.approx(0.5)


def test_real_microstructure_converts_seconds_to_prediction_milliseconds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[tuple[str, tuple[str, ...]]] = []

    def query(sql: str, params: tuple[str, ...]):
        captured.append((sql, params))
        return [
            {
                "symbol": "AAPLUSDT",
                "book_imbalance": 0.2,
                "book_imbalance_5": 0.1,
                "depth_levels": 100,
                "ts": 1_800_000_000,
                "received_at": 1_800_000_000_000,
            }
        ]

    monkeypatch.setattr(battle.store, "query", query)

    snapshot = battle._market_microstructure("AAPLUSDT", 1_800_000_001_000)

    assert snapshot is not None
    assert snapshot["received_at"] == 1_800_000_000_000
    assert captured[0][1] == ("AAPLUSDT",)
    assert "ts*1000 AS received_at" in captured[0][0]


def test_real_microstructure_rejects_stale_depth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        battle.store,
        "query",
        lambda *_args: [
            {
                "symbol": "AAPLUSDT",
                "book_imbalance": 0.9,
                "book_imbalance_5": 0.9,
                "depth_levels": 100,
                "received_at": 1_800_000_000_000,
            }
        ],
    )

    snapshot = battle._market_microstructure(
        "AAPLUSDT", 1_800_000_000_000 + battle.MAX_DEPTH_AGE_MS + 1
    )

    assert snapshot is None


def test_battle_migration_preserves_model_and_feature_versions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migration_path = (
        Path(__file__).parents[1] / "migrations" / "versions" / "0022_battle_prediction.py"
    )
    spec = importlib.util.spec_from_file_location(
        "battle_prediction_migration_0022", migration_path
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    assert module.MODEL_KEY == battle.MODEL_KEY
    assert module.MODEL_VERSION == 1
    assert module.FEATURE_SCHEMA_VERSION == 4

    metadata = MetaData()
    tables: dict[str, Table] = {}

    def capture_table(name: str, *elements: object, **kwargs: object) -> Table:
        table = Table(name, metadata, *elements, **kwargs)
        tables[name] = table
        return table

    fake_op = SimpleNamespace(
        get_bind=lambda: SimpleNamespace(dialect=SimpleNamespace(name="mysql")),
        create_table=capture_table,
        create_index=lambda *_args, **_kwargs: None,
        execute=lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(module, "op", fake_op)
    module.upgrade()

    model_table = tables["prediction_model_versions"]
    assert tuple(model_table.primary_key.columns.keys()) == ("model_key", "version")

    feature_table = tables["prediction_feature_snapshots"]
    feature_unique = next(
        constraint
        for constraint in feature_table.constraints
        if isinstance(constraint, UniqueConstraint)
        and constraint.name == "uq_prediction_feature_symbol_time_schema"
    )
    assert tuple(feature_unique.columns.keys()) == (
        "symbol",
        "as_of_ms",
        "feature_schema_version",
    )

    prediction_table = tables["battle_predictions"]
    model_fk = next(
        constraint
        for constraint in prediction_table.constraints
        if isinstance(constraint, ForeignKeyConstraint)
        and constraint.name == "fk_battle_predictions_model_version"
    )
    assert tuple(model_fk.columns.keys()) == ("model_key", "model_version")
    assert tuple(element.target_fullname for element in model_fk.elements) == (
        "prediction_model_versions.model_key",
        "prediction_model_versions.version",
    )


def test_kline_strategy_migration_registers_current_model_version() -> None:
    migration_path = (
        Path(__file__).parents[1]
        / "migrations"
        / "versions"
        / "0035_battle_kline_strategy_features.py"
    )
    spec = importlib.util.spec_from_file_location("battle_kline_features_0035", migration_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    assert module.MODEL_KEY == battle.MODEL_KEY
    assert module.MODEL_VERSION == battle.MODEL_VERSION
    assert module.FEATURE_SCHEMA_VERSION == battle.FEATURE_SCHEMA_VERSION


def test_kline_strategy_migration_downgrade_deletes_derived_data_before_registry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migration_path = (
        Path(__file__).parents[1]
        / "migrations"
        / "versions"
        / "0035_battle_kline_strategy_features.py"
    )
    spec = importlib.util.spec_from_file_location("battle_kline_features_0035", migration_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    executed: list[object] = []
    bind = SimpleNamespace(
        dialect=SimpleNamespace(name="mysql"),
        execute=lambda _statement: [
            SimpleNamespace(_mapping={"Column_name": "model_key"}),
            SimpleNamespace(_mapping={"Column_name": "version"}),
        ],
    )
    fake_op = SimpleNamespace(
        get_bind=lambda: bind,
        execute=lambda statement: executed.append(statement),
    )
    monkeypatch.setattr(module, "op", fake_op)

    module.downgrade()

    statements = [" ".join(str(statement).split()) for statement in executed]
    assert len(statements) == 3
    assert statements[0].startswith("DELETE FROM battle_predictions")
    assert "model_key=:model_key AND model_version=:model_version" in statements[0]
    assert statements[1].startswith(
        "DELETE feature FROM prediction_feature_snapshots AS feature"
    )
    assert "feature.feature_schema_version=:feature_schema_version" in statements[1]
    assert "prediction.id IS NULL" in statements[1]
    assert statements[2].startswith("DELETE FROM prediction_model_versions")

    parameters = [statement.compile().params for statement in executed]
    assert parameters == [
        {"model_key": module.MODEL_KEY, "model_version": module.MODEL_VERSION},
        {"feature_schema_version": module.FEATURE_SCHEMA_VERSION},
        {"model_key": module.MODEL_KEY, "model_version": module.MODEL_VERSION},
    ]


def test_prediction_algorithm_snapshot_migration_adds_one_to_one_snapshot_table(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migration_path = (
        Path(__file__).parents[1]
        / "migrations"
        / "versions"
        / "0036_prediction_algorithm_snapshot.py"
    )
    spec = importlib.util.spec_from_file_location(
        "prediction_algorithm_snapshot_0036", migration_path
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    metadata = MetaData()
    tables: dict[str, Table] = {}

    def capture_table(name: str, *elements: object, **kwargs: object) -> Table:
        table = Table(name, metadata, *elements, **kwargs)
        tables[name] = table
        return table

    fake_op = SimpleNamespace(
        get_bind=lambda: SimpleNamespace(dialect=SimpleNamespace(name="mysql")),
        create_table=capture_table,
    )
    monkeypatch.setattr(module, "op", fake_op)
    monkeypatch.setattr(
        module.sa, "inspect", lambda _bind: SimpleNamespace(has_table=lambda _name: False)
    )

    module.upgrade()

    assert module.down_revision == "0035_battle_kline_features"
    snapshot_table = tables["prediction_algorithm_snapshots"]
    assert tuple(snapshot_table.primary_key.columns.keys()) == ("prediction_id",)
    assert snapshot_table.c.algorithm_config_json.nullable is False
    prediction_fk = next(
        constraint
        for constraint in snapshot_table.constraints
        if isinstance(constraint, ForeignKeyConstraint)
        and constraint.name == "fk_prediction_algorithm_snapshots_prediction"
    )
    assert tuple(prediction_fk.columns.keys()) == ("prediction_id",)
    assert tuple(element.target_fullname for element in prediction_fk.elements) == (
        "battle_predictions.id",
    )
    assert prediction_fk.ondelete == "CASCADE"


def _pending_outcome(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "id": 7,
        "due_at_ms": 10_000,
        "max_favorable_bps": 100.0,
        "max_adverse_bps": 0.0,
        "last_observed_price": 101.0,
        "last_observed_at_ms": 9_000,
        "result": "long",
        "entry_price": 100.0,
        "target_bps": 500.0,
        "stop_bps": 500.0,
        "predicted_at_ms": 5_000,
        "spread_bps": 1.0,
        "mid_price": 120.0,
        "received_at": 11_000,
    }
    row.update(overrides)
    return row


def test_outcome_uses_last_pre_horizon_observation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    writes: list[tuple[str, tuple[object, ...]]] = []
    monkeypatch.setattr(battle.time, "time", lambda: 12.0)
    monkeypatch.setattr(battle.store, "query", lambda *_args, **_kwargs: [_pending_outcome()])
    monkeypatch.setattr(
        battle.store,
        "executemany",
        lambda sql, rows: writes.extend((sql, tuple(params)) for params in rows) or len(rows),
    )

    result = battle.update_prediction_outcomes()

    assert result == {"completed": 1, "updated": 0, "unavailable": 0}
    assert len(writes) == 1
    sql, params = writes[0]
    assert "status='completed'" in sql
    assert params[1] == 101.0
    assert params[2] == pytest.approx(100.0)
    assert params[8] == 9_000
    assert params[10] == 10_000


def test_outcome_rejects_post_horizon_price_without_prior_sample(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    writes: list[tuple[str, tuple[object, ...]]] = []
    row = _pending_outcome(
        last_observed_price=None,
        last_observed_at_ms=None,
        max_favorable_bps=None,
        max_adverse_bps=None,
    )
    monkeypatch.setattr(battle.time, "time", lambda: 71.0)
    monkeypatch.setattr(battle.store, "query", lambda *_args, **_kwargs: [row])
    monkeypatch.setattr(
        battle.store,
        "executemany",
        lambda sql, rows: writes.extend((sql, tuple(params)) for params in rows) or len(rows),
    )

    result = battle.update_prediction_outcomes()

    assert result == {"completed": 0, "updated": 0, "unavailable": 1}
    assert len(writes) == 1
    assert "status='unavailable'" in writes[0][0]


def test_outcome_finishes_on_first_observed_barrier(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    writes: list[tuple[str, tuple[object, ...]]] = []
    row = _pending_outcome(
        last_observed_price=None,
        last_observed_at_ms=None,
        max_favorable_bps=None,
        max_adverse_bps=None,
        target_bps=100.0,
        stop_bps=100.0,
        mid_price=102.0,
        received_at=6_000,
    )
    monkeypatch.setattr(battle.time, "time", lambda: 7.0)
    monkeypatch.setattr(battle.store, "query", lambda *_args, **_kwargs: [row])
    monkeypatch.setattr(
        battle.store,
        "executemany",
        lambda sql, rows: writes.extend((sql, tuple(params)) for params in rows) or len(rows),
    )

    result = battle.update_prediction_outcomes()

    assert result == {"completed": 1, "updated": 0, "unavailable": 0}
    assert writes[0][1][6] == "target"
    assert writes[0][1][10] == 6_000


def test_positioning_cycle_cascades_prediction_retention(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    writes: list[tuple[str, tuple[object, ...]]] = []
    monkeypatch.setattr(battle.time, "time", lambda: 4_000_000.0)
    monkeypatch.setattr(battle.store, "query", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(
        battle.store,
        "execute",
        lambda sql, params=(): writes.append((sql, tuple(params))) or 1,
    )

    result = battle.collect_positioning_cycle()

    assert result == {"collected": 0, "predictions": 0, "failures": 0}
    cutoff = 4_000_000_000 - battle.RETENTION_MS
    assert writes == [
        (
            "DELETE FROM market_positioning_snapshots WHERE snapshot_at_ms<?",
            (cutoff,),
        ),
        (
            "DELETE FROM prediction_feature_snapshots WHERE as_of_ms<?",
            (cutoff,),
        ),
    ]
