"""Deterministic policy gate for validated AI trade proposals."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from ..domain.ai_decision import (
    ProposalDisposition,
    ProposalGateResult,
    ReleaseStage,
    StrategyVersionStatus,
    TradeProposal,
    canonical_sha256,
)

_SYMBOL_PATTERN = re.compile(r"[A-Z0-9][A-Z0-9._-]{1,31}\Z")
_VERSION_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_AUDIT_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@/-]{0,127}\Z")


@dataclass(frozen=True, slots=True)
class ProposalGatePolicy:
    policy_version: str = "proposal-gate-v1"
    minimum_confidence: Decimal = Decimal("0.65")
    maximum_age: timedelta = timedelta(minutes=5)
    maximum_future_skew: timedelta = timedelta(seconds=15)
    maximum_validity_window: timedelta = timedelta(minutes=15)
    maximum_risk_fraction: Decimal = Decimal("0.01")
    canary_maximum_risk_fraction: Decimal = Decimal("0.0025")
    maximum_slippage_bps: int = 50
    maximum_leverage: int = 5

    def __post_init__(self) -> None:
        if not isinstance(self.policy_version, str) or not _VERSION_PATTERN.fullmatch(
            self.policy_version
        ):
            raise ValueError("invalid proposal gate policy version")
        for name in (
            "minimum_confidence",
            "maximum_risk_fraction",
            "canary_maximum_risk_fraction",
        ):
            value = getattr(self, name)
            if not isinstance(value, Decimal) or not value.is_finite():
                raise ValueError(f"{name} must be a finite Decimal")
        if not Decimal("0") <= self.minimum_confidence <= Decimal("1"):
            raise ValueError("minimum_confidence must be between zero and one")
        if not Decimal("0") < self.maximum_risk_fraction <= Decimal("1"):
            raise ValueError("maximum_risk_fraction must be between zero and one")
        if not Decimal("0") < self.canary_maximum_risk_fraction <= self.maximum_risk_fraction:
            raise ValueError("canary risk cap must not exceed the regular risk cap")
        for name in ("maximum_age", "maximum_future_skew", "maximum_validity_window"):
            if getattr(self, name) <= timedelta(0):
                raise ValueError(f"{name} must be positive")
        if isinstance(self.maximum_slippage_bps, bool) or self.maximum_slippage_bps < 0:
            raise ValueError("maximum_slippage_bps must be non-negative")
        if isinstance(self.maximum_leverage, bool) or self.maximum_leverage < 1:
            raise ValueError("maximum_leverage must be positive")

    def audit_snapshot(self) -> dict[str, object]:
        return {
            "policy_version": self.policy_version,
            "minimum_confidence": str(self.minimum_confidence),
            "maximum_age_seconds": str(self.maximum_age.total_seconds()),
            "maximum_future_skew_seconds": str(
                self.maximum_future_skew.total_seconds()
            ),
            "maximum_validity_window_seconds": str(
                self.maximum_validity_window.total_seconds()
            ),
            "maximum_risk_fraction": str(self.maximum_risk_fraction),
            "canary_maximum_risk_fraction": str(
                self.canary_maximum_risk_fraction
            ),
            "maximum_slippage_bps": self.maximum_slippage_bps,
            "maximum_leverage": self.maximum_leverage,
        }


@dataclass(frozen=True, slots=True)
class ProposalGateContext:
    strategy_version_id: str
    strategy_version_status: StrategyVersionStatus
    symbol_universe: frozenset[str]
    stage: ReleaseStage
    now: datetime
    manual_approved: bool = False
    manual_approval_id: str | None = None
    manual_approved_by: str | None = None
    manual_approved_at: datetime | None = None

    def __post_init__(self) -> None:
        version_id = str(self.strategy_version_id).strip()
        if not _VERSION_PATTERN.fullmatch(version_id):
            raise ValueError("invalid strategy version id")
        object.__setattr__(self, "strategy_version_id", version_id)
        object.__setattr__(
            self,
            "strategy_version_status",
            StrategyVersionStatus(self.strategy_version_status),
        )
        object.__setattr__(self, "stage", ReleaseStage(self.stage))
        normalized_symbols = frozenset(str(item).strip().upper() for item in self.symbol_universe)
        if not normalized_symbols or len(normalized_symbols) > 500 or any(
            not _SYMBOL_PATTERN.fullmatch(item) for item in normalized_symbols
        ):
            raise ValueError("symbol_universe must contain 1-500 valid symbols")
        object.__setattr__(self, "symbol_universe", normalized_symbols)
        if self.now.tzinfo is None or self.now.utcoffset() is None:
            raise ValueError("gate time must be timezone-aware")
        if not isinstance(self.manual_approved, bool):
            raise ValueError("manual_approved must be boolean")
        approval_values = (
            self.manual_approval_id,
            self.manual_approved_by,
            self.manual_approved_at,
        )
        if self.manual_approved:
            if self.stage is not ReleaseStage.MANUAL:
                raise ValueError("manual approval evidence is only valid in manual stage")
            if any(value is None for value in approval_values):
                raise ValueError("manual approval requires complete audit evidence")
            if not isinstance(self.manual_approval_id, str) or not _AUDIT_ID_PATTERN.fullmatch(
                self.manual_approval_id
            ):
                raise ValueError("invalid manual approval id")
            if not isinstance(self.manual_approved_by, str) or not _AUDIT_ID_PATTERN.fullmatch(
                self.manual_approved_by
            ):
                raise ValueError("invalid manual approval actor")
            if (
                not isinstance(self.manual_approved_at, datetime)
                or self.manual_approved_at.tzinfo is None
                or self.manual_approved_at.utcoffset() is None
            ):
                raise ValueError("manual approval time must be timezone-aware")
            if self.manual_approved_at > self.now:
                raise ValueError("manual approval cannot be in the future")
        elif any(value is not None for value in approval_values):
            raise ValueError("manual approval evidence requires manual_approved=true")

    def audit_snapshot(self) -> dict[str, object]:
        approval = None
        if self.manual_approved:
            approval = {
                "approval_id": self.manual_approval_id,
                "approved_by": self.manual_approved_by,
                "approved_at": (
                    self.manual_approved_at.isoformat()
                    if self.manual_approved_at is not None
                    else None
                ),
            }
        return {
            "strategy_version_id": self.strategy_version_id,
            "strategy_version_status": self.strategy_version_status.value,
            "symbol_universe": sorted(self.symbol_universe),
            "stage": self.stage.value,
            "evaluated_at": self.now.isoformat(),
            "manual_approval": approval,
        }


class ProposalGate:
    """Apply release, strategy, freshness, universe, confidence, and risk policy.

    A successful result only means the proposal may be passed to a separate,
    deterministic risk engine. ``may_submit_order`` is always false by domain
    contract, including for canary and live release stages.
    """

    def __init__(self, policy: ProposalGatePolicy | None = None) -> None:
        self.policy = policy or ProposalGatePolicy()

    def evaluate(
        self,
        proposal: TradeProposal,
        context: ProposalGateContext,
    ) -> ProposalGateResult:
        reasons: list[str] = []
        if proposal.strategy_version_id != context.strategy_version_id:
            reasons.append("strategy_version_mismatch")
        if context.strategy_version_status is StrategyVersionStatus.RETIRED:
            reasons.append("strategy_version_retired")
        elif (
            context.stage is not ReleaseStage.REPLAY
            and context.strategy_version_status is not StrategyVersionStatus.PUBLISHED
        ):
            reasons.append("strategy_version_not_published")
        if proposal.symbol not in context.symbol_universe:
            reasons.append("symbol_outside_universe")

        age = context.now - proposal.observed_at
        if age > self.policy.maximum_age:
            reasons.append("market_context_stale")
        if age < -self.policy.maximum_future_skew:
            reasons.append("market_context_from_future")
        if proposal.valid_until <= context.now:
            reasons.append("proposal_expired")
        if proposal.valid_until - proposal.observed_at > self.policy.maximum_validity_window:
            reasons.append("validity_window_excessive")
        if proposal.confidence < self.policy.minimum_confidence:
            reasons.append("confidence_below_minimum")

        risk = proposal.requested_risk
        if risk is not None:
            risk_cap = self.policy.maximum_risk_fraction
            if context.stage is ReleaseStage.CANARY:
                risk_cap = self.policy.canary_maximum_risk_fraction
            if risk.risk_fraction > risk_cap:
                reasons.append("risk_fraction_exceeds_limit")
            if risk.max_slippage_bps > self.policy.maximum_slippage_bps:
                reasons.append("slippage_exceeds_limit")
            if risk.leverage > self.policy.maximum_leverage:
                reasons.append("leverage_exceeds_limit")

        proposal_hash = canonical_sha256(proposal)
        if reasons:
            return ProposalGateResult(
                disposition=ProposalDisposition.REJECTED,
                reason_codes=tuple(reasons),
                stage=context.stage,
                evaluated_at=context.now,
                proposal_hash=proposal_hash,
                requires_deterministic_risk_review=False,
                may_submit_order=False,
            )

        if context.stage is ReleaseStage.REPLAY:
            disposition = ProposalDisposition.RECORD_ONLY
            reason_code = "replay_recorded"
        elif context.stage is ReleaseStage.SHADOW:
            disposition = ProposalDisposition.RECORD_ONLY
            reason_code = "shadow_recorded"
        elif context.stage is ReleaseStage.MANUAL and not context.manual_approved:
            disposition = ProposalDisposition.MANUAL_REVIEW_REQUIRED
            reason_code = "manual_approval_required"
        else:
            disposition = ProposalDisposition.RISK_REVIEW_REQUIRED
            reason_code = "ready_for_risk_review"

        return ProposalGateResult(
            disposition=disposition,
            reason_codes=(reason_code,),
            stage=context.stage,
            evaluated_at=context.now,
            proposal_hash=proposal_hash,
            requires_deterministic_risk_review=(
                disposition is ProposalDisposition.RISK_REVIEW_REQUIRED
            ),
            may_submit_order=False,
        )
