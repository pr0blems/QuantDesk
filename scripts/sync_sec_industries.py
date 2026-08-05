from __future__ import annotations

import argparse
import json
import time
from urllib.request import Request, urlopen

from sqlalchemy import select
from sqlalchemy.orm import Session
from translate_stock_library_zh import translate

from quantdesk_v2.config import get_settings
from quantdesk_v2.database import build_engine
from quantdesk_v2.models import CompanyProfile, Security, SecurityResearchSource


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--delay", type=float, default=0.12)
    args = parser.parse_args()
    engine = build_engine(get_settings())
    updated = failed = 0
    translations: dict[str, str] = {}
    with Session(engine) as db:
        securities = db.scalars(select(Security).where(Security.cik.is_not(None)).order_by(Security.symbol)).all()
        for security in securities:
            profile = db.get(CompanyProfile, security.id)
            if profile is None or profile.industry:
                continue
            url = f"https://data.sec.gov/submissions/CIK{security.cik}.json"
            request = Request(url, headers={"User-Agent": "QuantDesk stock research admin@example.com", "Accept": "application/json"})
            try:
                with urlopen(request, timeout=20) as response:  # noqa: S310 - fixed SEC HTTPS origin
                    payload = json.loads(response.read(2_000_000))
                industry = str(payload.get("sicDescription") or "").strip()[:128]
                if industry:
                    profile.industry = industry
                    profile.industry_zh = translations.setdefault(industry, translate(industry)[:128])
                    source = db.scalar(select(SecurityResearchSource).where(SecurityResearchSource.security_id == security.id, SecurityResearchSource.source_type == "SEC"))
                    if source:
                        source.raw_metadata_json = {**(source.raw_metadata_json or {}), "sic": payload.get("sic"), "sicDescription": industry, "entityType": payload.get("entityType")}
                    updated += 1
            except Exception:
                failed += 1
            if updated and updated % 20 == 0:
                db.commit()
            time.sleep(max(0.1, args.delay))
        db.commit()
    print(json.dumps({"updated": updated, "failed": failed, "industries": len(translations)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
