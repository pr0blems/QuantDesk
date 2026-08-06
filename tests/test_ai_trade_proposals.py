from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from quantdesk_v2.application.ai_ports import DecisionLedger
from quantdesk_v2.application.ai_proposals import (
    AiProposalService,
    ProposalEvaluationRequest,
)
from quantdesk_v2.application.proposal_gate import (
    ProposalGate,
    ProposalGateContext,
    ProposalGatePolicy,
)
from quantdesk_v2.domain.ai_decision import (
    DuplicateDecisionEventError,
    ProposalDisposition,
    ProposalValidationError,
    ReleaseStage,
    StrategyVersionStatus,
    canonical_json,
    parse_trade_proposal_json,
    trade_proposal_json_schema,
    trade_proposal_response_format,
)
from quantdesk_v2.infrastructure.in_memory_decision_ledger import (
    InMemoryDecisionLedger,
    verify_decision_chain,
)

NOW = datetime(2026, 8, 6, 4, 0, tzinfo=UTC)


def proposal_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": 1,
        "strategy_version_id": "strategy-42:revision-7",
        "symbol": "BTCUSDT",
        "action": "OPEN_LONG",
        "confidence": 0.82,
        "thesis": ["trend_confirmed", "volume_expansion"],
        "invalidation": "close_below_structure",
        "observed_at": (NOW - timedelta(minutes=1)).isoformat(),
        "valid_until": (NOW + timedelta(minutes=4)).isoformat(),
        "requested_risk": {
            "risk_fraction": 0.002,
            "max_slippage_bps": 12,
            "leverage": 2,
            "stop_loss_pct": 1.5,
            "take_profit_pct": 4.0,
        },
    }
    payload.update(overrides)
    return payload


def proposal_json(**overrides: object) -> str:
    return json.dumps(proposal_payload(**overrides), ensure_ascii=False)


def gate_context(
    stage: ReleaseStage = ReleaseStage.SHADOW,
    *,
    status: StrategyVersionStatus = StrategyVersionStatus.PUBLISHED,
    manual_approved: bool = False,
) -> ProposalGateContext:
    return ProposalGateContext(
        strategy_version_id="strategy-42:revision-7",
        strategy_version_status=status,
        symbol_universe=frozenset({"BTCUSDT", "ETHUSDT"}),
        stage=stage,
        now=NOW,
        manual_approved=manual_approved,
        manual_approval_id="approval:20260806:001" if manual_approved else None,
        manual_approved_by="user:operator-9" if manual_approved else None,
        manual_approved_at=(
            NOW - timedelta(seconds=30) if manual_approved else None
        ),
    )


def evaluation_request(
    *,
    raw_output: str | bytes | None = None,
    stage: ReleaseStage = ReleaseStage.SHADOW,
    prompt: str = "trade-proposal-v1",
    model_name: str = "model-a",
    input_snapshot: dict[str, object] | None = None,
) -> ProposalEvaluationRequest:
    return ProposalEvaluationRequest(
        raw_model_output=raw_output if raw_output is not None else proposal_json(),
        decision_run_id="run-20260806-001",
        actor_scope_id="user:17",
        prompt_template=prompt,
        provider_code="test-provider",
        model_name=model_name,
        model_version="2026-08-01",
        input_snapshot=input_snapshot or {"bar_close": "117000.25", "bar_id": 42},
        gate_context=gate_context(stage),
    )


def test_model_json_is_strictly_parsed_and_schema_is_exportable() -> None:
    proposal = parse_trade_proposal_json(proposal_json(symbol="btcusdt"))

    assert proposal.symbol == "BTCUSDT"
    assert proposal.confidence == Decimal("0.82")
    assert proposal.requested_risk is not None
    assert proposal.requested_risk.risk_fraction == Decimal("0.002")
    schema = trade_proposal_json_schema()
    assert schema["additionalProperties"] is False
    assert schema["properties"]["confidence"]["type"] == "number"
    assert schema["$defs"]["RequestedRisk"]["additionalProperties"] is False
    assert schema["$defs"]["RequestedRisk"]["properties"]["risk_fraction"]["type"] == "number"
    assert set(schema["required"]) == set(schema["properties"])
    assert trade_proposal_response_format()["json_schema"]["strict"] is True


@pytest.mark.parametrize(
    ("raw_output", "code"),
    [
        ('{"schema_version":1,"schema_version":1}', "duplicate_key"),
        ("not-json", "invalid_json"),
        (b"\xff", "invalid_encoding"),
        (json.dumps(["not", "an", "object"]), "invalid_schema"),
        ("{}", "invalid_schema"),
    ],
)
def test_model_output_errors_are_redacted_and_stable(
    raw_output: str | bytes,
    code: str,
) -> None:
    with pytest.raises(ProposalValidationError) as caught:
        parse_trade_proposal_json(raw_output)

    assert caught.value.code == code
    assert str(caught.value) == "AI trade proposal validation failed"


