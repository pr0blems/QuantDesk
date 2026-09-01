from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from .finnhub import FinnhubClient
from .models import CompanyProfile, Security, SecuritySymbolMapping, utcnow

SYMBOL_ALIASES = {"BRKB": "BRK.B"}
FINNHUB_SYMBOL_ALIASES = {"BBX": "BB"}
ETF_SYMBOLS = {
    "BITO", "BOT", "EWJ", "EWT", "EWY", "EWZ", "IWM", "KORU", "QQQ", "SMH", "SOXL",
    "SOXS", "SPY", "SQQQ", "TBT", "TMF", "TQQQ", "TZA", "URNM", "UVXY", "XBI", "XLE",
}
NON_STANDARD_SYMBOLS = {"DRAM", "FWDI", "INTW", "KSTR", "MUU", "MVLL", "QNTX", "SHAZ", "SKHY", "SNXX", "SPCX", "STXX"}


def normalize_contract_symbol(source_symbol: str) -> str:
    value = source_symbol.strip().upper()
    for suffix in ("USDT", "USD1"):
        if value.endswith(suffix):
            value = value[: -len(suffix)]
            break
    return SYMBOL_ALIASES.get(value, value)


def import_tradfi_equities(db: Session, config_path: Path) -> dict[str, int]:
    """Import a legacy JSON snapshot through the canonical universe synchronizer."""

    payload = json.loads(config_path.expanduser().resolve().read_text(encoding="utf-8"))
    rows = []
    for item in payload.get("symbols", []):
        if not isinstance(item, dict):
            continue
        row = dict(item)
        row.setdefault("contractType", "TRADIFI_PERPETUAL")
        row.setdefault("status", "TRADING")
        row.setdefault("quoteAsset", "USDT")
        rows.append(row)
    from .tradfi_universe import sync_tradfi_contracts

    return sync_tradfi_contracts(
        db,
        {"symbols": rows},
        preapproved_symbols=(str(row.get("symbol") or "") for row in rows),
    ).summary


def _mapping_out(mapping: SecuritySymbolMapping) -> dict[str, Any]:
    return {
        "source": mapping.source,
        "source_symbol": mapping.source_symbol,
        "normalized_symbol": mapping.normalized_symbol,
        "mapping_status": mapping.mapping_status,
        "source_status": mapping.source_status,
        "contract_type": mapping.contract_type,
        "underlying_type": mapping.underlying_type,
        "onboard_date_ms": mapping.onboard_date_ms,
        "monitor_enabled": mapping.monitor_enabled,
        "strategy_enabled": mapping.strategy_enabled,
        "live_trading_enabled": mapping.live_trading_enabled,
        "last_seen_at": mapping.last_seen_at,
    }


def security_out(
    security: Security,
    profile: Any = None,
    analysis: Any = None,
    mappings: list[SecuritySymbolMapping] | tuple[SecuritySymbolMapping, ...] = (),
) -> dict[str, Any]:
    return {
        "id": security.id, "symbol": security.symbol, "exchange": security.exchange,
        "security_type": security.security_type, "company_name": security.company_name,
        "company_name_zh": security.company_name_zh,
        "country": security.country, "cik": security.cik, "is_active": security.is_active,
        "verification_status": security.verification_status, "updated_at": security.updated_at,
        "profile_sync_supported": bool(
            security.exchange == "US" and security.finnhub_symbol
        ),
        "mappings": [_mapping_out(mapping) for mapping in mappings],
        "profile": None if profile is None else {"legal_name": profile.legal_name, "industry": profile.industry, "industry_zh": profile.industry_zh, "sector": profile.sector, "sector_zh": profile.sector_zh, "website": profile.website, "market_cap": float(profile.market_cap) if profile.market_cap is not None else None, "source": profile.source, "source_updated_at": profile.source_updated_at},
        "analysis": None if analysis is None else {"analysis_version": analysis.analysis_version, "as_of_date": analysis.as_of_date, "overall_score": float(analysis.overall_score) if analysis.overall_score is not None else None, "confidence_score": float(analysis.confidence_score) if analysis.confidence_score is not None else None, "business_summary": analysis.business_summary, "risk_analysis": analysis.risk_analysis, "evidence": analysis.evidence_json},
    }


def sync_company_profile(db: Session, client: FinnhubClient, security: Security) -> CompanyProfile:
    payload = client.company_profile(security.finnhub_symbol or security.symbol)
    security.company_name = str(payload.get("name") or security.company_name or "")[:255] or None
    security.country = str(payload.get("country") or "")[:64] or None
    security.currency = str(payload.get("currency") or "USD")[:8]
    security.isin = str(payload.get("isin") or "")[:32] or None
    security.verification_status = "VERIFIED"
    raw_ipo = payload.get("ipo")
    ipo_date = None
    if isinstance(raw_ipo, str):
        try:
            ipo_date = date.fromisoformat(raw_ipo)
        except ValueError:
            pass
    profile = db.get(CompanyProfile, security.id)
    if profile is None:
        profile = CompanyProfile(security_id=security.id)
        db.add(profile)
    profile.legal_name = security.company_name
    profile.industry = str(payload.get("finnhubIndustry") or "")[:128] or None
    profile.website = str(payload.get("weburl") or "") or None
    profile.ipo_date = ipo_date
    profile.market_cap = payload.get("marketCapitalization")
    profile.shares_outstanding = payload.get("shareOutstanding")
    profile.source = "finnhub"
    profile.source_updated_at = utcnow()
    profile.raw_json = payload
    db.commit()
    db.refresh(profile)
    return profile
