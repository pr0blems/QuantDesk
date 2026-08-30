from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from quantdesk_v2.domain.runtime import (
    DECISION_ENVELOPE_VERSION,
    DecisionEnvelope,
    MarketEvent,
    MarketEventType,
    StrategyDecision,
    StrategyDecisionType,
    build_decision_envelope,
    canonical_event_hash,
    decision_record_key,
    strategy_decision_id,
)

NOW = datetime(2026, 8, 26, 1, 2, 3, tzinfo=UTC)


def test_decision_id_is_identical_across_runtime_modes() -> None:
    decision_id = strategy_decision_id("source-hash-1", "NVDAUSDT", "1h", NOW, "LONG_ENTRY")
    assert decision_id == strategy_decision_id(
        "source-hash-1", "NVDAUSDT", "1h", NOW, StrategyDecisionType.LONG_ENTRY
    )
    assert decision_record_key("shadow", 1, decision_id) != decision_record_key(
        "paper", 2, decision_id
    )


def test_market_event_enforces_point_in_time_availability_and_hash() -> None:
    payload = {"open": "1", "close": "2"}
    event = MarketEvent(
        event_id="bar-1",
        event_type=MarketEventType.BAR_CLOSED,
        symbol="nvdausdt",
        timeframe="1h",
        event_time=NOW,
        availability_time=NOW + timedelta(seconds=1),
        sequence=1,
        payload=payload,
        payload_hash=canonical_event_hash(payload),
    )
    assert event.symbol == "NVDAUSDT"
    with pytest.raises(ValueError, match="availability_time"):
        MarketEvent(
            event_id="bar-2",
            event_type=MarketEventType.BAR_CLOSED,
            symbol="NVDAUSDT",
            timeframe="1h",
            event_time=NOW,
            availability_time=NOW - timedelta(seconds=1),
            sequence=2,
            payload=payload,
            payload_hash=canonical_event_hash(payload),
        )


def test_strategy_decision_rejects_forged_identity() -> None:
    with pytest.raises(ValueError, match="decision id mismatch"):
        StrategyDecision(
            decision_id="forged",
            revision_fingerprint="revision-1",
            event_id="bar-1",
            symbol="NVDAUSDT",
            timeframe="1h",
            event_time=NOW,
            decision=StrategyDecisionType.HOLD,
            confidence=Decimal("0.5"),
            reason_codes=("NO_ENTRY",),
            evidence={},
        )


def test_decision_envelope_is_mode_neutral_and_canonical() -> None:
    envelope = build_decision_envelope(
        revision_fingerprint="source-hash-1",
        event_id="bar-1",
        symbol="nvdausdt",
        timeframe="1h",
        event_time=NOW,
        decision="SHORT_ENTRY",
        confidence=Decimal("0.75"),
        reason_codes=("EMA_CROSS_DOWN",),
        evidence={"ema_fast": "99"},
        risk_proposal={"stop_distance": "2"},
        valid_until=NOW + timedelta(hours=1),
    )

    assert isinstance(envelope, DecisionEnvelope)
    assert envelope.direction == -1
    assert envelope.snapshot()["version"] == DECISION_ENVELOPE_VERSION
    assert envelope.snapshot(mode="paper")["mode"] == "paper"
    assert envelope.snapshot(mode="shadow")["decision_id"] == envelope.decision_id
    assert "mode" not in envelope.snapshot()


def test_decision_envelope_rejects_exit_metadata_on_entry() -> None:
    with pytest.raises(ValueError, match="EXIT envelope"):
        build_decision_envelope(
            revision_fingerprint="source-hash-1",
            event_id="bar-1",
            symbol="NVDAUSDT",
            timeframe="1h",
            event_time=NOW,
            decision="LONG_ENTRY",
            exit_decision={"reason": "stop_loss"},
        )
