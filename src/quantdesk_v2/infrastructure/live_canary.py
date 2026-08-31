"""Durable, observation-only acceptance windows for live execution.

Starting a canary never enables an account and never submits an order.  The
service only observes an already active account and records every violation so
that a transient UNKNOWN or unprotected position cannot disappear before the
final audit.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from ..application.live_canary import (
    LIVE_TICK_STALE_SECONDS,
    LIVE_WORKER_STALE_SECONDS,
    MINIMUM_CANARY_WINDOW_SECONDS,
    LiveCanaryError,
    LiveCanaryObservation,
)
from ..application.live_recovery import (
    LivePositionSyncService,
    ProtectionRecoveryService,
)
from ..models import (
    LiveCanaryRun,
    LiveCanarySample,
    LiveOrderIntent,
    LiveTradingAccount,
    WorkerHeartbeat,
    utcnow,
)


def _managed_positions(
    db: Session, run: LiveCanaryRun
) -> dict[tuple[str, str], dict[str, Any]]:
    rows = [
        dict(row)
        for row in db.execute(
            select(
                LiveOrderIntent.id,
                LiveOrderIntent.symbol,
                LiveOrderIntent.position_side,
                LiveOrderIntent.action,
                LiveOrderIntent.status,
            )
            .where(
                LiveOrderIntent.user_id == run.user_id,
                LiveOrderIntent.live_account_id == run.live_account_id,
                LiveOrderIntent.action.in_(("open", "close")),
                LiveOrderIntent.status == "filled",
            )
            .order_by(LiveOrderIntent.id.desc())
        ).mappings()
    ]
    return LivePositionSyncService.managed_positions(rows)


def _protection_coverage(
    db: Session,
    run: LiveCanaryRun,
    managed: dict[tuple[str, str], dict[str, Any]],
) -> dict[tuple[str, str], int]:
    rows = [
        dict(row)
        for row in db.execute(
            select(
                LiveOrderIntent.id,
                LiveOrderIntent.symbol,
                LiveOrderIntent.position_side,
                LiveOrderIntent.action,
            )
            .where(
                LiveOrderIntent.user_id == run.user_id,
                LiveOrderIntent.live_account_id == run.live_account_id,
                LiveOrderIntent.action.in_(("stop", "take_profit")),
                LiveOrderIntent.status == "submitted",
            )
            .order_by(LiveOrderIntent.id)
        ).mappings()
    ]
    return ProtectionRecoveryService.coverage_counts(rows, managed)


class LiveCanaryService:
    """Create, sample and finalize one explicit live canary window."""

    def start(
        self,
        db: Session,
        *,
        user_id: int,
        live_account_id: int,
        window_seconds: int,
        minimum_open_fills: int = 1,
    ) -> LiveCanaryRun:
        if window_seconds < MINIMUM_CANARY_WINDOW_SECONDS:
            raise LiveCanaryError("Canary 观察窗口不能少于 15 分钟")
        if minimum_open_fills < 0:
            raise LiveCanaryError("最少开仓成交数不能为负数")
        account = db.scalar(
            select(LiveTradingAccount).where(
                LiveTradingAccount.id == live_account_id,
                LiveTradingAccount.user_id == user_id,
            ).with_for_update()
        )
        if account is None:
            raise LiveCanaryError("实盘账户不存在")
        if account.status != "active":
            raise LiveCanaryError("Canary 只观察已由用户启用的实盘账户")
        active = db.scalar(
            select(LiveCanaryRun.id).where(
                LiveCanaryRun.live_account_id == live_account_id,
                LiveCanaryRun.status == "running",
            )
        )
        if active is not None:
            raise LiveCanaryError("该实盘账户已有运行中的 Canary")
        now = utcnow()
        run = LiveCanaryRun(
            user_id=user_id,
            live_account_id=live_account_id,
            status="running",
            window_seconds=window_seconds,
            minimum_open_fills=minimum_open_fills,
            sample_count=0,
            violation_count=0,
            failure_codes_json=[],
            metrics_json={},
            started_at=now,
            due_at=now + timedelta(seconds=window_seconds),
            created_at=now,
            updated_at=now,
        )
        db.add(run)
        db.flush()
        self.sample(db, run)
        return run

    def observe(self, db: Session, run: LiveCanaryRun) -> LiveCanaryObservation:
        now = utcnow()
        account = db.scalar(
            select(LiveTradingAccount).where(
                LiveTradingAccount.id == run.live_account_id,
                LiveTradingAccount.user_id == run.user_id,
            )
        )
        if account is None:
            return LiveCanaryObservation(False, ("account_missing",), {})
        latest_worker = db.scalar(
            select(WorkerHeartbeat)
            .where(WorkerHeartbeat.worker_type == "live")
            .order_by(WorkerHeartbeat.last_seen_at.desc(), WorkerHeartbeat.id.desc())
        )
        worker_age = (
            max(0.0, (now - latest_worker.last_seen_at).total_seconds())
            if latest_worker is not None
            else None
        )
        tick_age = (
            max(0.0, (now - account.last_tick_at).total_seconds())
            if account.last_tick_at is not None
            else None
        )
        unknown_intents = int(
            db.scalar(
                select(func.count(LiveOrderIntent.id)).where(
                    LiveOrderIntent.user_id == run.user_id,
                    LiveOrderIntent.live_account_id == run.live_account_id,
                    LiveOrderIntent.status == "unknown",
                )
            )
            or 0
        )
        stale_intents = int(
            db.scalar(
                select(func.count(LiveOrderIntent.id)).where(
                    LiveOrderIntent.user_id == run.user_id,
                    LiveOrderIntent.live_account_id == run.live_account_id,
                    LiveOrderIntent.status.in_(("created", "submitted", "unknown")),
                    LiveOrderIntent.updated_at < now - timedelta(minutes=5),
                )
            )
            or 0
        )
        duplicate_intents = int(
            db.execute(
                text(
                    """SELECT COUNT(*) FROM (
                           SELECT signal_key
                           FROM live_order_intents
                           WHERE user_id=:user_id AND live_account_id=:account_id
                           GROUP BY signal_key HAVING COUNT(*)>1
                       ) AS duplicate_signals"""
                ),
                {"user_id": run.user_id, "account_id": run.live_account_id},
            ).scalar_one()
            or 0
        )
        execution_counts = db.execute(
            text(
                """SELECT
                       COALESCE(SUM(
                           claim_status='in_progress'
                           AND lease_expires_at < :now
                       ),0) AS in_progress_count,
                       COALESCE(SUM(execution_state='unknown'),0) AS unknown_count
                   FROM execution_idempotency_records
                   WHERE user_scope=:user_scope AND account_scope=:account_scope"""
            ),
            {
                "user_scope": f"user:{run.user_id}",
                "account_scope": f"live-account:{run.live_account_id}",
                "now": now,
            },
        ).mappings().one()
        open_fills = int(
            db.scalar(
                select(func.count(LiveOrderIntent.id)).where(
                    LiveOrderIntent.user_id == run.user_id,
                    LiveOrderIntent.live_account_id == run.live_account_id,
                    LiveOrderIntent.action == "open",
                    LiveOrderIntent.status == "filled",
                    LiveOrderIntent.updated_at >= run.started_at,
                )
            )
            or 0
        )
        managed = _managed_positions(db, run)
        coverage = _protection_coverage(db, run, managed)
        unprotected = sorted(
            f"{symbol}:{side}"
            for symbol, side in managed
            if int(coverage.get((symbol, side), 0)) != 2
        )
        failure_codes: list[str] = []
        if account.status != "active":
            failure_codes.append("account_not_active")
        if account.last_error_code:
            failure_codes.append(f"account_error:{account.last_error_code}")
        if (
            latest_worker is None
            or latest_worker.status != "running"
            or worker_age is None
            or worker_age > LIVE_WORKER_STALE_SECONDS
        ):
            failure_codes.append("live_worker_unhealthy")
        if tick_age is None or tick_age > LIVE_TICK_STALE_SECONDS:
            failure_codes.append("live_tick_stale")
        if duplicate_intents:
            failure_codes.append("duplicate_order_intent")
        if unknown_intents:
            failure_codes.append("unknown_order_intent")
        if stale_intents:
            failure_codes.append("stale_order_intent")
        if int(execution_counts["in_progress_count"] or 0):
            failure_codes.append("execution_claim_stale")
        if int(execution_counts["unknown_count"] or 0):
            failure_codes.append("execution_unknown")
        if unprotected:
            failure_codes.append("unprotected_position")
        metrics = {
            "account_status": account.status,
            "account_last_error_code": account.last_error_code,
            "live_worker_age_seconds": worker_age,
            "live_tick_age_seconds": tick_age,
            "duplicate_intent_count": duplicate_intents,
            "unknown_intent_count": unknown_intents,
            "stale_intent_count": stale_intents,
            "in_progress_execution_count": int(
                execution_counts["in_progress_count"] or 0
            ),
            "unknown_execution_count": int(execution_counts["unknown_count"] or 0),
            "managed_position_count": len(managed),
            "unprotected_managed_positions": unprotected,
            "open_fills_during_window": open_fills,
        }
        return LiveCanaryObservation(
            passed=not failure_codes,
            failure_codes=tuple(failure_codes),
            metrics=metrics,
        )

    def sample(self, db: Session, run: LiveCanaryRun) -> LiveCanaryObservation:
        if run.status != "running":
            raise LiveCanaryError("Canary 已结束，不能继续采样")
        now = utcnow()
        observation = self.observe(db, run)
        db.add(
            LiveCanarySample(
                run_id=run.id,
                user_id=run.user_id,
                live_account_id=run.live_account_id,
                passed=observation.passed,
                failure_codes_json=list(observation.failure_codes),
                metrics_json=observation.metrics,
                sampled_at=now,
                created_at=now,
            )
        )
        run.sample_count += 1
        run.metrics_json = observation.metrics
        if not observation.passed:
            run.violation_count += 1
            run.failure_codes_json = sorted(
                set(run.failure_codes_json or []).union(observation.failure_codes)
            )
            run.status = "failed"
            run.completed_at = now
        elif now >= run.due_at:
            open_fills = int(observation.metrics.get("open_fills_during_window") or 0)
            if open_fills < run.minimum_open_fills:
                run.status = "failed"
                run.violation_count += 1
                run.failure_codes_json = sorted(
                    set(run.failure_codes_json or []).union(
                        {"insufficient_exercised_orders"}
                    )
                )
            else:
                run.status = "passed"
            run.completed_at = now
        run.updated_at = now
        db.flush()
        return observation

    def sample_running(self, db: Session) -> int:
        runs = list(
            db.scalars(
                select(LiveCanaryRun)
                .where(LiveCanaryRun.status == "running")
                .order_by(LiveCanaryRun.id)
                .with_for_update(skip_locked=True)
            ).all()
        )
        for run in runs:
            self.sample(db, run)
        return len(runs)

    @staticmethod
    def cancel(db: Session, run: LiveCanaryRun) -> None:
        if run.status != "running":
            raise LiveCanaryError("只有运行中的 Canary 可以取消")
        now = utcnow()
        run.status = "canceled"
        run.completed_at = now
        run.updated_at = now
        db.flush()

    @staticmethod
    def snapshot(run: LiveCanaryRun) -> dict[str, Any]:
        return {
            "public_id": run.public_id,
            "user_id": run.user_id,
            "live_account_id": run.live_account_id,
            "status": run.status,
            "window_seconds": run.window_seconds,
            "minimum_open_fills": run.minimum_open_fills,
            "sample_count": run.sample_count,
            "violation_count": run.violation_count,
            "failure_codes": list(run.failure_codes_json or []),
            "metrics": dict(run.metrics_json or {}),
            "started_at": run.started_at,
            "due_at": run.due_at,
            "completed_at": run.completed_at,
        }
