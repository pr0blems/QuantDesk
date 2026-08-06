from __future__ import annotations

import importlib.util
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from quantdesk_v2.application.execution_control import (
    ExecutionAccountScope,
    ExecutionControlSnapshot,
    RiskReservationState,
    SafetyCommand,
    SafetyCommandAction,
    account_snapshot_fingerprint,
    execution_account_scope_hash,
    policy_fingerprint,
    reservation_state_for_result,
)
from quantdesk_v2.application.risk import RiskPolicy
from quantdesk_v2.domain.execution import (
    ExecutionMode,
    ExecutionResult,
    ExecutionState,
    IntentAction,
    OrderIntent,
    RiskDecision,
    RiskOutcome,
)
from quantdesk_v2.domain.safety import PreflightCheck, PreflightReport, SafetyMode
from quantdesk_v2.domain.trading import (
    AccountSnapshot,
    AccountType,
    BrokerOrder,
    OrderReference,
    OrderSide,
    OrderStatus,
    OrderType,
    Position,
    PositionDirection,
    PositionMode,
    PositionSide,
    Quote,
)
from quantdesk_v2.infrastructure.persistence.execution_controls import (
    _quote_fingerprint,
    _quote_watermark_rejection,
    _reservation_position_is_reflected,
    _risk_watermark_rejection,
)

NOW = datetime(2026, 8, 6, 12, 0, tzinfo=UTC)


def _intent(action: IntentAction = IntentAction.OPEN) -> OrderIntent:
    return OrderIntent(
        intent_id=f"control-{action.value}",
        idempotency_key=f"control:{action.value}",
        strategy_version_id="strategy-v1",
        tenant_scope="tenant-1",
        user_scope="user-1",
        account_scope="account-1",
        deployment_scope="deployment-1",
        mode=ExecutionMode.LIVE,
        market="binance_usdm",
        symbol="BTCUSDT",
        timeframe="4h",
        action=action,
        side=OrderSide.BUY if action is IntentAction.OPEN else OrderSide.SELL,
        quantity=Decimal("0.1"),
        signal_time=NOW - timedelta(minutes=1),
        valid_until=NOW + timedelta(minutes=5),
        created_at=NOW,
        reduce_only=action is IntentAction.CLOSE,
    )


def _preflight() -> PreflightReport:
    return PreflightReport(NOW, (PreflightCheck("execution_ready", True),))


def _risk(intent: OrderIntent) -> RiskDecision:
    return RiskDecision(
        intent_id=intent.intent_id,
        outcome=RiskOutcome.APPROVED,
        reason_codes=("approved",),
        decided_at=NOW,
        requested_quantity=intent.quantity,
        approved_quantity=intent.quantity,
        reference_price=Decimal("100"),
        notional=intent.quantity * Decimal("100"),
    )


def _broker_result(
    state: ExecutionState,
    *,
    executed_quantity: Decimal,
) -> ExecutionResult:
    intent = _intent()
    status = {
        ExecutionState.SUBMITTED: OrderStatus.NEW,
        ExecutionState.PARTIALLY_FILLED: OrderStatus.PARTIALLY_FILLED,
        ExecutionState.FILLED: OrderStatus.FILLED,
        ExecutionState.CANCELED: OrderStatus.CANCELED,
        ExecutionState.BROKER_REJECTED: OrderStatus.REJECTED,
    }[state]
    order = BrokerOrder(
        reference=OrderReference("control-order-1", intent.symbol),
        exchange_order_id="exchange-1",
        symbol=intent.symbol,
        side=intent.side,
        position_side=PositionSide.BOTH,
        order_type=OrderType.MARKET,
        status=status,
        exchange_status=status.value,
        quantity=intent.quantity,
        executed_quantity=executed_quantity,
        average_price=Decimal("100") if executed_quantity else None,
    )
    return ExecutionResult(
        intent=intent,
        state=state,
        preflight=_preflight(),
        risk_decision=_risk(intent),
        broker_order=order,
    )


