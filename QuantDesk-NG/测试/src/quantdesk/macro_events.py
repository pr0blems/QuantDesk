"""Structured macro/official event extraction helpers.

The collector remains deliberately transport agnostic: RSS ingestion can keep
its legacy tuple contract while this module turns official headlines into a
small, auditable event record.  Values are only populated when an explicit
label (actual/consensus/forecast) is present; an unlabeled number is never
silently treated as a release value.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any

_NUMBER = r"[-+]?\d+(?:[.,]\d+)?"
_LABELED_NUMBER = re.compile(
    rf"(?P<label>actual|released|consensus|forecast|expected|estimate|previous|revision)"
    rf"\s*(?:value|reading|number)?\s*[:=]\s*(?P<value>{_NUMBER})\s*(?P<unit>%|bps?|bp)?",
    re.IGNORECASE,
)
_ISO_TIME = re.compile(
    r"(?P<date>20\d{2}-\d{2}-\d{2})(?:[ T](?P<time>\d{2}:\d{2}(?::\d{2})?))?\s*(?P<tz>UTC|GMT|Z)?",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class StructuredEvent:
    event_type: str
    scheduled_at: int | None = None
    actual_at: int | None = None
    actual_value: float | None = None
    consensus_value: float | None = None
    surprise_value: float | None = None
    unit: str | None = None
    affected_symbols: tuple[str, ...] = ()
    extraction_confidence: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _number(value: str) -> float:
    return float(value.replace(",", "."))


def _timestamp(value: str) -> int | None:
    match = _ISO_TIME.search(value or "")
    if not match:
        return None
    date = match.group("date")
    clock = match.group("time") or "00:00:00"
    if len(clock) == 5:
        clock += ":00"
    try:
        return int(datetime.fromisoformat(f"{date}T{clock}+00:00").timestamp())
    except ValueError:
        return None


def classify_official_event(text: str, source: str = "") -> str:
    """Return a stable macro event type, or ``other`` for ordinary news."""

    value = f"{source} {text}".casefold()
    patterns = (
        ("monetary_policy", ("fomc", "federal reserve", "interest rate", "fed funds")),
        ("macro_release", ("cpi", "consumer price", "inflation", "payroll", "nonfarm", "gdp")),
        ("labor_release", ("employment situation", "unemployment", "jobless claims", "bls")),
        ("energy_release", ("crude stocks", "petroleum status", "natural gas storage", "eia")),
        ("regulatory_filing", ("sec filing", "8-k", "10-q", "10-k", "hkex")),
        ("corporate_action", ("earnings", "revenue", "dividend", "buyback", "merger", "acquisition")),
    )
    for event_type, terms in patterns:
        if any(term in value for term in terms):
            return event_type
    return "other"


def extract_structured_event(
    title: str,
    *,
    published_at: int = 0,
    source: str = "",
    affected_symbols: list[str] | tuple[str, ...] = (),
) -> StructuredEvent:
    """Extract explicitly labelled release values from an official headline.

    ``published_at`` is used as ``actual_at`` only when an actual/released
    value is present.  This prevents scheduled headlines from masquerading as
    already released data.
    """

    text = str(title or "")
    event_type = classify_official_event(text, source)
    values: dict[str, float] = {}
    unit: str | None = None
    for match in _LABELED_NUMBER.finditer(text):
        label = match.group("label").casefold()
        values[label] = _number(match.group("value"))
        unit = unit or match.group("unit")
    actual = next((values[key] for key in ("actual", "released") if key in values), None)
    consensus = next(
        (values[key] for key in ("consensus", "forecast", "expected", "estimate") if key in values),
        None,
    )
    surprise = actual - consensus if actual is not None and consensus is not None else None
    scheduled_at = _timestamp(text)
    confidence = 0.0
    if event_type != "other":
        confidence += 0.45
    if actual is not None or consensus is not None:
        confidence += 0.30
    if scheduled_at is not None:
        confidence += 0.15
    if source:
        confidence += 0.10
    return StructuredEvent(
        event_type=event_type,
        scheduled_at=scheduled_at,
        actual_at=published_at if actual is not None and published_at > 0 else None,
        actual_value=actual,
        consensus_value=consensus,
        surprise_value=surprise,
        unit=unit,
        affected_symbols=tuple(sorted(set(str(item).upper() for item in affected_symbols if item))),
        extraction_confidence=min(1.0, confidence),
    )
