"""Pure domain contract for constrained AI trade proposals.

The model is allowed to propose an intent.  It is deliberately unable to
describe exchange orders, account identifiers, quantities, or credentials.
Application policy decides whether a validated proposal may progress to the
deterministic risk engine; this module never imports a broker or persistence
adapter.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
    field_validator,
    model_validator,
)

MAX_MODEL_OUTPUT_BYTES = 32 * 1024
MAX_LEDGER_PAYLOAD_BYTES = 48 * 1024
GENESIS_RECORD_HASH = "0" * 64

_SYMBOL_PATTERN = re.compile(r"[A-Z0-9][A-Z0-9._-]{1,31}\Z")
_VERSION_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_SCOPE_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@/-]{0,127}\Z")
_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
_CONTROL_PATTERN = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_URL_PATTERN = re.compile(
    r"(?i)(?:https?://|www\.|javascript:|data:text/|"
    r"\b[a-z0-9](?:[a-z0-9-]{0,62}\.)+(?:com|net|org|io|cn)\b)"
)
_SECRET_PATTERN = re.compile(
    r"(?ix)(?:"
    r"\bsk-[a-z0-9_-]{12,}\b|"
    r"\b(?:api[_ -]?(?:key|secret)|password|passwd|access[_ -]?token)\b"
    r"\s*[:=]\s*['\"]?[^\s'\"]{8,}"
    r")"
)
_CODE_PATTERN = re.compile(
    r"(?im)(?:```|<script\b|(?:^|\n)\s*(?:def|class|function)\s+[A-Za-z_$])"
)
_MARKUP_PATTERN = re.compile(r"<[/!?]?[A-Za-z][^>]{0,200}>")
_BIDI_PATTERN = re.compile(r"[\u202a-\u202e\u2066-\u2069]")

BoundedText = Annotated[str, StringConstraints(min_length=2, max_length=240)]
ReasonCode = Annotated[
    str,
    StringConstraints(pattern=r"^[a-z][a-z0-9_]{1,63}$", min_length=2, max_length=64),
]
Sha256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]


class ProposalAction(StrEnum):
    OPEN_LONG = "OPEN_LONG"
    OPEN_SHORT = "OPEN_SHORT"
    CLOSE_LONG = "CLOSE_LONG"
    CLOSE_SHORT = "CLOSE_SHORT"
    HOLD = "HOLD"

    @property
    def opens_risk(self) -> bool:
        return self in {self.OPEN_LONG, self.OPEN_SHORT}


class ReleaseStage(StrEnum):
    REPLAY = "replay"
    SHADOW = "shadow"
    MANUAL = "manual"
    CANARY = "canary"
    LIVE = "live"


class StrategyVersionStatus(StrEnum):
    DRAFT = "draft"
    PUBLISHED = "published"
    RETIRED = "retired"


class ProposalDisposition(StrEnum):
    REJECTED = "rejected"
    RECORD_ONLY = "record_only"
    MANUAL_REVIEW_REQUIRED = "manual_review_required"
    RISK_REVIEW_REQUIRED = "risk_review_required"


class RequestedRisk(BaseModel):
    """Risk requested by the model, expressed only as bounded policy inputs."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    risk_fraction: Decimal = Field(gt=Decimal("0"), le=Decimal("1"))
    max_slippage_bps: int = Field(ge=0, le=10_000)
    leverage: int = Field(ge=1, le=125)
    stop_loss_pct: Decimal | None = Field(
        gt=Decimal("0"),
        le=Decimal("100"),
    )
    take_profit_pct: Decimal | None = Field(
        gt=Decimal("0"),
        le=Decimal("1000"),
    )

    @field_validator("risk_fraction", "stop_loss_pct", "take_profit_pct", mode="before")
    @classmethod
    def require_json_number(cls, value: Any) -> Any:
        if value is not None and (
            isinstance(value, bool) or not isinstance(value, (int, float, Decimal))
        ):
            raise ValueError("risk values must be JSON numbers")
        if value is None or isinstance(value, Decimal):
            return value
        return Decimal(str(value))


