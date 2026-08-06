from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from test_execution_service import MemoryBroker, MemoryFeed

from quantdesk_v2.application.safety import (
    ExecutionSafetyController,
    PreflightPolicy,
    PreflightService,
)
from quantdesk_v2.domain.execution import ExecutionMode, IntentAction, OrderIntent
from quantdesk_v2.domain.safety import SafetyMode
from quantdesk_v2.domain.trading import OrderSide, Quote

NOW = datetime(2026, 8, 6, 12, 0, tzinfo=UTC)


def _intent(action: IntentAction = IntentAction.OPEN) -> OrderIntent:
    return OrderIntent(
        intent_id=f"safety-{action.value}",
        idempotency_key=f"safety:{action.value}",
        strategy_version_id="strategy-v1",
        tenant_scope="tenant-1",
        user_scope="user-1",
        account_scope="account-1",
        deployment_scope="deployment-1",
        mode=ExecutionMode.SHADOW,
        market="binance_usdm",
        symbol="BTCUSDT",
        timeframe="4h",
        action=action,
        side=OrderSide.BUY if action is IntentAction.OPEN else OrderSide.SELL,
        quantity=Decimal("0.1"),
        signal_time=NOW - timedelta(minutes=1),
        valid_until=NOW + timedelta(minutes=1),
        created_at=NOW,
        reduce_only=action is IntentAction.CLOSE,
    )


def test_preflight_fails_closed_on_stale_market_data() -> None:
    broker = MemoryBroker()
    stale = Quote("BTCUSDT", Decimal("60000"), NOW - timedelta(minutes=3))

    outcome = PreflightService(
        broker,
        MemoryFeed(stale),
        clock=lambda: NOW,
    ).run(_intent())

    assert outcome.context is None
    assert outcome.report.ready is False
    assert "quote_freshness_stale" in outcome.report.reason_codes


def test_preflight_requires_account_trade_permission() -> None:
    broker = MemoryBroker()
    broker._account = replace(broker._account, can_trade=False)

    outcome = PreflightService(broker, MemoryFeed(), clock=lambda: NOW).run(_intent())

    assert outcome.report.ready is False
    assert "account_cannot_trade" in outcome.report.reason_codes


def test_preflight_requires_explicit_trade_permission_by_default() -> None:
    broker = MemoryBroker()
    broker._account = replace(broker._account, can_trade=None)

    outcome = PreflightService(broker, MemoryFeed(), clock=lambda: NOW).run(_intent())

    assert outcome.report.ready is False
    assert "account_cannot_trade" in outcome.report.reason_codes


def test_preflight_uses_account_observation_time_not_exchange_source_version() -> None:
    broker = MemoryBroker()
    broker._account = replace(
        broker._account,
        updated_at=NOW - timedelta(days=1),
        observed_at=NOW,
    )

    fresh = PreflightService(broker, MemoryFeed(), clock=lambda: NOW).run(_intent())

    assert fresh.report.ready is True
    assert fresh.context is not None
    assert fresh.context.account.updated_at == NOW - timedelta(days=1)
    assert fresh.context.account.observed_at == NOW

    broker._account = replace(
        broker._account,
        updated_at=NOW,
        observed_at=NOW - timedelta(minutes=3),
    )
    stale = PreflightService(broker, MemoryFeed(), clock=lambda: NOW).run(_intent())

    assert stale.report.ready is False
    assert "account_freshness_stale" in stale.report.reason_codes


@pytest.mark.parametrize("invalid", [float("nan"), float("inf"), float("-inf")])
@pytest.mark.parametrize(
    "field_name",
    [
        "max_quote_age_seconds",
        "max_account_age_seconds",
        "max_future_skew_seconds",
    ],
)
def test_preflight_policy_rejects_non_finite_time_bounds(
    field_name: str,
    invalid: float,
) -> None:
    with pytest.raises(ValueError, match="must be finite"):
        PreflightPolicy(**{field_name: invalid})


def test_repeated_failures_latch_safe_mode_but_allow_risk_reducing_intents() -> None:
    safety = ExecutionSafetyController(failure_threshold=2, clock=lambda: NOW)

    first = safety.record_failure("broker_timeout")
    second = safety.record_failure("broker_timeout")

    assert first.mode is SafetyMode.NORMAL
    assert second.mode is SafetyMode.SAFE_MODE
    assert safety.decision(_intent()).allowed is False
    close_decision = safety.decision(_intent(IntentAction.CLOSE))
    assert close_decision.allowed is True
    assert close_decision.mode is SafetyMode.SAFE_MODE
    # A later success cannot silently clear a latched operational safety state.
    assert safety.record_success().mode is SafetyMode.SAFE_MODE
    assert safety.recover_safe_mode().mode is SafetyMode.NORMAL


def test_kill_switch_blocks_even_risk_reducing_broker_writes() -> None:
    safety = ExecutionSafetyController(clock=lambda: NOW)

    snapshot = safety.engage_kill_switch("operator_emergency_stop")

    assert snapshot.mode is SafetyMode.KILL_SWITCH
    assert safety.decision(_intent()).allowed is False
    assert safety.decision(_intent(IntentAction.CLOSE)).allowed is False
    assert safety.release_kill_switch().mode is SafetyMode.NORMAL
