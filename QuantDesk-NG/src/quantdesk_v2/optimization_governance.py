# ruff: noqa: B008
"""Auditable optimization and evidence-governed prediction release controls.

This module deliberately does not search data or call models.  Search workers
may use it to validate a proposed parameter set, but each candidate must point
at an immutable completed ``BacktestRun``.  Likewise, an evidence vote only
uses supplied provider assertions and refuses to infer a missing provider.
"""

from __future__ import annotations

import math
import time
import uuid
from decimal import Decimal
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .database import get_db
from .dependencies import get_current_user
from .models import (
    AuditLog,
    BacktestRun,
    ModelEvidence,
    ModelRelease,
    OptimizationCandidate,
    OptimizationRun,
    User,
    UserStrategy,
    utcnow,
)

router = APIRouter(prefix="/api/v2/governance", tags=["optimization-governance"])

MIN_VALIDATION_SAMPLES = 30
MIN_DIRECTIONAL_ACCURACY = 0.50
MAX_BRIER_SCORE = 0.25
MAX_CALIBRATION_GAP = 0.10
MIN_VOTE_PROVIDERS = 2


def _finite(value: Any, *, field: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{field} must be numeric")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be numeric") from exc
    if not math.isfinite(number):
        raise ValueError(f"{field} must be finite")
    return number


def validate_parameter_space(space: dict[str, Any]) -> dict[str, dict[str, float | int]]:
    """Validate a bounded, enumerable parameter search space.

    The optimizer receives a contract only; it cannot silently expand ranges
    after approval. ``step`` makes the cardinality auditable and bounded.
    """

    if not isinstance(space, dict) or not space:
        raise ValueError("parameter_space must not be empty")
    if len(space) > 32:
        raise ValueError("parameter_space supports at most 32 parameters")
    normalized: dict[str, dict[str, float | int]] = {}
    combinations = 1
    for key, raw in space.items():
        if (
            not isinstance(key, str)
            or not key
            or len(key) > 64
            or not key.replace("_", "a").isalnum()
        ):
            raise ValueError("parameter name is invalid")
        if not isinstance(raw, dict):
            raise ValueError(f"parameter {key} must be an object")
        lower = _finite(raw.get("min"), field=f"parameter {key}.min")
        upper = _finite(raw.get("max"), field=f"parameter {key}.max")
        step = _finite(raw.get("step"), field=f"parameter {key}.step")
        if not lower <= upper or step <= 0:
            raise ValueError(f"parameter {key} has invalid bounds")
        choices = int(math.floor((upper - lower) / step + 1e-9)) + 1
        if choices > 1_000:
            raise ValueError(f"parameter {key} has too many values")
        combinations *= choices
        if combinations > 10_000:
            raise ValueError("parameter space exceeds 10000 combinations")
        integer = raw.get("integer", False)
        if not isinstance(integer, bool):
            raise ValueError(f"parameter {key}.integer must be boolean")
        if integer and not all(float(value).is_integer() for value in (lower, upper, step)):
            raise ValueError(f"integer parameter {key} requires integer bounds and step")
        normalized[key] = {
            "min": int(lower) if integer else lower,
            "max": int(upper) if integer else upper,
            "step": int(step) if integer else step,
            "integer": integer,
        }
    return normalized


def evaluate_backtest_candidate(
    *, parameters: dict[str, Any], space: dict[str, dict[str, float | int]], metrics: dict[str, Any]
) -> tuple[bool, dict[str, Any], str | None]:
    """Evaluate only real persisted metrics; missing values reject, never score 0."""

    if set(parameters) != set(space):
        return False, {}, "candidate parameters do not match the approved search space"
    normalized: dict[str, float | int] = {}
    for name, rule in space.items():
        try:
            value = _finite(parameters[name], field=f"parameter {name}")
        except ValueError as exc:
            return False, {}, str(exc)
        if not float(rule["min"]) <= value <= float(rule["max"]):
            return False, {}, f"parameter {name} is outside approved bounds"
        step = float(rule["step"])
        if not math.isclose(
            (value - float(rule["min"])) / step,
            round((value - float(rule["min"])) / step),
            abs_tol=1e-8,
        ):
            return False, {}, f"parameter {name} is not aligned to the approved step"
        if bool(rule["integer"]) and not value.is_integer():
            return False, {}, f"parameter {name} must be an integer"
        normalized[name] = int(value) if bool(rule["integer"]) else value
    required = ("total_return_pct", "max_drawdown_pct", "sharpe_ratio", "trade_count")
    try:
        values = {
            name: _finite(metrics.get(name), field=f"backtest metric {name}") for name in required
        }
    except ValueError as exc:
        return False, {}, str(exc)
    if values["trade_count"] < 10:
        return (
            False,
            {},
            "backtest has fewer than 10 trades; candidate is not statistically reviewable",
        )
    if values["max_drawdown_pct"] < 0 or values["max_drawdown_pct"] > 100:
        return False, {}, "backtest max_drawdown_pct is invalid"
    # This ranking field is transparent, not a prediction or an accuracy claim.
    score = values["total_return_pct"] + values["sharpe_ratio"] * 10 - values["max_drawdown_pct"]
    return (
        True,
        {"parameters": normalized, "metrics": values, "ranking_score": round(score, 8)},
        None,
    )


