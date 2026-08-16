from datetime import UTC, datetime

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from quantdesk_v2.market_risk_sync import (
    economic_event_risk_level,
    sync_economic_calendar,
)
from quantdesk_v2.models import MarketRiskEvent


def test_economic_event_risk_level_only_blocks_known_market_movers() -> None:
    assert economic_event_risk_level("FOMC Interest Rate Decision") == "critical"
    assert economic_event_risk_level("Nonfarm Payrolls") == "critical"
    assert economic_event_risk_level("Initial Jobless Claims") == "high"
    assert economic_event_risk_level("Wholesale Inventories") == "medium"


def test_sync_economic_calendar_is_idempotent_and_advances_status() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    with engine.begin() as connection:
        connection.exec_driver_sql(
            """
            CREATE TABLE market_risk_events (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              public_id VARCHAR(36) NOT NULL UNIQUE,
              provider VARCHAR(32) NOT NULL,
              provider_event_id VARCHAR(96),
              event_type VARCHAR(32) NOT NULL,
              event_name VARCHAR(191) NOT NULL,
              symbol VARCHAR(32),
              scheduled_at DATETIME NOT NULL,
              actual_at DATETIME,
              risk_level VARCHAR(16) NOT NULL,
              blocking_before_seconds INTEGER NOT NULL,
              blocking_after_seconds INTEGER NOT NULL,
              status VARCHAR(16) NOT NULL,
              dedup_key VARCHAR(191) NOT NULL,
              source_payload_json JSON,
              source_updated_at DATETIME,
              created_at DATETIME NOT NULL,
              UNIQUE(provider, dedup_key)
            )
            """
        )
    now = datetime(2026, 8, 16, 12, 0)
    payload = {
        "events": [
            {
                "event": "FOMC Interest Rate Decision",
                "event_time_ms": int(
                    datetime(2026, 8, 16, 12, 10, tzinfo=UTC).timestamp() * 1_000
                ),
                "type": "fed",
                "forecast": "4.25%",
                "previous": "4.50%",
            },
            {
                "event": "Wholesale Inventories",
                "event_time_ms": int(
                    datetime(2026, 8, 15, 12, 0, tzinfo=UTC).timestamp() * 1_000
                ),
                "type": "inventory",
            },
        ]
    }

    with Session(engine) as db:
        first = sync_economic_calendar(db, payload, now=now)
        db.commit()
        second = sync_economic_calendar(db, payload, now=now)
        db.commit()
        rows = db.scalars(select(MarketRiskEvent).order_by(MarketRiskEvent.event_name)).all()

    assert first == {"created": 2, "updated": 0, "skipped": 0}
    assert second == {"created": 0, "updated": 2, "skipped": 0}
    assert len(rows) == 2
    fomc = next(row for row in rows if row.event_name.startswith("FOMC"))
    inventory = next(row for row in rows if row.event_name.startswith("Wholesale"))
    assert fomc.risk_level == "critical"
    assert fomc.status == "active"
    assert inventory.risk_level == "medium"
    assert inventory.status == "completed"
