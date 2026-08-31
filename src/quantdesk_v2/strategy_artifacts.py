"""Immutable revision artifacts and reproducible strategy run manifests."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import uuid
from collections.abc import Mapping
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import __version__
from .models import (
    BacktestRun,
    StrategyArtifact,
    StrategyDeployment,
    StrategyRevision,
    StrategyRunManifest,
    StrategyValidationRun,
    UserStrategy,
    utcnow,
)


def canonical_hash(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def record_revision_artifact(
    db: Session,
    strategy: UserStrategy,
    revision: StrategyRevision,
) -> StrategyArtifact:
    """Persist the build identity and initial static validation exactly once."""

    db.flush()
    existing = db.scalar(
        select(StrategyArtifact).where(StrategyArtifact.strategy_revision_id == revision.id)
    )
    if existing is not None:
        return existing
    validation = (
        copy.deepcopy(revision.validation_json)
        if isinstance(revision.validation_json, dict)
        else {}
    )
    runtime_digest = os.environ.get("QUANTDESK_RUNTIME_IMAGE_DIGEST", "").strip()
    if not runtime_digest:
        runtime_digest = f"quantdesk-v2:{__version__}"
    parameters = copy.deepcopy(strategy.parameters_json or {})
    dependency_manifest = {
        "engine_key": strategy.engine_key,
        "strategy_kind": strategy.strategy_kind,
        "source_runtime_version": strategy.source_runtime_version,
        "spec_schema_version": strategy.spec_schema_version,
        "validation_engine": validation.get("engine"),
        "application_version": __version__,
    }
    manifest = {
        "strategy_id": strategy.public_id,
        "strategy_version": revision.version,
        "revision_id": revision.id,
        "source_hash": revision.source_hash,
        "spec_hash": revision.spec_hash,
        "runtime_image_digest": runtime_digest,
        "parameter_hash": canonical_hash(parameters),
        "dependency_manifest": dependency_manifest,
        "dependency_hash": canonical_hash(dependency_manifest),
    }
    artifact = StrategyArtifact(
        public_id=str(uuid.uuid4()),
        strategy_revision_id=revision.id,
        user_id=revision.user_id,
        source_hash=revision.source_hash or revision.spec_hash,
        runtime_image_digest=runtime_digest[:191],
        parameter_hash=manifest["parameter_hash"],
        dependency_hash=manifest["dependency_hash"],
        artifact_manifest_json=manifest,
        created_at=utcnow(),
    )
    db.add(artifact)
    now = utcnow()
    db.add(
        StrategyValidationRun(
            public_id=str(uuid.uuid4()),
            strategy_revision_id=revision.id,
            user_id=revision.user_id,
            validation_type="static",
            status="passed" if validation.get("valid") is True else "failed",
            report_json=validation or {"valid": False, "reason": "validation_missing"},
            started_at=now,
            completed_at=now,
            created_at=now,
        )
    )
    return artifact


def add_run_manifest(
    db: Session,
    deployment: StrategyDeployment,
    revision: StrategyRevision,
    *,
    data_set_id: str | None = None,
    extra: Mapping[str, Any] | None = None,
) -> StrategyRunManifest:
    """Freeze all known inputs for a new deployment; never update an old manifest."""

    if deployment.mode == "backtest":
        raise ValueError("backtests must be owned by BacktestRun, not StrategyDeployment")
    db.flush()
    existing = db.scalar(
        select(StrategyRunManifest).where(StrategyRunManifest.deployment_id == deployment.id)
    )
    if existing is not None:
        return existing
    artifact = db.scalar(
        select(StrategyArtifact).where(StrategyArtifact.strategy_revision_id == revision.id)
    )
    engine_version = f"quantdesk-v2:{__version__}"
    cost_model_version = "costs_v2_fee_slippage_funding"
    fill_model_version = (
        "binance_authoritative_v1" if deployment.mode == "live" else "deterministic_bar_fill_v2"
    )
    risk_policy_version = "risk_unit_v2"
    resolved_data_set_id = data_set_id or (
        f"{deployment.mode}:{deployment.target_account_id}"
        if deployment.target_account_id is not None
        else f"{deployment.mode}:unbound"
    )
    body = {
        "deployment_id": deployment.public_id,
        "mode": deployment.mode,
        "strategy_revision": {
            "id": revision.id,
            "version": revision.version,
            "source_hash": revision.source_hash,
            "spec_hash": revision.spec_hash,
            "artifact_id": artifact.public_id if artifact is not None else None,
            "artifact_hash": (
                canonical_hash(artifact.artifact_manifest_json) if artifact is not None else None
            ),
        },
        "data_set_id": resolved_data_set_id,
        "engine_version": engine_version,
        "cost_model_version": cost_model_version,
        "fill_model_version": fill_model_version,
        "risk_policy_version": risk_policy_version,
        "universe_override": copy.deepcopy(deployment.universe_override_json),
        "risk_override": copy.deepcopy(deployment.risk_override_json),
        "runtime_state_initial": copy.deepcopy(deployment.runtime_state_json),
        "extra": copy.deepcopy(dict(extra or {})),
    }
    manifest_hash = canonical_hash(body)
    manifest = StrategyRunManifest(
        public_id=str(uuid.uuid4()),
        deployment_id=deployment.id,
        strategy_revision_id=revision.id,
        user_id=deployment.user_id,
        mode=deployment.mode,
        data_set_id=resolved_data_set_id[:191],
        engine_version=engine_version,
        cost_model_version=cost_model_version,
        fill_model_version=fill_model_version,
        risk_policy_version=risk_policy_version,
        manifest_json=body,
        manifest_hash=manifest_hash,
        created_at=utcnow(),
    )
    db.add(manifest)
    return manifest


def add_backtest_run_manifest(
    db: Session,
    run: BacktestRun,
    revision: StrategyRevision | None,
    *,
    data_set_id: str | None = None,
    extra: Mapping[str, Any] | None = None,
) -> StrategyRunManifest:
    """Freeze a backtest input snapshot without creating a fake deployment."""

    db.flush()
    existing = db.scalar(
        select(StrategyRunManifest).where(StrategyRunManifest.backtest_run_id == run.id)
    )
    if existing is not None:
        return existing
    artifact = None
    if revision is not None:
        artifact = db.scalar(
            select(StrategyArtifact).where(
                StrategyArtifact.strategy_revision_id == revision.id
            )
        )
    engine_version = f"quantdesk-v2:{__version__}"
    cost_model_version = "costs_v2_fee_slippage_funding"
    fill_model_version = "deterministic_bar_fill_v2"
    risk_policy_version = "risk_unit_v2"
    resolved_data_set_id = data_set_id or (
        f"backtest:{run.symbol}:{run.timeframe}:{run.start_at.isoformat()}:{run.end_at.isoformat()}"
    )
    revision_body = None
    if revision is not None:
        revision_body = {
            "id": revision.id,
            "version": revision.version,
            "source_hash": revision.source_hash,
            "spec_hash": revision.spec_hash,
            "artifact_id": artifact.public_id if artifact is not None else None,
            "artifact_hash": (
                canonical_hash(artifact.artifact_manifest_json)
                if artifact is not None
                else None
            ),
        }
    body = {
        "backtest_run_id": run.public_id,
        "mode": "backtest",
        "strategy_revision": revision_body,
        "strategy_key": run.strategy_id,
        "data_set_id": resolved_data_set_id,
        "engine_version": engine_version,
        "cost_model_version": cost_model_version,
        "fill_model_version": fill_model_version,
        "risk_policy_version": risk_policy_version,
        "config": copy.deepcopy(run.config_json),
        "extra": copy.deepcopy(dict(extra or {})),
    }
    manifest = StrategyRunManifest(
        public_id=str(uuid.uuid4()),
        deployment_id=None,
        backtest_run_id=run.id,
        strategy_revision_id=revision.id if revision is not None else None,
        user_id=run.user_id,
        mode="backtest",
        data_set_id=resolved_data_set_id[:191],
        engine_version=engine_version,
        cost_model_version=cost_model_version,
        fill_model_version=fill_model_version,
        risk_policy_version=risk_policy_version,
        manifest_json=body,
        manifest_hash=canonical_hash(body),
        created_at=utcnow(),
    )
    db.add(manifest)
    return manifest
