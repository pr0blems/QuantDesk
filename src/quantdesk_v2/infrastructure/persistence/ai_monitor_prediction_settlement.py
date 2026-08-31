"""Prediction settlement persistence adapter and row-level recovery protocol."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from ...application.ai_monitor.prediction_settlement import (
    adaptive_exit_precedes,
    historical_closed_settlement_price,
    prediction_adaptive_path_exit,
    prediction_cost_breakdown,
    prediction_estimated_cost_bps,
    prediction_net_outcome,
    prediction_outcome,
    prediction_path_metrics,
    prediction_price_barrier_exit,
    prediction_score_exit_price,
    prediction_score_exit_signal,
    prediction_settlement_cost_config,
    prediction_soft_exit_policy,
    settlement_exit_subreason,
    virtual_risk_plan_snapshot,
)
from ...models import AiMonitorPrediction, utcnow
from ...monitor import MonitorRepository, MonitorUnavailable

TIMEFRAME_SECONDS = {"15m": 15 * 60, "1h": 60 * 60, "4h": 4 * 60 * 60}
_TIMEFRAME_SECONDS = TIMEFRAME_SECONDS
PREDICTION_SETTLEMENT_RETRY_MINUTES = 5
PREDICTION_SETTLEMENT_GRACE_HOURS = 6
PREDICTION_SETTLEMENT_BACKFILL_DAYS = 7
PREDICTION_SETTLEMENT_BATCH_SIZE = 25
PREVIOUS_PREDICTION_SETTLEMENT_VERSION = "cost_consistent_exit_v7"
PREDICTION_SCORE_EXIT_POLICY_VERSION = "horizon_aligned_closed_bar_v3"


def _datetime_ms(value: datetime) -> int:
    current = value if value.tzinfo else value.replace(tzinfo=UTC)
    return int(current.timestamp() * 1_000)


def settle_due_predictions(
    db: Session,
    repository: MonitorRepository,
    *,
    user_id: int | None = None,
) -> dict[str, int]:
    """Manage virtual exits using barriers, score decay, then a hard time cap."""

    now = utcnow()
    retry_before = now - timedelta(minutes=PREDICTION_SETTLEMENT_RETRY_MINUTES)
    backfill_since = now - timedelta(days=PREDICTION_SETTLEMENT_BACKFILL_DAYS)
    grace_cutoff = now - timedelta(hours=PREDICTION_SETTLEMENT_GRACE_HOURS)
    # Opportunity rescans may refresh a pending prediction's generic
    # ``updated_at`` while its frozen entry and risk plan remain unchanged.
    # Hard stops therefore stay eligible on every poll; the retry cooldown is
    # only for rows whose settlement data was actually unavailable.
    statement = (
        select(AiMonitorPrediction)
        .where(
            or_(
                AiMonitorPrediction.status == "pending",
                (
                    (AiMonitorPrediction.status == "unavailable")
                    & (AiMonitorPrediction.updated_at <= retry_before)
                    & (AiMonitorPrediction.due_at <= now)
                    & (AiMonitorPrediction.due_at >= backfill_since)
                ),
            ),
        )
        .order_by(AiMonitorPrediction.due_at, AiMonitorPrediction.id)
        .limit(PREDICTION_SETTLEMENT_BATCH_SIZE)
    )
    if user_id is not None:
        statement = statement.where(AiMonitorPrediction.user_id == user_id)
    # Hold each candidate row until the surrounding transaction commits.  A
    # second scheduler/process skips rows already owned by another worker
    # instead of calculating the same settlement from stale evidence.  SQLite
    # safely omits the unsupported locking clause; production MySQL emits
    # ``FOR UPDATE SKIP LOCKED``.
    statement = statement.execution_options(populate_existing=True).with_for_update(
        skip_locked=True
    )
    items = db.scalars(statement).all()
    window_ms = 45 * 60 * 1_000
    grouped: dict[str, list[AiMonitorPrediction]] = {}
    for item in items:
        grouped.setdefault(item.contract_symbol, []).append(item)
    candles_by_symbol: dict[str, list[dict[str, Any]]] = {}
    for symbol, symbol_items in grouped.items():
        targets = [_datetime_ms(min(item.due_at, now)) for item in symbol_items]
        starts = [
            _datetime_ms(
                getattr(
                    item,
                    "predicted_at",
                    item.due_at
                    - timedelta(
                        seconds=_TIMEFRAME_SECONDS.get(
                            getattr(item, "timeframe", "15m"), 900
                        )
                    ),
                )
            )
            for item in symbol_items
        ]
        try:
            candles_by_symbol[symbol] = repository.kline_range(
                symbol,
                "15m",
                min(starts) - window_ms,
                max(targets) + window_ms,
            )
        except MonitorUnavailable:
            candles_by_symbol[symbol] = []
    completed = 0
    recovered = 0
    deferred = 0
    unavailable = 0
    take_profit = 0
    stop_loss = 0
    score_exit = 0
    max_holding = 0
    profit_protection = 0
    failed_follow_through = 0
    for item in items:
        previous_status = item.status
        entry_price = float(item.entry_price or 0)
        predicted_at = getattr(
            item,
            "predicted_at",
            item.due_at
            - timedelta(
                seconds=_TIMEFRAME_SECONDS.get(getattr(item, "timeframe", "15m"), 900)
            ),
        )
        start_ms = _datetime_ms(predicted_at)
        observed_until = min(item.due_at, now)
        observed_until_ms = _datetime_ms(observed_until)
        candles = candles_by_symbol.get(item.contract_symbol, [])
        evidence = dict(getattr(item, "evidence_json", None) or {})
        stored_risk_plan = evidence.get("risk_plan")
        has_frozen_risk_plan = isinstance(stored_risk_plan, Mapping)
        risk_plan = (
            dict(stored_risk_plan)
            if has_frozen_risk_plan
            else virtual_risk_plan_snapshot(
                entry_price=entry_price,
                direction=item.direction,
                timeframe=getattr(item, "timeframe", "15m"),
            )
        )
        # A policy version belongs to the risk plan frozen at signal time.  Do
        # not relabel an old pending signal merely because a newer worker later
        # settles it.  Legacy rows without a frozen plan stay in the previous
        # cohort and therefore cannot contaminate current readiness statistics.
        settlement_version = str(
            (
                risk_plan.get("settlement_version")
                or getattr(item, "settlement_version", None)
            )
            if has_frozen_risk_plan
            else PREVIOUS_PREDICTION_SETTLEMENT_VERSION
        )
        risk_plan["settlement_version"] = settlement_version
        cost_model = evidence.get("cost_model")
        actual_cost_config = prediction_settlement_cost_config(
            cost_model if isinstance(cost_model, Mapping) else None,
            settlement_version=settlement_version,
        )
        guard_cost_estimate = prediction_estimated_cost_bps(
            predicted_at,
            observed_until,
            actual_cost_config,
        )
        soft_exit_policy = prediction_soft_exit_policy(
            getattr(item, "timeframe", "15m"),
            start_ms=start_ms,
            due_ms=_datetime_ms(item.due_at),
        )
        barrier_exit = prediction_price_barrier_exit(
            candles,
            entry_price,
            item.direction,
            risk_plan,
            start_ms,
            observed_until_ms,
        )
        adaptive_exit = prediction_adaptive_path_exit(
            candles,
            entry_price,
            item.direction,
            start_ms,
            observed_until_ms,
            estimated_cost_bps=guard_cost_estimate,
            minimum_soft_exit_ms=int(soft_exit_policy["minimum_hold_ms"]),
            minimum_profit_protection_ms=(
                0
                if str(
                    dict(risk_plan.get("profit_protection") or {}).get("mode")
                    or ""
                )
                == "risk_unit"
                else int(soft_exit_policy["minimum_hold_ms"])
            ),
            minimum_failed_follow_through_ms=int(
                soft_exit_policy["minimum_hold_ms"]
            ),
            profit_protection=(
                dict(risk_plan.get("profit_protection") or {})
                if isinstance(risk_plan.get("profit_protection"), Mapping)
                else None
            ),
            failed_follow_through=(
                dict(risk_plan.get("failed_follow_through") or {})
                if isinstance(risk_plan.get("failed_follow_through"), Mapping)
                else None
            ),
        )
        score_signal = prediction_score_exit_signal(
            evidence,
            item.direction,
            start_ms=start_ms,
            end_ms=observed_until_ms,
            confirmation_bar_ms=int(soft_exit_policy["bar_ms"]),
            minimum_hold_ms=int(soft_exit_policy["minimum_hold_ms"]),
            confirmation_bars=int(soft_exit_policy["confirmation_bars"]),
            confirmation_unit=str(soft_exit_policy["confirmation_unit"]),
        )
        score_settlement = (
            prediction_score_exit_price(
                candles,
                score_signal,
                end_ms=observed_until_ms,
            )
            if score_signal is not None
            else None
        )
        exit_decision: dict[str, Any] | None = barrier_exit
        if score_signal is not None and score_settlement is not None:
            score_decision = {
                **score_signal,
                "price": float(score_settlement["price"]),
                "price_time_ms": int(score_settlement["price_time_ms"]),
            }
            if (
                exit_decision is None
                or int(score_decision["price_time_ms"])
                < int(exit_decision["price_time_ms"])
            ):
                exit_decision = score_decision
        if adaptive_exit is not None:
            # A frozen protective stop is an executable price barrier, but OHLC
            # cannot reveal whether it or the original stop was touched first.
            # Keep the loss-side barrier on an equal timestamp to avoid
            # overstating research returns.  Profit protection may still beat
            # an equal-time target or close-time score decision conservatively.
            if adaptive_exit_precedes(exit_decision, adaptive_exit):
                exit_decision = adaptive_exit
        if exit_decision is None and item.due_at <= now:
            settlement = historical_closed_settlement_price(
                candles,
                _datetime_ms(item.due_at),
                not_before_ms=start_ms,
            )
            if settlement is not None:
                exit_decision = {
                    "reason": "max_holding_time",
                    "price": float(settlement["price"]),
                    "price_time_ms": int(settlement["price_time_ms"]),
                    "same_bar_conflict": False,
                    "gap_execution": False,
                }
        if (
            exit_decision is not None
            and int(exit_decision.get("price_time_ms") or 0) < start_ms
        ):
            # Never persist an exit from before the virtual entry.  Leave the
            # prediction pending so the retry path can obtain causal data.
            exit_decision = None
        if exit_decision is None and item.due_at > now:
            continue
        exit_price = float(exit_decision["price"]) if exit_decision is not None else 0.0
        if entry_price <= 0 or exit_price <= 0:
            item.updated_at = now
            if entry_price > 0 and item.due_at > grace_cutoff:
                item.status = "pending"
                item.completed_at = None
                deferred += 1
            else:
                item.status = "unavailable"
                item.completed_at = now
                unavailable += 1
            continue
        outcome = prediction_outcome(entry_price, exit_price, item.direction)
        exit_at = datetime.fromtimestamp(
            int(exit_decision["price_time_ms"]) / 1_000,
            UTC,
        ).replace(tzinfo=None)
        estimated_cost = prediction_estimated_cost_bps(
            predicted_at,
            exit_at,
            actual_cost_config,
        )
        net_outcome = prediction_net_outcome(
            float(outcome["directional_return_bps"]), estimated_cost
        )
        path_metrics = prediction_path_metrics(
            candles,
            entry_price,
            item.direction,
            start_ms,
            int(exit_decision["price_time_ms"]),
        )
        exit_reason = str(exit_decision["reason"])
        exit_subreason = settlement_exit_subreason(
            exit_decision,
            net_result=str(net_outcome.get("net_result") or ""),
        )
        peak_favorable_bps_at_exit = (
            Decimal(str(path_metrics["max_favorable_bps"]))
            if path_metrics["max_favorable_bps"] is not None
            else None
        )
        protected_bps_at_exit = (
            Decimal(str(exit_decision["protected_bps"]))
            if exit_decision.get("protected_bps") is not None
            else None
        )
        item.status = "completed"
        item.result = str(outcome["result"])
        item.exit_price = Decimal(str(exit_price))
        item.exit_at = exit_at
        item.exit_reason = exit_reason
        item.exit_subreason = exit_subreason
        item.raw_return_bps = Decimal(str(outcome["raw_return_bps"]))
        item.directional_return_bps = Decimal(str(outcome["directional_return_bps"]))
        item.estimated_cost_bps = Decimal(str(net_outcome["estimated_cost_bps"]))
        item.net_directional_return_bps = Decimal(
            str(net_outcome["net_directional_return_bps"])
        )
        item.net_result = str(net_outcome["net_result"])
        item.max_favorable_bps = (
            Decimal(str(path_metrics["max_favorable_bps"]))
            if path_metrics["max_favorable_bps"] is not None
            else None
        )
        item.max_adverse_bps = (
            Decimal(str(path_metrics["max_adverse_bps"]))
            if path_metrics["max_adverse_bps"] is not None
            else None
        )
        item.peak_favorable_bps_at_exit = peak_favorable_bps_at_exit
        item.protected_bps_at_exit = protected_bps_at_exit
        item.settlement_version = settlement_version
        evidence["settlement"] = {
            "version": settlement_version,
            "score_exit_policy_version": PREDICTION_SCORE_EXIT_POLICY_VERSION,
            "exit_reason": exit_reason,
            "exit_subreason": exit_subreason,
            "exit_at": exit_at.replace(tzinfo=UTC).isoformat(),
            "exit_price": exit_price,
            "same_bar_conflict": bool(exit_decision.get("same_bar_conflict")),
            "gap_execution": bool(exit_decision.get("gap_execution")),
            "price_source": exit_decision.get("price_source") or "closed_candle_path",
            "reference_price_time_ms": exit_decision.get("reference_price_time_ms"),
            "peak_favorable_bps_at_decision": (
                float(peak_favorable_bps_at_exit)
                if peak_favorable_bps_at_exit is not None
                else None
            ),
            "protected_bps": exit_decision.get("protected_bps"),
            "observed_bar_count": exit_decision.get("observed_bar_count"),
            "risk_plan": risk_plan,
            "score_signal": (
                {
                    key: exit_decision.get(key)
                    for key in (
                        "combined",
                        "technical",
                        "exit_threshold",
                        "confirmation_points",
                        "confirmation_unit",
                        "confirmation_bar_times_ms",
                        "confirmation_scores",
                        "failed_loss_threshold_bps",
                        "failed_threshold_mode",
                        "minimum_hold_ms",
                        "ignored_duplicate_points",
                        "calculated_at",
                        "closed_bar_time_ms",
                        "reference_price_time_ms",
                    )
                    if exit_decision.get(key) is not None
                }
                if exit_reason in {"score_breakdown", "score_reversal"}
                else None
            ),
            "cost_breakdown": prediction_cost_breakdown(
                predicted_at, exit_at, actual_cost_config
            ),
            "policy": (
                "horizon_aligned_profit_guard_then_price_barrier_then_confirmed_"
                "native_bar_score_exit_then_failed_follow_through_then_hard_time_cap"
            ),
        }
        item.evidence_json = evidence
        item.completed_at = now
        item.updated_at = now
        completed += 1
        if exit_reason == "take_profit":
            take_profit += 1
        elif exit_reason == "stop_loss":
            stop_loss += 1
        elif exit_reason in {"score_breakdown", "score_reversal"}:
            score_exit += 1
        else:
            max_holding += 1
        if exit_subreason in {
            "profit_lock",
            "trailing_profit",
        }:
            profit_protection += 1
        elif exit_subreason == "failed_follow_through":
            failed_follow_through += 1
        if previous_status == "unavailable":
            recovered += 1
    db.flush()
    return {
        "completed": completed,
        "recovered": recovered,
        "deferred": deferred,
        "unavailable": unavailable,
        "take_profit": take_profit,
        "stop_loss": stop_loss,
        "score_exit": score_exit,
        "max_holding": max_holding,
        "profit_protection": profit_protection,
        "failed_follow_through": failed_follow_through,
    }


def reopen_legacy_prediction_settlements(
    db: Session,
    *,
    user_id: int | None = None,
    limit: int = PREDICTION_SETTLEMENT_BATCH_SIZE,
) -> int:
    """Reopen only unauditable horizon-close outcomes for reconstruction.

    A completed row from another frozen policy is valid historical evidence,
    not a migration candidate.  Reopening every version mismatch used to
    recalculate old signals with new code and then mix incompatible policies in
    one cohort.
    """

    statement = (
        select(AiMonitorPrediction)
        .where(
            AiMonitorPrediction.status == "completed",
            AiMonitorPrediction.exit_reason == "legacy_horizon_close",
        )
        .order_by(AiMonitorPrediction.predicted_at, AiMonitorPrediction.id)
        .limit(max(1, min(int(limit), 250)))
    )
    if user_id is not None:
        statement = statement.where(AiMonitorPrediction.user_id == user_id)
    # Reopening and settlement share the same row-level ownership protocol so
    # they cannot reset/complete one legacy row concurrently.  Refreshing an
    # identity-map hit is important because evidence_json is replaced as one
    # immutable JSON value.
    statement = statement.execution_options(populate_existing=True).with_for_update(
        skip_locked=True
    )
    items = list(db.scalars(statement).all())
    repair_started_at = utcnow()
    retry_ready_at = repair_started_at - timedelta(
        minutes=PREDICTION_SETTLEMENT_RETRY_MINUTES + 1
    )
    for item in items:
        evidence = dict(item.evidence_json or {})
        stored_risk_plan = evidence.get("risk_plan")
        if isinstance(stored_risk_plan, Mapping):
            frozen_risk_plan = dict(stored_risk_plan)
            frozen_risk_plan["settlement_version"] = (
                PREVIOUS_PREDICTION_SETTLEMENT_VERSION
            )
            evidence["risk_plan"] = frozen_risk_plan
        evidence["settlement_repair"] = {
            "requested_at": repair_started_at.replace(tzinfo=UTC).isoformat(),
            "source_version": item.settlement_version,
            "source_exit_reason": item.exit_reason,
            "target_version": PREVIOUS_PREDICTION_SETTLEMENT_VERSION,
            "status": "pending_recalculation",
        }
        item.status = "pending"
        item.result = None
        item.exit_price = None
        item.exit_at = None
        item.exit_reason = None
        item.exit_subreason = None
        item.raw_return_bps = None
        item.directional_return_bps = None
        item.net_directional_return_bps = None
        item.net_result = None
        item.max_favorable_bps = None
        item.max_adverse_bps = None
        item.peak_favorable_bps_at_exit = None
        item.protected_bps_at_exit = None
        item.completed_at = None
        item.settlement_version = "repair_pending_v4"
        item.evidence_json = evidence
        item.updated_at = retry_ready_at
    db.flush()
    return len(items)

def backfill_prediction_path_metrics(
    db: Session,
    repository: MonitorRepository,
    *,
    user_id: int | None = None,
) -> dict[str, int]:
    """Backfill MFE/MAE for recently completed predictions created before path tracking."""

    now = utcnow()
    statement = (
        select(AiMonitorPrediction)
        .where(
            AiMonitorPrediction.status == "completed",
            AiMonitorPrediction.entry_price.is_not(None),
            AiMonitorPrediction.due_at >= now
            - timedelta(days=PREDICTION_SETTLEMENT_BACKFILL_DAYS),
            or_(
                AiMonitorPrediction.max_favorable_bps.is_(None),
                AiMonitorPrediction.max_adverse_bps.is_(None),
            ),
        )
        .order_by(AiMonitorPrediction.predicted_at, AiMonitorPrediction.id)
        .limit(PREDICTION_SETTLEMENT_BATCH_SIZE)
    )
    if user_id is not None:
        statement = statement.where(AiMonitorPrediction.user_id == user_id)
    items = db.scalars(statement).all()
    grouped: dict[str, list[AiMonitorPrediction]] = {}
    for item in items:
        grouped.setdefault(item.contract_symbol, []).append(item)
    candles_by_symbol: dict[str, list[dict[str, Any]]] = {}
    for symbol, symbol_items in grouped.items():
        try:
            candles_by_symbol[symbol] = repository.kline_range(
                symbol,
                "15m",
                min(_datetime_ms(item.predicted_at) for item in symbol_items) - 15 * 60 * 1_000,
                max(_datetime_ms(item.due_at) for item in symbol_items) + 15 * 60 * 1_000,
            )
        except MonitorUnavailable:
            candles_by_symbol[symbol] = []
    completed = 0
    unavailable = 0
    for item in items:
        path_metrics = prediction_path_metrics(
            candles_by_symbol.get(item.contract_symbol, []),
            float(item.entry_price or 0),
            item.direction,
            _datetime_ms(item.predicted_at),
            _datetime_ms(item.due_at),
        )
        if path_metrics["max_favorable_bps"] is None:
            unavailable += 1
            continue
        item.max_favorable_bps = Decimal(str(path_metrics["max_favorable_bps"]))
        item.max_adverse_bps = Decimal(str(path_metrics["max_adverse_bps"]))
        item.updated_at = now
        completed += 1
    db.flush()
    return {"scanned": len(items), "completed": completed, "unavailable": unavailable}


