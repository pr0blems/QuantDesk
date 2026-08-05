from __future__ import annotations

import argparse
import json
import time
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from sqlalchemy import select
from sqlalchemy.orm import Session

from quantdesk_v2.config import get_settings
from quantdesk_v2.database import build_engine
from quantdesk_v2.models import CompanyProfile, Security

TRANSLATE_URL = "https://translate.googleapis.com/translate_a/single"
INDUSTRY_ZH = {
    "Technology": "科技",
    "Semiconductors": "半导体",
    "Software": "软件",
    "Biotechnology": "生物技术",
    "Banks": "银行",
    "Financial Services": "金融服务",
    "Consumer Electronics": "消费电子",
    "Internet Content & Information": "互联网内容与信息",
    "Auto Manufacturers": "汽车制造",
    "Communication Services": "通信服务",
    "Healthcare": "医疗保健",
    "Industrials": "工业",
    "Consumer Cyclical": "可选消费",
    "Consumer Defensive": "必需消费",
    "Energy": "能源",
    "Basic Materials": "基础材料",
    "Real Estate": "房地产",
    "Utilities": "公用事业",
}


def translate(value: str) -> str:
    params = urlencode({"client": "gtx", "sl": "en", "tl": "zh-CN", "dt": "t", "q": value})
    request = Request(  # noqa: S310 - fixed HTTPS origin
        f"{TRANSLATE_URL}?{params}", headers={"User-Agent": "QuantDesk/2 StockLibrary"}
    )
    with urlopen(request, timeout=15) as response:  # noqa: S310 - fixed HTTPS origin
        payload = json.loads(response.read(65536))
    translated = "".join(part[0] for part in payload[0] if part and part[0])
    return translated.strip()[:255]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--delay", type=float, default=0.08)
    args = parser.parse_args()
    engine = build_engine(get_settings())
    translated = failed = 0
    with Session(engine) as db:
        securities = db.scalars(select(Security).order_by(Security.symbol)).all()
        for security in securities:
            if security.company_name and not security.company_name_zh:
                try:
                    security.company_name_zh = translate(security.company_name)
                    translated += 1
                except Exception:
                    failed += 1
                time.sleep(max(0, args.delay))
            profile = db.get(CompanyProfile, security.id)
            if profile:
                if profile.industry and not profile.industry_zh:
                    profile.industry_zh = INDUSTRY_ZH.get(profile.industry) or translate(profile.industry)[:128]
                if profile.sector and not profile.sector_zh:
                    profile.sector_zh = INDUSTRY_ZH.get(profile.sector) or translate(profile.sector)[:128]
            if translated and translated % 20 == 0:
                db.commit()
        db.commit()
    print(json.dumps({"translated": translated, "failed": failed}, ensure_ascii=False))


if __name__ == "__main__":
    main()
