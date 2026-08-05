"""Leakage-safe forward-outcome validation for prediction models.

The module deliberately evaluates only labels that were completed before the
requested evaluation timestamp.  It never joins a prediction to later feature
values, and it reports insufficient samples instead of manufacturing an
accuracy percentage.
"""

from __future__ import annotations

import math
import time
from collections import defaultdict
from collections.abc import Iterable
from typing import Any

from . import store

WINDOW_DAYS = 30
MIN_DIRECTIONAL_SAMPLES = 30
DAY_MS = 24 * 60 * 60 * 1_000


def _number(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def _rounded(value: float | None, digits: int = 6) -> float | None:
    return round(value, digits) if value is not None else None


def summarize_rows(
    rows: Iterable[dict[str, Any]], *, window_start_ms: int, evaluated_until_ms: int
) -> list[dict[str, Any]]:
    """Summarize already-completed outcomes by model and horizon.

    ``actual_result='neutral'`` remains a failed directional call.  This avoids
    inflating accuracy by silently dropping non-events.  Brier score and
    calibration gap are only calculated for issued long/short calls.
    """

    grouped: dict[tuple[str, int, int], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        completed_at = int(row.get("completed_at_ms") or 0)
        if not (window_start_ms <= completed_at <= evaluated_until_ms):
            continue
        grouped[(str(row.get("model_key") or "unknown"), int(row.get("model_version") or 0), int(row.get("horizon_seconds") or 0))].append(row)

    summaries: list[dict[str, Any]] = []
    for (model_key, model_version, horizon_seconds), group in sorted(grouped.items()):
        directional = [row for row in group if row.get("result") in {"long", "short"}]
        correct = sum(row.get("result") == row.get("actual_result") for row in directional)
        probabilities = [
            _number(row.get("long_probability") if row.get("result") == "long" else row.get("short_probability"))
            for row in directional
        ]
        outcomes = [1.0 if row.get("result") == row.get("actual_result") else 0.0 for row in directional]
        count = len(directional)
        accuracy = correct / count if count else None
        brier = (
            sum(
                (probability - outcome) ** 2
                for probability, outcome in zip(probabilities, outcomes, strict=True)
            )
            / count
            if count
            else None
        )
        calibration_gap = abs(sum(probabilities) / count - accuracy) if count and accuracy is not None else None
        returns = [_number(row.get("directional_return_bps")) for row in directional if row.get("directional_return_bps") is not None]
        summaries.append(
            {
                "model_key": model_key,
                "model_version": model_version,
                "horizon_seconds": horizon_seconds,
                "window_start_ms": window_start_ms,
                "evaluated_until_ms": evaluated_until_ms,
                "completed_outcomes": len(group),
                "directional_predictions": count,
                "correct_directional": correct,
                "coverage_ratio": _rounded(count / len(group) if group else 0.0),
                "directional_accuracy": _rounded(accuracy),
                "brier_score": _rounded(brier),
                "calibration_gap": _rounded(calibration_gap),
                "mean_net_return_bps": _rounded(sum(returns) / len(returns) if returns else None),
                "last_completed_at_ms": max(int(row.get("completed_at_ms") or 0) for row in group),
                "status": "validated" if count >= MIN_DIRECTIONAL_SAMPLES else "collecting",
                "minimum_directional_samples": MIN_DIRECTIONAL_SAMPLES,
            }
        )
    return summaries


def completed_outcome_rows(window_start_ms: int, evaluated_until_ms: int) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in store.query(
            """SELECT p.model_key,p.model_version,p.horizon_seconds,p.result,
                      p.long_probability,p.short_probability,o.actual_result,
                      o.directional_return_bps,o.completed_at_ms
                 FROM prediction_outcomes o JOIN battle_predictions p ON p.id=o.prediction_id
                WHERE o.status='completed' AND o.completed_at_ms BETWEEN ? AND ?
                ORDER BY o.completed_at_ms""",
            (window_start_ms, evaluated_until_ms),
        )
    ]


def refresh_validation_metrics(now_ms: int | None = None) -> list[dict[str, Any]]:
    """Persist the rolling 30-day validation snapshot for audit/replay APIs."""

    evaluated_until = int(now_ms or time.time() * 1_000)
    day_start = evaluated_until // DAY_MS * DAY_MS
    window_start = day_start - WINDOW_DAYS * DAY_MS
    summaries = summarize_rows(
        completed_outcome_rows(window_start, evaluated_until),
        window_start_ms=window_start,
        evaluated_until_ms=evaluated_until,
    )
    for summary in summaries:
        store.execute(
            """INSERT INTO prediction_validation_metrics(
                   model_key,model_version,horizon_seconds,window_start_ms,evaluated_until_ms,
                   completed_outcomes,directional_predictions,correct_directional,coverage_ratio,
                   directional_accuracy,brier_score,calibration_gap,mean_net_return_bps,
                   last_completed_at_ms,status,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)
               ON DUPLICATE KEY UPDATE evaluated_until_ms=VALUES(evaluated_until_ms),
                   completed_outcomes=VALUES(completed_outcomes),
                   directional_predictions=VALUES(directional_predictions),
                   correct_directional=VALUES(correct_directional),coverage_ratio=VALUES(coverage_ratio),
                   directional_accuracy=VALUES(directional_accuracy),brier_score=VALUES(brier_score),
                   calibration_gap=VALUES(calibration_gap),mean_net_return_bps=VALUES(mean_net_return_bps),
                   last_completed_at_ms=VALUES(last_completed_at_ms),status=VALUES(status),
                   updated_at=CURRENT_TIMESTAMP""",
            (
                summary["model_key"], summary["model_version"], summary["horizon_seconds"],
                summary["window_start_ms"], summary["evaluated_until_ms"],
                summary["completed_outcomes"], summary["directional_predictions"], summary["correct_directional"],
                summary["coverage_ratio"], summary["directional_accuracy"], summary["brier_score"],
                summary["calibration_gap"], summary["mean_net_return_bps"], summary["last_completed_at_ms"],
                summary["status"],
            ),
        )
    return summaries
