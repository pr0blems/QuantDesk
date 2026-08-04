from __future__ import annotations

import os
import socket
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy.engine import Engine


@dataclass(frozen=True, slots=True)
class LeaseOwner:
    worker_key: str
    owner_id: str
    metadata: dict[str, Any]

    @classmethod
    def create(cls, worker_key: str) -> LeaseOwner:
        return cls(
            worker_key=worker_key,
            owner_id=str(uuid.uuid4()),
            metadata={"hostname": socket.gethostname(), "pid": os.getpid()},
        )


class WorkerLease:
    """Database-backed renewable lease preventing duplicate worker roles."""

    def __init__(self, engine: Engine, owner: LeaseOwner, ttl_seconds: int):
        if not 15 <= ttl_seconds <= 300:
            raise ValueError("lease TTL must be between 15 and 300 seconds")
        self.engine = engine
        self.owner = owner
        self.ttl_seconds = ttl_seconds

    def acquire(self) -> bool:
        statement = """
            INSERT INTO worker_leases(
                worker_key,owner_id,metadata_json,acquired_at,heartbeat_at,expires_at
            ) VALUES (
                %s,%s,%s,UTC_TIMESTAMP(6),UTC_TIMESTAMP(6),
                TIMESTAMPADD(SECOND,%s,UTC_TIMESTAMP(6))
            )
            ON DUPLICATE KEY UPDATE
                owner_id=IF(
                    expires_at < UTC_TIMESTAMP(6) OR owner_id=VALUES(owner_id),
                    VALUES(owner_id),owner_id
                ),
                metadata_json=IF(owner_id=VALUES(owner_id),VALUES(metadata_json),metadata_json),
                acquired_at=IF(owner_id=VALUES(owner_id),VALUES(acquired_at),acquired_at),
                heartbeat_at=IF(owner_id=VALUES(owner_id),VALUES(heartbeat_at),heartbeat_at),
                expires_at=IF(owner_id=VALUES(owner_id),VALUES(expires_at),expires_at)
        """
        import json

        with self.engine.begin() as connection:
            connection.exec_driver_sql(
                statement,
                (
                    self.owner.worker_key,
                    self.owner.owner_id,
                    json.dumps(self.owner.metadata, separators=(",", ":")),
                    self.ttl_seconds,
                ),
            )
            current = connection.exec_driver_sql(
                "SELECT owner_id FROM worker_leases WHERE worker_key=%s FOR UPDATE",
                (self.owner.worker_key,),
            ).scalar_one()
        return current == self.owner.owner_id

    def heartbeat(self) -> bool:
        with self.engine.begin() as connection:
            result = connection.exec_driver_sql(
                """
                UPDATE worker_leases
                   SET heartbeat_at=UTC_TIMESTAMP(6),
                       expires_at=TIMESTAMPADD(SECOND,%s,UTC_TIMESTAMP(6))
                 WHERE worker_key=%s AND owner_id=%s
                """,
                (self.ttl_seconds, self.owner.worker_key, self.owner.owner_id),
            )
        return result.rowcount == 1

    def release(self) -> None:
        with self.engine.begin() as connection:
            connection.exec_driver_sql(
                "DELETE FROM worker_leases WHERE worker_key=%s AND owner_id=%s",
                (self.owner.worker_key, self.owner.owner_id),
            )
