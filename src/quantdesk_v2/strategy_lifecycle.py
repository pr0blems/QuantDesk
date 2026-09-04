"""Fail-closed lifecycle and readiness rules for immutable strategy revisions."""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .domain.martingale_tp4 import strategy_parameters_from_catalog_parameters
from .models import (
    BacktestRun,
    StrategyRevision,
    StrategyValidationRun,
    UserStrategy,
    utcnow,
)

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
SHADOW_ELIGIBLE_STATUSES = frozenset({"shadow", "paper", "micro_live", "live"})
PAPER_ELIGIBLE_STATUSES = frozenset({"paper", "micro_live", "live"})
LIVE_ELIGIBLE_STATUSES = frozenset({"micro_live", "live"})

_NEXT_STATUS = {
    "draft": "validated",
    # 旧安装中的 published 只代表“曾发布”，不能视为实盘授权。
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
    elif strategy.strategy_kind == "basket_strategy":
        if strategy.engine_key != "martingale_tp4":
            return False, "当前篮子策略引擎不受支持"
        if validation.get("engine") != "martingale_tp4_engine_v1":
            return False, "当前修订缺少马丁 TP4 引擎校验证据"
        snapshot = revision.snapshot_json if isinstance(revision.snapshot_json, dict) else {}
        if (
            snapshot.get("engine_key") != strategy.engine_key
            or snapshot.get("strategy_kind") != strategy.strategy_kind
            or snapshot.get("version") != strategy.version
            or snapshot.get("parameters") != strategy.parameters_json
        ):
            return False, "马丁 TP4 参数与当前不可变修订不一致"
        try:
            strategy_parameters_from_catalog_parameters(strategy.parameters_json)
        except (TypeError, ValueError):
            return False, "马丁 TP4 当前修订参数未通过服务端校验"
    else:
        return False, "旧版信号策略不能进入新的受控生命周期"
    return True, "当前修订、哈希和静态校验结果一致"


def _backtest_evidence(db: Session, revision_id: int | None) -> tuple[int, int | None]:
    if revision_id is None:
        return 0, None
    rows = db.execute(
        select(func.count(BacktestRun.id), func.max(BacktestRun.id))
        .where(
            BacktestRun.strategy_revision_id == revision_id,
            BacktestRun.status == "completed",
        )
    ).one()
    return int(rows[0] or 0), int(rows[1]) if rows[1] is not None else None


def paper_eligibility(
    db: Session,
    strategy: UserStrategy,
    revision: StrategyRevision | None = None,
) -> tuple[bool, str]:
    """Return paper eligibility, including the curated TP4 paper-safe exception."""

    selected_revision = revision or current_strategy_revision(db, strategy)
    if selected_revision is None:
        return False, "当前版本缺少不可变修订记录"
    if selected_revision.lifecycle_status in PAPER_ELIGIBLE_STATUSES:
        return True, "当前修订已进入模拟盘或更高阶段"
    curated_tp4 = (
        strategy.strategy_kind == "basket_strategy"
        and strategy.engine_key == "martingale_tp4"
        and strategy.created_via == "system_default"
        and strategy.source_template_id is not None
        and strategy.version == selected_revision.version
    )
    if not curated_tp4:
        return False, "当前修订生命周期尚未进入模拟盘阶段"
    static_valid, static_detail = _static_validation_check(strategy, selected_revision)
    if not static_valid:
        return False, static_detail
    backtest_count, _latest_backtest_run_id = _backtest_evidence(db, selected_revision.id)
    if backtest_count <= 0:
        return False, "马丁 TP4 当前修订还没有完成回测"
    return True, f"系统内置马丁 TP4 当前修订已完成 {backtest_count} 次回测"


def _latest_passed_validation(
    db: Session,
    revision_id: int | None,
    validation_type: str,
) -> StrategyValidationRun | None:
    if revision_id is None:
        return None
    return db.scalar(
        select(StrategyValidationRun)
        .where(
            StrategyValidationRun.strategy_revision_id == revision_id,
            StrategyValidationRun.validation_type == validation_type,
            StrategyValidationRun.status == "passed",
        )
        .order_by(StrategyValidationRun.completed_at.desc(), StrategyValidationRun.id.desc())
        .limit(1)
    )


def _validation_check(
    db: Session,
    revision_id: int | None,
    validation_type: str,
    label: str,
) -> dict[str, Any]:
    run = _latest_passed_validation(db, revision_id, validation_type)
    return {
        "code": f"{validation_type}_validation_passed",
        "label": label,
        "passed": run is not None,
        "detail": (
            "已找到绑定当前修订的通过报告" if run is not None else "没有绑定当前修订的通过报告"
        ),
        "evidence_id": run.public_id if run is not None else None,
        "report": run.report_json if run is not None else None,
    }


def strategy_readiness(db: Session, strategy: UserStrategy) -> dict[str, Any]:
    """Return revision-bound, explainable, evidence-based promotion readiness."""

    revision = current_strategy_revision(db, strategy)
    revision_id = revision.id if revision is not None else None
    static_valid, static_detail = _static_validation_check(strategy, revision)
    backtest_count, latest_backtest_run_id = _backtest_evidence(db, revision_id)
    checks_by_code: dict[str, dict[str, Any]] = {
        "current_revision_valid": {
            "code": "current_revision_valid",
            "label": "当前修订通过服务端校验",
            "passed": static_valid,
            "detail": static_detail,
            "evidence_id": revision_id,
        },
        "current_revision_backtested": {
            "code": "current_revision_backtested",
            "label": "当前修订已完成可复现回测",
            "passed": backtest_count > 0,
            "detail": (
                f"已找到 {backtest_count} 次绑定当前修订的完成回测"
                if backtest_count
                else "没有找到绑定当前修订的完成回测"
            ),
            "evidence_id": latest_backtest_run_id,
        },
    }
    for validation_type, label in (
        ("oos", "样本外报告通过"),
        ("stress", "成本与参数压力测试通过"),
        ("shadow", "影子运行报告通过"),
        ("paper", "同执行链模拟盘报告通过"),
        ("fault_drill", "关键故障演练全部通过"),
        ("micro_live", "微型实盘观察报告通过"),
    ):
        item = _validation_check(db, revision_id, validation_type, label)
        checks_by_code[item["code"]] = item

    status = revision.lifecycle_status if revision is not None else strategy.lifecycle_status
    next_status = _NEXT_STATUS.get(status)
    required_codes = {
        "validated": ("current_revision_valid",),
        "backtested": ("current_revision_valid", "current_revision_backtested"),
        "shadow": (
            "current_revision_valid",
            "current_revision_backtested",
            "oos_validation_passed",
            "stress_validation_passed",
        ),
        "paper": (
            "current_revision_valid",
            "shadow_validation_passed",
        ),
        "micro_live": (
            "current_revision_valid",
            "paper_validation_passed",
            "fault_drill_validation_passed",
        ),
        "live": (
            "current_revision_valid",
            "micro_live_validation_passed",
        ),
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
            "shadow": status in SHADOW_ELIGIBLE_STATUSES,
            "paper": paper_eligibility(db, strategy, revision)[0],
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
        raise ValueError(f"不允许从 {current_status} 直接晋级到 {target_status}")
    readiness = strategy_readiness(db, strategy)
    if not readiness["can_promote"]:
        raise PermissionError(",".join(readiness["blockers"]) or "promotion_not_ready")
    strategy.lifecycle_status = target_status
    strategy.updated_at = utcnow()
    revision.lifecycle_status = target_status
    if target_status != "draft" and revision.published_at is None:
        revision.published_at = utcnow()
    return revision, readiness, current_status
