"""Read-only audit of canonical strategy runtime records after the cutover."""

from __future__ import annotations

import argparse
import json
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from quantdesk_v2.config import get_settings
from quantdesk_v2.database import build_engine
from quantdesk_v2.models import (
    LiveTradingAccount,
    PaperAccount,
    StrategyDeployment,
    StrategyRevision,
    StrategyTemplate,
    StrategyValidationRun,
    UserStrategy,
)

BUILTIN_KIND = "builtin_strategy"
RETIRED_KIND = "legacy_signal"
RETIRED_PAPER_MODE = "legacy_score_v1"
NONTERMINAL_DEPLOYMENT_STATUSES = ("created", "running", "paused", "error")


def _count(db: Session, statement: Any) -> int:
    return int(db.scalar(statement) or 0)


def inventory(db: Session) -> dict[str, Any]:
    builtin_deployments = (
        select(func.count(StrategyDeployment.id))
        .select_from(StrategyDeployment)
        .join(UserStrategy, UserStrategy.id == StrategyDeployment.strategy_id)
        .where(
            UserStrategy.strategy_kind == BUILTIN_KIND,
            StrategyDeployment.status.in_(NONTERMINAL_DEPLOYMENT_STATUSES),
        )
    )
    canonical_counts = {
        "builtin_templates": _count(
            db,
            select(func.count(StrategyTemplate.id)).where(
                StrategyTemplate.template_kind == BUILTIN_KIND
            ),
        ),
        "builtin_user_strategies": _count(
            db,
            select(func.count(UserStrategy.id)).where(
                UserStrategy.strategy_kind == BUILTIN_KIND
            ),
        ),
        "builtin_strategy_revisions": _count(
            db,
            select(func.count(StrategyRevision.id))
            .join(UserStrategy, UserStrategy.id == StrategyRevision.user_strategy_id)
            .where(UserStrategy.strategy_kind == BUILTIN_KIND),
        ),
        "nonterminal_builtin_deployments": _count(db, builtin_deployments),
    }
    retired_markers = {
        "retired_templates": _count(
            db,
            select(func.count(StrategyTemplate.id)).where(
                StrategyTemplate.template_kind == RETIRED_KIND
            ),
        ),
        "retired_template_implementations": _count(
            db,
            select(func.count(StrategyTemplate.id)).where(
                StrategyTemplate.implementation_version == "legacy_v1"
            ),
        ),
        "retired_user_strategies": _count(
            db,
            select(func.count(UserStrategy.id)).where(
                UserStrategy.strategy_kind == RETIRED_KIND
            ),
        ),
        "retired_revision_snapshots": _count(
            db,
            select(func.count(StrategyRevision.id)).where(
                func.json_unquote(
                    func.json_extract(StrategyRevision.snapshot_json, "$.strategy_kind")
                )
                == RETIRED_KIND
            ),
        ),
        "retired_revision_validation_markers": _count(
            db,
            select(func.count(StrategyRevision.id)).where(
                func.json_extract(StrategyRevision.validation_json, "$.legacy").is_not(
                    None
                )
            ),
        ),
        "retired_validation_run_markers": _count(
            db,
            select(func.count(StrategyValidationRun.id)).where(
                func.json_extract(StrategyValidationRun.report_json, "$.legacy").is_not(
                    None
                )
            ),
        ),
        "retired_paper_modes": _count(
            db,
            select(func.count(PaperAccount.id)).where(
                func.json_unquote(func.json_extract(PaperAccount.config_json, "$.signal_mode"))
                == RETIRED_PAPER_MODE
            ),
        ),
        "retired_paper_migration_metadata": _count(
            db,
            select(func.count(PaperAccount.id)).where(
                or_(
                    func.json_extract(
                        PaperAccount.config_json,
                        "$.legacy_previous_signal_mode",
                    ).is_not(None),
                    func.json_extract(
                        PaperAccount.config_json,
                        "$.legacy_signal_migrated_at",
                    ).is_not(None),
                    func.json_extract(
                        PaperAccount.config_json,
                        "$.legacy_signal_cutoff_revision",
                    ).is_not(None),
                )
            ),
        ),
        "retired_paper_snapshots": _count(
            db,
            select(func.count(PaperAccount.id)).where(
                func.json_unquote(
                    func.json_extract(PaperAccount.strategy_snapshot_json, "$.strategy_kind")
                )
                == RETIRED_KIND
            ),
        ),
        "retired_live_snapshots": _count(
            db,
            select(func.count(LiveTradingAccount.id)).where(
                func.json_unquote(
                    func.json_extract(
                        LiveTradingAccount.strategy_snapshot_json,
                        "$.strategy_kind",
                    )
                )
                == RETIRED_KIND
            ),
        ),
    }
    return {
        "mode": "dry-run",
        "read_only": True,
        "canonical_counts": canonical_counts,
        "retired_runtime_markers": retired_markers,
        "runtime_compatibility_removed": not any(retired_markers.values()),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fail-on-retired", action="store_true")
    args = parser.parse_args()
    engine = build_engine(
        get_settings(),
        connect_timeout=15,
        read_timeout=120,
        write_timeout=120,
    )
    with Session(engine, expire_on_commit=False) as db:
        report = inventory(db)
        db.rollback()
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    return 2 if args.fail_on_retired and not report["runtime_compatibility_removed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
