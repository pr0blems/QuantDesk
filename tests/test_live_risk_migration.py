from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest


def _migration_module():
    migration_path = (
        Path(__file__).parents[1]
        / "migrations"
        / "versions"
        / "0021_persist_live_risk_defaults.py"
    )
    spec = importlib.util.spec_from_file_location("live_risk_migration_0021", migration_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_live_risk_migration_inherits_target_account_values_by_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _migration_module()
    statements: list[str] = []
    bind = SimpleNamespace(dialect=SimpleNamespace(name="mariadb"))
    monkeypatch.setattr(module.op, "get_bind", lambda: bind)
    monkeypatch.setattr(module.op, "execute", statements.append)

    module.upgrade()

    assert len(statements) == 4
    live_account, paper_account, live_deployment, paper_deployment = statements
    assert live_account.startswith("UPDATE live_trading_accounts")
    assert paper_account.startswith("UPDATE paper_accounts")
    assert "round_trip_cost_bps" in live_account
    assert "round_trip_cost_bps" not in paper_account
    assert "JSON_EXTRACT('0.5', '$')" in live_account

    assert "JOIN live_trading_accounts AS a" in live_deployment
    assert "WHERE d.mode='live'" in live_deployment
    assert "round_trip_cost_bps" in live_deployment
    assert "JSON_EXTRACT(a.config_json, '$.risk_per_trade_pct')" in live_deployment
    assert "JSON_EXTRACT(d.risk_override_json, '$.risk_per_trade_pct')" in live_deployment

    assert "JOIN paper_accounts AS a" in paper_deployment
    assert "WHERE d.mode='paper'" in paper_deployment
    assert "round_trip_cost_bps" not in paper_deployment
    assert "JSON_EXTRACT(a.config_json, '$.risk_per_trade_pct')" in paper_deployment
    assert all("backtest" not in statement and "shadow" not in statement for statement in statements)
