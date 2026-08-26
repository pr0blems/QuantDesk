"""Exchange-neutral event contracts shared by replay, shadow, paper and live."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

_SYMBOL = re.compile(r"[A-Z0-9][A-Z0-9._-]{1,31}")


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
