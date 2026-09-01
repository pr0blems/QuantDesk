"""Synchronize Binance TradFi contracts with the existing security master."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from .finnhub import FinnhubClient, FinnhubClientError
from .market_data_client import fetch_exchange_info
from .models import (
    AdminSetting,
    CompanyProfile,
    Security,
    SecuritySymbolMapping,
    utcnow,
)
from .stock_library import (
    ETF_SYMBOLS,
    FINNHUB_SYMBOL_ALIASES,
    NON_STANDARD_SYMBOLS,
    normalize_contract_symbol,
    sync_company_profile,
)

BINANCE_TRADFI_SOURCE = "binance_tradfi"
BINANCE_TRADFI_CONTRACT_TYPE = "TRADIFI_PERPETUAL"
BINANCE_TRADFI_STATUS_KEY = "market_universe:binance_tradfi:v1"
SUPPORTED_UNDERLYING_TYPES = frozenset(
    {
        "EQUITY",
        "HK_EQUITY",
        "KR_EQUITY",
        "CN_EQUITY",
        "PREMARKET",
        "COMMODITY",
    }
)
_EXCHANGE_BY_UNDERLYING = {
    "EQUITY": "US",
    "HK_EQUITY": "HK",
    "KR_EQUITY": "KR",
    "CN_EQUITY": "CN",
    "PREMARKET": "US",
    "COMMODITY": "GLOBAL",
}
_CURRENCY_BY_EXCHANGE = {
    "US": "USD",
    "HK": "HKD",
    "KR": "KRW",
    "CN": "CNY",
    "GLOBAL": "USD",
}
_KNOWN_NON_US_ETFS = frozenset(
    {"CSOPSAMSUNG2L", "CSOPSKHYNIX2L", "KODEX200"}
)
_PROFILE_NO_DATA_RETRY = timedelta(days=7)


@dataclass(frozen=True, slots=True)
class TradfiSyncResult:
    summary: dict[str, Any]
    profile_security_ids: tuple[int, ...]


def parse_tradfi_contracts(payload: Any) -> list[dict[str, Any]]:
    """Return validated TradFi perpetual rows from Binance exchangeInfo."""

    if not isinstance(payload, Mapping) or not isinstance(payload.get("symbols"), list):
        raise ValueError("Binance exchangeInfo must contain a symbols array")
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in payload["symbols"]:
        if not isinstance(raw, Mapping):
            continue
        if str(raw.get("contractType") or "").upper() != BINANCE_TRADFI_CONTRACT_TYPE:
            continue
        symbol = str(raw.get("symbol") or "").strip().upper()
        underlying_type = str(raw.get("underlyingType") or "").strip().upper()
        if (
            not 2 <= len(symbol) <= 32
            or not symbol.isalnum()
            or symbol in seen
            or underlying_type not in SUPPORTED_UNDERLYING_TYPES
        ):
            continue
        seen.add(symbol)
        row = dict(raw)
        row["symbol"] = symbol
        row["pair"] = str(raw.get("pair") or symbol).strip().upper()
        row["status"] = str(raw.get("status") or "UNKNOWN").strip().upper()
        row["contractType"] = BINANCE_TRADFI_CONTRACT_TYPE
        row["underlyingType"] = underlying_type
        rows.append(row)
    rows.sort(key=lambda item: (int(item.get("onboardDate") or 0), item["symbol"]))
    return rows


def _security_type(symbol: str, underlying_type: str) -> str:
    if underlying_type == "PREMARKET":
        return "PRE_IPO"
    if underlying_type == "COMMODITY":
        return "COMMODITY"
    if symbol in ETF_SYMBOLS or symbol in _KNOWN_NON_US_ETFS:
        return "ETF"
    if symbol in NON_STANDARD_SYMBOLS:
        return "UNKNOWN"
    if underlying_type in {"EQUITY", "HK_EQUITY", "KR_EQUITY", "CN_EQUITY"}:
        return "COMMON_STOCK"
    return "UNKNOWN"


def _mapping_status(exchange: str, security_type: str) -> str:
    if exchange != "US" or security_type == "UNKNOWN":
        return "REVIEW_REQUIRED"
    return "AUTO"


def _metadata(
    row: Mapping[str, Any],
    *,
    previous: Mapping[str, Any] | None = None,
    missing_sync_count: int = 0,
) -> dict[str, Any]:
    # Keep Binance precision and order-rule fields for audit and order validation,
    # but never use the JSON column for high-frequency universe filtering.
    result = dict(row)
    for key, value in (previous or {}).items():
        if str(key).startswith("_") and key != "_missing_sync_count":
            result[str(key)] = value
    result["_missing_sync_count"] = max(0, int(missing_sync_count))
    return result


def _profile_retry_due(mapping: SecuritySymbolMapping, now: datetime) -> bool:
    metadata = mapping.source_metadata_json or {}
    profile_sync = metadata.get("_profile_sync")
    if not isinstance(profile_sync, Mapping) or profile_sync.get("status") != "no_data":
        return True
    raw_checked_at = profile_sync.get("checked_at")
    if not isinstance(raw_checked_at, str):
        return True
    try:
        checked_at = datetime.fromisoformat(raw_checked_at.replace("Z", "+00:00"))
    except ValueError:
        return True
    checked_at = (
        checked_at.replace(tzinfo=UTC)
        if checked_at.tzinfo is None
        else checked_at.astimezone(UTC)
    )
    normalized_now = now.replace(tzinfo=UTC) if now.tzinfo is None else now.astimezone(UTC)
    return normalized_now - checked_at >= _PROFILE_NO_DATA_RETRY


def sync_tradfi_contracts(
    db: Session,
    payload: Any,
    *,
    preapproved_symbols: Iterable[str] = (),
) -> TradfiSyncResult:
    """Upsert Binance contracts, securities and source mappings in one transaction."""

    rows = parse_tradfi_contracts(payload)
    if not rows:
        raise ValueError("Binance exchangeInfo returned no TradFi perpetual contracts")
    now = utcnow()
    preapproved = {
        str(symbol).strip().upper() for symbol in preapproved_symbols if symbol
    }
    mappings = list(
        db.scalars(
            select(SecuritySymbolMapping).where(
                SecuritySymbolMapping.source == BINANCE_TRADFI_SOURCE
            )
        ).all()
    )
    mapping_by_symbol = {row.source_symbol: row for row in mappings}
    security_ids = {int(row.security_id) for row in mappings}
    securities = (
        list(db.scalars(select(Security).where(Security.id.in_(security_ids))).all())
        if security_ids
        else []
    )
    security_by_id = {int(row.id): row for row in securities}
    all_security_rows = list(db.scalars(select(Security)).all())
    security_by_key = {(row.exchange, row.symbol): row for row in all_security_rows}

    created = updated = new_contracts = review_required = 0
    seen_symbols: set[str] = set()
    profile_candidates: set[int] = set()
    for row in rows:
        source_symbol = row["symbol"]
        seen_symbols.add(source_symbol)
        normalized_symbol = normalize_contract_symbol(source_symbol)
        underlying_type = row["underlyingType"]
        exchange = _EXCHANGE_BY_UNDERLYING[underlying_type]
        inferred_type = _security_type(normalized_symbol, underlying_type)
        mapping = mapping_by_symbol.get(source_symbol)
        security = security_by_id.get(int(mapping.security_id)) if mapping else None
        if security is None:
            security = security_by_key.get((exchange, normalized_symbol))
        if security is None:
            verification = (
                "REVIEW_REQUIRED"
                if _mapping_status(exchange, inferred_type) == "REVIEW_REQUIRED"
                else "AUTO_VERIFIED"
            )
            security = Security(
                symbol=normalized_symbol,
                exchange=exchange,
                finnhub_symbol=(
                    FINNHUB_SYMBOL_ALIASES.get(normalized_symbol, normalized_symbol)
                    if exchange == "US" and underlying_type == "EQUITY"
                    else None
                ),
                security_type=inferred_type,
                currency=_CURRENCY_BY_EXCHANGE[exchange],
                verification_status=verification,
                is_active=True,
            )
            db.add(security)
            db.flush()
            security_by_key[(exchange, normalized_symbol)] = security
            security_by_id[int(security.id)] = security
            created += 1
        else:
            security.is_active = True
            if security.security_type == "UNKNOWN" and inferred_type != "UNKNOWN":
                security.security_type = inferred_type
            if (
                exchange == "US"
                and underlying_type == "EQUITY"
                and not security.finnhub_symbol
            ):
                security.finnhub_symbol = FINNHUB_SYMBOL_ALIASES.get(
                    normalized_symbol, normalized_symbol
                )
            updated += 1

        status = _mapping_status(exchange, security.security_type)
        if mapping is None:
            previously_approved = source_symbol in preapproved
            source_metadata = _metadata(row)
            if previously_approved:
                source_metadata["_admission_origin"] = "legacy_packaged_universe"
            mapping = SecuritySymbolMapping(
                security_id=security.id,
                source=BINANCE_TRADFI_SOURCE,
                source_symbol=source_symbol,
                normalized_symbol=normalized_symbol,
                mapping_status=status,
                mapping_method="explicit_alias_or_strip_quote_suffix",
                source_status=row["status"],
                contract_type=BINANCE_TRADFI_CONTRACT_TYPE,
                underlying_type=underlying_type,
                onboard_date_ms=int(row.get("onboardDate") or 0) or None,
                monitor_enabled=True,
                # Contracts in the previously reviewed packaged universe keep
                # their behavior. Only genuinely new contracts are monitor-only.
                strategy_enabled=previously_approved,
                live_trading_enabled=previously_approved,
                source_metadata_json=source_metadata,
                first_seen_at=now,
                last_seen_at=now,
                updated_at=now,
            )
            db.add(mapping)
            mapping_by_symbol[source_symbol] = mapping
            new_contracts += 1
        else:
            mapping.security_id = security.id
            mapping.normalized_symbol = normalized_symbol
            if mapping.mapping_status not in {"MANUAL", "VERIFIED"}:
                previous_profile = (mapping.source_metadata_json or {}).get(
                    "_profile_sync"
                )
                mapping.mapping_status = (
                    "REVIEW_REQUIRED"
                    if isinstance(previous_profile, Mapping)
                    and previous_profile.get("status") == "no_data"
                    else status
                )
                mapping.mapping_method = "explicit_alias_or_strip_quote_suffix"
            mapping.source_status = row["status"]
            mapping.contract_type = BINANCE_TRADFI_CONTRACT_TYPE
            mapping.underlying_type = underlying_type
            mapping.onboard_date_ms = int(row.get("onboardDate") or 0) or None
            mapping.source_metadata_json = _metadata(
                row,
                previous=mapping.source_metadata_json,
            )
            mapping.last_seen_at = now
            mapping.updated_at = now
        if mapping.mapping_status == "REVIEW_REQUIRED":
            review_required += 1
        if exchange == "US" and underlying_type == "EQUITY":
            profile_candidates.add(int(security.id))

    missing = suspended = 0
    for mapping in mappings:
        if mapping.source_symbol in seen_symbols:
            continue
        missing += 1
        previous = mapping.source_metadata_json or {}
        missing_sync_count = int(previous.get("_missing_sync_count") or 0) + 1
        mapping.source_metadata_json = _metadata(
            previous,
            previous=previous,
            missing_sync_count=missing_sync_count,
        )
        mapping.updated_at = now
        if missing_sync_count >= 3:
            mapping.source_status = "MISSING"
            mapping.monitor_enabled = False
            mapping.strategy_enabled = False
            # Preserve live_trading_enabled so an existing position remains in
            # the execution allowlist and can still be reduced or closed.
            suspended += 1

    profile_rows = {
        int(row.security_id)
        for row in db.scalars(
            select(CompanyProfile).where(CompanyProfile.security_id.in_(profile_candidates))
        ).all()
    } if profile_candidates else set()
    deferred_profiles = {
        int(mapping.security_id)
        for mapping in mapping_by_symbol.values()
        if int(mapping.security_id) in profile_candidates
        and not _profile_retry_due(mapping, now)
    }
    pending_profiles = tuple(
        sorted(profile_candidates - profile_rows - deferred_profiles)
    )
    summary = {
        "source": BINANCE_TRADFI_SOURCE,
        "contract_type": BINANCE_TRADFI_CONTRACT_TYPE,
        "remote_total": len(rows),
        "remote_trading": sum(row["status"] == "TRADING" for row in rows),
        "created": created,
        "updated": updated,
        "new_contracts": new_contracts,
        "missing": missing,
        "suspended": suspended,
        "review_required": review_required,
        "pending_profiles": len(pending_profiles),
        "synced_at": now.isoformat(),
    }
    state = db.get(AdminSetting, BINANCE_TRADFI_STATUS_KEY)
    if state is None:
        state = AdminSetting(
            key=BINANCE_TRADFI_STATUS_KEY,
            value_json=summary,
            version=1,
        )
        db.add(state)
    else:
        state.value_json = summary
        state.version += 1
    db.commit()
    return TradfiSyncResult(summary=summary, profile_security_ids=pending_profiles)


def sync_tradfi_universe(
    engine: Engine,
    *,
    payload: Any | None = None,
    fetcher: Callable[[], Any] = fetch_exchange_info,
) -> TradfiSyncResult:
    from .market_config import packaged_tradfi_symbols

    response = fetcher() if payload is None else payload
    with Session(engine) as db:
        return sync_tradfi_contracts(
            db,
            response,
            preapproved_symbols=packaged_tradfi_symbols(),
        )


def sync_missing_company_profiles(
    engine: Engine,
    client: FinnhubClient,
    security_ids: Iterable[int],
    *,
    delay_seconds: float = 1.05,
    limit: int = 250,
) -> dict[str, int]:
    """Fill newly discovered US security profiles without blocking universe sync."""

    counts = {"synced": 0, "review_required": 0, "failed": 0}
    if not client.configured:
        return counts
    selected_ids = tuple(dict.fromkeys(int(value) for value in security_ids))[:limit]
    for index, security_id in enumerate(selected_ids):
        with Session(engine) as db:
            security = db.get(Security, security_id)
            if security is None or security.exchange != "US" or not security.finnhub_symbol:
                continue
            try:
                sync_company_profile(db, client, security)
                mappings = db.scalars(
                    select(SecuritySymbolMapping).where(
                        SecuritySymbolMapping.security_id == security.id,
                        SecuritySymbolMapping.source == BINANCE_TRADFI_SOURCE,
                    )
                ).all()
                for mapping in mappings:
                    if mapping.mapping_status != "MANUAL":
                        mapping.mapping_status = "VERIFIED"
                    metadata = dict(mapping.source_metadata_json or {})
                    metadata["_profile_sync"] = {
                        "status": "ok",
                        "synced_at": utcnow().isoformat(),
                    }
                    mapping.source_metadata_json = metadata
                db.commit()
                counts["synced"] += 1
            except FinnhubClientError as exc:
                db.rollback()
                security = db.get(Security, security_id)
                if security is not None and exc.category == "no_data":
                    security.verification_status = "REVIEW_REQUIRED"
                    mappings = db.scalars(
                        select(SecuritySymbolMapping).where(
                            SecuritySymbolMapping.security_id == security.id,
                            SecuritySymbolMapping.source == BINANCE_TRADFI_SOURCE,
                        )
                    ).all()
                    for mapping in mappings:
                        if mapping.mapping_status != "MANUAL":
                            mapping.mapping_status = "REVIEW_REQUIRED"
                        metadata = dict(mapping.source_metadata_json or {})
                        metadata["_profile_sync"] = {
                            "status": "no_data",
                            "checked_at": utcnow().isoformat(),
                        }
                        mapping.source_metadata_json = metadata
                    db.commit()
                    counts["review_required"] += 1
                else:
                    counts["failed"] += 1
            except Exception:
                db.rollback()
                counts["failed"] += 1
        if delay_seconds > 0 and index + 1 < len(selected_ids):
            time.sleep(delay_seconds)
    return counts


def start_tradfi_sync_loop(
    engine: Engine,
    *,
    interval_seconds: float = 3600,
    on_updated: Callable[[TradfiSyncResult], None] | None = None,
) -> threading.Event:
    """Start the singleton market-worker refresh loop and return its stop event."""

    stop = threading.Event()
    interval = max(300.0, float(interval_seconds))

    def loop() -> None:
        while not stop.wait(interval):
            try:
                result = sync_tradfi_universe(engine)
                if on_updated is not None:
                    on_updated(result)
            except Exception as exc:
                print(
                    f"[tradfi-universe] sync failed: {type(exc).__name__}: {str(exc)[:120]}"
                )

    threading.Thread(
        target=loop,
        daemon=True,
        name="tradfi-universe-sync",
    ).start()
    return stop