def release_gate(metric: dict[str, Any] | None) -> tuple[bool, dict[str, Any]]:
    """Return a conservative model-release gate from forward validation only."""

    gate = {
        "minimum_directional_samples": MIN_VALIDATION_SAMPLES,
        "minimum_directional_accuracy": MIN_DIRECTIONAL_ACCURACY,
        "maximum_brier_score": MAX_BRIER_SCORE,
        "maximum_calibration_gap": MAX_CALIBRATION_GAP,
    }
    if not isinstance(metric, dict):
        return False, {**gate, "reason": "no forward validation metric exists"}
    if metric.get("status") != "validated":
        return False, {**gate, "reason": "validation is still collecting"}
    try:
        count = int(metric.get("directional_predictions") or 0)
        accuracy = _finite(metric.get("directional_accuracy"), field="directional_accuracy")
        brier = _finite(metric.get("brier_score"), field="brier_score")
        calibration = _finite(metric.get("calibration_gap"), field="calibration_gap")
    except (ValueError, TypeError):
        return False, {**gate, "reason": "validation metric is incomplete"}
    passed = (
        count >= MIN_VALIDATION_SAMPLES
        and accuracy >= MIN_DIRECTIONAL_ACCURACY
        and brier <= MAX_BRIER_SCORE
        and calibration <= MAX_CALIBRATION_GAP
    )
    return passed, {
        **gate,
        "observed": {
            "directional_predictions": count,
            "directional_accuracy": accuracy,
            "brier_score": brier,
            "calibration_gap": calibration,
        },
        "reason": "passed" if passed else "validation metric does not meet release gate",
    }


def build_consensus(evidence: list[dict[str, Any]], *, now_ms: int | None = None) -> dict[str, Any]:
    """Combine current independent evidence or return an explicit observation state."""

    now = int(now_ms or time.time() * 1_000)
    fresh = [
        item
        for item in evidence
        if int(item.get("as_of_ms") or 0) <= now < int(item.get("expires_at_ms") or 0)
    ]
    providers = {str(item.get("provider_key") or "") for item in fresh if item.get("provider_key")}
    if len(providers) < MIN_VOTE_PROVIDERS:
        return {
            "state": "insufficient_evidence",
            "direction": "neutral",
            "provider_count": len(providers),
            "reason": "at least two independent fresh providers are required",
        }
    weights = {"long": 0.0, "short": 0.0, "neutral": 0.0}
    for item in fresh:
        direction = str(item.get("direction"))
        if direction not in weights:
            continue
        probability = max(0.0, min(1.0, float(item.get("probability") or 0)))
        quality = max(0.0, min(1.0, float(item.get("quality_score") or 0)))
        weights[direction] += probability * quality
    total = sum(weights.values())
    if total <= 0:
        return {
            "state": "insufficient_evidence",
            "direction": "neutral",
            "provider_count": len(providers),
            "reason": "fresh evidence has no usable quality-weighted vote",
        }
    direction = max(weights, key=weights.get)
    probability = weights[direction] / total
    # A weak majority is not converted into a trade recommendation.
    state = "consensus" if direction != "neutral" and probability >= 0.55 else "observe"
    return {
        "state": state,
        "direction": direction if state == "consensus" else "neutral",
        "probability": round(probability, 8),
        "provider_count": len(providers),
        "weights": {key: round(value / total, 8) for key, value in weights.items()},
    }


class OptimizationCreate(BaseModel):
    strategy_id: str = Field(min_length=1, max_length=36)
    parameter_space: dict[str, Any]
    objective: dict[str, Any] = Field(default_factory=lambda: {"primary": "ranking_score"})
    baseline_backtest_run_id: int | None = Field(default=None, ge=1)


