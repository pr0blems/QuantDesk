"""MySQL append-only persistence adapter for AI decision events."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Engine, column, select, table, text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import IntegrityError

from ...domain.ai_decision import (
    GENESIS_RECORD_HASH,
    DecisionLedgerEvent,
    DecisionLedgerRecord,
    DecisionProvenance,
    DuplicateDecisionEventError,
    decision_record_hash,
)

_LEDGER_RECORDS = table(
    "ai_decision_ledger_records",
    column("event_id"),
    column("actor_scope_id"),
    column("sequence"),
    column("decision_run_id"),
    column("event_type"),
    column("occurred_at"),
    column("prompt_hash"),
    column("model_hash"),
    column("input_hash"),
    column("output_hash"),
    column("payload_json"),
    column("previous_record_hash"),
    column("record_hash"),
)
_LEDGER_HEADS = table(
    "ai_decision_ledger_heads",
    column("actor_scope_id"),
    column("last_sequence"),
    column("last_record_hash"),
)


class DecisionLedgerIntegrityError(RuntimeError):
    """Raised when persisted ledger content no longer matches its record hash."""


class MySqlDecisionLedger:
    """Store a separately chained, immutable event stream per actor scope.

    The actor head row is locked while assigning a sequence number. This makes
    concurrent appends atomic without imposing a single global ledger lock.
    Only hashes and the already-redacted canonical payload are persisted.
    """

    def __init__(self, engine: Engine) -> None:
        if engine.dialect.name not in {"mysql", "mariadb"}:
            raise RuntimeError("AI decision ledger requires MySQL or MariaDB")
        self._engine = engine

    def append(self, event: DecisionLedgerEvent) -> DecisionLedgerRecord:
        if not isinstance(event, DecisionLedgerEvent):
            raise TypeError("event must be a DecisionLedgerEvent")
        event = DecisionLedgerEvent.model_validate(event.model_dump())
        try:
            with self._engine.begin() as connection:
                connection.execute(
                    text(
                        """
                        INSERT INTO ai_decision_ledger_heads (
                            actor_scope_id,
                            last_sequence,
                            last_record_hash,
                            updated_at
                        ) VALUES (
                            :actor_scope_id,
                            0,
                            :genesis_record_hash,
                            UTC_TIMESTAMP(6)
                        )
                        ON DUPLICATE KEY UPDATE
                            actor_scope_id = VALUES(actor_scope_id)
                        """
                    ),
                    {
                        "actor_scope_id": event.actor_scope_id,
                        "genesis_record_hash": GENESIS_RECORD_HASH,
                    },
                )
                head = connection.execute(
                    text(
                        """
                        SELECT last_sequence, last_record_hash
                        FROM ai_decision_ledger_heads
                        WHERE actor_scope_id = :actor_scope_id
                        FOR UPDATE
                        """
                    ),
                    {"actor_scope_id": event.actor_scope_id},
                ).mappings().one()
                duplicate = connection.execute(
                    text(
                        """
                        SELECT 1
                        FROM ai_decision_ledger_records
                        WHERE event_id = :event_id
                        LIMIT 1
                        """
                    ),
                    {"event_id": event.event_id},
                ).first()
                if duplicate is not None:
                    raise DuplicateDecisionEventError("decision event already exists")

                sequence = int(head["last_sequence"]) + 1
                previous_record_hash = str(head["last_record_hash"])
                record_hash = decision_record_hash(
                    sequence=sequence,
                    previous_record_hash=previous_record_hash,
                    event=event,
                )
                connection.execute(
                    text(
                        """
                        INSERT INTO ai_decision_ledger_records (
                            event_id,
                            actor_scope_id,
                            sequence,
                            decision_run_id,
                            event_type,
                            occurred_at,
                            prompt_hash,
                            model_hash,
                            input_hash,
                            output_hash,
                            payload_json,
                            previous_record_hash,
                            record_hash,
                            created_at
                        ) VALUES (
                            :event_id,
                            :actor_scope_id,
                            :sequence,
                            :decision_run_id,
                            :event_type,
                            :occurred_at,
                            :prompt_hash,
                            :model_hash,
                            :input_hash,
                            :output_hash,
                            :payload_json,
                            :previous_record_hash,
                            :record_hash,
                            UTC_TIMESTAMP(6)
                        )
                        """
                    ),
                    {
                        "event_id": event.event_id,
                        "actor_scope_id": event.actor_scope_id,
                        "sequence": sequence,
                        "decision_run_id": event.decision_run_id,
                        "event_type": event.event_type,
                        "occurred_at": _mysql_datetime(event.occurred_at),
                        "prompt_hash": event.provenance.prompt_hash,
                        "model_hash": event.provenance.model_hash,
                        "input_hash": event.provenance.input_hash,
                        "output_hash": event.provenance.output_hash,
                        "payload_json": event.payload_json,
                        "previous_record_hash": previous_record_hash,
                        "record_hash": record_hash,
                    },
                )
                connection.execute(
                    text(
                        """
                        UPDATE ai_decision_ledger_heads
                        SET last_sequence = :sequence,
                            last_record_hash = :record_hash,
                            updated_at = UTC_TIMESTAMP(6)
                        WHERE actor_scope_id = :actor_scope_id
                        """
                    ),
                    {
                        "actor_scope_id": event.actor_scope_id,
                        "sequence": sequence,
                        "record_hash": record_hash,
                    },
                )
        except DuplicateDecisionEventError:
            raise
        except IntegrityError as exc:
            if self._event_exists(event.event_id):
                raise DuplicateDecisionEventError("decision event already exists") from None
            raise exc

        return DecisionLedgerRecord(
            sequence=sequence,
            previous_record_hash=previous_record_hash,
            record_hash=record_hash,
            event=event,
        )

    def records(
        self,
        *,
        decision_run_id: str | None = None,
        actor_scope_id: str | None = None,
    ) -> tuple[DecisionLedgerRecord, ...]:
        statement = select(*_LEDGER_RECORDS.c).order_by(
            _LEDGER_RECORDS.c.actor_scope_id.asc(),
            _LEDGER_RECORDS.c.sequence.asc(),
        )
        if decision_run_id is not None:
            statement = statement.where(
                _LEDGER_RECORDS.c.decision_run_id == decision_run_id
            )
        if actor_scope_id is not None:
            statement = statement.where(_LEDGER_RECORDS.c.actor_scope_id == actor_scope_id)
        with self._engine.connect() as connection:
            rows = connection.execute(statement).mappings().all()
            if actor_scope_id is not None:
                verification_scopes: frozenset[str] | None = frozenset({actor_scope_id})
            elif decision_run_id is not None:
                verification_scopes = frozenset(str(row["actor_scope_id"]) for row in rows)
            else:
                verification_scopes = None
            verified = _verified_chain_records(connection, verification_scopes)
        return tuple(
            verified[(str(row["actor_scope_id"]), int(row["sequence"]))]
            for row in rows
        )

    def _event_exists(self, event_id: str) -> bool:
        with self._engine.connect() as connection:
            return (
                connection.execute(
                    text(
                        """
                        SELECT 1
                        FROM ai_decision_ledger_records
                        WHERE event_id = :event_id
                        LIMIT 1
                        """
                    ),
                    {"event_id": event_id},
                ).first()
                is not None
            )


def _mysql_datetime(value: datetime) -> datetime:
    return value.astimezone(UTC).replace(tzinfo=None)


def _utc_datetime(value: object) -> datetime:
    if not isinstance(value, datetime):
        raise DecisionLedgerIntegrityError("ledger datetime has an invalid type")
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _record_from_row(row: Mapping[str, Any]) -> DecisionLedgerRecord:
    provenance = DecisionProvenance(
        prompt_hash=str(row["prompt_hash"]),
        model_hash=str(row["model_hash"]),
        input_hash=str(row["input_hash"]),
        output_hash=str(row["output_hash"]),
    )
    event = DecisionLedgerEvent(
        event_id=str(row["event_id"]),
        decision_run_id=str(row["decision_run_id"]),
        actor_scope_id=str(row["actor_scope_id"]),
        event_type=str(row["event_type"]),
        occurred_at=_utc_datetime(row["occurred_at"]),
        provenance=provenance,
        payload_json=str(row["payload_json"]),
    )
    sequence = int(row["sequence"])
    previous_record_hash = str(row["previous_record_hash"])
    record_hash = str(row["record_hash"])
    expected_hash = decision_record_hash(
        sequence=sequence,
        previous_record_hash=previous_record_hash,
        event=event,
    )
    if record_hash != expected_hash:
        raise DecisionLedgerIntegrityError("AI decision ledger hash verification failed")
    return DecisionLedgerRecord(
        sequence=sequence,
        previous_record_hash=previous_record_hash,
        record_hash=record_hash,
        event=event,
    )


def _verified_chain_records(
    connection: Connection,
    actor_scope_ids: frozenset[str] | None,
) -> dict[tuple[str, int], DecisionLedgerRecord]:
    if actor_scope_ids == frozenset():
        return {}
    records_statement = select(*_LEDGER_RECORDS.c).order_by(
        _LEDGER_RECORDS.c.actor_scope_id.asc(),
        _LEDGER_RECORDS.c.sequence.asc(),
    )
    heads_statement = select(*_LEDGER_HEADS.c).order_by(
        _LEDGER_HEADS.c.actor_scope_id.asc()
    )
    if actor_scope_ids is not None:
        records_statement = records_statement.where(
            _LEDGER_RECORDS.c.actor_scope_id.in_(actor_scope_ids)
        )
        heads_statement = heads_statement.where(
            _LEDGER_HEADS.c.actor_scope_id.in_(actor_scope_ids)
        )

    rows = connection.execute(records_statement).mappings().all()
    head_rows = connection.execute(heads_statement).mappings().all()
    heads = {str(row["actor_scope_id"]): row for row in head_rows}
    grouped: dict[str, list[Mapping[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(str(row["actor_scope_id"]), []).append(row)
    if not set(grouped).issubset(heads):
        raise DecisionLedgerIntegrityError("AI decision ledger head is missing")

    verified: dict[tuple[str, int], DecisionLedgerRecord] = {}
    for scope_id, head in heads.items():
        previous_hash = GENESIS_RECORD_HASH
        last_sequence = 0
        for row in grouped.get(scope_id, []):
            record = _record_from_row(row)
            if record.sequence != last_sequence + 1:
                raise DecisionLedgerIntegrityError("AI decision ledger sequence gap detected")
            if record.previous_record_hash != previous_hash:
                raise DecisionLedgerIntegrityError("AI decision ledger link mismatch detected")
            verified[(scope_id, record.sequence)] = record
            last_sequence = record.sequence
            previous_hash = record.record_hash
        if (
            int(head["last_sequence"]) != last_sequence
            or str(head["last_record_hash"]) != previous_hash
        ):
            raise DecisionLedgerIntegrityError("AI decision ledger head mismatch detected")
    return verified