def test_execution_account_scope_is_stable_and_separates_market_and_mode() -> None:
    scope = ExecutionAccountScope.from_intent(
        _intent(),
        broker_name="binance",
        physical_account_id="uid-1",
    )

    assert len(execution_account_scope_hash(scope)) == 64
    assert execution_account_scope_hash(scope) == execution_account_scope_hash(
        ExecutionAccountScope.from_intent(
            _intent(),
            broker_name="binance",
            physical_account_id="uid-1",
        )
    )
    assert execution_account_scope_hash(scope) != execution_account_scope_hash(
        replace(scope, execution_mode=ExecutionMode.SHADOW)
    )
    assert execution_account_scope_hash(scope) != execution_account_scope_hash(
        replace(scope, market="other_market")
    )
    assert execution_account_scope_hash(scope) == execution_account_scope_hash(
        replace(
            scope,
            tenant_scope="tenant-2",
            user_scope="user-2",
            account_scope="alias-2",
        )
    )
    assert execution_account_scope_hash(scope) != execution_account_scope_hash(
        replace(scope, physical_account_id="uid-2")
    )


def test_control_snapshot_preserves_both_latches_and_kill_has_precedence() -> None:
    scope = ExecutionAccountScope.from_intent(
        _intent(),
        broker_name="binance",
        physical_account_id="uid-1",
    )
    snapshot = ExecutionControlSnapshot(
        scope=scope,
        safe_reason="repeated_broker_timeout",
        kill_reason="operator_emergency_stop",
        consecutive_failures=3,
        version=9,
        changed_at=NOW,
        changed_by="operator-1",
    )

    assert snapshot.mode is SafetyMode.KILL_SWITCH
    assert snapshot.reason_codes == (
        "operator_emergency_stop",
        "repeated_broker_timeout",
    )
    assert snapshot.decision(_intent()).allowed is False
    assert snapshot.decision(_intent(IntentAction.CLOSE)).allowed is False

    safe_only = replace(snapshot, kill_reason=None)
    assert safe_only.mode is SafetyMode.SAFE_MODE
    assert safe_only.decision(_intent()).allowed is False
    assert safe_only.decision(_intent(IntentAction.CLOSE)).allowed is True


def test_safety_command_requires_cas_and_normalizes_clear_reasons() -> None:
    command = SafetyCommand(
        command_id="safe-release-1",
        action=SafetyCommandAction.RELEASE_KILL_SWITCH,
        actor="operator-1",
        expected_version=7,
    )

    assert command.reason_code == "operator_kill_switch_release"
    with pytest.raises(ValueError, match="requires a reason_code"):
        SafetyCommand(
            command_id="safe-engage-1",
            action=SafetyCommandAction.ENGAGE_SAFE_MODE,
            actor="operator-1",
            expected_version=7,
        )
    with pytest.raises(ValueError, match="non-negative integer"):
        replace(command, expected_version=-1)


def test_policy_fingerprint_is_order_independent_but_changes_with_budget() -> None:
    first = RiskPolicy(allowed_symbols=frozenset({"BTCUSDT", "ETHUSDT"}))
    reordered = RiskPolicy(allowed_symbols=frozenset({"ETHUSDT", "BTCUSDT"}))
    tighter = replace(first, max_open_positions=2)

    assert policy_fingerprint(first) == policy_fingerprint(reordered)
    assert policy_fingerprint(first) != policy_fingerprint(tighter)


def test_reservation_result_mapping_never_releases_unknown_or_partial_fill() -> None:
    intent = _intent()
    unknown = ExecutionResult(
        intent=intent,
        state=ExecutionState.UNKNOWN,
        preflight=_preflight(),
        risk_decision=_risk(intent),
        error_code="broker_timeout",
    )

    assert reservation_state_for_result(unknown) is RiskReservationState.HELD
    assert (
        reservation_state_for_result(
            _broker_result(
                ExecutionState.PARTIALLY_FILLED,
                executed_quantity=Decimal("0.01"),
            )
        )
        is RiskReservationState.COMMITTED_UNREFLECTED
    )
    assert (
        reservation_state_for_result(
            _broker_result(ExecutionState.CANCELED, executed_quantity=Decimal("0"))
        )
        is RiskReservationState.RELEASED
    )
    assert (
        reservation_state_for_result(
            _broker_result(ExecutionState.CANCELED, executed_quantity=Decimal("0.01"))
        )
        is RiskReservationState.COMMITTED_UNREFLECTED
    )


