"""Fail-closed lifecycle and readiness rules for immutable strategy revisions."""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .models import BacktestRun, StrategyDeployment, StrategyRevision, UserStrategy, utcnow

LIFECYCLE_SEQUENCE = (
    "draft",
    "validated",
    "backtested",
    "shadow",
    "paper",
    "micro_live",
    "live",
)
LIFECYCLE_STATUSES = frozenset((*LIFECYCLE_SEQUENCE, "published", "retired"))
BACKTEST_ELIGIBLE_STATUSES = frozenset(
    {"validated", "backtested", "shadow", "paper", "micro_live", "live"}
)
PAPER_ELIGIBLE_STATUSES = frozenset({"shadow", "paper", "micro_live", "live"})
LIVE_ELIGIBLE_STATUSES = frozenset({"micro_live", "live"})

_NEXT_STATUS = {
    "draft": "validated",
    # Existing installations used published as an all-purpose status. It must
    # pass through the new validation stage before gaining any deployment rights.
    "published": "validated",
    "validated": "backtested",
    "backtested": "shadow",
    "shadow": "paper",
    "paper": "micro_live",
    "micro_live": "live",
}


def current_strategy_revision(
    db: Session, strategy: UserStrategy, *, for_update: bool = False
) -> StrategyRevision | None:
    statement = select(StrategyRevision).where(
        StrategyRevision.user_strategy_id == strategy.id,
        StrategyRevision.user_id == strategy.user_id,
        StrategyRevision.version == strategy.version,
    )
    if for_update:
        statement = statement.with_for_update()
    return db.scalar(statement)


def _static_validation_check(
    strategy: UserStrategy, revision: StrategyRevision | None
) -> tuple[bool, str]:
    if revision is None:
        return False, "当前版本缺少不可变修订记录"
    if revision.lifecycle_status != strategy.lifecycle_status:
        return False, "策略与当前修订的生命周期状态不一致"
    validation = revision.validation_json if isinstance(revision.validation_json, dict) else {}
    if validation.get("valid") is not True:
        return False, "当前修订尚未通过服务端静态校验"
    if strategy.strategy_kind == "source_strategy":
        if not strategy.source_hash or revision.source_hash != strategy.source_hash:
            return False, "源码哈希与当前修订不一致"
        if not strategy.source_code or not strategy.source_code.strip():
            return False, "当前修订缺少可执行源码"
    elif strategy.strategy_kind == "full_strategy":
        if not strategy.spec_hash or revision.spec_hash != strategy.spec_hash:
            return False, "策略定义哈希与当前修订不一致"
        if not isinstance(strategy.spec_json, dict):
            return False, "当前修订缺少完整策略定义"
    else:
        return False, "旧版信号策略不能进入新的受控生命周期"
    return True, "当前修订、哈希和静态校验结果一致"


def _backtest_evidence(db: Session, revision_id: int | None) -> tuple[int, int | None]:
    if revision_id is None:
        return 0, None
    rows = db.execute(
        select(func.count(BacktestRun.id), func.max(BacktestRun.id))
        .select_from(StrategyDeployment)
        .join(BacktestRun, BacktestRun.id == StrategyDeployment.target_account_id)
        .where(
            StrategyDeployment.strategy_revision_id == revision_id,
            StrategyDeployment.mode == "backtest",
            BacktestRun.status == "completed",
        )
    ).one()
    return int(rows[0] or 0), int(rows[1]) if rows[1] is not None else None


def _deployment_evidence(
    db: Session, revision_id: int | None, mode: str
) -> tuple[int, int | None]:
    if revision_id is None:
        return 0, None
    rows = db.execute(
        select(func.count(StrategyDeployment.id), func.max(StrategyDeployment.id)).where(
            StrategyDeployment.strategy_revision_id == revision_id,
            StrategyDeployment.mode == mode,
            StrategyDeployment.status.in_(("created", "running", "paused", "stopped")),
        )
    ).one()
    return int(rows[0] or 0), int(rows[1]) if rows[1] is not None else None


