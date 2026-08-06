from __future__ import annotations

import importlib.util
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import Engine, text

from quantdesk_v2.application.ai_ports import DecisionLedger
from quantdesk_v2.domain.ai_decision import (
    DecisionLedgerEvent,
    DecisionProvenance,
    DuplicateDecisionEventError,
    canonical_json,
)
from quantdesk_v2.infrastructure.persistence.ai_decisions import (
    DecisionLedgerIntegrityError,
    MySqlDecisionLedger,
)


def _event(
    event_id: str,
    *,
    actor_scope_id: str = "tenant:7/user:17",
    decision_run_id: str = "run-20260806-001",
) -> DecisionLedgerEvent:
    return DecisionLedgerEvent(
        event_id=event_id,
        decision_run_id=decision_run_id,
        actor_scope_id=actor_scope_id,
        event_type="proposal_gated",
        occurred_at=datetime(2026, 8, 6, 4, 5, 6, 123456, tzinfo=UTC),
        provenance=DecisionProvenance(
            prompt_hash="1" * 64,
            model_hash="2" * 64,
            input_hash="3" * 64,
            output_hash="4" * 64,
        ),
        payload_json=canonical_json({"disposition": "record_only"}),
    )


def test_ai_decision_ledger_migration_follows_current_head() -> None:
    migration_path = (
        Path(__file__).parents[1]
        / "migrations"
        / "versions"
        / "0030_ai_decision_ledger.py"
    )
    spec = importlib.util.spec_from_file_location("ai_decision_ledger_0030", migration_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    assert module.revision == "0030_ai_decision_ledger"
    assert module.down_revision == "0029_market_microstructure"


def test_mysql_ledger_is_scoped_append_only_and_hash_verified(
    mysql_test_engine: Engine,
) -> None:
    ledger = MySqlDecisionLedger(mysql_test_engine)
    assert isinstance(ledger, DecisionLedger)

    first = ledger.append(_event("a" * 32))
    second = ledger.append(_event("b" * 32))
    other = ledger.append(
        _event(
            "c" * 32,
            actor_scope_id="tenant:8/user:19",
            decision_run_id="run-other",
        )
    )

    assert (first.sequence, second.sequence, other.sequence) == (1, 2, 1)
    assert second.previous_record_hash == first.record_hash
    assert len(ledger.records(actor_scope_id="tenant:7/user:17")) == 2
    assert len(ledger.records(decision_run_id="run-other")) == 1
    assert ledger.records(actor_scope_id="tenant:missing") == ()

    with pytest.raises(DuplicateDecisionEventError):
        ledger.append(_event("a" * 32))

    with mysql_test_engine.connect() as connection:
        stored = connection.execute(
            text(
                """
                SELECT prompt_hash, model_hash, input_hash, output_hash, payload_json
                FROM ai_decision_ledger_records
                WHERE event_id = :event_id
                """
            ),
            {"event_id": "a" * 32},
        ).mappings().one()
    assert set(stored) == {
        "prompt_hash",
        "model_hash",
        "input_hash",
        "output_hash",
        "payload_json",
    }


def test_mysql_ledger_detects_persisted_payload_tampering(mysql_test_engine: Engine) -> None:
    ledger = MySqlDecisionLedger(mysql_test_engine)
    ledger.append(_event("d" * 32))
    with mysql_test_engine.begin() as connection:
        connection.execute(
            text(
                """
                UPDATE ai_decision_ledger_records
                SET payload_json = :payload_json
                WHERE event_id = :event_id
                """
            ),
            {
                "event_id": "d" * 32,
                "payload_json": canonical_json({"disposition": "tampered"}),
            },
        )

    with pytest.raises(DecisionLedgerIntegrityError):
        ledger.records(actor_scope_id="tenant:7/user:17")


def test_mysql_ledger_normalizes_offsets_and_detects_deleted_links(
    mysql_test_engine: Engine,
) -> None:
    ledger = MySqlDecisionLedger(mysql_test_engine)
    shifted = _event("e" * 32).model_copy(
        update={
            "occurred_at": datetime(
                2026,
                8,
                6,
                12,
                5,
                6,
                123456,
                tzinfo=timezone(timedelta(hours=8)),
            )
        }
    )
    first = ledger.append(shifted)
    ledger.append(_event("f" * 32))

    persisted = ledger.records(actor_scope_id="tenant:7/user:17")
    assert persisted[0].record_hash == first.record_hash
    assert persisted[0].event.occurred_at.tzinfo is UTC

    with mysql_test_engine.begin() as connection:
        connection.execute(
            text(
                """
                DELETE FROM ai_decision_ledger_records
                WHERE actor_scope_id = :actor_scope_id AND sequence = 1
                """
            ),
            {"actor_scope_id": "tenant:7/user:17"},
        )
    with pytest.raises(DecisionLedgerIntegrityError, match="sequence gap"):
        ledger.records(actor_scope_id="tenant:7/user:17")
