from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from quantdesk_v2.application.order_plans import build_entry_order_plan
from quantdesk_v2.domain.execution import ExecutionMode
from quantdesk_v2.domain.runtime import build_decision_envelope, canonical_event_hash
from quantdesk_v2.domain.trading import OrderSide

NOW = datetime(2026, 8, 30, 12, 0, tzinfo=UTC)


def _envelope(decision: str):
    return build_decision_envelope(
        revision_fingerprint="revision-hash",
        event_id=canonical_event_hash({"event": "bar", "time": NOW.isoformat()}),
        symbol="BTCUSDT",
        timeframe="1h",
        event_time=NOW - timedelta(minutes=1),
        decision=decision,
        reason_codes=("TREND_CONFIRMED",),
        valid_until=NOW + timedelta(minutes=59),
    )


def _plan(decision: str):
    return build_entry_order_plan(
        _envelope(decision),
        mode=ExecutionMode.SHADOW,
        quantity=Decimal("0.1"),
        reference_price=Decimal("100"),
        tenant_scope="tenant-1",
        user_scope="user-1",
        account_scope="shadow-1",
        deployment_scope="deployment-1",
        created_at=NOW,
        config={"stop_loss_pct": 3, "take_profit_pct": 5},
    )


def test_entry_plan_preserves_direction_timeframe_and_exit_policy() -> None:
    long_plan = _plan("LONG_ENTRY")
    short_plan = _plan("SHORT_ENTRY")

    assert long_plan is not None
    assert long_plan.intent.side is OrderSide.BUY
    assert long_plan.intent.timeframe == "1h"
    assert long_plan.exit_levels is not None
    assert long_plan.exit_levels.stop == 97
    assert long_plan.exit_levels.target == 105

    assert short_plan is not None
    assert short_plan.intent.side is OrderSide.SELL
    assert short_plan.intent.timeframe == "1h"
    assert short_plan.exit_levels is not None
    assert short_plan.exit_levels.stop == 103
    assert short_plan.exit_levels.target == 95


def test_one_decision_builds_one_stable_intent_and_hold_builds_none() -> None:
    first = _plan("LONG_ENTRY")
    replay = _plan("LONG_ENTRY")

    assert first is not None and replay is not None
    assert first.intent.intent_id == replay.intent.intent_id
    assert first.intent.idempotency_key == replay.intent.idempotency_key
    assert first.snapshot()["decision_id"] == _envelope("LONG_ENTRY").decision_id
    assert _plan("HOLD") is None
