from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from quantdesk_v2.application.martingale_tp4.market_gate import (
    BinanceExecutionQuote,
    TigerReferenceQuote,
    evaluate_market_data_gate,
)

NOW = datetime(2026, 9, 2, 4, 0, tzinfo=UTC)


def _tiger(*, price: str = "100", age: int = 1) -> TigerReferenceQuote:
    middle = Decimal(price)
    return TigerReferenceQuote(
        symbol="AMD",
        bid=middle - Decimal("0.01"),
        ask=middle + Decimal("0.01"),
        observed_at=NOW - timedelta(seconds=age),
    )


def _binance(*, mark: str = "100.20", age: int = 1) -> BinanceExecutionQuote:
    middle = Decimal(mark)
    return BinanceExecutionQuote(
        symbol="AMDUSDT",
        bid=middle - Decimal("0.01"),
        ask=middle + Decimal("0.01"),
        mark=middle,
        observed_at=NOW - timedelta(seconds=age),
    )


def _evaluate(action: str, **overrides: object):
    values: dict[str, object] = {
        "action": action,
        "mapping_verified": True,
        "tiger": _tiger(),
        "binance": _binance(),
        "maximum_tiger_age_seconds": Decimal("15"),
        "maximum_binance_age_seconds": Decimal("5"),
        "maximum_clock_skew_seconds": Decimal("10"),
        "maximum_basis_bps": Decimal("100"),
        "now": NOW,
    }
    values.update(overrides)
    return evaluate_market_data_gate(**values)  # type: ignore[arg-type]


def test_fresh_mapped_quotes_allow_new_risk() -> None:
    decision = _evaluate("open")

    assert decision.allowed is True
    assert decision.new_risk_allowed is True
    assert decision.basis_bps == Decimal("20.000")
    assert decision.reason_codes == ()
    json.dumps(decision.evidence)


def test_large_cross_market_basis_blocks_open_and_add() -> None:
    for action in ("open", "add"):
        decision = _evaluate(action, binance=_binance(mark="102"))
        assert decision.allowed is False
        assert decision.new_risk_allowed is False
        assert "tiger_binance_basis_exceeded" in decision.reason_codes


def test_tiger_outage_never_blocks_risk_reducing_exit() -> None:
    decision = _evaluate(
        "exit",
        tiger=None,
        mapping_verified=False,
        binance=_binance(age=20),
    )

    assert decision.allowed is True
    assert decision.exit_allowed is True
    assert decision.new_risk_allowed is False
    assert decision.reason_codes == ()
    assert "tiger_quote_missing" in decision.warning_codes
    assert "binance_quote_stale" in decision.warning_codes


def test_stale_tiger_quote_blocks_new_cycle() -> None:
    decision = _evaluate("open", tiger=_tiger(age=16))

    assert decision.allowed is False
    assert "tiger_quote_stale" in decision.reason_codes
    assert "market_clock_skew_exceeded" in decision.reason_codes
