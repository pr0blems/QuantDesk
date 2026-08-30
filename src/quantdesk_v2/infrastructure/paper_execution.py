"""Composition root for formal paper execution through the shared core."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from ..application.execution_service import ExecutionService
from ..application.risk import DeterministicRiskEvaluator, RiskPolicy
from ..application.safety import ExecutionSafetyController, PreflightPolicy, PreflightService
from ..domain.execution import ExecutionMode, ExecutionResult, ExecutionState, OrderIntent
from .callback_broker import PaperBroker
from .persistence.executions import MySqlExecutionJournal


class PaperExecutionRuntime:
    """Bind one persisted paper account to the common execution service."""

    __slots__ = ("broker", "journal", "safety", "service")

    def __init__(
        self,
        *,
        broker: PaperBroker,
        engine,
        tenant_scope: str,
        user_scope: str,
        account_scope: str,
        physical_account_id: str,
        risk_policy: RiskPolicy,
        safety: ExecutionSafetyController,
        preflight_policy: PreflightPolicy | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.broker = broker
        self.journal = MySqlExecutionJournal(engine)
        self.safety = safety
        self.service = ExecutionService(
            broker=broker,
            preflight=PreflightService(broker, broker.feed, preflight_policy, clock=clock),
            risk=DeterministicRiskEvaluator(risk_policy, clock=clock),
            idempotency=self.journal,
            safety=safety,
            expected_mode=ExecutionMode.PAPER,
            expected_broker_name="paper",
            expected_physical_account_id=physical_account_id,
            expected_market="binance_usdm",
            expected_account_scope=account_scope,
            expected_tenant_scope=tenant_scope,
            expected_user_scope=user_scope,
            clock=clock,
        )

    def execute(self, intent: OrderIntent) -> ExecutionResult:
        result = self.service.execute(intent)
        if result.state is ExecutionState.BLOCKED and result.error_code == "idempotency_in_progress":
            return self.service.reconcile_in_progress(intent)
        return result
