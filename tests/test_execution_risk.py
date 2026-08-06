from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from quantdesk_v2.application.risk import DeterministicRiskEvaluator
from quantdesk_v2.domain.execution import (
    ExecutionContext,
    ExecutionMode,
    IntentAction,
    OrderIntent,
)
from quantdesk_v2.domain.trading import (
    AccountSnapshot,
    AccountType,
    InstrumentRules,
    OrderSide,
    Position,
    PositionDirection,
    PositionMode,
    PositionSide,
    Quote,
)

NOW = datetime(2026, 8, 6, 12, 0, tzinfo=UTC)


def _intent() -> OrderIntent:
    return OrderIntent(
        intent_id="risk-intent-1",
        idempotency_key="risk:strategy:v1:signal:1",
        strategy_version_id="strategy-v1",
        tenant_scope="tenant-1",
        user_scope="user-1",
        account_scope="account-1",
        deployment_scope="deployment-1",
        mode=ExecutionMode.SHADOW,
        market="binance_usdm",
        symbol="BTCUSDT",
        timeframe="4h",
        action=IntentAction.OPEN,
        side=OrderSide.BUY,
        quantity=Decimal("0.01"),
        signal_time=NOW - timedelta(minutes=1),
        valid_until=NOW + timedelta(minutes=1),
        created_at=NOW,
    )


def _context(position: Position) -> ExecutionContext:
    return ExecutionContext(
        account=AccountSnapshot(
            account_type=AccountType.USD_M_FUTURES,
            can_trade=True,
            wallet_balance=Decimal("1000"),
            available_balance=Decimal("100"),
            unrealized_pnl=Decimal(0),
            currency="USDT",
            updated_at=NOW,
            positions=(position,),
        ),
        quote=Quote("BTCUSDT", Decimal("60000"), NOW),
        rules=InstrumentRules(
            symbol="BTCUSDT",
            quantity_step=Decimal("0.001"),
            minimum_quantity=Decimal("0.001"),
            maximum_quantity=Decimal("100"),
            price_tick=Decimal("0.1"),
            minimum_notional=Decimal("5"),
        ),
        position_mode=PositionMode.ONE_WAY,
    )


def test_risk_limit_uses_existing_portfolio_exposure_not_only_the_new_order() -> None:
    context = _context(
        Position(
            symbol="ETHUSDT",
            direction=PositionDirection.LONG,
            position_side=PositionSide.BOTH,
            quantity=Decimal("3"),
            notional=Decimal("9900"),
        )
    )

    decision = DeterministicRiskEvaluator(clock=lambda: NOW).evaluate(
        _intent(),
        context,
    )

    assert decision.approved is False
    assert "account_exposure_limit" in decision.reason_codes


def test_open_risk_fails_closed_when_existing_exposure_cannot_be_valued() -> None:
    context = _context(
        Position(
            symbol="ETHUSDT",
            direction=PositionDirection.LONG,
            position_side=PositionSide.BOTH,
            quantity=Decimal("3"),
        )
    )

    decision = DeterministicRiskEvaluator(clock=lambda: NOW).evaluate(
        _intent(),
        context,
    )

    assert decision.approved is False
    assert "account_exposure_snapshot_incomplete" in decision.reason_codes
