"""Read-only inventory for legacy strategy records and their live relationships.

This command never mutates data.  It is the required dry-run gate before any
future schema contraction removes legacy strategy columns or enum values.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from typing import Any

from sqlalchemy import String, cast, func, select
from sqlalchemy.orm import Session

from quantdesk_v2.config import get_settings
from quantdesk_v2.database import build_engine
from quantdesk_v2.models import (
    LiveTradingAccount,
    PaperAccount,
    StrategyDeployment,
    StrategyRevision,
    StrategyTemplate,
    UserStrategy,
)

LEGACY_KIND = "legacy_signal"
LEGACY_PAPER_MODE = "legacy_score_v1"
ACTIVE_DEPLOYMENT_STATUSES = ("created", "running", "paused", "error")


def _count(db: Session, statement: Any) -> int:
    return int(db.scalar(statement) or 0)


def _sample_hashes(db: Session, limit: int) -> list[dict[str, Any]]:
    rows = db.execute(
        select(
            UserStrategy.id,
            UserStrategy.public_id,
            UserStrategy.user_id,
            UserStrategy.version,
            UserStrategy.engine_key,
            UserStrategy.spec_hash,
            UserStrategy.source_hash,
        )
        .where(UserStrategy.strategy_kind == LEGACY_KIND)
        .order_by(UserStrategy.id)
        .limit(limit)
    ).all()
    result: list[dict[str, Any]] = []
    for row in rows:
        payload = {
            "id": int(row.id),
            "public_id": str(row.public_id),
            "user_id": int(row.user_id),
            "version": int(row.version),
            "engine_key": str(row.engine_key),
            "spec_hash": row.spec_hash,
            "source_hash": row.source_hash,
        }
        canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        result.append({**payload, "inventory_hash": hashlib.sha256(canonical.encode()).hexdigest()})
    return result


def inventory(db: Session, *, sample_limit: int = 20) -> dict[str, Any]:
    legacy_strategy_filter = UserStrategy.strategy_kind == LEGACY_KIND
    legacy_deployment_join = StrategyDeployment.__table__.join(
        UserStrategy.__table__,
        UserStrategy.id == StrategyDeployment.strategy_id,
    )
    counts = {
        "legacy_templates": _count(
            db,
            select(func.count(StrategyTemplate.id)).where(
                StrategyTemplate.template_kind == LEGACY_KIND
            ),
        ),
        "legacy_user_strategies": _count(
            db,
            select(func.count(UserStrategy.id)).where(legacy_strategy_filter),
        ),
        "legacy_strategy_revisions": _count(
            db,
            select(func.count(StrategyRevision.id))
            .select_from(StrategyRevision)
            .join(UserStrategy, UserStrategy.id == StrategyRevision.user_strategy_id)
            .where(legacy_strategy_filter),
        ),
        "legacy_deployments_total": _count(
            db,
            select(func.count(StrategyDeployment.id))
            .select_from(legacy_deployment_join)
            .where(legacy_strategy_filter),
        ),
        "legacy_deployments_active": _count(
            db,
            select(func.count(StrategyDeployment.id))
            .select_from(legacy_deployment_join)
            .where(
                legacy_strategy_filter,
                StrategyDeployment.status.in_(ACTIVE_DEPLOYMENT_STATUSES),
            ),
        ),
        "paper_accounts_with_legacy_signal_mode": _count(
            db,
            select(func.count(PaperAccount.id)).where(
                cast(PaperAccount.config_json, String).contains(LEGACY_PAPER_MODE)
            ),
        ),
        "live_accounts_with_legacy_snapshot": _count(
            db,
            select(func.count(LiveTradingAccount.id)).where(
                cast(LiveTradingAccount.strategy_snapshot_json, String).contains(LEGACY_KIND)
            ),
        ),
    }
    blockers = {
        key: value
        for key, value in counts.items()
        if key
        in {
            "legacy_deployments_active",
            "paper_accounts_with_legacy_signal_mode",
            "live_accounts_with_legacy_snapshot",
        }
        and value > 0
    }
    return {
        "mode": "dry-run",
        "read_only": True,
        "counts": counts,
        "active_blockers": blockers,
        "safe_to_remove_runtime_compatibility": not blockers,
        "sample_strategy_hashes": _sample_hashes(db, max(1, min(sample_limit, 100))),
        "decision": (
            "legacy history may remain sealed read-only; runtime compatibility is unused"
            if not blockers
            else "retain runtime compatibility until active legacy relationships are migrated"
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sample-limit", type=int, default=20)
    parser.add_argument("--fail-on-active", action="store_true")
    args = parser.parse_args()
    engine = build_engine(
        get_settings(),
        connect_timeout=15,
        read_timeout=120,
        write_timeout=120,
    )
    with Session(engine, expire_on_commit=False) as db:
        report = inventory(db, sample_limit=args.sample_limit)
        db.rollback()
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    return 2 if args.fail_on_active and report["active_blockers"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