class TradeProposal(BaseModel):
    """Strict, versioned model output. Unknown fields are always rejected."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal[1]
    strategy_version_id: Annotated[
        str,
        StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$"),
    ]
    symbol: Annotated[str, StringConstraints(min_length=2, max_length=32)]
    action: ProposalAction
    confidence: Decimal = Field(ge=Decimal("0"), le=Decimal("1"))
    thesis: tuple[BoundedText, ...] = Field(min_length=1, max_length=8)
    invalidation: Annotated[str, StringConstraints(min_length=2, max_length=500)]
    observed_at: AwareDatetime
    valid_until: AwareDatetime
    requested_risk: RequestedRisk | None

    @field_validator("confidence", mode="before")
    @classmethod
    def require_numeric_confidence(cls, value: Any) -> Any:
        if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
            raise ValueError("confidence must be a JSON number")
        return value if isinstance(value, Decimal) else Decimal(str(value))

    @field_validator("symbol", mode="before")
    @classmethod
    def normalize_symbol(cls, value: Any) -> str:
        if not isinstance(value, str):
            raise ValueError("symbol must be a string")
        normalized = value.strip().upper()
        if not _SYMBOL_PATTERN.fullmatch(normalized):
            raise ValueError("invalid instrument symbol")
        return normalized

    @field_validator("strategy_version_id")
    @classmethod
    def validate_strategy_version_id(cls, value: str) -> str:
        if not _VERSION_PATTERN.fullmatch(value):
            raise ValueError("invalid strategy version id")
        return value

    @field_validator("thesis")
    @classmethod
    def validate_thesis(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(item.strip() for item in value)
        if any(len(item) < 2 for item in normalized):
            raise ValueError("thesis entries cannot be blank")
        if len({item.casefold() for item in normalized}) != len(normalized):
            raise ValueError("thesis entries must be unique")
        for item in normalized:
            _safe_model_text(item)
        return normalized

    @field_validator("invalidation")
    @classmethod
    def validate_invalidation(cls, value: str) -> str:
        normalized = value.strip()
        if len(normalized) < 2:
            raise ValueError("invalidation cannot be blank")
        _safe_model_text(normalized)
        return normalized

    @model_validator(mode="after")
    def validate_action_contract(self) -> TradeProposal:
        if self.valid_until <= self.observed_at:
            raise ValueError("valid_until must be after observed_at")
        if self.action.opens_risk and self.requested_risk is None:
            raise ValueError("entry proposals require requested_risk")
        if not self.action.opens_risk and self.requested_risk is not None:
            raise ValueError("non-entry proposals cannot request new risk")
        return self


class ProposalGateResult(BaseModel):
    """Deterministic gate result; it is never itself an executable order."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    disposition: ProposalDisposition
    reason_codes: tuple[ReasonCode, ...] = Field(min_length=1, max_length=32)
    stage: ReleaseStage
    evaluated_at: AwareDatetime
    proposal_hash: Sha256 | None = None
    requires_deterministic_risk_review: bool
    may_submit_order: Literal[False] = False

    @property
    def rejected(self) -> bool:
        return self.disposition is ProposalDisposition.REJECTED


class DecisionProvenance(BaseModel):
    """Hashes retained in place of raw prompts, model settings, and market input."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    prompt_hash: Sha256
    model_hash: Sha256
    input_hash: Sha256
    output_hash: Sha256


class DecisionLedgerEvent(BaseModel):
    """Immutable event offered to an append-only decision-ledger port."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    event_id: Annotated[
        str,
        StringConstraints(pattern=r"^[0-9a-f]{32}$"),
    ]
    decision_run_id: Annotated[str, StringConstraints(min_length=1, max_length=128)]
    actor_scope_id: Annotated[str, StringConstraints(min_length=1, max_length=128)]
    event_type: Literal["proposal_gated", "proposal_rejected"]
    occurred_at: AwareDatetime
    provenance: DecisionProvenance
    payload_json: Annotated[str, StringConstraints(min_length=2, max_length=MAX_LEDGER_PAYLOAD_BYTES)]

    @field_validator("decision_run_id", "actor_scope_id")
    @classmethod
    def validate_scope_id(cls, value: str) -> str:
        normalized = value.strip()
        if not _SCOPE_PATTERN.fullmatch(normalized):
            raise ValueError("invalid ledger scope id")
        return normalized

    @field_validator("occurred_at")
    @classmethod
    def normalize_occurred_at(cls, value: Any) -> Any:
        # MySQL DATETIME has no timezone offset. Canonicalizing before hashing
        # guarantees that append/read round trips retain the same record hash.
        return value.astimezone(UTC)

    @field_validator("payload_json")
    @classmethod
    def validate_payload_json(cls, value: str) -> str:
        encoded = value.encode("utf-8")
        if len(encoded) > MAX_LEDGER_PAYLOAD_BYTES:
            raise ValueError("ledger payload is too large")
        parsed = _strict_json_loads(value)
        if not isinstance(parsed, dict):
            raise ValueError("ledger payload must be an object")
        canonical = canonical_json(parsed)
        if canonical != value:
            raise ValueError("ledger payload must use canonical JSON")
        return value


