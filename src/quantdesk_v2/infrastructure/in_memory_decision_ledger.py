"""Thread-safe, append-only decision ledger for tests and single-process use."""

from __future__ import annotations

from collections.abc import Iterable
from threading import RLock

from ..domain.ai_decision import (
    GENESIS_RECORD_HASH,
    DecisionLedgerEvent,
    DecisionLedgerRecord,
    DuplicateDecisionEventError,
    decision_record_hash,
)


class InMemoryDecisionLedger:
    """A minimal hash-chained implementation of the decision-ledger port.

    There are intentionally no update or delete methods. Production storage can
    implement the same port with an INSERT-only table and database permissions.
    """

    def __init__(self) -> None:
        self._lock = RLock()
        self._records: list[DecisionLedgerRecord] = []
        self._event_ids: set[str] = set()
        self._heads: dict[str, tuple[int, str]] = {}

    def append(self, event: DecisionLedgerEvent) -> DecisionLedgerRecord:
        if not isinstance(event, DecisionLedgerEvent):
            raise TypeError("event must be a DecisionLedgerEvent")
        event = DecisionLedgerEvent.model_validate(event.model_dump())
        with self._lock:
            if event.event_id in self._event_ids:
                raise DuplicateDecisionEventError("decision event already exists")
            previous_sequence, previous_hash = self._heads.get(
                event.actor_scope_id,
                (0, GENESIS_RECORD_HASH),
            )
            sequence = previous_sequence + 1
            record = DecisionLedgerRecord(
                sequence=sequence,
                previous_record_hash=previous_hash,
                record_hash=decision_record_hash(
                    sequence=sequence,
                    previous_record_hash=previous_hash,
                    event=event,
                ),
                event=event,
            )
            self._records.append(record)
            self._event_ids.add(event.event_id)
            self._heads[event.actor_scope_id] = (sequence, record.record_hash)
            return record

    def records(
        self,
        *,
        decision_run_id: str | None = None,
        actor_scope_id: str | None = None,
    ) -> tuple[DecisionLedgerRecord, ...]:
        with self._lock:
            return tuple(
                record
                for record in self._records
                if (
                    (decision_run_id is None or record.event.decision_run_id == decision_run_id)
                    and (actor_scope_id is None or record.event.actor_scope_id == actor_scope_id)
                )
            )

    def verify_chain(self) -> bool:
        with self._lock:
            return verify_decision_chain(self._records)


def verify_decision_chain(records: Iterable[DecisionLedgerRecord]) -> bool:
    heads: dict[str, tuple[int, str]] = {}
    event_ids: set[str] = set()
    for record in records:
        scope_id = record.event.actor_scope_id
        previous_sequence, previous_hash = heads.get(scope_id, (0, GENESIS_RECORD_HASH))
        expected_sequence = previous_sequence + 1
        if record.sequence != expected_sequence:
            return False
        if record.previous_record_hash != previous_hash:
            return False
        if record.event.event_id in event_ids:
            return False
        expected_hash = decision_record_hash(
            sequence=record.sequence,
            previous_record_hash=previous_hash,
            event=record.event,
        )
        if record.record_hash != expected_hash:
            return False
        heads[scope_id] = (record.sequence, record.record_hash)
        event_ids.add(record.event.event_id)
    return True
