"""Exchange-neutral event contracts shared by replay, shadow, paper and live."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

_SYMBOL = re.compile(r"[A-Z0-9][A-Z0-9._-]{1,31}")
DECISION_ENVELOPE_VERSION = "decision_envelope_v1"


def _aware(value: datetime, name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")


def canonical_event_hash(value: object) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


class MarketEventType(StrEnum):
    BAR_CLOSED = "bar_closed"
    QUOTE = "quote"
    TRADE = "trade"
    FUNDING = "funding"
    INSTRUMENT_RULES = "instrument_rules"


class StrategyDecisionType(StrEnum):
    LONG_ENTRY = "LONG_ENTRY"
    SHORT_ENTRY = "SHORT_ENTRY"
    EXIT = "EXIT"
    HOLD = "HOLD"
    SKIP = "SKIP"


class UnifiedOrderEventType(StrEnum):
    INTENT_CREATED = "intent_created"
    RISK_REJECTED = "risk_rejected"
    SUBMITTED = "submitted"
    ACKNOWLEDGED = "acknowledged"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCELED = "canceled"
    REJECTED = "rejected"
    EXPIRED = "expired"
    UNKNOWN = "unknown"
    RECONCILED = "reconciled"


@dataclass(frozen=True, slots=True)
class MarketEvent:
    event_id: str
    event_type: MarketEventType
    symbol: str
    timeframe: str
    event_time: datetime
    availability_time: datetime
    sequence: int
    payload: dict[str, Any]
    payload_hash: str

    def __post_init__(self) -> None:
        symbol = self.symbol.strip().upper()
        if not _SYMBOL.fullmatch(symbol):
            raise ValueError("invalid market event symbol")
        object.__setattr__(self, "symbol", symbol)
        if not self.event_id or len(self.event_id) > 191:
            raise ValueError("invalid market event id")
        if not self.timeframe or len(self.timeframe) > 16:
            raise ValueError("invalid market event timeframe")
        _aware(self.event_time, "event_time")
        _aware(self.availability_time, "availability_time")
        if self.availability_time < self.event_time:
            raise ValueError("availability_time cannot precede event_time")
        if self.sequence < 0:
            raise ValueError("sequence must be non-negative")
        if canonical_event_hash(self.payload) != self.payload_hash:
            raise ValueError("market event payload hash mismatch")


@dataclass(frozen=True, slots=True)
class StrategyDecision:
    decision_id: str
    revision_fingerprint: str
    event_id: str
    symbol: str
    timeframe: str
    event_time: datetime
    decision: StrategyDecisionType
    confidence: Decimal | None
    reason_codes: tuple[str, ...]
    evidence: dict[str, Any]

    def __post_init__(self) -> None:
        _aware(self.event_time, "event_time")
        expected = strategy_decision_id(
            self.revision_fingerprint,
            self.symbol,
            self.timeframe,
            self.event_time,
            self.decision,
        )
        if self.decision_id != expected:
            raise ValueError("strategy decision id mismatch")
        if self.confidence is not None and (
            not self.confidence.is_finite() or self.confidence < 0 or self.confidence > 1
        ):
            raise ValueError("confidence must be between zero and one")


@dataclass(frozen=True, slots=True)
class DecisionEnvelope:
    """Mode-neutral strategy decision consumed by every execution runtime.

    ``mode`` deliberately does not belong to the immutable envelope.  Replay,
    shadow, paper and live may persist different delivery records, but the
    semantic decision generated from the same strategy revision and market
    event must remain identical.
    """

    decision_id: str
    revision_fingerprint: str
    event_id: str
    symbol: str
    timeframe: str
    event_time: datetime
    decision: StrategyDecisionType
    confidence: Decimal | None = None
    reason_codes: tuple[str, ...] = ()
    evidence: dict[str, Any] = field(default_factory=dict)
    risk_proposal: dict[str, Any] = field(default_factory=dict)
    valid_until: datetime | None = None
    exit_decision: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        symbol = self.symbol.strip().upper()
        if not _SYMBOL.fullmatch(symbol):
            raise ValueError("invalid decision envelope symbol")
        object.__setattr__(self, "symbol", symbol)
        timeframe = self.timeframe.strip()
        if not timeframe or len(timeframe) > 16:
            raise ValueError("invalid decision envelope timeframe")
        object.__setattr__(self, "timeframe", timeframe)
        decision = StrategyDecisionType(self.decision)
        object.__setattr__(self, "decision", decision)
        _aware(self.event_time, "event_time")
        if self.valid_until is not None:
            _aware(self.valid_until, "valid_until")
            if self.valid_until < self.event_time:
                raise ValueError("valid_until cannot precede event_time")
        expected = strategy_decision_id(
            self.revision_fingerprint,
            symbol,
            timeframe,
            self.event_time,
            decision,
        )
        if self.decision_id != expected:
            raise ValueError("decision envelope id mismatch")
        if not self.event_id or len(self.event_id) > 191:
            raise ValueError("invalid decision envelope event id")
        if self.confidence is not None and (
            not self.confidence.is_finite()
            or self.confidence < 0
            or self.confidence > 1
        ):
            raise ValueError("confidence must be between zero and one")
        reasons = tuple(
            item
            for item in (str(value).strip() for value in self.reason_codes)
            if item
        )
        object.__setattr__(self, "reason_codes", reasons)
        if self.exit_decision is not None and decision is not StrategyDecisionType.EXIT:
            raise ValueError("exit decision metadata requires an EXIT envelope")

    @property
    def direction(self) -> int:
        return {
            StrategyDecisionType.LONG_ENTRY: 1,
            StrategyDecisionType.SHORT_ENTRY: -1,
        }.get(self.decision, 0)

    @property
    def semantic_hash(self) -> str:
        return canonical_event_hash(self.snapshot())

    def snapshot(self, *, mode: str | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "version": DECISION_ENVELOPE_VERSION,
            "decision_id": self.decision_id,
            "revision_fingerprint": self.revision_fingerprint,
            "event_id": self.event_id,
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "event_time": self.event_time.isoformat(),
            "decision": self.decision.value,
            "direction": self.direction,
            "confidence": (
                str(self.confidence) if self.confidence is not None else None
            ),
            "reason_codes": list(self.reason_codes),
            "evidence": self.evidence,
            "risk_proposal": self.risk_proposal,
            "valid_until": (
                self.valid_until.isoformat() if self.valid_until is not None else None
            ),
            "exit_decision": self.exit_decision,
        }
        if mode is not None:
            normalized_mode = str(mode).strip().lower()
            if not normalized_mode:
                raise ValueError("decision envelope mode is required")
            payload["mode"] = normalized_mode
        return payload


def build_decision_envelope(
    *,
    revision_fingerprint: str,
    event_id: str,
    symbol: str,
    timeframe: str,
    event_time: datetime,
    decision: StrategyDecisionType | str,
    confidence: Decimal | None = None,
    reason_codes: tuple[str, ...] = (),
    evidence: dict[str, Any] | None = None,
    risk_proposal: dict[str, Any] | None = None,
    valid_until: datetime | None = None,
    exit_decision: dict[str, Any] | None = None,
) -> DecisionEnvelope:
    """Create a validated envelope with its canonical cross-mode identity."""

    normalized_decision = StrategyDecisionType(decision)
    return DecisionEnvelope(
        decision_id=strategy_decision_id(
            revision_fingerprint,
            symbol,
            timeframe,
            event_time,
            normalized_decision,
        ),
        revision_fingerprint=revision_fingerprint,
        event_id=event_id,
        symbol=symbol,
        timeframe=timeframe,
        event_time=event_time,
        decision=normalized_decision,
        confidence=confidence,
        reason_codes=reason_codes,
        evidence=dict(evidence or {}),
        risk_proposal=dict(risk_proposal or {}),
        valid_until=valid_until,
        exit_decision=dict(exit_decision) if exit_decision is not None else None,
    )


@dataclass(frozen=True, slots=True)
class UnifiedOrderEvent:
    execution_id: str
    sequence: int
    event_type: UnifiedOrderEventType
    occurred_at: datetime
    received_at: datetime
    payload: dict[str, Any]
    payload_hash: str

    def __post_init__(self) -> None:
        if not self.execution_id or len(self.execution_id) > 191:
            raise ValueError("invalid execution id")
        if self.sequence < 0:
            raise ValueError("sequence must be non-negative")
        _aware(self.occurred_at, "occurred_at")
        _aware(self.received_at, "received_at")
        if canonical_event_hash(self.payload) != self.payload_hash:
            raise ValueError("order event payload hash mismatch")


def strategy_decision_id(
    revision_fingerprint: str,
    symbol: str,
    timeframe: str,
    event_time: datetime | int,
    decision: StrategyDecisionType | str,
) -> str:
    if isinstance(event_time, datetime):
        _aware(event_time, "event_time")
        event_key = event_time.isoformat()
    else:
        event_key = str(int(event_time))
    normalized = {
        "revision": revision_fingerprint.strip(),
        "symbol": symbol.strip().upper(),
        "timeframe": timeframe.strip(),
        "event_time": event_key,
        "decision": StrategyDecisionType(decision).value,
    }
    if not normalized["revision"]:
        raise ValueError("revision fingerprint is required")
    return canonical_event_hash(normalized)


def decision_record_key(mode: str, deployment_id: int | str, decision_id: str) -> str:
    return canonical_event_hash(
        {
            "mode": mode.strip().lower(),
            "deployment_id": str(deployment_id),
            "decision_id": decision_id,
        }
    )