class DecisionLedgerRecord(BaseModel):
    """A hash-chained ledger record assigned by the persistence adapter."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    sequence: int = Field(ge=1)
    previous_record_hash: Sha256
    record_hash: Sha256
    event: DecisionLedgerEvent


class ProposalValidationError(ValueError):
    """Redacted model-output validation failure safe for logs and ledgers."""

    allowed_codes = frozenset(
        {"duplicate_key", "invalid_encoding", "invalid_json", "invalid_schema", "too_large"}
    )

    def __init__(self, code: str) -> None:
        if code not in self.allowed_codes:
            raise ValueError("unsupported proposal validation code")
        super().__init__("AI trade proposal validation failed")
        self.code = code


class DuplicateDecisionEventError(ValueError):
    """Raised when an event id is appended more than once."""


def trade_proposal_json_schema() -> dict[str, Any]:
    """Return a fresh JSON Schema suitable for a provider response format."""

    schema = TradeProposal.model_json_schema()
    # Pydantic normally advertises Decimal as either a JSON number or numeric
    # string. The model-output boundary intentionally rejects numeric strings,
    # so the provider-facing schema must match that stricter contract.
    schema["properties"]["confidence"] = {
        "type": "number",
        "minimum": 0,
        "maximum": 1,
        "title": "Confidence",
    }
    risk_properties = schema["$defs"]["RequestedRisk"]["properties"]
    risk_properties["risk_fraction"] = {
        "type": "number",
        "exclusiveMinimum": 0,
        "maximum": 1,
        "title": "Risk Fraction",
    }
    risk_properties["stop_loss_pct"] = {
        "anyOf": [
            {"type": "number", "exclusiveMinimum": 0, "maximum": 100},
            {"type": "null"},
        ],
        "title": "Stop Loss Pct",
    }
    risk_properties["take_profit_pct"] = {
        "anyOf": [
            {"type": "number", "exclusiveMinimum": 0, "maximum": 1000},
            {"type": "null"},
        ],
        "title": "Take Profit Pct",
    }
    return schema


def trade_proposal_response_format() -> dict[str, Any]:
    """Provider payload for APIs that support strict JSON Schema output."""

    return {
        "type": "json_schema",
        "json_schema": {
            "name": "trade_proposal_v1",
            "strict": True,
            "schema": trade_proposal_json_schema(),
        },
    }


def parse_trade_proposal_json(raw_output: str | bytes) -> TradeProposal:
    """Parse model output with duplicate-key and Pydantic schema validation."""

    if isinstance(raw_output, bytes):
        raw_bytes = raw_output
        try:
            raw_text = raw_bytes.decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            raise ProposalValidationError("invalid_encoding") from None
    elif isinstance(raw_output, str):
        try:
            raw_bytes = raw_output.encode("utf-8", errors="strict")
        except UnicodeEncodeError:
            raise ProposalValidationError("invalid_encoding") from None
        raw_text = raw_output
    else:
        raise ProposalValidationError("invalid_schema")
    if len(raw_bytes) > MAX_MODEL_OUTPUT_BYTES:
        raise ProposalValidationError("too_large")
    try:
        parsed = _strict_json_loads(raw_text)
    except _DuplicateJsonKey:
        raise ProposalValidationError("duplicate_key") from None
    except (json.JSONDecodeError, RecursionError, ValueError):
        raise ProposalValidationError("invalid_json") from None
    if not isinstance(parsed, dict):
        raise ProposalValidationError("invalid_schema")
    try:
        canonical = canonical_json(parsed)
    except RecursionError:
        raise ProposalValidationError("invalid_json") from None
    try:
        # Validate JSON mode rather than Python mode so strict Decimal and
        # datetime fields accept their standard JSON representations.
        return TradeProposal.model_validate_json(canonical)
    except ValidationError:
        raise ProposalValidationError("invalid_schema") from None


def canonical_json(value: Any) -> str:
    """Canonical, finite JSON used for hashes and ledger payloads."""

    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def sha256_text(value: str) -> str:
    if not isinstance(value, str):
        raise TypeError("hash input must be text")
    return hashlib.sha256(value.encode("utf-8", errors="strict")).hexdigest()


def sha256_bytes(value: bytes) -> str:
    if not isinstance(value, bytes):
        raise TypeError("hash input must be bytes")
    return hashlib.sha256(value).hexdigest()


def canonical_sha256(value: Any) -> str:
    return sha256_text(canonical_json(value))


def decision_record_hash(
    *,
    sequence: int,
    previous_record_hash: str,
    event: DecisionLedgerEvent,
) -> str:
    if sequence < 1:
        raise ValueError("ledger sequence must be positive")
    if not _SHA256_PATTERN.fullmatch(previous_record_hash):
        raise ValueError("invalid previous record hash")
    return canonical_sha256(
        {
            "sequence": sequence,
            "previous_record_hash": previous_record_hash,
            "event": event.model_dump(mode="json"),
        }
    )


def _safe_model_text(value: str) -> None:
    if (
        _CONTROL_PATTERN.search(value)
        or _URL_PATTERN.search(value)
        or _SECRET_PATTERN.search(value)
        or _CODE_PATTERN.search(value)
        or _MARKUP_PATTERN.search(value)
        or _BIDI_PATTERN.search(value)
    ):
        raise ValueError("unsafe model text")


class _DuplicateJsonKey(ValueError):
    pass


def _reject_duplicate_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateJsonKey
        result[key] = value
    return result


def _reject_nonfinite(_: str) -> None:
    raise ValueError("non-finite JSON number")


def _strict_json_loads(value: str) -> Any:
    return json.loads(
        value,
        object_pairs_hook=_reject_duplicate_pairs,
        parse_constant=_reject_nonfinite,
    )
