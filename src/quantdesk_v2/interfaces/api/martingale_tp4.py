"""Configuration and Tiger-data control plane for Martingale TP4.

These endpoints expose validation and data preparation only.  They do not arm
or execute a live basket strategy.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from ...database import get_db
from ...dependencies import get_current_user, require_admin_write
from ...domain.martingale_tp4 import (
    MartingaleTp4Config,
    preview_configuration_risk,
)
from ...models import ReferenceMarketDataQuality, Security, User
from ...tiger_market_data import (
    TIGER_SOURCE,
    TigerBarBackfillService,
    TigerBarClient,
    TigerMarketDataError,
    TigerMarketDataRepository,
    build_tiger_quote_api,
    ensure_tiger_security_mapping,
    resolve_verified_market_link,
)
from .common import add_audit_log

router = APIRouter(
    prefix="/api/v2/basket-strategies/martingale-tp4",
    tags=["basket-strategies"],
)


class _StrictRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class MartingaleRiskPreviewRequest(_StrictRequest):
    config: MartingaleTp4Config
    account_equity: Decimal = Field(gt=0)
    reference_price: Decimal = Field(gt=0)
    leverage: Decimal = Field(gt=0, le=125)


class TigerBackfillRequest(_StrictRequest):
    underlying_symbol: str = Field(min_length=1, max_length=32)
    timeframe: str
    trade_session: str = "regular"
    adjustment: str = "none"
    begin_at: datetime
    end_at: datetime
    expected_bars: int = Field(ge=1, le=100_000)
    maximum_age_seconds: int = Field(ge=0, le=31_536_000)
    total: int = Field(default=10_000, ge=1, le=100_000)

    @model_validator(mode="after")
    def validate_period(self) -> TigerBackfillRequest:
        begin_at = (
            self.begin_at.replace(tzinfo=UTC)
            if self.begin_at.tzinfo is None
            else self.begin_at.astimezone(UTC)
        )
        end_at = (
            self.end_at.replace(tzinfo=UTC)
            if self.end_at.tzinfo is None
            else self.end_at.astimezone(UTC)
        )
        if end_at <= begin_at:
            raise ValueError("end_at must be after begin_at")
        if (end_at - begin_at).days > 3_700:
            raise ValueError("one backfill request cannot exceed 3700 days")
        self.begin_at = begin_at
        self.end_at = end_at
        self.underlying_symbol = self.underlying_symbol.strip().upper()
        return self


def _quality_out(row: ReferenceMarketDataQuality | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {
        "source": row.source,
        "symbol": row.symbol,
        "timeframe": row.timeframe,
        "trade_session": row.trade_session,
        "adjustment": row.adjustment,
        "expected_bars": row.expected_bars,
        "actual_bars": row.actual_bars,
        "gap_count": row.gap_count,
        "duplicate_count": row.duplicate_count,
        "invalid_ohlc_count": row.invalid_ohlc_count,
        "newest_closed_time": row.newest_closed_time,
        "age_seconds": row.age_seconds,
        "completeness_ratio": str(row.completeness_ratio),
        "status": row.status,
        "reason_codes": list(row.reason_codes_json or []),
        "evaluated_at": row.evaluated_at,
    }


@router.post("/validate")
def validate_martingale_tp4(
    payload: MartingaleRiskPreviewRequest,
    db: Annotated[Session, Depends(get_db)],
    _user: Annotated[User, Depends(get_current_user)],
) -> dict[str, Any]:
    config = payload.config
    preview = preview_configuration_risk(
        config,
        account_equity=payload.account_equity,
        reference_price=payload.reference_price,
        leverage=payload.leverage,
    )
    market_data = config.market_data
    link = resolve_verified_market_link(
        db,
        underlying_symbol=market_data.underlying_symbol,
        contract_symbol=market_data.contract_symbol,
    )
    required_streams = []
    for timeframe in dict.fromkeys((config.parameters.box.timeframe, "1d")):
        row = db.scalar(
            select(ReferenceMarketDataQuality).where(
                ReferenceMarketDataQuality.source == TIGER_SOURCE,
                ReferenceMarketDataQuality.symbol == market_data.underlying_symbol,
                ReferenceMarketDataQuality.timeframe == timeframe,
                ReferenceMarketDataQuality.trade_session == market_data.trade_sessions[0],
                ReferenceMarketDataQuality.adjustment == market_data.adjustment,
            )
        )
        required_streams.append(_quality_out(row))
    blockers = []
    if link is None:
        blockers.append("market_mapping_unverified")
    if any(item is None for item in required_streams):
        blockers.append("tiger_bar_quality_missing")
    elif any(item["status"] != "usable" for item in required_streams if item):
        blockers.append("tiger_bar_quality_blocked")
    blockers.extend(
        warning
        for warning in preview.warnings
        if warning
        in {
            "configured_cycle_margin_limit_exceeded",
            "configured_cycle_notional_limit_exceeded",
        }
    )
    return {
        "valid": True,
        "config": config.model_dump(mode="json"),
        "risk_preview": preview.model_dump(mode="json"),
        "market_mapping": (
            {
                "verified": True,
                "security_id": link.security_id,
                "underlying_symbol": link.underlying_symbol,
                "contract_symbol": link.contract_symbol,
            }
            if link is not None
            else {"verified": False}
        ),
        "required_market_data": required_streams,
        "research_ready": not blockers,
        "live_ready": False,
        "blocking_reasons": list(dict.fromkeys(blockers)),
        "note": "validation does not arm or execute live trading",
    }


@router.post("/tiger-bars/backfill", status_code=status.HTTP_201_CREATED)
def backfill_tiger_bars(
    payload: TigerBackfillRequest,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    admin: Annotated[User, Depends(require_admin_write)],
) -> dict[str, Any]:
    security = db.scalar(
        select(Security).where(
            Security.exchange == "US",
            Security.symbol == payload.underlying_symbol,
            Security.is_active.is_(True),
        )
    )
    if security is None:
        raise HTTPException(status_code=404, detail="security master symbol not found")
    settings = request.app.state.settings
    private_key_path = settings.tiger_openapi_private_key_path
    if (
        not settings.tiger_openapi_tiger_id.strip()
        or not settings.tiger_openapi_account.strip()
        or private_key_path is None
    ):
        raise HTTPException(status_code=503, detail="Tiger Open API is not configured")
    try:
        quote_api = build_tiger_quote_api(
            tiger_id=settings.tiger_openapi_tiger_id,
            account=settings.tiger_openapi_account,
            private_key_path=private_key_path,
            sandbox=settings.tiger_openapi_sandbox,
        )
        service = TigerBarBackfillService(TigerBarClient(quote_api), TigerMarketDataRepository(db))
        mapping = ensure_tiger_security_mapping(db, security)
        result = service.backfill(
            security_id=int(security.id),
            symbol=payload.underlying_symbol,
            timeframe=payload.timeframe,
            begin_at=payload.begin_at,
            end_at=payload.end_at,
            trade_session=payload.trade_session,
            adjustment=payload.adjustment,
            expected_bars=payload.expected_bars,
            maximum_age_seconds=payload.maximum_age_seconds,
            total=payload.total,
        )
    except TigerMarketDataError as exc:
        db.rollback()
        raise HTTPException(
            status_code=503, detail=f"Tiger Open API unavailable: {exc.category}"
        ) from None
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from None
    add_audit_log(
        db,
        request,
        "martingale_tp4.tiger_bars_backfill",
        admin.id,
        resource_type="security",
        resource_id=str(security.id),
        metadata={
            "symbol": result.symbol,
            "timeframe": result.timeframe,
            "trade_session": result.trade_session,
            "adjustment": result.adjustment,
            "fetched_bars": result.fetched_bars,
            "quality_status": result.quality.status,
        },
    )
    db.commit()
    return {
        "mapping_id": mapping.id,
        "fetched_bars": result.fetched_bars,
        "stored_rows": result.stored_rows,
        "quality": result.quality.audit_payload(),
        "live_trading_changed": False,
    }
