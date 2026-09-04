"""Mode-neutral strategy evaluation, audit persistence and entry snapshots."""

from __future__ import annotations

import json
import math
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from ..domain.exit_policy import ExitLevelPlan
from ..domain.runtime import DecisionEnvelope, decision_record_key
from ..strategy_evaluator import resolve_strategy_timing_policy
from .strategy_parameter_profiles import apply_parameter_profile
from .strategy_signals import (
    EvaluatedStrategySignal,
    build_builtin_strategy_evidence,
    evaluate_strategy_snapshot,
)

ENTRY_BASIS_SCHEMA_VERSION = 2
DEFAULT_MAX_HOLDING_BARS = 12

QueryRows = Callable[[str, tuple[Any, ...]], Sequence[Mapping[str, Any]]]
ExecuteStatement = Callable[[str, tuple[Any, ...]], Any]
DecisionRecorder = Callable[
    [
        dict[str, Any],
        str,
        dict[str, Any],
        Any,
        dict[str, Any] | None,
        DecisionEnvelope | None,
    ],
    bool,
]


def strategy_snapshots(account: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Return the immutable strategy bundle captured on an execution account."""

    snapshot = account.get("strategy_snapshot_json")
    selected = dict(snapshot) if isinstance(snapshot, Mapping) else {}
    bundled = selected.get("strategy_snapshots")
    if isinstance(bundled, list):
        normalized = [dict(item) for item in bundled if isinstance(item, Mapping)]
        if normalized:
            return normalized
    return [selected] if selected else []


def evaluate_account_strategy(
    account: dict[str, Any],
    symbol: str,
    snapshot: dict[str, Any] | None = None,
    *,
    load_klines: Callable[[str, str, int], Sequence[Mapping[str, Any]]],
    record_decision: DecisionRecorder | None,
    full_validator: Callable[[Any], dict[str, Any]] | None = None,
    full_evaluator: Callable[[dict[str, Any], dict[str, Any]], Any] | None = None,
    source_validator: Callable[[str, str], Any] | None = None,
    source_evaluator: Callable[..., Any] | None = None,
    query: QueryRows | None = None,
) -> EvaluatedStrategySignal:
    """Evaluate the selected revision and fail closed if it cannot be audited."""

    snapshots = strategy_snapshots(account)
    selected = dict(snapshot) if snapshot is not None else (snapshots[0] if snapshots else {})
    effective_account = account
    if query is not None and selected and account.get("strategy_id"):
        effective_account, selected, _ = apply_parameter_profile(
            account,
            selected,
            symbol,
            query=query,
        )
    evaluator_options: dict[str, Any] = {}
    if full_validator is not None:
        evaluator_options["full_validator"] = full_validator
    if full_evaluator is not None:
        evaluator_options["full_evaluator"] = full_evaluator
    if source_validator is not None:
        evaluator_options["source_validator"] = source_validator
    if source_evaluator is not None:
        evaluator_options["source_evaluator"] = source_evaluator
    result = evaluate_strategy_snapshot(
        selected,
        symbol,
        effective_account.get("config_json")
        if isinstance(effective_account.get("config_json"), Mapping)
        else None,
        load_klines=load_klines,
        evidence_builder=build_builtin_strategy_evidence,
        **evaluator_options,
    )
    if (
        str(selected.get("strategy_kind") or "")
        not in {
            "source_strategy",
            "full_strategy",
        }
        or not result.direction
    ):
        return result
    persisted = (
        result.runtime_decision is not None
        and result.audit_spec is not None
        and result.envelope is not None
        and record_decision is not None
        and record_decision(
            account,
            symbol,
            result.audit_spec,
            result.runtime_decision,
            selected,
            result.envelope,
        )
    )
    if persisted:
        return result
    return EvaluatedStrategySignal(
        direction=0,
        atr=result.atr,
        basis=(*result.basis, "信号未执行：缺少可审计的策略部署记录"),
        signal_time=result.signal_time,
        evidence={},
        envelope=None,
    )


def record_strategy_decision(
    account: dict[str, Any],
    symbol: str,
    spec: dict[str, Any],
    decision: Any,
    snapshot: dict[str, Any] | None = None,
    envelope: DecisionEnvelope | None = None,
    *,
    query: QueryRows,
    execute: ExecuteStatement,
    log_mode: str | None = None,
) -> bool:
    """Persist one deterministic strategy decision for paper or live execution."""

    deployment_mode = str(account.get("deployment_mode") or "paper")
    if deployment_mode not in {"paper", "shadow", "live"}:
        return False
    if deployment_mode == "shadow":
        return decision.signal_time is not None
    strategy_public_id = (snapshot or {}).get("public_id")
    if strategy_public_id:
        deployments = query(
            """SELECT d.id,d.strategy_revision_id FROM strategy_deployments d
               JOIN user_strategies s ON s.id=d.strategy_id AND s.user_id=d.user_id
               WHERE d.user_id=? AND d.mode=? AND d.target_account_id=?
                 AND d.status='running' AND s.public_id=?
               ORDER BY d.id DESC LIMIT 1""",
            (
                account["user_id"],
                deployment_mode,
                account["id"],
                strategy_public_id,
            ),
        )
    else:
        deployments = query(
            """SELECT id,strategy_revision_id FROM strategy_deployments
               WHERE user_id=? AND mode=? AND target_account_id=? AND status='running'
               ORDER BY id DESC LIMIT 1""",
            (account["user_id"], deployment_mode, account["id"]),
        )
    if not deployments or decision.signal_time is None:
        return False
    deployment = deployments[0]
    timeframe = str(spec["timeframes"]["trigger"])
    revision_fingerprint = str(
        (snapshot or {}).get("source_hash")
        or (snapshot or {}).get("spec_hash")
        or f"strategy-revision:{deployment['strategy_revision_id']}"
    )
    signal_seconds = int(decision.signal_time)
    if signal_seconds >= 100_000_000_000:
        signal_seconds //= 1_000
    if (
        envelope is None
        or envelope.revision_fingerprint != revision_fingerprint
        or envelope.symbol != symbol.strip().upper()
        or envelope.timeframe != timeframe
        or envelope.decision.value != str(decision.decision)
        or int(envelope.event_time.timestamp()) != signal_seconds
    ):
        return False
    stable_decision_id = envelope.decision_id
    idempotency_key = decision_record_key(
        deployment_mode,
        deployment["id"],
        stable_decision_id,
    )
    opportunity_direction = {
        "LONG_ENTRY": "long",
        "SHORT_ENTRY": "short",
    }.get(decision.decision)
    opportunity_id = None
    if opportunity_direction:
        opportunities = query(
            """SELECT id FROM market_opportunities
               WHERE symbol=? AND direction=?
                 AND status IN ('detected','watching','confirmed')
                 AND detected_bar_time<=? AND expires_bar_time>=?
               ORDER BY quality_score DESC,detected_bar_time DESC,id DESC LIMIT 1""",
            (
                symbol,
                opportunity_direction,
                decision.signal_time,
                decision.signal_time,
            ),
        )
        if opportunities:
            opportunity_id = opportunities[0]["id"]
    valid_until = decision.valid_until
    if valid_until is not None and int(valid_until) >= 100_000_000_000:
        valid_until = int(valid_until) // 1_000
    try:
        execute(
            """INSERT IGNORE INTO strategy_signals(
                   public_id,user_id,deployment_id,strategy_revision_id,opportunity_id,
                   symbol,timeframe,signal_bar_time,decision,confidence,status,valid_until,
                   reason_codes_json,evidence_json,risk_decision_json,idempotency_key,created_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?,'approved',FROM_UNIXTIME(?),?,?,?,?,CURRENT_TIMESTAMP)""",
            (
                str(uuid.uuid4()),
                account["user_id"],
                deployment["id"],
                deployment["strategy_revision_id"],
                opportunity_id,
                symbol,
                timeframe,
                decision.signal_time,
                decision.decision,
                decision.confidence,
                valid_until,
                json.dumps(list(decision.reason_codes), ensure_ascii=False),
                json.dumps(
                    {
                        **decision.evidence,
                        "decision_id": stable_decision_id,
                        "decision_envelope": envelope.snapshot(),
                    },
                    ensure_ascii=False,
                ),
                json.dumps(decision.risk_proposal, ensure_ascii=False),
                idempotency_key,
            ),
        )
        execute(
            """UPDATE strategy_deployments
               SET last_evaluated_bar_time=?,last_error_code=NULL,updated_at=CURRENT_TIMESTAMP
               WHERE id=? AND user_id=?""",
            (decision.signal_time, deployment["id"], account["user_id"]),
        )
    except Exception as exc:
        prefix = log_mode or deployment_mode
        print(f"[{prefix}] strategy signal persistence failed: {type(exc).__name__}")
        return False
    return True


def build_entry_basis_snapshot(
    account: dict[str, Any],
    *,
    mode: str,
    symbol: str,
    direction: int,
    signal_time: int | None,
    reasons: list[str],
    evidence: dict[str, Any] | None,
    entry_price: float,
    atr: float | None,
    stop: float | None,
    target: float | None,
    leverage: int,
    margin: float | None,
    query: QueryRows,
    clock: Callable[[], float] = time.time,
    default_max_holding_bars: int = DEFAULT_MAX_HOLDING_BARS,
) -> tuple[dict[str, Any], int | None]:
    """Build one immutable, self-contained entry audit snapshot."""

    strategies = strategy_snapshots(account)
    strategy = dict(strategies[0]) if strategies else {}
    signal_evidence = dict(evidence or {})
    strategy_signal_id = None
    strategy_revision_id = account.get("strategy_revision_id")
    deployment_id = account.get("deployment_id")
    if deployment_id is not None and signal_time is not None:
        try:
            rows = query(
                """SELECT id,strategy_revision_id,decision,confidence,reason_codes_json,
                          evidence_json,risk_decision_json
                   FROM strategy_signals
                   WHERE user_id=? AND deployment_id=? AND symbol=? AND signal_bar_time=?
                     AND decision IN ('LONG_ENTRY','SHORT_ENTRY')
                   ORDER BY id DESC LIMIT 1""",
                (account["user_id"], deployment_id, symbol, signal_time),
            )
        except Exception as exc:
            print(f"[{mode}] strategy signal lookup failed: {type(exc).__name__}")
            rows = []
        if rows:
            signal = dict(rows[0])
            strategy_signal_id = int(signal["id"])
            strategy_revision_id = signal.get("strategy_revision_id")
            persisted_evidence = _json_object(signal.get("evidence_json"))
            if persisted_evidence:
                signal_evidence = persisted_evidence
            persisted_reasons = signal.get("reason_codes_json")
            if isinstance(persisted_reasons, str):
                try:
                    persisted_reasons = json.loads(persisted_reasons)
                except (TypeError, ValueError, json.JSONDecodeError):
                    persisted_reasons = None
            if isinstance(persisted_reasons, list) and persisted_reasons:
                reasons = [
                    *reasons,
                    f"信号代码：{' / '.join(map(str, persisted_reasons))}",
                ]
            signal_evidence = {
                **signal_evidence,
                "decision": signal.get("decision"),
                "confidence": (
                    float(signal["confidence"])
                    if signal.get("confidence") is not None
                    else signal_evidence.get("confidence")
                ),
                "risk_decision": _json_object(signal.get("risk_decision_json")),
            }

    score = signal_evidence.get("score")
    timing_policy = resolve_strategy_timing_policy(
        strategy,
        account.get("config_json") if isinstance(account.get("config_json"), Mapping) else None,
        evidence=signal_evidence,
        default_max_holding_bars=default_max_holding_bars,
    )
    risk_proposal = signal_evidence.get("risk_proposal")
    try:
        valid_atr = atr is not None and math.isfinite(float(atr)) and float(atr) > 0
    except (TypeError, ValueError, OverflowError):
        valid_atr = False
    exit_source = (
        "strategy_risk_proposal"
        if isinstance(risk_proposal, dict)
        else "atr"
        if valid_atr
        else "configured_percentage"
    )
    exit_policy = ExitLevelPlan(
        entry_price=float(entry_price),
        direction=direction,
        stop=float(stop),
        target=float(target),
        source=exit_source,
    ).snapshot()
    snapshot = {
        "schema_version": ENTRY_BASIS_SCHEMA_VERSION,
        "availability": "captured",
        "mode": mode,
        "captured_at": int(clock()),
        "symbol": symbol,
        "direction": "long" if direction > 0 else "short",
        "reasons": list(dict.fromkeys(str(item) for item in reasons if item)),
        "strategy": _strategy_identity(strategy),
        "strategies": [_strategy_identity(item) for item in strategies],
        "combination_mode": "all",
        "execution_policy": timing_policy.snapshot(),
        "exit_policy": exit_policy,
        "signal": {
            "strategy_signal_id": strategy_signal_id,
            "deployment_id": deployment_id,
            "strategy_revision_id": strategy_revision_id,
            "bar_time": signal_time,
            "score": score,
            "evidence": signal_evidence,
        },
        "execution": {
            "entry_price": entry_price,
            "atr": atr,
            "stop": stop,
            "target": target,
            "leverage": leverage,
            "margin": margin,
        },
    }
    return snapshot, strategy_signal_id


def _strategy_identity(strategy: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "public_id": strategy.get("public_id"),
        "name": strategy.get("name"),
        "kind": strategy.get("strategy_kind"),
        "engine_key": strategy.get("engine_key"),
        "version": strategy.get("version"),
        "spec_schema_version": strategy.get("spec_schema_version"),
        "spec_hash": strategy.get("spec_hash"),
        "parameters": strategy.get("parameters"),
    }


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError, json.JSONDecodeError):
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}
