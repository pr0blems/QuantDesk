from __future__ import annotations

import json
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from quantdesk_v2.config import get_settings
from quantdesk_v2.database import build_engine
from quantdesk_v2.models import CompanyProfile, Security, SecurityFundamentalAnalysis


def main() -> None:
    engine = build_engine(get_settings())
    created = updated = 0
    today = date.today()
    with Session(engine) as db:
        for security in db.scalars(select(Security).order_by(Security.symbol)).all():
            profile = db.get(CompanyProfile, security.id)
            name = security.company_name_zh or security.company_name or security.symbol
            industry = (profile.industry_zh if profile else None) or "行业资料待补充"
            if security.security_type == "ETF":
                summary = f"{name}（{security.symbol}）为交易所交易基金，当前已完成证券身份与官方来源核验。"
                risk = "ETF 仍需补充跟踪指数、费用率、资产规模、持仓集中度和跟踪误差，才能形成完整基金分析。"
            elif security.security_type == "PRE_IPO":
                summary = f"{name}（{security.symbol}）为 Pre-IPO 相关合约，所属方向为{industry}。"
                risk = "尚未形成稳定公开市场财务与估值数据；合约价格不等同于正式上市股票价格。"
            else:
                summary = f"{name}（{security.symbol}）属于{industry}，已完成交易所身份、公司名称、SEC CIK（如适用）及研究来源入库。"
                risk = "当前为资料级初步分析；营收、利润率、现金流、负债和估值指标尚未完整接入，不应视为投资评级。"
            analysis = db.scalar(select(SecurityFundamentalAnalysis).where(SecurityFundamentalAnalysis.security_id == security.id, SecurityFundamentalAnalysis.analysis_version == "baseline-v1", SecurityFundamentalAnalysis.as_of_date == today))
            if analysis is None:
                analysis = SecurityFundamentalAnalysis(security_id=security.id, analysis_version="baseline-v1", as_of_date=today)
                db.add(analysis)
                created += 1
            else:
                updated += 1
            analysis.business_summary = summary
            analysis.risk_analysis = risk
            analysis.confidence_score = 0.35 if security.verification_status.startswith("VERIFIED") else 0.15
            analysis.evidence_json = {"stage": "baseline", "security_verified": security.verification_status, "company_profile": bool(profile), "financial_metrics_complete": False, "disclaimer": "资料级分析，不构成投资建议"}
        db.commit()
    print(json.dumps({"created": created, "updated": updated}, ensure_ascii=False))


if __name__ == "__main__":
    main()