class CandidateCreate(BaseModel):
    backtest_run_id: int = Field(ge=1)
    parameters: dict[str, Any]


class DecisionRequest(BaseModel):
    reason: str = Field(min_length=3, max_length=500)


class EvidenceCreate(BaseModel):
    source_kind: Literal["model", "evidence"]
    provider_key: str = Field(min_length=2, max_length=64, pattern=r"^[a-zA-Z0-9_.-]+$")
    provider_version: str = Field(min_length=1, max_length=64)
    symbol: str = Field(min_length=3, max_length=32)
    horizon_seconds: int = Field(ge=60, le=86_400)
    direction: Literal["long", "short", "neutral"]
    probability: float = Field(ge=0, le=1)
    quality_score: float = Field(ge=0, le=1)
    as_of_ms: int = Field(ge=1)
    expires_at_ms: int = Field(ge=1)
    evidence: dict[str, Any]

    @field_validator("symbol")
    @classmethod
    def normalize_symbol(cls, value: str) -> str:
        return value.strip().upper()


def _audit(
    db: Session,
    request: Request,
    action: str,
    user: User,
    resource_id: str,
    metadata: dict[str, Any],
) -> None:
    db.add(
        AuditLog(
            user_id=user.id,
            action=action,
            resource_type="optimization_governance",
            resource_id=resource_id,
            ip_address=request.client.host[:45] if request.client else None,
            metadata_json=metadata,
        )
    )


def _run_for_user(db: Session, user: User, public_id: str) -> OptimizationRun:
    record = db.scalar(
        select(OptimizationRun).where(
            OptimizationRun.public_id == public_id, OptimizationRun.user_id == user.id
        )
    )
    if record is None:
        raise HTTPException(status_code=404, detail="optimization run not found")
    return record


def _serialize_run(db: Session, run: OptimizationRun) -> dict[str, Any]:
    candidates = db.scalars(
        select(OptimizationCandidate)
        .where(OptimizationCandidate.optimization_run_id == run.id)
        .order_by(OptimizationCandidate.id)
    ).all()
    return {
        "id": run.public_id,
        "strategy_id": run.strategy_id,
        "status": run.status,
        "parameter_space": run.parameter_space_json,
        "objective": run.objective_json,
        "proposal": run.proposal_json,
        "decision_reason": run.decision_reason,
        "candidates": [
            {
                "backtest_run_id": item.backtest_run_id,
                "parameters": item.parameters_json,
                "evaluation": item.evaluation_json,
                "status": item.status,
                "rejection_reason": item.rejection_reason,
            }
            for item in candidates
        ],
    }


