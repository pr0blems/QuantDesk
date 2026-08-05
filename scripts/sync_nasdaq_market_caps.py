from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from quantdesk_v2.config import get_settings
from quantdesk_v2.database import build_engine
from quantdesk_v2.models import CompanyProfile, Security, SecurityResearchSource, utcnow

SOURCE_URL = "https://api.nasdaq.com/api/screener/stocks"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
    args = parser.parse_args()
    payload = json.loads(args.input.read_text(encoding="utf-8-sig"))
    rows = {
        str(row.get("symbol") or "").upper().replace("-", ".").replace("/", "."): row
        for row in payload.get("data", {}).get("rows", [])
    }
    engine = build_engine(get_settings())
    updated = missing = 0
    with Session(engine) as db:
        for security in db.scalars(select(Security).order_by(Security.symbol)).all():
            row = rows.get(security.symbol)
            if not row:
                missing += 1
                continue
            try:
                market_cap_usd = float(row.get("marketCap") or 0)
            except (TypeError, ValueError):
                market_cap_usd = 0
            if market_cap_usd <= 0:
                missing += 1
                continue
            profile = db.get(CompanyProfile, security.id)
            if profile is None:
                profile = CompanyProfile(security_id=security.id)
                db.add(profile)
            profile.market_cap = market_cap_usd / 1_000_000
            profile.source_updated_at = utcnow()
            profile.raw_json = {**(profile.raw_json or {}), "nasdaq_screener": row, "market_cap_unit": "USD_millions"}
            digest = hashlib.sha256(f"NASDAQ_MARKET_CAP\0{security.symbol}".encode()).hexdigest()
            source = db.scalar(select(SecurityResearchSource).where(SecurityResearchSource.security_id == security.id, SecurityResearchSource.content_hash == digest))
            summary = f"Nasdaq screener market capitalization: USD {market_cap_usd:,.0f}"
            if source is None:
                source = SecurityResearchSource(security_id=security.id, source_type="NASDAQ_MARKET_CAP", title=f"{security.symbol} market capitalization", url=f"https://www.nasdaq.com{row.get('url') or ''}", publisher="Nasdaq", content_hash=digest, status="ACTIVE")
                db.add(source)
            source.content_summary = summary
            source.raw_metadata_json = row
            source.retrieved_at = utcnow()
            updated += 1
        db.commit()
    print(json.dumps({"updated": updated, "missing_or_not_applicable": missing}, ensure_ascii=False))


if __name__ == "__main__":
    main()
