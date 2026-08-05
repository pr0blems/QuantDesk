from __future__ import annotations

import importlib.util
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import CheckConstraint, MetaData, Table

from quantdesk_v2 import monitor
from quantdesk_v2.monitor import MonitorRepository, MonitorUnavailable


def _migration_module():
    migration_path = (
        Path(__file__).parents[1]
        / "migrations"
        / "versions"
        / "0029_market_microstructure.py"
    )
    spec = importlib.util.spec_from_file_location(
        "market_microstructure_migration_0029",
        migration_path,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_market_microstructure_migration_has_bounded_latest_snapshot_schema(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _migration_module()
    metadata = MetaData()
    tables: dict[str, Table] = {}
    indexes: list[tuple[Any, ...]] = []

    def capture_table(name: str, *elements: object, **kwargs: object) -> Table:
        table = Table(name, metadata, *elements, **kwargs)
        tables[name] = table
        return table

    fake_op = SimpleNamespace(
        get_bind=lambda: SimpleNamespace(dialect=SimpleNamespace(name="mysql")),
        create_table=capture_table,
        create_index=lambda *args, **_kwargs: indexes.append(args),
    )
    monkeypatch.setattr(module, "op", fake_op)
    module.upgrade()

    assert module.down_revision == "0028_stock_library_zh"
    table = tables["market_microstructure"]
    assert tuple(table.primary_key.columns.keys()) == ("symbol",)
    assert tuple(table.columns.keys()) == (
        "symbol",
        "bid_depth_notional",
        "ask_depth_notional",
        "book_imbalance",
        "book_imbalance_5",
        "depth_levels",
        "ts",
    )
    checks = {
        constraint.name
        for constraint in table.constraints
        if isinstance(constraint, CheckConstraint)
    }
    assert checks == {
        "ck_market_microstructure_bid_notional_nonnegative",
        "ck_market_microstructure_ask_notional_nonnegative",
        "ck_market_microstructure_book_imbalance_range",
        "ck_market_microstructure_book_imbalance_5_range",
        "ck_market_microstructure_depth_levels_range",
    }
    assert indexes == [("ix_market_microstructure_ts", "market_microstructure", ["ts"])]


def _repository() -> MonitorRepository:
    repository = MonitorRepository.__new__(MonitorRepository)
    repository.engine = SimpleNamespace(dialect=SimpleNamespace(name="mysql"))
    repository.symbols_config = Path("unused-in-isolated-unit-test")
    repository.symbols_meta = [{"symbol": "TESTUSDT", "underlyingType": "stock"}]
    repository.symbols = ["TESTUSDT"]
    repository.symbol_set = {"TESTUSDT"}
    return repository


def _overview_queries(
    *,
    snapshot_ts: int,
    microstructure_error: bool = False,
) -> tuple[list[str], Any]:
    calls: list[str] = []

    def query(sql: str, _params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        calls.append(sql)
        if "SELECT * FROM ticker" in sql:
            return [
                {
                    "symbol": "TESTUSDT",
                    "price": 101.5,
                    "pct_24h": 2.25,
                    "quote_volume": 5_000,
                    "ts": 1_800_000_000,
                }
            ]
        if "FROM market_microstructure" in sql:
            if microstructure_error:
                raise MonitorUnavailable("table unavailable during rolling migration")
            return [
                {
                    "symbol": "TESTUSDT",
                    "bid_depth_notional": Decimal("1200000.25"),
                    "ask_depth_notional": Decimal("800000.75"),
                    "book_imbalance": Decimal("0.1999995"),
                    "book_imbalance_5": Decimal("-0.125"),
                    "depth_levels": 100,
                    "ts": snapshot_ts,
                }
            ]
        return []

    return calls, query


def test_monitor_overview_bulk_reads_fresh_market_microstructure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = 1_800_000_000
    repository = _repository()
    calls, query = _overview_queries(snapshot_ts=now - 3)
    monkeypatch.setattr(repository, "_query", query)
    monkeypatch.setattr(monitor.time, "time", lambda: float(now))

    item = repository.overview([])["items"][0]

    assert item["bid_depth_notional"] == pytest.approx(1_200_000.25)
    assert item["ask_depth_notional"] == pytest.approx(800_000.75)
    assert item["book_imbalance"] == pytest.approx(0.1999995)
    assert item["book_imbalance_5"] == pytest.approx(-0.125)
    assert item["depth_levels"] == 100
    assert sum("FROM market_microstructure" in sql for sql in calls) == 1


@pytest.mark.parametrize("age_seconds", [31, -6])
def test_monitor_overview_fails_depth_metrics_to_null_when_snapshot_is_not_fresh(
    monkeypatch: pytest.MonkeyPatch,
    age_seconds: int,
) -> None:
    now = 1_800_000_000
    repository = _repository()
    _, query = _overview_queries(snapshot_ts=now - age_seconds)
    monkeypatch.setattr(repository, "_query", query)
    monkeypatch.setattr(monitor.time, "time", lambda: float(now))

    item = repository.overview([])["items"][0]

    assert item["bid_depth_notional"] is None
    assert item["ask_depth_notional"] is None
    assert item["book_imbalance"] is None
    assert item["book_imbalance_5"] is None
    assert item["depth_levels"] is None


def test_monitor_overview_tolerates_missing_microstructure_table_during_rollout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = 1_800_000_000
    repository = _repository()
    _, query = _overview_queries(snapshot_ts=now, microstructure_error=True)
    monkeypatch.setattr(repository, "_query", query)
    monkeypatch.setattr(monitor.time, "time", lambda: float(now))

    item = repository.overview([])["items"][0]

    assert item["book_imbalance"] is None
    assert item["depth_levels"] is None


def test_monitor_intelligence_coverage_uses_fresh_depth_not_ticker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = 1_800_000_000
    repository = _repository()
    captured_params: list[tuple[Any, ...]] = []

    def query(sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        if "FROM market_microstructure" in sql:
            captured_params.append(params)
            return [{"symbol": "TESTUSDT"}, {"symbol": "REMOVEDUSDT"}]
        return [{}]

    monkeypatch.setattr(repository, "_query", query)
    monkeypatch.setattr(monitor.time, "time", lambda: float(now))

    intelligence = repository.intelligence()

    assert intelligence["market_data"]["fresh_microstructure"] == 1
    assert intelligence["market_data"]["coverage_pct"] == 100
    assert captured_params == [(now - 30, now + 5)]


def test_monitor_intelligence_fails_depth_coverage_closed_during_migration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _repository()

    def query(sql: str, _params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        if "FROM market_microstructure" in sql:
            raise MonitorUnavailable("table unavailable during rolling migration")
        return [{}]

    monkeypatch.setattr(repository, "_query", query)

    assert repository.intelligence()["market_data"]["coverage_pct"] == 0
