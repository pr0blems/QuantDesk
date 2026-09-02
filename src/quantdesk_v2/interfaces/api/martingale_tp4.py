"""Configuration and Tiger-data control plane for Martingale TP4.

These endpoints expose validation and data preparation only.  They do not arm
or execute a live basket strategy.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from ...application.martingale_tp4.replay import (
    ReplayCosts,
    ReplayDataError,
    assess_replay_coverage,
    run_bar_replay,
)
from ...database import get_db
from ...dependencies import get_current_user, require_admin_write
from ...domain.martingale_tp4 import (
    LiveRiskConfig,
    MarketDataConfig,
    MartingaleTp4Config,
    Mq4Inputs,
    dump_mq4_settings_csv,
    mq4_inputs_from_strategy_parameters,
    parse_mq4_settings_csv,
    preview_configuration_risk,
    strategy_parameters_from_mq4,
)
from ...domain.martingale_tp4_engine import EnginePolicy
from ...models import (
    ReferenceMarketDataQuality,
    Security,
    StrategyMarketDataManifest,
    User,
)
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


class Mq4ImportRequest(_StrictRequest):
    inputs: Mq4Inputs
    market_data: MarketDataConfig
    live_risk: LiveRiskConfig


class Mq4ExportRequest(_StrictRequest):
    config: MartingaleTp4Config


class Mq4CsvImportRequest(_StrictRequest):
    settings_csv: str = Field(min_length=1, max_length=16_384)
    defaults: Mq4Inputs | None = None
    market_data: MarketDataConfig
    live_risk: LiveRiskConfig


class MartingaleBarReplayRequest(_StrictRequest):
    config: MartingaleTp4Config
    begin_at: datetime
    end_at: datetime
    initial_capital: Decimal = Field(gt=0)
    point_size: Decimal = Field(gt=0)
    fee_bps: Decimal = Field(default=Decimal("5"), ge=0, le=1000)
    slippage_bps: Decimal = Field(default=Decimal("2"), ge=0, le=1000)
    synthetic_spread_points: Decimal = Field(default=Decimal("1"), ge=0, le=100_000)
    engine_policy: EnginePolicy = EnginePolicy.RESEARCH_COMPATIBILITY
    warmup_bars: int = Field(default=250, ge=30, le=1000)
    manual_entry_direction: str | None = None
    manual_entry_time: datetime | None = None

    @model_validator(mode="after")
    def validate_replay_period(self) -> MartingaleBarReplayRequest:
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
        if end_at - begin_at > timedelta(days=366):
            raise ValueError("one bar replay cannot exceed 366 days")
        direction = (
            self.manual_entry_direction.strip().lower()
            if self.manual_entry_direction is not None
            else None
        )
        if direction not in {None, "buy", "sell"}:
            raise ValueError("manual_entry_direction must be buy or sell")
        manual_time = self.manual_entry_time
        if manual_time is not None:
            manual_time = (
                manual_time.replace(tzinfo=UTC)
                if manual_time.tzinfo is None
                else manual_time.astimezone(UTC)
            )
        if self.config.parameters.mode in {"recovery", "grid"}:
            if direction is None or manual_time is None:
                raise ValueError("recovery/grid replay requires manual entry direction and time")
            if not begin_at <= manual_time < end_at:
                raise ValueError("manual_entry_time must fall inside the replay period")
        self.begin_at = begin_at
        self.end_at = end_at
        self.manual_entry_direction = direction
        self.manual_entry_time = manual_time
        return self


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
    timeframes = [config.parameters.box.timeframe]
    if config.parameters.box.auto_range:
        timeframes.append("1d")
    for timeframe in dict.fromkeys(timeframes):
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


@router.post("/mq4/import")
def import_mq4_parameters(
    payload: Mq4ImportRequest,
    _user: Annotated[User, Depends(get_current_user)],
) -> dict[str, Any]:
    config = MartingaleTp4Config(
        market_data=payload.market_data,
        parameters=strategy_parameters_from_mq4(payload.inputs),
        live_risk=payload.live_risk,
    )
    return {
        "config": config.model_dump(mode="json"),
        "source_format": "mq4_inputs",
        "live_ready": False,
    }


@router.post("/mq4/csv/import")
def import_mq4_settings_csv(
    payload: Mq4CsvImportRequest,
    _user: Annotated[User, Depends(get_current_user)],
) -> dict[str, Any]:
    try:
        inputs = parse_mq4_settings_csv(payload.settings_csv, defaults=payload.defaults)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    config = MartingaleTp4Config(
        market_data=payload.market_data,
        parameters=strategy_parameters_from_mq4(inputs),
        live_risk=payload.live_risk,
    )
    return {
        "inputs": inputs.model_dump(by_alias=True, mode="json"),
        "config": config.model_dump(mode="json"),
        "source_format": "mq4_legacy_settings_csv",
        "fields_restored_from_defaults": [
            "MaxSpred",
            "Kol_Ord_for_TP2",
            "TP2",
            "Kol_Ord_for_TP3",
            "TP3",
            "Kol_Ord_for_TP4",
            "TP4",
            "ShowStat",
            "ShowButton",
            "ShowMainSetting",
            "Section",
            "BoxLength",
            "BoxTimeFrame",
            "BoxRange",
            "AutoBoxRange",
            "AutoBoxRangeDailyATRperiod",
            "AutoBoxRangeDailyATRfactor",
            "BoxBufferPips",
        ],
        "live_ready": False,
    }


@router.post("/mq4/export")
def export_mq4_parameters(
    payload: Mq4ExportRequest,
    _user: Annotated[User, Depends(get_current_user)],
) -> dict[str, Any]:
    inputs = mq4_inputs_from_strategy_parameters(payload.config.parameters)
    return {
        "inputs": inputs.model_dump(by_alias=True, mode="json"),
        "legacy_settings_csv": dump_mq4_settings_csv(inputs),
        "warning": (
            "The original EA settings CSV persists only 20 fields; use the JSON inputs "
            "object for a lossless configuration export."
        ),
    }


def _timeframe_milliseconds(timeframe: str) -> int:
    values = {
        "1m": 60_000,
        "5m": 300_000,
        "15m": 900_000,
        "30m": 1_800_000,
        "1h": 3_600_000,
    }
    try:
        return values[timeframe]
    except KeyError as exc:
        raise HTTPException(status_code=422, detail="unsupported replay timeframe") from exc


def _required_quality(
    db: Session,
    *,
    symbol: str,
    timeframe: str,
    trade_session: str,
    adjustment: str,
    allow_historical_stale: bool = False,
) -> ReferenceMarketDataQuality:
    row = db.scalar(
        select(ReferenceMarketDataQuality).where(
            ReferenceMarketDataQuality.source == TIGER_SOURCE,
            ReferenceMarketDataQuality.symbol == symbol,
            ReferenceMarketDataQuality.timeframe == timeframe,
            ReferenceMarketDataQuality.trade_session == trade_session,
            ReferenceMarketDataQuality.adjustment == adjustment,
        )
    )
    if row is None:
        raise HTTPException(
            status_code=409,
            detail=f"Tiger {timeframe} data quality has not been evaluated",
        )
    reason_codes = set(row.reason_codes_json or [])
    stale_only = bool(reason_codes) and reason_codes <= {"newest_bar_stale"}
    if row.status != "usable" and not (allow_historical_stale and stale_only):
        reasons = ",".join(sorted(reason_codes)) or "quality_blocked"
        raise HTTPException(
            status_code=409,
            detail=f"Tiger {timeframe} data quality is blocked: {reasons}",
        )
    return row


@router.post("/backtests")
def run_martingale_bar_backtest(
    payload: MartingaleBarReplayRequest,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
) -> dict[str, Any]:
    config = payload.config
    market_data = config.market_data
    timeframe = config.parameters.box.timeframe
    trade_session = market_data.trade_sessions[0]
    link = resolve_verified_market_link(
        db,
        underlying_symbol=market_data.underlying_symbol,
        contract_symbol=market_data.contract_symbol,
    )
    if link is None:
        raise HTTPException(status_code=409, detail="Tiger/Binance market mapping is not verified")
    signal_quality = _required_quality(
        db,
        symbol=market_data.underlying_symbol,
        timeframe=timeframe,
        trade_session=trade_session,
        adjustment=market_data.adjustment,
        allow_historical_stale=True,
    )
    daily_quality = (
        _required_quality(
            db,
            symbol=market_data.underlying_symbol,
            timeframe="1d",
            trade_session=trade_session,
            adjustment=market_data.adjustment,
            allow_historical_stale=True,
        )
        if config.parameters.box.auto_range
        else None
    )
    begin_ms = int(payload.begin_at.timestamp() * 1000)
    end_ms = int(payload.end_at.timestamp() * 1000)
    timeframe_ms = _timeframe_milliseconds(timeframe)
    signal_lookback = max(
        timedelta(days=14),
        timedelta(milliseconds=timeframe_ms * payload.warmup_bars * 6),
    )
    warmup_begin_ms = int((payload.begin_at - signal_lookback).timestamp() * 1000)
    daily_warmup_days = max(60, config.parameters.box.daily_atr_period * 3)
    daily_begin_ms = int(
        (payload.begin_at - timedelta(days=daily_warmup_days)).timestamp() * 1000
    )
    repository = TigerMarketDataRepository(db)
    signal_bars = repository.load_bars(
        symbol=market_data.underlying_symbol,
        timeframe=timeframe,
        trade_session=trade_session,
        adjustment=market_data.adjustment,
        begin_time=warmup_begin_ms,
        end_time=end_ms,
    )
    daily_bars = (
        repository.load_bars(
            symbol=market_data.underlying_symbol,
            timeframe="1d",
            trade_session=trade_session,
            adjustment=market_data.adjustment,
            begin_time=daily_begin_ms,
            end_time=end_ms,
        )
        if config.parameters.box.auto_range
        else ()
    )
    if len(signal_bars) > 20_000:
        raise HTTPException(status_code=422, detail="replay exceeds the 20000 bar limit")
    try:
        coverage = assess_replay_coverage(
            signal_bars,
            daily_bars,
            evaluation_begin_time=begin_ms,
            evaluation_end_time=end_ms,
            required_signal_warmup_bars=payload.warmup_bars,
            required_daily_warmup_bars=(
                config.parameters.box.daily_atr_period
                if config.parameters.box.auto_range
                else 0
            ),
            timezone=config.parameters.session.timezone,
        )
        if coverage.status != "usable":
            raise ReplayDataError(
                "replay data coverage is blocked: " + ",".join(coverage.reason_codes)
            )
        result = run_bar_replay(
            config,
            signal_bars,
            daily_bars,
            initial_capital=payload.initial_capital,
            point_size=payload.point_size,
            costs=ReplayCosts(
                fee_bps=payload.fee_bps,
                slippage_bps=payload.slippage_bps,
                synthetic_spread_points=payload.synthetic_spread_points,
            ),
            engine_policy=payload.engine_policy,
            manual_entry_direction=payload.manual_entry_direction,
            manual_entry_time=(
                int(payload.manual_entry_time.timestamp() * 1000)
                if payload.manual_entry_time is not None
                else None
            ),
            evaluation_begin_time=begin_ms,
            evaluation_end_time=end_ms,
        )
    except (ReplayDataError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None

    quality_payload = {
        "signal": _quality_out(signal_quality),
        "daily": _quality_out(daily_quality),
        "requested_range": coverage.audit_payload(),
        "freshness_policy": "historical_range_allows_stale_closed_bars",
        "mapping_security_id": link.security_id,
        "warmup_begin_time": warmup_begin_ms,
        "evaluation_begin_time": begin_ms,
        "evaluation_end_time": end_ms,
    }
    manifest = db.scalar(
        select(StrategyMarketDataManifest).where(
            StrategyMarketDataManifest.content_sha256 == result.dataset_sha256
        )
    )
    if manifest is None:
        manifest = StrategyMarketDataManifest(
            signal_source=TIGER_SOURCE,
            execution_source="tiger_reference_proxy",
            underlying_symbol=market_data.underlying_symbol,
            contract_symbol=market_data.contract_symbol,
            timeframe=timeframe,
            trade_session=trade_session,
            adjustment=market_data.adjustment,
            begin_time=warmup_begin_ms,
            end_time=end_ms,
            row_count=len(signal_bars) + len(daily_bars),
            quality_json=quality_payload,
            storage_uri=(
                f"mysql://reference_market_bars/{market_data.underlying_symbol}/"
                f"{timeframe}/{result.dataset_sha256}"
            ),
            content_sha256=result.dataset_sha256,
        )
        db.add(manifest)
        db.flush()
    add_audit_log(
        db,
        request,
        "martingale_tp4.bar_backtest",
        user.id,
        resource_type="strategy_market_data_manifest",
        resource_id=manifest.public_id,
        metadata={
            "run_sha256": result.run_sha256,
            "engine_policy": result.engine_policy,
            "engine_version": result.engine_version,
            "box_algorithm_version": result.box_algorithm_version,
            "fill_model_version": result.fill_model_version,
            "config_snapshot": result.config_snapshot,
            "replay_costs": result.replay_costs,
            "underlying_symbol": market_data.underlying_symbol,
            "contract_symbol": market_data.contract_symbol,
            "cycle_count": result.metrics["cycle_count"],
            "live_trading_changed": False,
        },
    )
    db.commit()
    return {
        "manifest_id": manifest.public_id,
        "result": result.audit_payload(),
        "live_trading_changed": False,
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
