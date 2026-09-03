"""Configuration and Tiger-data control plane for Martingale TP4.

These endpoints expose validation and data preparation only.  They do not arm
or execute a live basket strategy.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
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
from ...application.martingale_tp4.runtime import DEFAULT_SIGNAL_POINT_SIZE
from ...backtest import BacktestRepository, BacktestUnavailable
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
    strategy_parameters_from_catalog_parameters,
    strategy_parameters_from_mq4,
)
from ...domain.martingale_tp4_engine import EnginePolicy
from ...models import (
    ReferenceMarketDataQuality,
    Security,
    StrategyBasketCycle,
    StrategyBasketEvent,
    StrategyBasketLeg,
    StrategyDeployment,
    StrategyMarketDataManifest,
    User,
)
from ...tiger_market_data import (
    TIGER_SOURCE,
    TigerBar,
    TigerBarBackfillService,
    TigerBarClient,
    TigerMarketDataError,
    TigerMarketDataRepository,
    VerifiedMarketLink,
    build_tiger_quote_api,
    ensure_tiger_security_mapping,
    resolve_research_contract_market_link,
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
    leverage: int = Field(default=1, ge=1, le=20)
    maintenance_margin_rate: Decimal = Field(default=Decimal("0.005"), ge=0, lt=Decimal("0.5"))
    liquidation_fee_rate: Decimal = Field(default=Decimal("0"), ge=0, le=Decimal("0.1"))
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


def _cycle_out(row: StrategyBasketCycle) -> dict[str, Any]:
    return {
        "id": row.public_id,
        "deployment_id": row.deployment_id,
        "strategy_revision_id": row.strategy_revision_id,
        "underlying_symbol": row.underlying_symbol,
        "contract_symbol": row.contract_symbol,
        "mode": row.mode,
        "cycle_sequence": row.cycle_seq,
        "state": row.state,
        "box_high": str(row.box_high) if row.box_high is not None else None,
        "box_low": str(row.box_low) if row.box_low is not None else None,
        "box_time": row.box_time,
        "gross_quantity": str(row.gross_quantity),
        "net_quantity": str(row.net_quantity),
        "weighted_cost": str(row.weighted_cost) if row.weighted_cost is not None else None,
        "realized_pnl": str(row.realized_pnl),
        "unrealized_pnl": str(row.unrealized_pnl),
        "reserved_risk": str(row.reserved_risk),
        "max_risk": str(row.max_risk),
        "version": row.version,
        "opened_at": row.opened_at,
        "closed_at": row.closed_at,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


def _leg_out(row: StrategyBasketLeg) -> dict[str, Any]:
    return {
        "leg_index": row.leg_index,
        "action": row.action,
        "direction": row.direction,
        "position_side": row.position_side,
        "planned_quantity": str(row.planned_quantity),
        "filled_quantity": str(row.filled_quantity),
        "planned_price": str(row.planned_price) if row.planned_price is not None else None,
        "average_fill_price": (
            str(row.average_fill_price) if row.average_fill_price is not None else None
        ),
        "fee": str(row.fee),
        "funding": str(row.funding),
        "realized_pnl": str(row.realized_pnl),
        "state": row.state,
        "reason_code": row.reason_code,
        "created_at": row.created_at,
        "filled_at": row.filled_at,
    }


@router.get("/shadow/cycles")
def list_martingale_shadow_cycles(
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
    deployment_id: str | None = None,
    active_only: bool = False,
    limit: int = 50,
) -> dict[str, Any]:
    if limit < 1 or limit > 100:
        raise HTTPException(status_code=422, detail="limit must be between 1 and 100")
    statement = select(StrategyBasketCycle).where(
        StrategyBasketCycle.user_id == user.id
    )
    if deployment_id is not None:
        deployment = db.scalar(
            select(StrategyDeployment).where(
                StrategyDeployment.user_id == user.id,
                StrategyDeployment.public_id == deployment_id,
                StrategyDeployment.mode == "shadow",
            )
        )
        if deployment is None:
            raise HTTPException(status_code=404, detail="Shadow deployment not found")
        statement = statement.where(
            StrategyBasketCycle.deployment_id == deployment.id
        )
    if active_only:
        statement = statement.where(StrategyBasketCycle.active_key.is_not(None))
    rows = db.scalars(
        statement.order_by(StrategyBasketCycle.created_at.desc()).limit(limit)
    ).all()
    return {
        "items": [_cycle_out(row) for row in rows],
        "network_write": False,
        "live_ready": False,
    }


@router.get("/shadow/cycles/{cycle_id}")
def get_martingale_shadow_cycle(
    cycle_id: str,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
) -> dict[str, Any]:
    cycle = db.scalar(
        select(StrategyBasketCycle).where(
            StrategyBasketCycle.user_id == user.id,
            StrategyBasketCycle.public_id == cycle_id,
        )
    )
    if cycle is None:
        raise HTTPException(status_code=404, detail="Shadow basket cycle not found")
    legs = db.scalars(
        select(StrategyBasketLeg)
        .where(
            StrategyBasketLeg.user_id == user.id,
            StrategyBasketLeg.cycle_id == cycle.id,
        )
        .order_by(StrategyBasketLeg.created_at, StrategyBasketLeg.id)
    ).all()
    events = db.scalars(
        select(StrategyBasketEvent)
        .where(
            StrategyBasketEvent.user_id == user.id,
            StrategyBasketEvent.cycle_id == cycle.id,
        )
        .order_by(StrategyBasketEvent.sequence_no.desc())
        .limit(100)
    ).all()
    return {
        "cycle": _cycle_out(cycle),
        "legs": [_leg_out(row) for row in legs],
        "events": [
            {
                "id": row.public_id,
                "sequence": row.sequence_no,
                "event_type": row.event_type,
                "actor_type": row.actor_type,
                "reason_code": row.reason_code,
                "payload": row.payload_json,
                "occurred_at": row.occurred_at,
            }
            for row in events
        ],
        "network_write": False,
        "live_ready": False,
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
        "1d": 86_400_000,
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


def _execute_martingale_bar_backtest(
    payload: MartingaleBarReplayRequest,
    request: Request,
    db: Session,
    user: User,
    *,
    link: VerifiedMarketLink,
    signal_bars_override: Sequence[TigerBar] | None = None,
    daily_bars_override: Sequence[TigerBar] | None = None,
    source_name: str = TIGER_SOURCE,
    source_quality: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    config = payload.config
    market_data = config.market_data
    timeframe = config.parameters.box.timeframe
    trade_session = market_data.trade_sessions[0]
    if signal_bars_override is None:
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
    else:
        signal_quality = None
        daily_quality = None
    begin_ms = int(payload.begin_at.timestamp() * 1000)
    end_ms = int(payload.end_at.timestamp() * 1000)
    timeframe_ms = _timeframe_milliseconds(timeframe)
    signal_lookback = max(
        timedelta(days=30),
        timedelta(milliseconds=timeframe_ms * payload.warmup_bars * 10),
    )
    warmup_begin_ms = int((payload.begin_at - signal_lookback).timestamp() * 1000)
    daily_warmup_days = max(60, config.parameters.box.daily_atr_period * 3)
    daily_begin_ms = int(
        (payload.begin_at - timedelta(days=daily_warmup_days)).timestamp() * 1000
    )
    if signal_bars_override is None:
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
    else:
        signal_bars = tuple(signal_bars_override)
        daily_bars = tuple(daily_bars_override or ())
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
            leverage=payload.leverage,
            maintenance_margin_rate=payload.maintenance_margin_rate,
            liquidation_fee_rate=payload.liquidation_fee_rate,
        )
    except (ReplayDataError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None

    quality_payload = {
        "signal": (
            _quality_out(signal_quality)
            if signal_quality is not None
            else dict((source_quality or {}).get("signal") or {})
        ),
        "daily": (
            _quality_out(daily_quality)
            if daily_quality is not None
            else dict((source_quality or {}).get("daily") or {})
        ),
        "requested_range": coverage.audit_payload(),
        "freshness_policy": "historical_range_allows_stale_closed_bars",
        "source": source_name,
        "source_fallback_reason": (source_quality or {}).get("fallback_reason"),
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
            signal_source=source_name,
            execution_source=(
                "binance_contract_research"
                if source_name == "binance_fapi"
                else "tiger_reference_proxy"
            ),
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
                f"mysql://{'klines' if source_name == 'binance_fapi' else 'reference_market_bars'}/"
                f"{market_data.contract_symbol if source_name == 'binance_fapi' else market_data.underlying_symbol}/"
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
    chart_bars = [
        bar for bar in signal_bars if begin_ms <= int(bar.open_time) <= end_ms
    ]
    bucket_size = max(1, math.ceil(len(chart_bars) / 1_200)) if chart_bars else 1
    price_candles: list[dict[str, Any]] = []
    for index in range(0, len(chart_bars), bucket_size):
        bucket = chart_bars[index : index + bucket_size]
        price_candles.append(
            {
                "ts": int(bucket[0].open_time) // 1_000,
                "open": float(bucket[0].open),
                "high": float(max(item.high for item in bucket)),
                "low": float(min(item.low for item in bucket)),
                "close": float(bucket[-1].close),
                "volume": float(sum((item.volume for item in bucket), Decimal("0"))),
            }
        )
    return {
        "manifest_id": manifest.public_id,
        "result": result.audit_payload(),
        "price_candles": price_candles,
        "market_data_source": source_name,
        "market_data_quality": quality_payload,
        "live_trading_changed": False,
    }


@router.post("/backtests")
def run_martingale_bar_backtest(
    payload: MartingaleBarReplayRequest,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
) -> dict[str, Any]:
    market_data = payload.config.market_data
    link = resolve_verified_market_link(
        db,
        underlying_symbol=market_data.underlying_symbol,
        contract_symbol=market_data.contract_symbol,
    )
    if link is None:
        raise HTTPException(status_code=409, detail="Tiger/Binance market mapping is not verified")
    return _execute_martingale_bar_backtest(
        payload,
        request,
        db,
        user,
        link=link,
    )


def run_catalog_martingale_backtest(
    *,
    request: Request,
    db: Session,
    user: User,
    strategy_parameters: Mapping[str, Any],
    contract_symbol: str,
    timeframe: str,
    begin_at: datetime,
    end_at: datetime,
    initial_capital: Decimal,
    fee_bps: Decimal,
    slippage_bps: Decimal,
    leverage: int = 1,
    maintenance_margin_rate: Decimal = Decimal("0.005"),
    liquidation_fee_rate: Decimal = Decimal("0"),
    backtest_repository: BacktestRepository | None = None,
    market_data_source: str = "auto",
) -> dict[str, Any]:
    """Run the basket engine through the standard Data Backtest workflow.

    The shared page supplies a contract and timeframe.  Basket sizing, ladder,
    exits and session behavior remain owned by the immutable strategy revision.
    Missing Tiger bars are fetched and persisted on demand before replay.
    """

    timeframe_minutes = {"1m": 1, "5m": 5, "15m": 15, "30m": 30, "1h": 60}
    if timeframe not in timeframe_minutes:
        raise HTTPException(
            status_code=422,
            detail="马丁 TP4 仅支持 1m、5m、15m、30m、1h 周期",
        )
    link = resolve_research_contract_market_link(db, contract_symbol=contract_symbol)
    if link is None:
        raise HTTPException(
            status_code=409,
            detail="该合约尚未建立可用于研究回测的 Tiger/Binance 标的映射",
        )
    source_mode = str(market_data_source or "auto").strip().lower()
    if source_mode not in {"auto", "tiger", "binance"}:
        raise HTTPException(status_code=422, detail="行情数据源必须是自动选择、Tiger 或 Binance")

    normalized_parameters = dict(strategy_parameters)
    normalized_parameters["BoxTimeFrameMinutes"] = timeframe_minutes[timeframe]
    try:
        parameters = strategy_parameters_from_catalog_parameters(normalized_parameters)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=f"马丁 TP4 参数无效：{exc}") from None
    if parameters.mode != "auto":
        raise HTTPException(
            status_code=422,
            detail="统一数据回测当前支持马丁 TP4 自动模式；恢复/网格模式需要专用手动入场回放",
        )

    config = MartingaleTp4Config(
        market_data=MarketDataConfig(
            underlying_symbol=link.underlying_symbol,
            contract_symbol=link.contract_symbol,
            trade_sessions=("regular",),
            adjustment="none",
        ),
        parameters=parameters,
        live_risk=LiveRiskConfig(
            max_cycle_loss_pct=Decimal("10"),
            max_cycle_margin_pct=Decimal("50"),
            minimum_liquidation_buffer_pct=Decimal("5"),
            daily_loss_limit_pct=Decimal("10"),
            additions_enabled=True,
        ),
    )
    replay_request = MartingaleBarReplayRequest(
        config=config,
        begin_at=begin_at,
        end_at=end_at,
        initial_capital=initial_capital,
        leverage=leverage,
        maintenance_margin_rate=maintenance_margin_rate,
        liquidation_fee_rate=liquidation_fee_rate,
        point_size=DEFAULT_SIGNAL_POINT_SIZE,
        fee_bps=fee_bps,
        slippage_bps=slippage_bps,
        synthetic_spread_points=Decimal("1"),
        engine_policy=EnginePolicy.RESEARCH_COMPATIBILITY,
        warmup_bars=250,
    )

    settings = request.app.state.settings
    private_key_path = settings.tiger_openapi_private_key_path
    tiger_configured = bool(
        settings.tiger_openapi_tiger_id.strip()
        and settings.tiger_openapi_account.strip()
        and private_key_path is not None
    )
    timeframe_ms = _timeframe_milliseconds(timeframe)
    signal_lookback = max(
        timedelta(days=30),
        timedelta(milliseconds=timeframe_ms * replay_request.warmup_bars * 10),
    )
    signal_begin_at = replay_request.begin_at - signal_lookback
    daily_begin_at = replay_request.begin_at - timedelta(
        days=max(60, parameters.box.daily_atr_period * 3)
    )
    fallback_reason = (
        "binance_selected_by_user"
        if source_mode == "binance"
        else "tiger_historical_data_unavailable"
    )

    def tiger_data_unusable(exc: HTTPException) -> bool:
        detail = str(exc.detail)
        return detail.startswith("Tiger ") or "replay data coverage is blocked" in detail

    # Tiger's quote/depth/news authorization used by AI Monitor is not the same
    # credential as the official Open API used to backfill historical OHLCV.
    # Existing, already-qualified Tiger bars remain valid even when the Open API
    # credential is not present, so always try the local historical store first.
    if source_mode != "binance":
        try:
            return _execute_martingale_bar_backtest(
                replay_request,
                request,
                db,
                user,
                link=link,
            )
        except HTTPException as exc:
            if not tiger_data_unusable(exc):
                raise
            db.rollback()
            fallback_reason = f"tiger_cached_data_unusable:{exc.detail}"

    if source_mode != "binance" and tiger_configured:
        try:
            quote_api = build_tiger_quote_api(
                tiger_id=settings.tiger_openapi_tiger_id,
                account=settings.tiger_openapi_account,
                private_key_path=private_key_path,
                sandbox=settings.tiger_openapi_sandbox,
            )
            service = TigerBarBackfillService(
                TigerBarClient(quote_api),
                TigerMarketDataRepository(db),
            )
            service.backfill(
                security_id=link.security_id,
                symbol=link.underlying_symbol,
                timeframe=timeframe,
                begin_at=signal_begin_at,
                end_at=replay_request.end_at,
                trade_session="regular",
                adjustment="none",
                expected_bars=None,
                maximum_age_seconds=config.market_data.maximum_tiger_age_seconds,
                total=20_000,
            )
            if parameters.box.auto_range:
                service.backfill(
                    security_id=link.security_id,
                    symbol=link.underlying_symbol,
                    timeframe="1d",
                    begin_at=daily_begin_at,
                    end_at=replay_request.end_at,
                    trade_session="regular",
                    adjustment="none",
                    expected_bars=None,
                    maximum_age_seconds=config.market_data.maximum_tiger_age_seconds,
                    total=1_000,
                )
        except TigerMarketDataError as exc:
            db.rollback()
            fallback_reason = f"tiger_openapi_unavailable:{exc.category}"
        except ValueError as exc:
            db.rollback()
            raise HTTPException(status_code=422, detail=str(exc)) from None
        else:
            try:
                return _execute_martingale_bar_backtest(
                    replay_request,
                    request,
                    db,
                    user,
                    link=link,
                )
            except HTTPException as exc:
                if not tiger_data_unusable(exc):
                    raise
                db.rollback()
                fallback_reason = f"tiger_data_unusable:{exc.detail}"

    if source_mode != "binance" and not tiger_configured:
        # The HAR-derived Tiger integration provides live quote, depth and news
        # data, but it has no trustworthy historical OHLCV endpoint.  Keep the
        # user's Tiger preference and transparently use the verified Binance
        # mapping for the missing historical segment instead of failing the run.
        fallback_reason = (
            "tiger_openapi_not_configured_and_cached_history_unavailable"
        )

    if backtest_repository is None:
        raise HTTPException(
            status_code=503,
            detail="Tiger Open API 尚不可用，且 Binance 映射合约回测源未初始化",
        )
    try:
        signal_candles, signal_source_quality = backtest_repository.load_market_candles(
            link.contract_symbol,
            timeframe,
            int(signal_begin_at.timestamp()),
            int(replay_request.end_at.timestamp()),
            max_bars=20_000,
        )
        daily_candles: Sequence[Any] = ()
        daily_source_quality: Mapping[str, Any] = {}
        if parameters.box.auto_range:
            daily_candles, daily_source_quality = backtest_repository.load_market_candles(
                link.contract_symbol,
                "1d",
                int(daily_begin_at.timestamp()),
                int(replay_request.end_at.timestamp()),
                max_bars=1_000,
            )
    except BacktestUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from None

    received_at = datetime.now(UTC)

    def reference_bars(candles: Sequence[Any], bar_timeframe: str) -> tuple[TigerBar, ...]:
        interval_ms = _timeframe_milliseconds(bar_timeframe)
        return tuple(
            TigerBar(
                symbol=link.underlying_symbol,
                timeframe=bar_timeframe,
                trade_session="regular",
                adjustment="none",
                open_time=int(candle.ts) * 1_000,
                close_time=int(candle.ts) * 1_000 + interval_ms - 1,
                open=Decimal(str(candle.open)),
                high=Decimal(str(candle.high)),
                low=Decimal(str(candle.low)),
                close=Decimal(str(candle.close)),
                volume=Decimal(str(candle.volume)),
                amount=None,
                received_at=received_at,
                source_version="binance_fapi_mapped_v1",
            )
            for candle in candles
        )

    return _execute_martingale_bar_backtest(
        replay_request,
        request,
        db,
        user,
        link=link,
        signal_bars_override=reference_bars(signal_candles, timeframe),
        daily_bars_override=reference_bars(daily_candles, "1d"),
        source_name="binance_fapi",
        source_quality={
            "signal": signal_source_quality,
            "daily": daily_source_quality,
            "fallback_reason": fallback_reason,
        },
    )


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
