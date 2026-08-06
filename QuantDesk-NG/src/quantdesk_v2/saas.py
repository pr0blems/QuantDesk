"""Tenant plans, quotas, and the deliberately non-payment SaaS control plane.

The module contains no payment-provider implementation.  A plan can only be
changed by an administrator until a separately configured provider is added.
This makes an accidentally enabled billing button impossible in self-hosted
deployments.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import JSON, BigInteger, Date, DateTime, ForeignKey, Integer, String
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Mapped, Session, mapped_column

from .database import get_db
from .dependencies import get_current_user, require_admin_write
from .models import Base, User, utcnow

router = APIRouter(prefix="/api/v1", tags=["public-api-v1"])

PLAN_CATALOG: dict[str, dict[str, Any]] = {
    "free": {
        "label": "Free",
        "features": {"market_monitor", "paper_trading", "backtest", "prediction"},
        "limits": {"backtest_runs_day": 2, "paper_accounts": 3, "watchlist_symbols": 20},
    },
    "pro": {
        "label": "Pro",
        "features": {"market_monitor", "paper_trading", "backtest", "prediction", "ai_analysis"},
        "limits": {"backtest_runs_day": 25, "paper_accounts": 20, "watchlist_symbols": 100},
    },
    "enterprise": {
        "label": "Enterprise",
        "features": {
            "market_monitor",
            "paper_trading",
            "backtest",
            "prediction",
            "ai_analysis",
            "team_workspace",
        },
        "limits": {"backtest_runs_day": 200, "paper_accounts": 100, "watchlist_symbols": 250},
    },
}


class SaasEntitlement(Base):
    __tablename__ = "saas_entitlements"

    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    plan_code: Mapped[str] = mapped_column(String(32), nullable=False, default="free")
    overrides_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    starts_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
    ends_at: Mapped[datetime | None] = mapped_column(DateTime)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utcnow, onupdate=utcnow
    )


class SaasUsageCounter(Base):
    __tablename__ = "saas_usage_counters"

    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    metric: Mapped[str] = mapped_column(String(64), primary_key=True)
    period_start: Mapped[date] = mapped_column(Date, primary_key=True)
    amount: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utcnow, onupdate=utcnow
    )


def _today() -> date:
    return datetime.now(UTC).date()


def _normalized_plan(plan_code: str) -> str:
    plan_code = plan_code.strip().lower()
    if plan_code not in PLAN_CATALOG:
        raise HTTPException(status_code=422, detail="unsupported plan code")
    return plan_code


def ensure_entitlement(db: Session, user_id: int) -> SaasEntitlement:
    entitlement = db.get(SaasEntitlement, user_id)
    if entitlement is None:
        entitlement = SaasEntitlement(user_id=user_id, plan_code="free", overrides_json={})
        db.add(entitlement)
        db.flush()
    return entitlement


def entitlement_snapshot(db: Session, user_id: int) -> dict[str, Any]:
    entitlement = ensure_entitlement(db, user_id)
    plan_code = _normalized_plan(entitlement.plan_code)
    base = PLAN_CATALOG[plan_code]
    overrides = entitlement.overrides_json or {}
    features = set(base["features"])
    features.update(
        str(item) for item in overrides.get("grant_features", []) if isinstance(item, str)
    )
    features.difference_update(
        str(item) for item in overrides.get("revoke_features", []) if isinstance(item, str)
    )
    limits = dict(base["limits"])
    for key, value in (overrides.get("limits") or {}).items():
        if (
            isinstance(key, str)
            and isinstance(value, int)
            and not isinstance(value, bool)
            and value >= 0
        ):
            limits[key] = value
    active = entitlement.ends_at is None or entitlement.ends_at >= utcnow()
    return {
        "plan_code": plan_code,
        "plan_label": base["label"],
        "active": active,
        "features": sorted(features) if active else [],
        "limits": limits if active else {},
        "starts_at": entitlement.starts_at,
        "ends_at": entitlement.ends_at,
    }


def require_feature(db: Session, user: User, feature: str) -> None:
    snapshot = entitlement_snapshot(db, user.id)
    if feature not in snapshot["features"]:
        raise HTTPException(status_code=403, detail=f"plan does not include feature: {feature}")


def consume_daily_quota(db: Session, user: User, metric: str, units: int = 1) -> None:
    if units < 1:
        raise ValueError("units must be positive")
    # Serialize quota increments by tenant, including the first counter for a
    # new metric, rather than relying on a race-prone missing-row lock.
    if db.get(SaasEntitlement, user.id, with_for_update=True) is None:
        ensure_entitlement(db, user.id)
    snapshot = entitlement_snapshot(db, user.id)
    limit = snapshot["limits"].get(metric)
    if limit is None:
        return
    period_start = _today()
    counter = db.get(SaasUsageCounter, (user.id, metric, period_start), with_for_update=True)
    current = counter.amount if counter else 0
    if current + units > limit:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"{metric} quota exhausted for the current day",
            headers={"Retry-After": "86400"},
        )
    if counter is None:
        counter = SaasUsageCounter(
            user_id=user.id, metric=metric, period_start=period_start, amount=units
        )
        db.add(counter)
    else:
        counter.amount += units


class EntitlementOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    plan_code: str
    plan_label: str
    active: bool
    features: list[str]
    limits: dict[str, int]
    starts_at: datetime
    ends_at: datetime | None
    payment_available: bool = False


class EntitlementUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    plan_code: str = Field(pattern=r"^(free|pro|enterprise)$")
    ends_at: datetime | None = None
    overrides: dict[str, Any] = Field(default_factory=dict)

    @field_validator("overrides")
    @classmethod
    def safe_overrides(cls, value: dict[str, Any]) -> dict[str, Any]:
        allowed = {"grant_features", "revoke_features", "limits"}
        if set(value) - allowed:
            raise ValueError("unsupported entitlement override")
        return value


@router.get("/entitlements/me", response_model=EntitlementOut)
def get_my_entitlements(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Stable v1 external API: return the caller's effective read-only plan."""

    return EntitlementOut(**entitlement_snapshot(db, user.id))


@router.get("/plans")
def list_plans() -> dict[str, Any]:
    """Public plan metadata. Payment checkout is intentionally unavailable."""

    return {
        "plans": [
            {
                "code": code,
                "label": plan["label"],
                "features": sorted(plan["features"]),
                "limits": plan["limits"],
            }
            for code, plan in PLAN_CATALOG.items()
        ],
        "payment_available": False,
        "payment_reason": "No payment provider is configured for this deployment.",
    }


@router.put("/admin/entitlements/{user_id}", response_model=EntitlementOut)
def update_entitlement(
    user_id: int,
    payload: EntitlementUpdate,
    request: Request,
    db: Session = Depends(get_db),
    _: User = Depends(require_admin_write),
):
    """Admin-only manual entitlement control while payments remain feature-gated."""

    if db.get(User, user_id) is None:
        raise HTTPException(status_code=404, detail="user not found")
    entitlement = ensure_entitlement(db, user_id)
    entitlement.plan_code = _normalized_plan(payload.plan_code)
    entitlement.ends_at = payload.ends_at
    entitlement.overrides_json = payload.overrides
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="entitlement update conflict") from None
    return EntitlementOut(**entitlement_snapshot(db, user_id))
