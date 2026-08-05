from __future__ import annotations

import argparse
import csv
import hashlib
import json
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from quantdesk_v2.config import get_settings
from quantdesk_v2.database import build_engine
from quantdesk_v2.models import CompanyProfile, Security, SecurityResearchSource, utcnow
from quantdesk_v2.stock_library import import_tradfi_equities

NASDAQ_URL = "https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt"
OTHER_URL = "https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt"
SEC_URL = "https://www.sec.gov/files/company_tickers.json"


def _pipe_rows(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return [row for row in csv.DictReader(handle, delimiter="|") if row and not next(iter(row.values()), "").startswith("File Creation Time")]


def _source(db: Session, security: Security, source_type: str, title: str, url: str, summary: str, metadata: dict) -> None:
    digest = hashlib.sha256(f"{source_type}\0{url}".encode()).hexdigest()
    exists = db.scalar(select(SecurityResearchSource).where(SecurityResearchSource.security_id == security.id, SecurityResearchSource.content_hash == digest))
    if exists is None:
        db.add(SecurityResearchSource(security_id=security.id, source_type=source_type, title=title[:512], url=url, publisher="SEC" if source_type == "SEC" else "Nasdaq Trader", content_summary=summary, content_hash=digest, raw_metadata_json=metadata, status="ACTIVE"))
    else:
        exists.title = title[:512]
        exists.content_summary = summary
        exists.raw_metadata_json = metadata
        exists.retrieved_at = utcnow()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sec", type=Path, required=True)
    parser.add_argument("--nasdaq", type=Path, required=True)
    parser.add_argument("--other", type=Path, required=True)
    args = parser.parse_args()
    settings = get_settings()
    sec_payload = json.loads(args.sec.read_text(encoding="utf-8-sig"))
    sec = {str(item.get("ticker", "")).upper().replace("-", "."): item for item in sec_payload.values()}
    listings: dict[str, dict] = {}
    for row in _pipe_rows(args.nasdaq):
        symbol = row.get("Symbol", "").upper().replace("-", ".")
        if symbol:
            listings[symbol] = {"name": row.get("Security Name"), "exchange": "NASDAQ", "etf": row.get("ETF") == "Y", "row": row, "url": NASDAQ_URL}
    exchange_names = {"N": "NYSE", "A": "NYSE American", "P": "NYSE Arca", "Z": "Cboe", "V": "IEX"}
    for row in _pipe_rows(args.other):
        symbol = row.get("ACT Symbol", "").upper().replace("-", ".")
        if symbol:
            listings[symbol] = {"name": row.get("Security Name"), "exchange": exchange_names.get(row.get("Exchange", ""), row.get("Exchange") or "US"), "etf": row.get("ETF") == "Y", "row": row, "url": OTHER_URL}

    engine = build_engine(settings)
    with Session(engine) as db:
        imported = import_tradfi_equities(db, settings.monitor_symbols_config)
        matched_listing = matched_sec = 0
        securities = db.scalars(select(Security).order_by(Security.symbol)).all()
        for security in securities:
            listing = listings.get(security.symbol)
            sec_item = sec.get(security.symbol)
            if listing:
                matched_listing += 1
                security.company_name = str(listing["name"] or "")[:255] or security.company_name
                security.security_type = "ETF" if listing["etf"] else "COMMON_STOCK"
                security.verification_status = "VERIFIED"
                _source(db, security, "EXCHANGE", f"{security.symbol} · {security.company_name}", listing["url"], f"{listing['exchange']} listed security; ETF={listing['etf']}", listing["row"])
            if sec_item:
                matched_sec += 1
                security.cik = str(sec_item.get("cik_str", "")).zfill(10)
                if not security.company_name:
                    security.company_name = str(sec_item.get("title") or "")[:255] or None
                _source(db, security, "SEC", f"SEC issuer mapping · {sec_item.get('title')}", SEC_URL, f"SEC CIK {security.cik} mapped to ticker {security.symbol}", sec_item)
            profile = db.get(CompanyProfile, security.id)
            if profile is None:
                profile = CompanyProfile(security_id=security.id)
                db.add(profile)
            profile.legal_name = security.company_name
            profile.source = "SEC+NASDAQ" if listing and sec_item else "NASDAQ" if listing else "SEC" if sec_item else None
            profile.source_updated_at = utcnow() if listing or sec_item else profile.source_updated_at
        db.commit()
        print(json.dumps({**imported, "total": len(securities), "nasdaq_matched": matched_listing, "sec_matched": matched_sec, "unverified": sum(row.verification_status == "REVIEW_REQUIRED" for row in securities)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
