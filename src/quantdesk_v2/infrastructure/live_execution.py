"""Production Binance composition root for the unified execution service."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from ..application.execution_control import (
    ExecutionAccountScope,
    ReflectedRiskEvidence,
)
from ..application.execution_service import ExecutionService
from ..application.risk import DeterministicRiskEvaluator, RiskPolicy
from ..application.safety import PreflightPolicy, PreflightService
from ..domain.execution import ExecutionMode, ExecutionResult, ExecutionState, OrderIntent
from .binance_broker import BinanceBroker
from .persistence.execution_controls import MySqlExecutionControlStore
from .persistence.executions import MySqlExecutionJournal
from .store_market_data import StoreMarketDataFeed


class LiveExecutionRuntime:
    """Bind one registered Binance wallet to the fail-closed shared core."""

    __slots__ = (
        "broker",
        "controls",
        "feed",
        "journal",
        "last_settlement_error",
        "service",
    )

    def __init__(
        self,
        *,
        account_client,
        trading_client,
        engine,
        api_key: str,
        api_secret: str,
        tenant_scope: str,
        user_scope: str,
        account_scope: str,
        physical_account_id: str,
        risk_policy: RiskPolicy,
        preflight_policy: PreflightPolicy | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.feed = StoreMarketDataFeed()
        self.broker = BinanceBroker(
            account_client,
            trading_client,
            api_key=api_key,
            api_secret=api_secret,
            account_scope=account_scope,
            physical_account_id=physical_account_id,
        )
        self.journal = MySqlExecutionJournal(engine)
        self.controls = MySqlExecutionControlStore(engine)
        self.last_settlement_error: str | None = None
        self.service = ExecutionService(
            broker=self.broker,
            preflight=PreflightService(self.broker, self.feed, preflight_policy, clock=clock),
            risk=DeterministicRiskEvaluator(risk_policy, clock=clock),
            idempotency=self.journal,
            expected_mode=ExecutionMode.LIVE,
            expected_broker_name="binance-usdm",
            expected_physical_account_id=physical_account_id,
            expected_market="binance_usdm",
            expected_account_scope=account_scope,
            expected_tenant_scope=tenant_scope,
            expected_user_scope=user_scope,
            execution_control=self.controls,
            risk_policy=risk_policy,
            clock=clock,
        )

    def execute(self, intent: OrderIntent) -> ExecutionResult:
        scope = self._scope(intent)
        before = self.broker.account_snapshot()
        self.controls.observe_account_snapshot(scope, before, f"preflight:{intent.intent_id}")
        result = self.service.execute(intent)
        if result.state is ExecutionState.BLOCKED and result.error_code == "idempotency_in_progress":
            result = self.service.reconcile_in_progress(intent)
        if result.state in {
            ExecutionState.FILLED,
            ExecutionState.PARTIALLY_FILLED,
        }:
            try:
                after = self.broker.account_snapshot()
                self.controls.settle_reflected(
                    intent,
                    evidence=ReflectedRiskEvidence(
                        snapshot=after,
                        reference=f"postfill:{intent.intent_id}",
                    ),
                )
                self.last_settlement_error = None
            except Exception as exc:
                # The order is already authoritative.  Return its fill so the
                # caller can install protection; the committed reservation
                # remains fail-closed and prevents another exposure increase.
                self.last_settlement_error = type(exc).__name__
        return result

    def _scope(self, intent: OrderIntent) -> ExecutionAccountScope:
        return ExecutionAccountScope.from_intent(
            intent,
            broker_name=self.broker.name,
            physical_account_id=self.broker.physical_account_id,
        )
