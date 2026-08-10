from __future__ import annotations

import argparse
import hashlib
import json
import time
from datetime import date
from http.client import HTTPSConnection
from typing import Any
from urllib.parse import urlencode, urlsplit

from sqlalchemy import select
from sqlalchemy.orm import Session

from quantdesk_v2.config import get_settings
from quantdesk_v2.database import build_engine
from quantdesk_v2.fundamentals import build_financial_snapshot, number
from quantdesk_v2.models import (
    CompanyProfile,
    Security,
    SecurityFinancialSnapshot,
    SecurityFundamentalAnalysis,
    SecurityResearchSource,
    utcnow,
)
from quantdesk_v2.stock_library import FINNHUB_SYMBOL_ALIASES

FINNHUB_ORIGIN = "https://finnhub.io"
FINNHUB_DOCS_URL = "https://finnhub.io/docs/api"
SEC_FILING_FALLBACKS: dict[str, dict[str, Any]] = {
    "BSP": {
        "source_url": "https://www.sec.gov/Archives/edgar/data/2004711/000110465926071170/tm2613674-7_f1.htm",
        "payload": {
            "data": [
                {
                    "endDate": "2026-03-31 00:00:00",
                    "form": "F-1",
                    "accessNumber": "0001104659-26-071170",
                    "report": {
                        "bs": [
                            {"concept": "Assets", "label": "Total assets", "value": 6_982_872_000},
                            {"concept": "Liabilities", "label": "Total liabilities", "value": 5_920_182_000},
                            {"concept": "StockholdersEquity", "label": "Total shareholders' equity", "value": 1_062_690_000},
                            {"concept": "LongTermDebtAndFinanceLeaseObligations", "label": "Total debt, including current portion", "value": 4_356_067_000},
                            {"concept": "CashAndCashEquivalentsAtCarryingValue", "label": "Cash, cash equivalents, and restricted cash", "value": 788_823_000},
                        ],
                        "ic": [
                            {"concept": "Revenues", "label": "Revenue", "value": 1_306_404_000},
                            {"concept": "GrossProfit", "label": "Gross profit", "value": 857_270_000},
                            {"concept": "OperatingIncomeLoss", "label": "Operating income", "value": 277_851_000},
                            {"concept": "NetIncomeLoss", "label": "Net income (loss)", "value": -204_000},
                        ],
                        "cf": [
                            {"concept": "NetCashProvidedByUsedInOperatingActivities", "label": "Net cash from operating activities", "value": 290_600_000},
                            {"concept": "PaymentsToAcquirePropertyPlantAndEquipment", "label": "Purchase of property, plant, and equipment", "value": 501_000},
                        ],
                    },
                }
            ]
        },
    }
}
FUND_CLASSIFICATIONS = {"BOT"}


def _fetch_json(path: str, params: dict[str, str], token: str, timeout: float) -> dict[str, Any]:
    query = urlencode(params)
    parsed = urlsplit(f"{FINNHUB_ORIGIN}{path}?{query}")
    if parsed.scheme != "https" or parsed.hostname != "finnhub.io":
        raise RuntimeError("rejected upstream origin")
    headers = {
        "Accept": "application/json",
        "User-Agent": "QuantDesk-NG/2 fundamentals sync",
        "X-Finnhub-Token": token,
    }
    last_error = "upstream"
    for attempt in range(4):
        connection = HTTPSConnection(parsed.hostname, 443, timeout=timeout)
        try:
            connection.request("GET", f"{parsed.path}?{parsed.query}", headers=headers)
            response = connection.getresponse()
            body = response.read(8 * 1024 * 1024 + 1)
            if len(body) > 8 * 1024 * 1024:
                raise RuntimeError("upstream response exceeded 8 MiB")
            if response.status == 429:
                last_error = "rate_limit"
                time.sleep(20 * (attempt + 1))
                continue
            if response.status in {500, 502, 503, 504}:
                last_error = f"http_{response.status}"
                time.sleep(2 ** attempt)
                continue
            if response.status < 200 or response.status >= 300:
                raise RuntimeError(f"upstream HTTP {response.status}")
            payload = json.loads(body)
            if not isinstance(payload, dict):
                raise RuntimeError("upstream response is not an object")
            return payload
        finally:
            connection.close()
    raise RuntimeError(last_error)


def _assign_snapshot(row: SecurityFinancialSnapshot, values: dict[str, Any]) -> None:
    ignored = {"scores", "analysis_text"}
    for key, value in values.items():
        if key not in ignored:
            setattr(row, key, value)
    row.retrieved_at = utcnow()