def strategy_readiness(db: Session, strategy: UserStrategy) -> dict[str, Any]:
    """Return a revision-bound, explainable promotion and deployment report."""

    revision = current_strategy_revision(db, strategy)
    revision_id = revision.id if revision is not None else None
    static_valid, static_detail = _static_validation_check(strategy, revision)
    backtest_count, latest_backtest_run_id = _backtest_evidence(db, revision_id)
    shadow_count, latest_shadow_deployment_id = _deployment_evidence(db, revision_id, "shadow")
    paper_count, latest_paper_deployment_id = _deployment_evidence(db, revision_id, "paper")
    live_count, latest_live_deployment_id = _deployment_evidence(db, revision_id, "live")
    checks_by_code = {
        "current_revision_valid": {
            "code": "current_revision_valid",
            "label": "当前修订通过服务端校验",
            "passed": static_valid,
            "detail": static_detail,
        },
        "current_revision_backtested": {
            "code": "current_revision_backtested",
            "label": "当前修订已完成回测",
            "passed": backtest_count > 0,
            "detail": (
                f"已找到 {backtest_count} 次完成的当前版本回测"
                if backtest_count
                else "没有找到绑定当前修订的完成回测"
            ),
            "evidence_id": latest_backtest_run_id,
        },
        "shadow_deployment_exists": {
            "code": "shadow_deployment_exists",
            "label": "当前修订已有影子部署记录",
            "passed": shadow_count > 0,
            "detail": (
                f"已找到 {shadow_count} 个影子部署"
                if shadow_count
                else "没有找到当前修订的影子部署"
            ),
            "evidence_id": latest_shadow_deployment_id,
        },
        "paper_deployment_exists": {
            "code": "paper_deployment_exists",
            "label": "当前修订已有模拟盘部署记录",
            "passed": paper_count > 0,
            "detail": (
                f"已找到 {paper_count} 个模拟盘部署"
                if paper_count
                else "没有找到当前修订的模拟盘部署"
            ),
            "evidence_id": latest_paper_deployment_id,
        },
        "live_deployment_exists": {
            "code": "live_deployment_exists",
            "label": "当前修订已有微型实盘部署记录",
            "passed": live_count > 0,
            "detail": (
                f"已找到 {live_count} 个实盘部署"
                if live_count
                else "没有找到当前修订的微型实盘部署"
            ),
            "evidence_id": latest_live_deployment_id,
        },
    }
    status = revision.lifecycle_status if revision is not None else strategy.lifecycle_status
    next_status = _NEXT_STATUS.get(status)
    required_codes = {
        "validated": ("current_revision_valid",),
        "backtested": ("current_revision_valid", "current_revision_backtested"),
        "shadow": (
            "current_revision_valid",
            "current_revision_backtested",
            "shadow_deployment_exists",
        ),
        "paper": ("current_revision_valid", "shadow_deployment_exists"),
        "micro_live": ("current_revision_valid", "paper_deployment_exists"),
        "live": ("current_revision_valid", "live_deployment_exists"),
    }.get(next_status, ())
    promotion_checks = [checks_by_code[code] for code in required_codes]
    blockers = [item["code"] for item in promotion_checks if not item["passed"]]
    return {
        "strategy_id": strategy.public_id,
        "strategy_version": strategy.version,
        "revision_id": revision_id,
        "revision_status": status,
        "next_status": next_status,
        "can_promote": bool(next_status) and not blockers and strategy.status == "active",
        "blockers": blockers,
        "checks": list(checks_by_code.values()),
        "promotion_checks": promotion_checks,
        "eligibility": {
            "backtest": status in BACKTEST_ELIGIBLE_STATUSES,
            "paper": status in PAPER_ELIGIBLE_STATUSES,
            "live": status in LIVE_ELIGIBLE_STATUSES,
        },
    }


def promote_current_revision(
    db: Session,
    strategy: UserStrategy,
    *,
    expected_version: int,
    target_status: str,
) -> tuple[StrategyRevision, dict[str, Any], str]:
    """Apply one legal promotion after all revision-bound checks pass."""

    if strategy.status != "active":
        raise ValueError("策略已归档，不能晋级")
    if strategy.version != expected_version:
        raise RuntimeError("strategy_version_conflict")
    revision = current_strategy_revision(db, strategy, for_update=True)
    if revision is None:
        raise ValueError("当前策略版本缺少不可变修订记录")
    current_status = revision.lifecycle_status
    if strategy.lifecycle_status != current_status:
        raise ValueError("策略与当前修订的生命周期状态不一致")
    expected_target = _NEXT_STATUS.get(current_status)
    if expected_target != target_status:
        raise ValueError(
            f"不允许从 {current_status} 直接晋级到 {target_status}"
        )
    readiness = strategy_readiness(db, strategy)
    if not readiness["can_promote"]:
        raise PermissionError(",".join(readiness["blockers"]) or "promotion_not_ready")
    strategy.lifecycle_status = target_status
    strategy.updated_at = utcnow()
    revision.lifecycle_status = target_status
    if target_status != "draft" and revision.published_at is None:
        revision.published_at = utcnow()
    return revision, readiness, current_status
