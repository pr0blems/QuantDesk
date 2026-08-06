from decimal import Decimal

import pytest

from quantdesk_v2.execution import OrderProposal, RiskLimits, evaluate_pre_trade


def limits() -> RiskLimits:
    return RiskLimits(
        max_order_notional=Decimal("1000"),
        max_symbol_notional=Decimal("2000"),
        max_total_notional=Decimal("5000"),
        max_leverage=5,
        max_daily_loss_pct=Decimal("3"),
        max_drawdown_pct=Decimal("10"),
    )


def proposal(**overrides) -> OrderProposal:
    values = {
        "symbol": "AAPLUSDT",
        "side": "buy",
        "quantity": Decimal("2"),
        "price": Decimal("100"),
        "leverage": 2,
        "reduce_only": False,
        "account_equity": Decimal("10000"),
        "symbol_notional": Decimal("500"),
        "total_notional": Decimal("1500"),
        "daily_pnl": Decimal("-50"),
        "drawdown_pct": Decimal("2"),
        "market_age_seconds": 2,
    }
    values.update(overrides)
    return OrderProposal(**values)


def test_pre_trade_approves_with_decimal_exposure_projection() -> None:
    decision = evaluate_pre_trade(proposal(), limits())
    assert decision.approved is True
    assert decision.order_notional == Decimal("200")
    assert decision.projected_symbol_notional == Decimal("700")


def test_pre_trade_returns_all_stable_rejection_codes() -> None:
    decision = evaluate_pre_trade(
        proposal(
            quantity=Decimal("20"),
            leverage=10,
            symbol_notional=Decimal("1500"),
            total_notional=Decimal("4500"),
            daily_pnl=Decimal("-400"),
            drawdown_pct=Decimal("12"),
            market_age_seconds=30,
        ),
        limits(),
    )
    assert decision.approved is False
    assert decision.reason_codes == (
        "MARKET_DATA_STALE",
        "LEVERAGE_LIMIT",
        "ORDER_NOTIONAL_LIMIT",
        "SYMBOL_EXPOSURE_LIMIT",
        "TOTAL_EXPOSURE_LIMIT",
        "DAILY_LOSS_LIMIT",
        "DRAWDOWN_LIMIT",
    )


def test_reduce_only_does_not_increase_exposure_but_still_checks_safety() -> None:
    decision = evaluate_pre_trade(
        proposal(
            quantity=Decimal("100"),
            reduce_only=True,
            symbol_notional=Decimal("3000"),
            total_notional=Decimal("6000"),
        ),
        limits(),
    )
    assert decision.approved is True
    assert decision.projected_symbol_notional == Decimal("3000")


def test_invalid_numeric_input_is_rejected() -> None:
    with pytest.raises(ValueError, match="quantity"):
        proposal(quantity=Decimal("NaN"))
