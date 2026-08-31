"""Persistence adapters for AI Monitor market-feature inputs."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ...models import CompanyProfile, RealtimeMarketFeatureSnapshot, Security
from ...monitor import MonitorRepository


def latest_realtime_feature_snapshots(
    db: Session,
    symbols: Sequence[str],
) -> dict[str, RealtimeMarketFeatureSnapshot]:
    """Load the newest feature bucket per requested symbol in one bounded query."""

    normalized = sorted(
        {str(symbol).strip().upper() for symbol in symbols if str(symbol).strip()}
    )
    if not normalized:
        return {}
    rows = db.scalars(
        select(RealtimeMarketFeatureSnapshot)
        .where(RealtimeMarketFeatureSnapshot.symbol.in_(normalized))
        .order_by(
            RealtimeMarketFeatureSnapshot.bucket_at.desc(),
            RealtimeMarketFeatureSnapshot.id.desc(),
        )
        .limit(max(100, len(normalized) * 4))
    ).all()
    result: dict[str, RealtimeMarketFeatureSnapshot] = {}
    for row in rows:
        result.setdefault(row.symbol.strip().upper(), row)
    return result


def load_market_flow_input_maps(
    db: Session,
    repository: MonitorRepository,
) -> dict[str, dict[str, dict[str, Any]]]:
    """Assemble normalized symbol maps from monitor and company-profile facts."""

    source_rows = repository.market_flow_input_rows()
    profile_rows = db.execute(
        select(
            Security.symbol,
            CompanyProfile.market_cap,
            CompanyProfile.shares_outstanding,
            CompanyProfile.source,
            CompanyProfile.sector,
            CompanyProfile.industry,
        ).outerjoin(CompanyProfile, CompanyProfile.security_id == Security.id)
    ).all()
    return {
        "depth": {
            str(item.get("symbol") or "").upper(): item
            for item in source_rows.get("depth", [])
        },
        "positioning": {
            str(item.get("symbol") or "").upper(): item
            for item in source_rows.get("positioning", [])
        },
        "ticker": {
            str(item.get("symbol") or "").upper(): item
            for item in source_rows.get("ticker", [])
        },
        "underlying": {
            str(item.get("contract_symbol") or "").upper(): item
            for item in source_rows.get("underlying", [])
        },
        "profile": {
            str(symbol or "").upper(): {
                "market_cap": market_cap,
                "shares_outstanding": shares_outstanding,
                "source": source,
                "sector": sector,
                "industry": industry,
            }
            for symbol, market_cap, shares_outstanding, source, sector, industry in profile_rows
        },
    }
