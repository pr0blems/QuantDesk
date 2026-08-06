from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from quantdesk_v2 import api


def _strategy(
    *,
    kind: str,
    parameters: dict | None = None,
    risk_defaults: dict | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        public_id="00000000-0000-0000-0000-000000000019",
        name="strategy",
        engine_key="strategy_dsl" if kind == "full_strategy" else "multi_factor",
        strategy_kind=kind,
        version=3,
        spec_schema_version=1 if kind == "full_strategy" else None,
        spec_json={"timeframes": {"trigger": "15m"}} if kind == "full_strategy" else None,
        spec_hash="abc" if kind == "full_strategy" else None,
        parameters_json=parameters or {},
        risk_defaults_json=risk_defaults or {},
    )


def test_legacy_execution_snapshot_freezes_configured_timeframe() -> None:
    strategy = _strategy(
        kind="legacy_signal",
        risk_defaults={"timeframe": "1h"},
    )

    snapshot = api._execution_strategy_snapshot(strategy)

    assert snapshot["timeframe"] == "1h"


def test_legacy_execution_snapshot_defaults_to_historical_four_hours() -> None:
    snapshot = api._execution_strategy_snapshot(_strategy(kind="legacy_signal"))

    assert snapshot["timeframe"] == "4h"


def test_full_strategy_snapshot_uses_spec_timeframes_without_duplicate_field() -> None:
    snapshot = api._execution_strategy_snapshot(_strategy(kind="full_strategy"))

    assert "timeframe" not in snapshot
    assert snapshot["spec"]["timeframes"]["trigger"] == "15m"


def test_legacy_execution_snapshot_rejects_invalid_explicit_timeframe() -> None:
    strategy = _strategy(
        kind="legacy_signal",
        risk_defaults={"timeframe": "5m"},
    )

    with pytest.raises(HTTPException) as exc_info:
        api._execution_strategy_snapshot(strategy)

    assert exc_info.value.status_code == 409