def _upsert_analysis(
    db: Session,
    security: Security,
    profile: CompanyProfile | None,
    snapshot: SecurityFinancialSnapshot,
    values: dict[str, Any],
) -> SecurityFundamentalAnalysis:
    today = values["snapshot_date"]
    analysis = db.scalar(
        select(SecurityFundamentalAnalysis).where(
            SecurityFundamentalAnalysis.security_id == security.id,
            SecurityFundamentalAnalysis.analysis_version == "fundamentals-v2",
            SecurityFundamentalAnalysis.as_of_date == today,
        )
    )
    if analysis is None:
        analysis = SecurityFundamentalAnalysis(
            security_id=security.id,
            analysis_version="fundamentals-v2",
            as_of_date=today,
        )
        db.add(analysis)
    name = security.company_name_zh or security.company_name or security.symbol
    industry = (profile.industry_zh or profile.industry) if profile else None
    is_company = security.security_type not in {"ETF", "PRE_IPO"}
    if is_company:
        analysis.business_summary = (
            f"{name}（{security.symbol}）属于{industry or '待分类行业'}。"
            "财务快照已接入最新标准化指标与公开申报数据。"
        )
    else:
        analysis.business_summary = (
            f"{name}（{security.symbol}）属于{security.security_type}，"
            "公司营收、利润率、现金流、债务和估值口径已明确标记为不适用。"
        )
    texts = values["analysis_text"]
    analysis.growth_analysis = texts["growth"]
    analysis.profitability_analysis = texts["profitability"]
    analysis.valuation_analysis = texts["valuation"]
    analysis.risk_analysis = texts["risk"]
    scores = values["scores"]
    analysis.quality_score = scores["quality"]
    analysis.growth_score = scores["growth"]
    analysis.valuation_score = scores["valuation"]
    analysis.financial_health_score = scores["financial_health"]
    analysis.overall_score = scores["overall"]
    analysis.confidence_score = min(0.98, 0.70 + float(values["coverage_pct"]) / 500)
    analysis.catalysts_json = [
        "营收增长与盈利能力持续改善" if is_company else "跟踪资产趋势与资金流入改善",
        "自由现金流转正或继续扩张" if is_company else "折溢价和跟踪误差维持稳定",
    ]
    analysis.risk_factors_json = [
        "估值倍数压缩与行业景气回落" if is_company else "杠杆、波动损耗与跟踪误差",
        "现金流弱化或债务负担上升" if is_company else "底层资产集中度与流动性风险",
    ]
    complete = values["data_status"] in {"COMPLETE", "NOT_APPLICABLE"}
    analysis.evidence_json = {
        "stage": "complete_fundamentals",
        "fundamental_data_complete": complete,
        "financial_metrics_complete": values["data_status"] == "COMPLETE",
        "financial_metrics_status": values["data_status"],
        "financial_snapshot_id": snapshot.id,
        "coverage_pct": float(values["coverage_pct"]),
        "categories": values["applicable_metrics_json"]["categories"],
        "sources": [
            "Finnhub Basic Financials",
            "Finnhub Financials As Reported / SEC filing data",
        ],
        "source_url": FINNHUB_DOCS_URL,
        "disclaimer": "公开财务资料的量化整理，不构成投资建议。",
    }
    analysis.generated_at = utcnow()
    return analysis


