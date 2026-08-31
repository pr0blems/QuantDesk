from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from quantdesk_v2.application.strategy_execution import record_strategy_decision
from quantdesk_v2.domain.runtime import build_decision_envelope
from quantdesk_v2.models import PositionSnapshot, StrategyDeployment, StrategyRunManifest
from quantdesk_v2.strategy_artifacts import add_backtest_run_manifest, add_run_manifest

ROOT = Path(__file__).parents[1]


class _ManifestSession:
    def __init__(self) -> None:
        self.added: list[object] = []

    def flush(self) -> None:
        return None

    def scalar(self, _statement):
        return None

    def add(self, value: object) -> None:
        self.added.append(value)


def test_backtest_manifest_is_owned_directly_by_the_backtest_run() -> None:
    db = _ManifestSession()
    run = SimpleNamespace(
        id=17,
        public_id="run-17",
        user_id=9,
        symbol="AAPLUSDT",
        timeframe="1h",
        start_at=datetime(2026, 8, 1, tzinfo=UTC),
        end_at=datetime(2026, 8, 2, tzinfo=UTC),
        strategy_id="strategy-key",
        config_json={"fee_bps": 5},
    )

    manifest = add_backtest_run_manifest(db, run, None)

    assert manifest.mode == "backtest"
    assert manifest.backtest_run_id == 17
    assert manifest.deployment_id is None
    assert manifest.strategy_revision_id is None
    assert manifest.manifest_json["backtest_run_id"] == "run-17"


def test_fake_backtest_deployment_manifest_is_rejected() -> None:
    db = _ManifestSession()
    deployment = SimpleNamespace(mode="backtest")

    with pytest.raises(ValueError, match="BacktestRun"):
        add_run_manifest(db, deployment, SimpleNamespace())


def test_model_boundaries_expose_separate_run_and_position_facts() -> None:
    deployment_modes = next(
        item.sqltext.text
        for item in StrategyDeployment.__table__.constraints
        if str(getattr(item, "name", "")).endswith("valid_mode")
    )
    owner_rule = next(
        item.sqltext.text
        for item in StrategyRunManifest.__table__.constraints
        if str(getattr(item, "name", "")).endswith("valid_owner")
    )

    assert "backtest" not in deployment_modes
    assert "backtest_run_id IS NOT NULL" in owner_rule
    assert PositionSnapshot.__table__.name == "position_snapshots"
    assert PositionSnapshot.quantity.type.scale == 18


def test_governance_migrations_are_linear_and_backfill_facts() -> None:
    governance = (ROOT / "migrations/versions/0078_data_model_governance.py").read_text(
        encoding="utf-8"
    )
    positions = (ROOT / "migrations/versions/0079_position_snapshot_facts.py").read_text(
        encoding="utf-8"
    )

    assert 'down_revision: str | None = "0077_live_canary_observations"' in governance
    assert "DELETE FROM strategy_deployments WHERE mode = 'backtest'" in governance
    assert "backtest_run_id" in governance
    assert 'down_revision: str | None = "0078_data_model_governance"' in positions
    assert "paper_order_executions" in positions
    assert "live_order_intents" in positions


def test_backtest_api_no_longer_creates_a_fake_deployment() -> None:
    api = (ROOT / "src/quantdesk_v2/api.py").read_text(encoding="utf-8")
    lifecycle = (ROOT / "src/quantdesk_v2/strategy_lifecycle.py").read_text(
        encoding="utf-8"
    )

    backtest_block = api[api.index("run = BacktestRun(") : api.index("known_trade_fields")]
    assert "StrategyDeployment(" not in backtest_block
    assert "add_backtest_run_manifest(" in backtest_block
    assert "BacktestRun.strategy_revision_id == revision_id" in lifecycle
    assert "StrategyDeployment.mode == \"backtest\"" not in lifecycle


def test_position_facts_are_written_from_both_execution_paths() -> None:
    paper = (
        ROOT / "src/quantdesk_v2/infrastructure/persistence/paper_projections.py"
    ).read_text(encoding="utf-8")
    live = (ROOT / "src/quantdesk_v2/live_engine.py").read_text(encoding="utf-8")

    assert "_record_position_snapshot(transaction, execution, payload)" in paper
    assert '"paper_execution"' in paper
    assert "_record_live_position_snapshot(" in live
    assert '"live_execution"' in live


def test_strategy_signal_persists_the_exact_mode_neutral_envelope() -> None:
    event_time = datetime(2026, 8, 31, 12, tzinfo=UTC)
    envelope = build_decision_envelope(
        revision_fingerprint="revision-hash",
        event_id="closed-bar-1",
        symbol="AAPLUSDT",
        timeframe="1h",
        event_time=event_time,
        decision="LONG_ENTRY",
    )
    decision = SimpleNamespace(
        signal_time=int(event_time.timestamp()),
        decision="LONG_ENTRY",
        confidence=None,
        valid_until=None,
        reason_codes=("trend",),
        evidence={"ema": 21},
        risk_proposal={},
    )
    writes: list[tuple[str, tuple[object, ...]]] = []

    def query(sql: str, _params: tuple[object, ...]):
        if "FROM strategy_deployments" in sql:
            return [{"id": 19, "strategy_revision_id": 11}]
        return []

    def execute(sql: str, params: tuple[object, ...]):
        writes.append((sql, params))
        return 1

    persisted = record_strategy_decision(
        {
            "id": 3,
            "user_id": 7,
            "deployment_mode": "live",
        },
        "AAPLUSDT",
        {"timeframes": {"trigger": "1h"}},
        decision,
        {"source_hash": "revision-hash"},
        envelope,
        query=query,
        execute=execute,
    )

    assert persisted is True
    signal_insert = next(item for item in writes if "INSERT IGNORE INTO strategy_signals" in item[0])
    stored_evidence = json.loads(str(signal_insert[1][12]))
    assert stored_evidence["decision_envelope"] == envelope.snapshot()
