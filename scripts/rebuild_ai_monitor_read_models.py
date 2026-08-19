"""Backfill and reconcile disposable AI-monitor read models.

The command is intentionally dry-run by default.  Apply migration 0059 first,
then pass ``--apply`` during a controlled maintenance window.
"""

from __future__ import annotations

import argparse
import json

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from quantdesk_v2.ai_monitor_read_models import (
    read_models_available,
    reconcile_ai_monitor_read_models,
    refresh_ai_monitor_read_models,
)
from quantdesk_v2.config import get_settings
from quantdesk_v2.database import build_engine
from quantdesk_v2.models import (
    AiMonitorOpportunity,
    AiMonitorOpportunityCurrent,
    AiMonitorPrediction,
    AiMonitorPredictionFact,
    AiMonitorScoreHistory,
    OpportunityGateDecision,
)


def _source_counts(db: Session, user_id: int | None) -> dict[str, int]:
    filters = [] if user_id is None else [AiMonitorPrediction.user_id == user_id]
    opportunity_filters = [] if user_id is None else [AiMonitorOpportunity.user_id == user_id]
    gate_filters = [] if user_id is None else [OpportunityGateDecision.user_id == user_id]
    return {
        "predictions": int(
            db.scalar(select(func.count()).select_from(AiMonitorPrediction).where(*filters)) or 0
        ),
        "opportunities": int(
            db.scalar(
                select(func.count()).select_from(AiMonitorOpportunity).where(*opportunity_filters)
            )
            or 0
        ),
        "gate_decisions": int(
            db.scalar(
                select(func.count()).select_from(OpportunityGateDecision).where(*gate_filters)
            )
            or 0
        ),
    }


def _projection_counts(db: Session, user_id: int | None) -> dict[str, int]:
    facts = [] if user_id is None else [AiMonitorPredictionFact.user_id == user_id]
    current = [] if user_id is None else [AiMonitorOpportunityCurrent.user_id == user_id]
    history = [] if user_id is None else [AiMonitorScoreHistory.user_id == user_id]
    return {
        "prediction_facts": int(
            db.scalar(select(func.count()).select_from(AiMonitorPredictionFact).where(*facts)) or 0
        ),
        "current_opportunities": int(
            db.scalar(select(func.count()).select_from(AiMonitorOpportunityCurrent).where(*current))
            or 0
        ),
        "score_history": int(
            db.scalar(select(func.count()).select_from(AiMonitorScoreHistory).where(*history)) or 0
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--user-id", type=int)
    parser.add_argument("--prediction-limit", type=int, default=10000)
    parser.add_argument("--score-limit", type=int, default=20000)
    parser.add_argument(
        "--fail-on-drift",
        action="store_true",
        help="return a non-zero status when source/read-model counts do not reconcile",
    )
    args = parser.parse_args()

    engine = build_engine(get_settings())
    with Session(engine, expire_on_commit=False) as db:
        available = read_models_available(db, refresh=True)
        report: dict[str, object] = {
            "mode": "apply" if args.apply else "dry-run",
            "tables_available": available,
            "user_id": args.user_id,
            "source": _source_counts(db, args.user_id),
        }
        if not available:
            report["next_step"] = "apply Alembic migration 0059_ai_monitor_read_models"
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 2 if args.apply else 0
        report["before"] = _projection_counts(db, args.user_id)
        if args.apply:
            report["changed"] = refresh_ai_monitor_read_models(
                db,
                user_id=args.user_id,
                prediction_limit=args.prediction_limit,
                score_limit=args.score_limit,
                force_availability_check=True,
            )
            db.commit()
            report["after"] = _projection_counts(db, args.user_id)
            report["reconciliation"] = reconcile_ai_monitor_read_models(
                db,
                user_id=args.user_id,
            )
        else:
            report["reconciliation"] = reconcile_ai_monitor_read_models(
                db,
                user_id=args.user_id,
            )
            db.rollback()
        print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
        if args.fail_on_drift and not bool(
            dict(report.get("reconciliation") or {}).get("ready")
        ):
            return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