def test_execution_control_migration_follows_execution_journal() -> None:
    migration_path = (
        Path(__file__).parents[1]
        / "migrations"
        / "versions"
        / "0032_execution_controls.py"
    )
    spec = importlib.util.spec_from_file_location("execution_controls_0032", migration_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    assert module.revision == "0032_execution_controls"
    assert module.down_revision == "0031_execution_journal"


def test_close_reflection_requires_the_executed_position_delta() -> None:
    intent = _intent(IntentAction.CLOSE)
    reservation = {
        "risk_reducing": 1,
        "symbol": intent.symbol,
        "position_mode": PositionMode.ONE_WAY.value,
        "target_direction": PositionDirection.LONG.value,
        "target_position_side": PositionSide.BOTH.value,
        "baseline_direction": PositionDirection.LONG.value,
        "baseline_quantity": Decimal("1"),
    }

    def snapshot(quantity: Decimal) -> AccountSnapshot:
        return AccountSnapshot(
            account_type=AccountType.USD_M_FUTURES,
            can_trade=True,
            wallet_balance=Decimal("1000"),
            available_balance=Decimal("1000"),
            unrealized_pnl=Decimal(0),
            currency="USDT",
            updated_at=NOW,
            positions=(
                Position(
                    symbol=intent.symbol,
                    direction=PositionDirection.LONG,
                    position_side=PositionSide.BOTH,
                    quantity=quantity,
                ),
            ),
        )

    assert not _reservation_position_is_reflected(
        reservation,
        intent,
        snapshot(Decimal("1")),
        Decimal("0.5"),
    )
    assert _reservation_position_is_reflected(
        reservation,
        intent,
        snapshot(Decimal("0.5")),
        Decimal("0.5"),
    )


def test_quote_watermark_rejects_time_regression_and_equal_time_conflict() -> None:
    current = Quote("BTCUSDT", Decimal("60000"), NOW)
    row = {
        "symbol": current.symbol,
        "observed_at": current.observed_at,
        "quote_hash": _quote_fingerprint(current),
    }

    assert (
        _quote_watermark_rejection(
            row,
            replace(current, observed_at=NOW - timedelta(microseconds=1)),
        )
        == "quote_snapshot_stale"
    )
    assert (
        _quote_watermark_rejection(
            row,
            replace(current, price=Decimal("59999")),
        )
        == "quote_snapshot_conflict"
    )
    assert _quote_watermark_rejection(row, current) is None


def test_account_watermark_rejects_late_observation_of_older_source_state() -> None:
    current = AccountSnapshot(
        account_type=AccountType.USD_M_FUTURES,
        can_trade=True,
        wallet_balance=Decimal("1000"),
        available_balance=Decimal("1000"),
        unrealized_pnl=Decimal(0),
        currency="USDT",
        updated_at=NOW,
        observed_at=NOW,
    )
    row = {
        "risk_snapshot_high_watermark_at": current.observed_at,
        "risk_snapshot_source_updated_at": current.updated_at,
        "risk_snapshot_high_watermark_hash": account_snapshot_fingerprint(current),
    }
    late_old_snapshot = replace(
        current,
        updated_at=NOW - timedelta(seconds=1),
        observed_at=NOW + timedelta(seconds=1),
    )

    assert (
        _risk_watermark_rejection(
            row,
            late_old_snapshot.updated_at,
            late_old_snapshot.observed_at,
            account_snapshot_fingerprint(late_old_snapshot),
        )
        == "risk_snapshot_stale"
    )