@pytest.mark.parametrize(
    "overrides",
    [
        {"exchange_order": {"type": "MARKET"}},
        {"action": "BUY_NOW"},
        {"confidence": "0.99"},
        {"thesis": ["same_reason", "same_reason"]},
        {"thesis": ["  "]},
        {"invalidation": "  "},
        {"invalidation": "api_key=abcdefghijklmnop"},
        {"invalidation": "<img src=x onerror=trade()>"},
        {"requested_risk": {"risk_fraction": 0.1, "max_slippage_bps": 1, "leverage": 2}, "action": "HOLD"},
        {"requested_risk": None, "action": "OPEN_SHORT"},
        {"observed_at": "2026-08-06T03:59:00", "valid_until": "2026-08-06T04:04:00"},
    ],
)
def test_unsafe_ambiguous_or_unknown_model_fields_are_rejected(
    overrides: dict[str, object],
) -> None:
    with pytest.raises(ProposalValidationError) as caught:
        parse_trade_proposal_json(proposal_json(**overrides))

    assert caught.value.code == "invalid_schema"


@pytest.mark.parametrize(
    ("stage", "manual_approved", "disposition", "risk_review"),
    [
        (ReleaseStage.REPLAY, False, ProposalDisposition.RECORD_ONLY, False),
        (ReleaseStage.SHADOW, False, ProposalDisposition.RECORD_ONLY, False),
        (
            ReleaseStage.MANUAL,
            False,
            ProposalDisposition.MANUAL_REVIEW_REQUIRED,
            False,
        ),
        (
            ReleaseStage.MANUAL,
            True,
            ProposalDisposition.RISK_REVIEW_REQUIRED,
            True,
        ),
        (ReleaseStage.CANARY, False, ProposalDisposition.RISK_REVIEW_REQUIRED, True),
        (ReleaseStage.LIVE, False, ProposalDisposition.RISK_REVIEW_REQUIRED, True),
    ],
)
def test_release_stage_never_grants_direct_order_permission(
    stage: ReleaseStage,
    manual_approved: bool,
    disposition: ProposalDisposition,
    risk_review: bool,
) -> None:
    proposal = parse_trade_proposal_json(proposal_json())
    result = ProposalGate().evaluate(
        proposal,
        gate_context(stage, manual_approved=manual_approved),
    )

    assert result.disposition is disposition
    assert result.requires_deterministic_risk_review is risk_review
    assert result.may_submit_order is False


def test_deeply_nested_json_is_rejected_without_escaping_the_audit_boundary() -> None:
    nested = '{"x":' * 5_000 + "0" + "}" * 5_000

    with pytest.raises(ProposalValidationError) as caught:
        parse_trade_proposal_json(nested)

    assert caught.value.code == "invalid_json"


def test_gate_collects_strategy_universe_freshness_confidence_and_risk_failures() -> None:
    proposal = parse_trade_proposal_json(
        proposal_json(
            strategy_version_id="strategy-other:revision-8",
            symbol="SOLUSDT",
            confidence=0.2,
            observed_at=(NOW - timedelta(minutes=20)).isoformat(),
            valid_until=(NOW + timedelta(minutes=30)).isoformat(),
            requested_risk={
                "risk_fraction": 0.2,
                "max_slippage_bps": 500,
                "leverage": 20,
                "stop_loss_pct": None,
                "take_profit_pct": None,
            },
        )
    )

    result = ProposalGate().evaluate(proposal, gate_context(ReleaseStage.LIVE))

    assert result.disposition is ProposalDisposition.REJECTED
    assert set(result.reason_codes) == {
        "strategy_version_mismatch",
        "symbol_outside_universe",
        "market_context_stale",
        "validity_window_excessive",
        "confidence_below_minimum",
        "risk_fraction_exceeds_limit",
        "slippage_exceeds_limit",
        "leverage_exceeds_limit",
    }
    assert result.may_submit_order is False


def test_replay_allows_draft_but_forward_stages_require_published_revision() -> None:
    proposal = parse_trade_proposal_json(proposal_json())

    replay = ProposalGate().evaluate(
        proposal,
        gate_context(ReleaseStage.REPLAY, status=StrategyVersionStatus.DRAFT),
    )
    shadow = ProposalGate().evaluate(
        proposal,
        gate_context(ReleaseStage.SHADOW, status=StrategyVersionStatus.DRAFT),
    )

    assert replay.disposition is ProposalDisposition.RECORD_ONLY
    assert shadow.disposition is ProposalDisposition.REJECTED
    assert "strategy_version_not_published" in shadow.reason_codes


def test_canary_uses_a_tighter_risk_cap() -> None:
    proposal = parse_trade_proposal_json(
        proposal_json(
            requested_risk={
                "risk_fraction": 0.005,
                "max_slippage_bps": 12,
                "leverage": 2,
                "stop_loss_pct": None,
                "take_profit_pct": None,
            }
        )
    )
    gate = ProposalGate(
        ProposalGatePolicy(
            maximum_risk_fraction=Decimal("0.01"),
            canary_maximum_risk_fraction=Decimal("0.0025"),
        )
    )

    canary = gate.evaluate(proposal, gate_context(ReleaseStage.CANARY))
    live = gate.evaluate(proposal, gate_context(ReleaseStage.LIVE))

    assert "risk_fraction_exceeds_limit" in canary.reason_codes
    assert live.disposition is ProposalDisposition.RISK_REVIEW_REQUIRED


