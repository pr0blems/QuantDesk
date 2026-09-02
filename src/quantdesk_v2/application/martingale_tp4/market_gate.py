"""Cross-source market-data gate for Tiger signals and Binance execution."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Literal

RiskAction = Literal["open", "add", "hold", "reduce", "exit"]


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class TigerReferenceQuote:
    symbol: str
    bid: Decimal
    ask: Decimal
    observed_at: datetime

    @property
    def mid(self) -> Decimal:
        return (self.bid + self.ask) / Decimal("2")


@dataclass(frozen=True, slots=True)
class BinanceExecutionQuote:
    symbol: str
    bid: Decimal
    ask: Decimal
    mark: Decimal
    observed_at: datetime


@dataclass(frozen=True, slots=True)
class MarketDataGateDecision:
    action: RiskAction
    allowed: bool
    new_risk_allowed: bool
    exit_allowed: bool
    basis_bps: Decimal | None
    tiger_age_seconds: Decimal | None
    binance_age_seconds: Decimal | None
    clock_skew_seconds: Decimal | None
    reason_codes: tuple[str, ...]
    warning_codes: tuple[str, ...]
    evidence: dict[str, object]


def _age_seconds(observed_at: datetime, now: datetime) -> Decimal:
    return max(
        Decimal("0"),
        Decimal(str((_utc(now) - _utc(observed_at)).total_seconds())),
    )


def evaluate_market_data_gate(
    *,
    action: RiskAction,
    mapping_verified: bool,
    tiger: TigerReferenceQuote | None,
    binance: BinanceExecutionQuote | None,
    maximum_tiger_age_seconds: Decimal,
    maximum_binance_age_seconds: Decimal,
    maximum_clock_skew_seconds: Decimal,
    maximum_basis_bps: Decimal,
    now: datetime | None = None,
) -> MarketDataGateDecision:
    """Fail closed for added exposure and fail open for risk-reducing exits.

    Tiger is the signal fact source and Binance is the execution fact source.
    A Tiger outage or an unusual cross-market basis must never trap an existing
    Binance position by blocking a reduce or exit command.
    """

    if action not in {"open", "add", "hold", "reduce", "exit"}:
        raise ValueError("unsupported market gate action")
    for name, value in (
        ("maximum_tiger_age_seconds", maximum_tiger_age_seconds),
        ("maximum_binance_age_seconds", maximum_binance_age_seconds),
        ("maximum_clock_skew_seconds", maximum_clock_skew_seconds),
        ("maximum_basis_bps", maximum_basis_bps),
    ):
        if value < 0:
            raise ValueError(f"{name} must not be negative")

    evaluated_at = now or datetime.now(UTC)
    reasons: list[str] = []
    warnings: list[str] = []
    tiger_age: Decimal | None = None
    binance_age: Decimal | None = None
    clock_skew: Decimal | None = None
    basis: Decimal | None = None

    if not mapping_verified:
        reasons.append("market_mapping_unverified")

    if tiger is None:
        reasons.append("tiger_quote_missing")
    else:
        if tiger.bid <= 0 or tiger.ask <= 0 or tiger.bid >= tiger.ask:
            reasons.append("tiger_quote_invalid")
        tiger_age = _age_seconds(tiger.observed_at, evaluated_at)
        if tiger_age > maximum_tiger_age_seconds:
            reasons.append("tiger_quote_stale")

    if binance is None:
        reasons.append("binance_quote_missing")
    else:
        if binance.bid <= 0 or binance.ask <= 0 or binance.mark <= 0 or binance.bid >= binance.ask:
            reasons.append("binance_quote_invalid")
        binance_age = _age_seconds(binance.observed_at, evaluated_at)
        if binance_age > maximum_binance_age_seconds:
            reasons.append("binance_quote_stale")

    tiger_valid = tiger is not None and tiger.bid > 0 and tiger.ask > tiger.bid
    binance_valid = (
        binance is not None and binance.bid > 0 and binance.ask > binance.bid and binance.mark > 0
    )
    if tiger_valid and binance_valid and tiger is not None and binance is not None:
        clock_skew = abs(
            Decimal(str((_utc(tiger.observed_at) - _utc(binance.observed_at)).total_seconds()))
        )
        if clock_skew > maximum_clock_skew_seconds:
            reasons.append("market_clock_skew_exceeded")
        basis = (binance.mark / tiger.mid - Decimal("1")) * Decimal("10000")
        if abs(basis) > maximum_basis_bps:
            reasons.append("tiger_binance_basis_exceeded")

    new_risk_allowed = not reasons
    risk_reducing = action in {"reduce", "exit"}
    if risk_reducing and reasons:
        warnings.extend(reasons)
    allowed = True if risk_reducing else new_risk_allowed
    evidence: dict[str, object] = {
        "mapping_verified": mapping_verified,
        "evaluated_at": _utc(evaluated_at).isoformat(),
        "limits": {
            "maximum_tiger_age_seconds": str(maximum_tiger_age_seconds),
            "maximum_binance_age_seconds": str(maximum_binance_age_seconds),
            "maximum_clock_skew_seconds": str(maximum_clock_skew_seconds),
            "maximum_basis_bps": str(maximum_basis_bps),
        },
        "tiger": (
            {
                **asdict(tiger),
                "bid": str(tiger.bid),
                "ask": str(tiger.ask),
                "observed_at": _utc(tiger.observed_at).isoformat(),
            }
            if tiger is not None
            else None
        ),
        "binance": (
            {
                **asdict(binance),
                "bid": str(binance.bid),
                "ask": str(binance.ask),
                "mark": str(binance.mark),
                "observed_at": _utc(binance.observed_at).isoformat(),
            }
            if binance is not None
            else None
        ),
    }
    return MarketDataGateDecision(
        action=action,
        allowed=allowed,
        new_risk_allowed=new_risk_allowed,
        exit_allowed=True,
        basis_bps=basis,
        tiger_age_seconds=tiger_age,
        binance_age_seconds=binance_age,
        clock_skew_seconds=clock_skew,
        reason_codes=tuple(reasons if not risk_reducing else ()),
        warning_codes=tuple(warnings),
        evidence=evidence,
    )
