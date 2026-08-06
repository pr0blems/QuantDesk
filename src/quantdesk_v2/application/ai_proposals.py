"""Use case that validates, gates, and audits AI trade proposals."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from ..domain.ai_decision import (
    DecisionLedgerEvent,
    DecisionLedgerRecord,
    DecisionProvenance,
    ProposalDisposition,
    ProposalGateResult,
    ProposalValidationError,
    TradeProposal,
    canonical_json,
    canonical_sha256,
    parse_trade_proposal_json,
    sha256_bytes,
    sha256_text,
)
from .ai_ports import DecisionLedger
from .proposal_gate import ProposalGate, ProposalGateContext

MAX_PROMPT_TEMPLATE_BYTES = 128 * 1024
MAX_INPUT_SNAPSHOT_BYTES = 512 * 1024


@dataclass(frozen=True, slots=True)
class ProposalEvaluationRequest:
    """Data needed to evaluate one already-returned model output.

    The raw prompt and market input are used only to compute hashes. They are
    intentionally absent from the persisted ledger event.
    """

    raw_model_output: str | bytes
    decision_run_id: str
    actor_scope_id: str
    prompt_template: str
    provider_code: str
    model_name: str
    input_snapshot: Mapping[str, Any]
    gate_context: ProposalGateContext
    model_version: str | None = None


@dataclass(frozen=True, slots=True)
class ProposalEvaluation:
    proposal: TradeProposal | None
    gate_result: ProposalGateResult
    ledger_record: DecisionLedgerRecord
    validation_code: str | None = None


class AiProposalService:
    """Validate model JSON, apply policy, and append a tamper-evident event.

    This service has no broker dependency by construction. A risk-review result
    is a hand-off candidate for a different use case, never an order command.
    """

    def __init__(self, ledger: DecisionLedger, gate: ProposalGate | None = None) -> None:
        self._ledger = ledger
        self._gate = gate or ProposalGate()

    def evaluate(self, request: ProposalEvaluationRequest) -> ProposalEvaluation:
        provenance = _provenance(request)
        gate_evidence = _gate_evidence(self._gate, request.gate_context)
        try:
            proposal = parse_trade_proposal_json(request.raw_model_output)
        except ProposalValidationError as exc:
            result = ProposalGateResult(
                disposition=ProposalDisposition.REJECTED,
                reason_codes=("invalid_model_output",),
                stage=request.gate_context.stage,
                evaluated_at=request.gate_context.now,
                proposal_hash=None,
                requires_deterministic_risk_review=False,
                may_submit_order=False,
            )
            record = self._ledger.append(
                _event(
                    request=request,
                    provenance=provenance,
                    event_type="proposal_rejected",
                    payload={
                        "gate_evidence": gate_evidence,
                        "gate_result": result.model_dump(mode="json"),
                        "validation_code": exc.code,
                    },
                )
            )
            return ProposalEvaluation(
                proposal=None,
                gate_result=result,
                ledger_record=record,
                validation_code=exc.code,
            )

        result = self._gate.evaluate(proposal, request.gate_context)
        record = self._ledger.append(
            _event(
                request=request,
                provenance=provenance,
                event_type=("proposal_rejected" if result.rejected else "proposal_gated"),
                payload={
                    "gate_evidence": gate_evidence,
                    "proposal": proposal.model_dump(mode="json"),
                    "gate_result": result.model_dump(mode="json"),
                },
            )
        )
        return ProposalEvaluation(
            proposal=proposal,
            gate_result=result,
            ledger_record=record,
        )


def _provenance(request: ProposalEvaluationRequest) -> DecisionProvenance:
    if not isinstance(request.prompt_template, str):
        raise TypeError("prompt_template must be text")
    prompt_bytes = request.prompt_template.encode("utf-8", errors="strict")
    if len(prompt_bytes) > MAX_PROMPT_TEMPLATE_BYTES:
        raise ValueError("prompt template is too large")
    for name in ("provider_code", "model_name"):
        value = getattr(request, name)
        if not isinstance(value, str) or not value.strip() or len(value) > 128:
            raise ValueError(f"invalid {name}")
    if request.model_version is not None and (
        not isinstance(request.model_version, str)
        or not request.model_version.strip()
        or len(request.model_version) > 128
    ):
        raise ValueError("invalid model_version")
    input_json = canonical_json(dict(request.input_snapshot))
    if len(input_json.encode("utf-8")) > MAX_INPUT_SNAPSHOT_BYTES:
        raise ValueError("input snapshot is too large")
    output_bytes = _output_bytes(request.raw_model_output)
    return DecisionProvenance(
        prompt_hash=sha256_text(request.prompt_template),
        model_hash=canonical_sha256(
            {
                "provider_code": request.provider_code.strip(),
                "model_name": request.model_name.strip(),
                "model_version": (
                    request.model_version.strip() if request.model_version is not None else None
                ),
            }
        ),
        input_hash=sha256_text(input_json),
        output_hash=sha256_bytes(output_bytes),
    )


def _output_bytes(value: str | bytes) -> bytes:
    if isinstance(value, bytes):
        return value
    if isinstance(value, str):
        # Surrogate-pass makes even malformed model text hashable. The strict
        # proposal parser will still reject it as invalid encoding.
        return value.encode("utf-8", errors="surrogatepass")
    raise TypeError("raw_model_output must be text or bytes")


def _gate_evidence(
    gate: ProposalGate,
    context: ProposalGateContext,
) -> dict[str, object]:
    policy_snapshot = gate.policy.audit_snapshot()
    context_snapshot = context.audit_snapshot()
    return {
        "policy_version": gate.policy.policy_version,
        "policy_hash": canonical_sha256(policy_snapshot),
        "context_hash": canonical_sha256(context_snapshot),
        "policy": policy_snapshot,
        "context": context_snapshot,
    }


def _event(
    *,
    request: ProposalEvaluationRequest,
    provenance: DecisionProvenance,
    event_type: str,
    payload: dict[str, Any],
) -> DecisionLedgerEvent:
    return DecisionLedgerEvent(
        event_id=uuid4().hex,
        decision_run_id=request.decision_run_id,
        actor_scope_id=request.actor_scope_id,
        event_type=event_type,
        occurred_at=request.gate_context.now,
        provenance=provenance,
        payload_json=canonical_json(payload),
    )