def _upsert_source(
    db: Session,
    security: Security,
    snapshot: SecurityFinancialSnapshot,
) -> None:
    digest = hashlib.sha256(f"FINNHUB_FUNDAMENTALS\0{security.symbol}".encode()).hexdigest()
    source = db.scalar(
        select(SecurityResearchSource).where(
            SecurityResearchSource.security_id == security.id,
            SecurityResearchSource.content_hash == digest,
        )
    )
    if source is None:
        source = SecurityResearchSource(
            security_id=security.id,
            source_type="FUNDAMENTALS",
            title=f"{security.symbol} 完整基本面财务与估值",
            url=FINNHUB_DOCS_URL,
            publisher="Finnhub / SEC",
            content_hash=digest,
            status="ACTIVE",
        )
        db.add(source)
    source.retrieved_at = utcnow()
    source.content_summary = (
        f"status={snapshot.data_status}; coverage={float(snapshot.coverage_pct or 0):.1f}%; "
        f"period_end={snapshot.fiscal_period_end or 'n/a'}"
    )
    source.raw_metadata_json = {
        "snapshot_id": snapshot.id,
        "snapshot_date": snapshot.snapshot_date.isoformat(),
        "filing_form": snapshot.filing_form,
        "filing_accession": snapshot.filing_accession,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbols", default="", help="comma-separated symbols; default is all active")
    parser.add_argument("--delay-seconds", type=float, default=1.10)
    parser.add_argument("--timeout-seconds", type=float, default=20.0)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--resume-complete", action="store_true")
    args = parser.parse_args()
    settings = get_settings()
    token = settings.finnhub_api_key.get_secret_value()
    if not token:
        raise RuntimeError("FINNHUB_API_KEY is required")
    requested = {item.strip().upper() for item in args.symbols.split(",") if item.strip()}
    today = date.today()
    engine = build_engine(settings)
    counts = {"complete": 0, "not_applicable": 0, "partial": 0, "failed": 0}
    failures: dict[str, str] = {}
    with Session(engine) as db:
        query = select(Security).where(Security.is_active.is_(True)).order_by(Security.symbol)
        securities = list(db.scalars(query).all())
        if requested:
            securities = [row for row in securities if row.symbol in requested]
        if args.limit > 0:
            securities = securities[: args.limit]
        total = len(securities)
        for index, security in enumerate(securities, start=1):
            try:
                existing_snapshot = db.scalar(
                    select(SecurityFinancialSnapshot).where(
                        SecurityFinancialSnapshot.security_id == security.id,
                        SecurityFinancialSnapshot.snapshot_date == today,
                    )
                )
                if (
                    args.resume_complete
                    and existing_snapshot is not None
                    and existing_snapshot.data_status in {"COMPLETE", "NOT_APPLICABLE"}
                ):
                    key = existing_snapshot.data_status.lower()
                    counts[key] = counts.get(key, 0) + 1
                    print(
                        f"[{index}/{total}] {security.symbol}: SKIPPED {existing_snapshot.data_status}",
                        flush=True,
                    )
                    continue
                if security.symbol in FUND_CLASSIFICATIONS:
                    security.security_type = "ETF"
                if security.symbol in FINNHUB_SYMBOL_ALIASES:
                    security.security_type = "COMMON_STOCK"
                    if security.verification_status == "REVIEW_REQUIRED":
                        security.verification_status = "VERIFIED_ALIAS"
                upstream_symbol = FINNHUB_SYMBOL_ALIASES.get(
                    security.symbol,
                    security.finnhub_symbol or security.symbol,
                )
                security.finnhub_symbol = upstream_symbol
                profile = db.get(CompanyProfile, security.id)
                if profile is None:
                    profile = CompanyProfile(security_id=security.id)
                    db.add(profile)
                shares = number(profile.shares_outstanding)
                stored_company_payload = (profile.raw_json or {}).get("finnhub_profile")
                company_payload: dict[str, Any] = (
                    dict(stored_company_payload) if isinstance(stored_company_payload, dict) else {}
                )
                if (
                    security.security_type not in {"ETF", "PRE_IPO"}
                    and (shares is None or not company_payload)
                ):
                    company_payload = _fetch_json(
                        "/api/v1/stock/profile2",
                        {"symbol": upstream_symbol},
                        token,
                        args.timeout_seconds,
                    )
                    time.sleep(max(0.0, args.delay_seconds))
                    shares = number(company_payload.get("shareOutstanding"))
                metric_payload = _fetch_json(
                    "/api/v1/stock/metric",
                    {"symbol": upstream_symbol, "metric": "all"},
                    token,
                    args.timeout_seconds,
                )
                time.sleep(max(0.0, args.delay_seconds))
                reported_payload: dict[str, Any] = {}
                if security.security_type not in {"ETF", "PRE_IPO"}:
                    reported_payload = _fetch_json(
                        "/api/v1/stock/financials-reported",
                        {"symbol": upstream_symbol, "freq": "annual"},
                        token,
                        args.timeout_seconds,
                    )
                    time.sleep(max(0.0, args.delay_seconds))
                    fallback = SEC_FILING_FALLBACKS.get(security.symbol)
                    if not reported_payload.get("data") and fallback:
                        reported_payload = dict(fallback["payload"])
                metric_values = metric_payload.get("metric")
                quote_payload: dict[str, Any] = {}
                if (
                    security.security_type not in {"ETF", "PRE_IPO"}
                    and (
                        not isinstance(metric_values, dict)
                        or number(metric_values.get("marketCapitalization")) is None
                    )
                ):
                    quote_payload = _fetch_json(
                        "/api/v1/quote",
                        {"symbol": upstream_symbol},
                        token,
                        args.timeout_seconds,
                    )
                    time.sleep(max(0.0, args.delay_seconds))
                fundamental_currency = str(company_payload.get("currency") or security.currency or "USD")[:8]
                values = build_financial_snapshot(
                    security_type=security.security_type,
                    currency=fundamental_currency,
                    shares_outstanding_millions=shares,
                    existing_market_cap_millions=number(profile.market_cap),
                    metric_payload=metric_payload,
                    reported_payload=reported_payload,
                    quote_payload=quote_payload,
                    snapshot_date=today,
                )
                if (
                    values["data_status"] == "PARTIAL"
                    and security.security_type not in {"ETF", "PRE_IPO"}
                    and not reported_payload.get("data")
                ):
                    quarterly_payload = _fetch_json(
                        "/api/v1/stock/financials-reported",
                        {"symbol": upstream_symbol, "freq": "quarterly"},
                        token,
                        args.timeout_seconds,
                    )
                    time.sleep(max(0.0, args.delay_seconds))
                    if quarterly_payload.get("data"):
                        values = build_financial_snapshot(
                            security_type=security.security_type,
                            currency=fundamental_currency,
                            shares_outstanding_millions=shares,
                            existing_market_cap_millions=number(profile.market_cap),
                            metric_payload=metric_payload,
                            reported_payload=quarterly_payload,
                            quote_payload=quote_payload,
                            snapshot_date=today,
                        )
                fallback = SEC_FILING_FALLBACKS.get(security.symbol)
                if fallback and values["data_status"] == "COMPLETE":
                    values["source"] = "SEC F-1 + Nasdaq"
                    values["source_url"] = fallback["source_url"]
                snapshot = existing_snapshot
                if snapshot is None:
                    snapshot = SecurityFinancialSnapshot(security_id=security.id)
                    db.add(snapshot)
                _assign_snapshot(snapshot, values)
                db.flush()
                raw_shares = number((values.get("raw_json") or {}).get("shares_used"))
                if profile is not None:
                    if (
                        values.get("market_cap") is not None
                        and fundamental_currency == security.currency
                    ):
                        profile.market_cap = float(values["market_cap"]) / 1_000_000
                    if raw_shares is not None:
                        profile.shares_outstanding = raw_shares / 1_000_000
                    if company_payload:
                        if not security.company_name:
                            security.company_name = (
                                str(company_payload.get("name") or "")[:255] or None
                            )
                        if not security.country:
                            security.country = (
                                str(company_payload.get("country") or "")[:64] or None
                            )
                        if security.verification_status == "REVIEW_REQUIRED":
                            security.verification_status = "VERIFIED_ALIAS"
                        profile.legal_name = (
                            str(company_payload.get("name") or profile.legal_name or "")[:255]
                            or None
                        )
                        profile.industry = (
                            str(company_payload.get("finnhubIndustry") or profile.industry or "")[:128]
                            or None
                        )
                        profile.website = str(company_payload.get("weburl") or profile.website or "") or None
                        raw_ipo = company_payload.get("ipo")
                        if isinstance(raw_ipo, str):
                            try:
                                profile.ipo_date = date.fromisoformat(raw_ipo[:10])
                            except ValueError:
                                pass
                    profile.source_updated_at = utcnow()
                    profile.raw_json = {
                        **(profile.raw_json or {}),
                        **({"finnhub_profile": company_payload} if company_payload else {}),
                        "fundamental_sync": {
                            "snapshot_date": today.isoformat(),
                            "status": values["data_status"],
                            "coverage_pct": values["coverage_pct"],
                        },
                    }
                _upsert_analysis(db, security, profile, snapshot, values)
                _upsert_source(db, security, snapshot)
                if args.dry_run:
                    db.rollback()
                else:
                    db.commit()
                key = values["data_status"].lower()
                counts[key] = counts.get(key, 0) + 1
                print(
                    f"[{index}/{total}] {security.symbol}: {values['data_status']} "
                    f"{float(values['coverage_pct']):.1f}%",
                    flush=True,
                )
            except Exception as exc:
                db.rollback()
                counts["failed"] += 1
                failures[security.symbol] = str(exc)[:200]
                print(f"[{index}/{total}] {security.symbol}: FAILED {exc}", flush=True)
    print(json.dumps({"total": len(securities), **counts, "failures": failures}, ensure_ascii=False))
    if failures:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