@router.post("/optimizations", status_code=status.HTTP_201_CREATED)
def create_optimization(
    payload: OptimizationCreate,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict[str, Any]:
    try:
        space = validate_parameter_space(payload.parameter_space)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    strategy = db.scalar(
        select(UserStrategy).where(
            UserStrategy.public_id == payload.strategy_id,
            UserStrategy.user_id == user.id,
            UserStrategy.status == "active",
        )
    )
    if strategy is None:
        raise HTTPException(
            status_code=422, detail="optimization strategy must be this user's active strategy"
        )
    if payload.baseline_backtest_run_id is not None:
        baseline = db.scalar(
            select(BacktestRun).where(
                BacktestRun.id == payload.baseline_backtest_run_id,
                BacktestRun.user_id == user.id,
                BacktestRun.status == "completed",
            )
        )
        if baseline is None:
            raise HTTPException(
                status_code=422, detail="baseline must be this user's completed backtest"
            )
    run = OptimizationRun(
        public_id=str(uuid.uuid4()),
        user_id=user.id,
        strategy_id=payload.strategy_id,
        baseline_backtest_run_id=payload.baseline_backtest_run_id,
        parameter_space_json=space,
        objective_json=payload.objective,
        status="evaluating",
        created_at=utcnow(),
        updated_at=utcnow(),
    )
    db.add(run)
    _audit(db, request, "optimization.create", user, run.public_id, {"parameter_count": len(space)})
    db.commit()
    return _serialize_run(db, run)


@router.post("/optimizations/{public_id}/candidates", status_code=status.HTTP_201_CREATED)
def add_candidate(
    public_id: str,
    payload: CandidateCreate,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict[str, Any]:
    run = _run_for_user(db, user, public_id)
    if run.status not in {"draft", "evaluating"}:
        raise HTTPException(status_code=409, detail="optimization no longer accepts candidates")
    backtest = db.scalar(
        select(BacktestRun).where(
            BacktestRun.id == payload.backtest_run_id,
            BacktestRun.user_id == user.id,
            BacktestRun.status == "completed",
        )
    )
    if backtest is None:
        raise HTTPException(
            status_code=422, detail="candidate must reference this user's completed backtest"
        )
    if backtest.strategy_id != run.strategy_id:
        raise HTTPException(
            status_code=422, detail="candidate backtest strategy does not match optimization"
        )
    accepted, evaluation, reason = evaluate_backtest_candidate(
        parameters=payload.parameters,
        space=run.parameter_space_json,
        metrics=backtest.metrics_json or {},
    )
    candidate = OptimizationCandidate(
        optimization_run_id=run.id,
        backtest_run_id=backtest.id,
        parameters_json=payload.parameters,
        evaluation_json=evaluation,
        status="accepted" if accepted else "rejected",
        rejection_reason=reason,
    )
    db.add(candidate)
    run.updated_at = utcnow()
    _audit(
        db,
        request,
        "optimization.candidate.evaluate",
        user,
        run.public_id,
        {"backtest_run_id": backtest.id, "accepted": accepted, "reason": reason},
    )
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=409, detail="this backtest is already registered as a candidate"
        ) from None
    return _serialize_run(db, run)


@router.post("/optimizations/{public_id}/propose")
def propose_best_candidate(
    public_id: str,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict[str, Any]:
    run = _run_for_user(db, user, public_id)
    if run.status != "evaluating":
        raise HTTPException(status_code=409, detail="optimization is not evaluating")
    candidates = db.scalars(
        select(OptimizationCandidate).where(
            OptimizationCandidate.optimization_run_id == run.id,
            OptimizationCandidate.status == "accepted",
        )
    ).all()
    if not candidates:
        raise HTTPException(
            status_code=422, detail="no accepted backtest candidate can be proposed"
        )
    best = max(
        candidates,
        key=lambda item: float((item.evaluation_json or {}).get("ranking_score", -math.inf)),
    )
    run.proposal_json = {
        "candidate_id": best.id,
        "backtest_run_id": best.backtest_run_id,
        "parameters": best.parameters_json,
        "evaluation": best.evaluation_json,
        "generated_from": "completed_backtest",
    }
    run.status, run.updated_at = "proposed", utcnow()
    _audit(db, request, "optimization.propose", user, run.public_id, {"candidate_id": best.id})
    db.commit()
    return _serialize_run(db, run)


@router.post("/optimizations/{public_id}/{decision}")
def decide_optimization(
    public_id: str,
    decision: Literal["approve", "reject", "rollback"],
    payload: DecisionRequest,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict[str, Any]:
    run = _run_for_user(db, user, public_id)
    expected = {
        "approve": {"proposed"},
        "reject": {"proposed", "evaluating"},
        "rollback": {"approved"},
    }
    if run.status not in expected[decision]:
        raise HTTPException(status_code=409, detail="invalid optimization decision transition")
    run.status = {"approve": "approved", "reject": "rejected", "rollback": "rolled_back"}[decision]
    run.decision_reason, run.decided_at, run.updated_at = payload.reason, utcnow(), utcnow()
    _audit(db, request, f"optimization.{decision}", user, run.public_id, {"reason": payload.reason})
    db.commit()
    return _serialize_run(db, run)


@router.post("/evidence", status_code=status.HTTP_201_CREATED)
def record_evidence(
    payload: EvidenceCreate,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict[str, Any]:
    if payload.expires_at_ms <= payload.as_of_ms:
        raise HTTPException(status_code=422, detail="evidence expiry must be after as_of_ms")
    key = f"{payload.provider_key}:{payload.provider_version}:{payload.symbol}:{payload.horizon_seconds}:{payload.as_of_ms}"
    if payload.source_kind == "model":
        try:
            if int(payload.provider_version) < 1:
                raise ValueError
        except ValueError:
            raise HTTPException(
                status_code=422,
                detail="model evidence requires a positive integer provider_version",
            ) from None
    record = ModelEvidence(
        evidence_key=key,
        source_kind=payload.source_kind,
        provider_key=payload.provider_key,
        provider_version=payload.provider_version,
        symbol=payload.symbol,
        horizon_seconds=payload.horizon_seconds,
        direction=payload.direction,
        probability=Decimal(str(payload.probability)),
        quality_score=Decimal(str(payload.quality_score)),
        as_of_ms=payload.as_of_ms,
        expires_at_ms=payload.expires_at_ms,
        evidence_json=payload.evidence,
    )
    db.add(record)
    _audit(
        db,
        request,
        "model_evidence.record",
        user,
        key,
        {
            "provider": payload.provider_key,
            "symbol": payload.symbol,
            "horizon_seconds": payload.horizon_seconds,
        },
    )
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=409, detail="duplicate evidence assertion is immutable"
        ) from None
    return {"evidence_key": key, "status": "recorded"}


@router.get("/consensus/{symbol}/{horizon_seconds}")
def consensus(
    symbol: str,
    horizon_seconds: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict[str, Any]:
    del user
    now = int(time.time() * 1_000)
    rows = db.scalars(
        select(ModelEvidence).where(
            ModelEvidence.symbol == symbol.upper(),
            ModelEvidence.horizon_seconds == horizon_seconds,
            ModelEvidence.as_of_ms <= now,
            ModelEvidence.expires_at_ms > now,
        )
    ).all()
    releases = {
        (release.model_key, release.model_version)
        for release in db.scalars(
            select(ModelRelease).where(
                ModelRelease.horizon_seconds == horizon_seconds, ModelRelease.status == "approved"
            )
        ).all()
    }
    eligible, blocked_models = [], 0
    for row in rows:
        if row.source_kind == "model":
            try:
                approved = (row.provider_key, int(row.provider_version)) in releases
            except ValueError:
                approved = False
            if not approved:
                blocked_models += 1
                continue
        eligible.append(
            {
                "provider_key": row.provider_key,
                "direction": row.direction,
                "probability": float(row.probability),
                "quality_score": float(row.quality_score),
                "as_of_ms": row.as_of_ms,
                "expires_at_ms": row.expires_at_ms,
            }
        )
    output = build_consensus(eligible, now_ms=now)
    output.update(
        {
            "symbol": symbol.upper(),
            "horizon_seconds": horizon_seconds,
            "as_of_ms": now,
            "blocked_unreleased_model_votes": blocked_models,
        }
    )
    return output


@router.post("/releases/{model_key}/{model_version}/{horizon_seconds}")
def evaluate_release(
    model_key: str,
    model_version: int,
    horizon_seconds: int,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict[str, Any]:
    # The metrics table is maintained by the prediction worker. Query through
    # SQL here because it is intentionally not an ORM domain model.
    row = (
        db.execute(
            text(
                """SELECT model_key,model_version,horizon_seconds,status,directional_predictions,directional_accuracy,brier_score,calibration_gap,evaluated_until_ms FROM prediction_validation_metrics WHERE model_key=:key AND model_version=:version AND horizon_seconds=:horizon ORDER BY evaluated_until_ms DESC LIMIT 1"""
            ),
            {"key": model_key, "version": model_version, "horizon": horizon_seconds},
        )
        .mappings()
        .first()
    )
    snapshot = dict(row) if row else None
    passed, gate = release_gate(snapshot)
    release = db.scalar(
        select(ModelRelease).where(
            ModelRelease.model_key == model_key,
            ModelRelease.model_version == model_version,
            ModelRelease.horizon_seconds == horizon_seconds,
        )
    )
    if release is None:
        release = ModelRelease(
            model_key=model_key,
            model_version=model_version,
            horizon_seconds=horizon_seconds,
            status="approved" if passed else "rejected",
            validation_snapshot_json=snapshot or {},
            gate_json=gate,
            decision_reason=gate["reason"],
            decided_at=utcnow(),
        )
        db.add(release)
    else:
        (
            release.status,
            release.validation_snapshot_json,
            release.gate_json,
            release.decision_reason,
            release.decided_at,
        ) = ("approved" if passed else "rejected"), (snapshot or {}), gate, gate["reason"], utcnow()
    _audit(
        db,
        request,
        "model_release.evaluate",
        user,
        f"{model_key}:{model_version}:{horizon_seconds}",
        {"passed": passed, "gate": gate},
    )
    db.commit()
    return {
        "model_key": model_key,
        "model_version": model_version,
        "horizon_seconds": horizon_seconds,
        "status": release.status,
        "validation": snapshot,
        "gate": gate,
    }