def test_service_hashes_sensitive_inputs_and_appends_a_valid_chain() -> None:
    ledger = InMemoryDecisionLedger()
    service = AiProposalService(ledger)

    evaluation = service.evaluate(evaluation_request())
    second = service.evaluate(
        evaluation_request(
            stage=ReleaseStage.REPLAY,
            prompt="trade-proposal-v2",
            model_name="model-b",
            input_snapshot={"bar_close": "117100.50", "bar_id": 43},
        )
    )

    assert isinstance(ledger, DecisionLedger)
    assert evaluation.proposal is not None
    assert evaluation.gate_result.disposition is ProposalDisposition.RECORD_ONLY
    assert evaluation.ledger_record.sequence == 1
    assert second.ledger_record.sequence == 2
    assert second.ledger_record.previous_record_hash == evaluation.ledger_record.record_hash
    assert ledger.verify_chain() is True
    first_event = evaluation.ledger_record.event
    assert first_event.provenance.prompt_hash != second.ledger_record.event.provenance.prompt_hash
    assert first_event.provenance.model_hash != second.ledger_record.event.provenance.model_hash
    assert first_event.provenance.input_hash != second.ledger_record.event.provenance.input_hash
    assert "trade-proposal-v1" not in first_event.payload_json
    assert "test-provider" not in first_event.payload_json
    assert "117000.25" not in first_event.payload_json
    payload = json.loads(first_event.payload_json)
    evidence = payload["gate_evidence"]
    assert evidence["policy_version"] == "proposal-gate-v1"
    assert len(evidence["policy_hash"]) == 64
    assert len(evidence["context_hash"]) == 64
    assert evidence["context"]["strategy_version_id"] == "strategy-42:revision-7"


def test_invalid_model_output_is_audited_without_persisting_raw_content() -> None:
    ledger = InMemoryDecisionLedger()
    raw = '{"thesis":"secret-marker","password":"do-not-store"}'

    evaluation = AiProposalService(ledger).evaluate(evaluation_request(raw_output=raw))

    assert evaluation.proposal is None
    assert evaluation.validation_code == "invalid_schema"
    assert evaluation.gate_result.reason_codes == ("invalid_model_output",)
    assert evaluation.ledger_record.event.event_type == "proposal_rejected"
    assert "secret-marker" not in evaluation.ledger_record.event.payload_json
    assert "do-not-store" not in evaluation.ledger_record.event.payload_json
    assert evaluation.ledger_record.event.provenance.output_hash


def test_ledger_rejects_duplicate_events_and_detects_tampering() -> None:
    source = InMemoryDecisionLedger()
    evaluation = AiProposalService(source).evaluate(evaluation_request())
    event = evaluation.ledger_record.event
    ledger = InMemoryDecisionLedger()
    first = ledger.append(event)

    with pytest.raises(DuplicateDecisionEventError):
        ledger.append(event)

    tampered = first.model_copy(update={"record_hash": "f" * 64})
    assert verify_decision_chain((tampered,)) is False


def test_ledger_filters_are_read_only_snapshots() -> None:
    ledger = InMemoryDecisionLedger()
    AiProposalService(ledger).evaluate(evaluation_request())

    assert len(ledger.records(decision_run_id="run-20260806-001")) == 1
    assert ledger.records(actor_scope_id="user:other") == ()
    assert isinstance(ledger.records(), tuple)
    payload = json.loads(ledger.records()[0].event.payload_json)
    assert canonical_json(payload) == ledger.records()[0].event.payload_json


def test_ledger_events_canonicalize_non_utc_offsets_before_hashing() -> None:
    event = AiProposalService(InMemoryDecisionLedger()).evaluate(
        evaluation_request()
    ).ledger_record.event
    shifted = event.model_copy(
        update={"occurred_at": event.occurred_at.astimezone(timezone(timedelta(hours=8)))}
    )

    normalized = type(event).model_validate(shifted.model_dump())

    assert normalized.occurred_at.tzinfo is UTC
    assert normalized.occurred_at == event.occurred_at


def test_manual_gate_requires_a_complete_approval_audit_reference() -> None:
    with pytest.raises(ValueError, match="complete audit evidence"):
        ProposalGateContext(
            strategy_version_id="strategy-42:revision-7",
            strategy_version_status=StrategyVersionStatus.PUBLISHED,
            symbol_universe=frozenset({"BTCUSDT"}),
            stage=ReleaseStage.MANUAL,
            now=NOW,
            manual_approved=True,
        )

    result = ProposalGate().evaluate(
        parse_trade_proposal_json(proposal_json()),
        gate_context(ReleaseStage.MANUAL, manual_approved=True),
    )
    assert result.disposition is ProposalDisposition.RISK_REVIEW_REQUIRED
