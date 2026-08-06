"""Ready-to-use Shadow composition root for the unified execution core."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime
from decimal import Decimal

from ..application.execution_service import ExecutionService
from ..application.ports import MarketDataFeed
from ..application.risk import DeterministicRiskEvaluator, RiskPolicy
from ..application.safety import (
    ExecutionSafetyController,
    PreflightPolicy,
    PreflightService,
)
from ..domain.execution import ExecutionMode, ExecutionResult, OrderIntent
from ..domain.trading import AccountSnapshot, InstrumentRules, PositionMode
from .memory_execution import InMemoryIdempotencyStore
from .shadow_broker import ShadowBroker


class ShadowExecutionRuntime:
    """Wire shared application services to a no-network broker adapter."""

    __slots__ = ("broker", "idempotency", "safety", "service")

    def __init__(
        self,
        *,
        account: AccountSnapshot,
        feed: MarketDataFeed,
        rules: Mapping[str, InstrumentRules],
        tenant_scope: str,
        user_scope: str,
        account_scope: str,
        physical_account_id: str,
        market: str = "binance_usdm",
        position_mode: PositionMode = PositionMode.ONE_WAY,
        slippage_bps: Decimal = Decimal("0"),
        risk_policy: RiskPolicy | None = None,
        preflight_policy: PreflightPolicy | None = None,
        failure_threshold: int = 3,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.broker = ShadowBroker(
            account=account,
            feed=feed,
            rules=rules,
            account_scope=account_scope,
            physical_account_id=physical_account_id,
            market=market,
            position_mode=position_mode,
            slippage_bps=slippage_bps,
            clock=clock,
        )
        self.idempotency = InMemoryIdempotencyStore()
        self.safety = ExecutionSafetyController(
            failure_threshold=failure_threshold,
            clock=clock,
        )
        self.service = ExecutionService(
            broker=self.broker,
            preflight=PreflightService(
                self.broker,
                feed,
                preflight_policy,
                clock=clock,
            ),
            risk=DeterministicRiskEvaluator(risk_policy, clock=clock),
            idempotency=self.idempotency,
            safety=self.safety,
            expected_mode=ExecutionMode.SHADOW,
            expected_broker_name="shadow",
            expected_physical_account_id=physical_account_id,
            expected_market=market,
            expected_account_scope=account_scope,
            expected_tenant_scope=tenant_scope,
            expected_user_scope=user_scope,
            clock=clock,
        )

    def execute(self, intent: OrderIntent) -> ExecutionResult:
        return self.service.execute(intent)
