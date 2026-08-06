from __future__ import annotations

import importlib.util
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from quantdesk_v2.application.execution_service import deterministic_client_order_id
from quantdesk_v2.domain.execution import (
    ExecutionCheckpoint,
    ExecutionMode,
    ExecutionResult,
    ExecutionState,
    Fill,
    IntentAction,
    OrderIntent,
    RiskDecision,
    RiskOutcome,
)
from quantdesk_v2.domain.safety import PreflightCheck, PreflightReport
from quantdesk_v2.domain.trading import (
    BrokerOrder,
    OrderReference,
    OrderSide,
    OrderStatus,
    OrderType,
    PositionSide,
)
from quantdesk_v2.infrastructure.persistence.executions import (
    ExecutionJournalIntegrityError,
    deserialize_execution_checkpoint,
    deserialize_execution_result,
    deserialize_order_intent,
    execution_scope_hash,
    serialize_execution_checkpoint,
    serialize_execution_result,
    serialize_order_intent,
)

NOW = datetime(2026, 8, 6, 8, 0, 0, 123456, tzinfo=UTC)


def execution_intent(
    suffix: str = "1",
    *,
    tenant_scope: str = "tenant-1",
    account_scope: str = "account-1",
) -> OrderIntent:
    return OrderIntent(
        intent_id=f"journal-intent-{suffix}",
        idempotency_key=f"journal:strategy-v1:signal-{suffix}",
        strategy_version_id="strategy-v1",
        tenant_scope=tenant_scope,
        user_scope="user-1",
        account_scope=account_scope,
        deployment_scope="deployment-1",
        mode=ExecutionMode.SHADOW,
        market="binance_usdm",
        symbol="BTCUSDT",
        timeframe="4h",
        action=IntentAction.OPEN,
        side=OrderSide.BUY,
        quantity=Decimal("0.1"),
        signal_time=NOW - timedelta(minutes=1),
        valid_until=NOW + timedelta(minutes=5),
        created_at=NOW,
        position_side=PositionSide.BOTH,
        max_slippage_bps=Decimal("10"),
    )


def execution_preflight() -> PreflightReport:
    return PreflightReport(
        checked_at=NOW,
        checks=(PreflightCheck("execution_ready", True),),
    )


def execution_risk(intent: OrderIntent) -> RiskDecision:
    return RiskDecision(
        intent_id=intent.intent_id,
        outcome=RiskOutcome.APPROVED,
        reason_codes=("approved",),
        decided_at=NOW,
        requested_quantity=intent.quantity,
        approved_quantity=intent.quantity,
        reference_price=Decimal("60000"),
        notional=Decimal("6000"),
    )


def execution_checkpoint(intent: OrderIntent) -> ExecutionCheckpoint:
    return ExecutionCheckpoint(
        intent=intent,
        preflight=execution_preflight(),
        risk_decision=execution_risk(intent),
        broker_name="shadow",
        physical_account_id="shadow-wallet-1",
        client_order_id=deterministic_client_order_id(intent),
        checkpointed_at=NOW,
    )


def unknown_result(intent: OrderIntent) -> ExecutionResult:
    return ExecutionResult(
        intent=intent,
        state=ExecutionState.UNKNOWN,
        preflight=execution_preflight(),
        risk_decision=execution_risk(intent),
        error_code="broker_timeout",
    )


def filled_result(intent: OrderIntent) -> ExecutionResult:
    client_order_id = deterministic_client_order_id(intent)
    order = BrokerOrder(
        reference=OrderReference(client_order_id, intent.symbol),
        exchange_order_id="shadow-1",
        symbol=intent.symbol,
        side=intent.side,
        position_side=intent.position_side,
        order_type=OrderType.MARKET,
        status=OrderStatus.FILLED,
        exchange_status="FILLED",
        quantity=intent.quantity,
        executed_quantity=intent.quantity,
        average_price=Decimal("60000"),
    )
    fill = Fill(
        fill_id=f"fill:{client_order_id}",
        client_order_id=client_order_id,
        exchange_order_id="shadow-1",
        symbol=intent.symbol,
        side=intent.side,
        quantity=intent.quantity,
        price=Decimal("60000"),
        occurred_at=NOW,
        simulated=True,
    )
    return ExecutionResult(
        intent=intent,
        state=ExecutionState.FILLED,
        preflight=execution_preflight(),
        risk_decision=execution_risk(intent),
        broker_order=order,
        fills=(fill,),
    )


def test_execution_journal_migration_follows_current_head() -> None:
    migration_path = (
        Path(__file__).parents[1]
        / "migrations"
        / "versions"
        / "0031_execution_journal.py"
    )
    spec = importlib.util.spec_from_file_location("execution_journal_0031", migration_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    assert module.revision == "0031_execution_journal"
    assert module.down_revision == "0030_ai_decision_ledger"


def test_execution_journal_payloads_round_trip_every_recovery_link() -> None:
    intent = execution_intent()
    checkpoint = execution_checkpoint(intent)
    result = filled_result(intent)

    assert deserialize_order_intent(serialize_order_intent(intent)) == intent
    assert (
        deserialize_execution_checkpoint(serialize_execution_checkpoint(checkpoint))
        == checkpoint
    )
    assert deserialize_execution_result(serialize_execution_result(result)) == result
    assert len(execution_scope_hash(intent)) == 64


def test_execution_journal_rejects_tampered_payload_schema() -> None:
    payload = serialize_execution_result(filled_result(execution_intent()))

    with pytest.raises(ExecutionJournalIntegrityError):
        deserialize_execution_result(payload.replace('"schema_version":1', '"schema_version":2'))
