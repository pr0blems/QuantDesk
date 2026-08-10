from __future__ import annotations

from datetime import date
from typing import Any


def number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if parsed != parsed or parsed in {float("inf"), float("-inf")}:
        return None
    return parsed


def metric_value(metrics: dict[str, Any], *keys: str) -> float | None:
    for key in keys:
        parsed = number(metrics.get(key))
        if parsed is not None:
            return parsed
    return None


def _concept_name(item: dict[str, Any]) -> str:
    return str(item.get("concept") or "").rsplit("_", 1)[-1]


def report_value(
    report: dict[str, Any],
    section: str,
    concepts: tuple[str, ...],
    *,
    labels: tuple[str, ...] = (),
) -> float | None:
    items = report.get(section)
    if not isinstance(items, list):
        return None
    normalized = {
        _concept_name(item): number(item.get("value"))
        for item in items
        if isinstance(item, dict)
    }
    for concept in concepts:
        value = normalized.get(concept)
        if value is not None:
            return value
    lowered_labels = tuple(label.lower() for label in labels)
    for item in items:
        if not isinstance(item, dict):
            continue
        label = str(item.get("label") or "").strip().lower()
        if label and any(candidate in label for candidate in lowered_labels):
            value = number(item.get("value"))
            if value is not None:
                return value
    return None


def _latest_report(payload: dict[str, Any]) -> dict[str, Any]:
    rows = payload.get("data")
    if not isinstance(rows, list):
        return {}
    for row in rows:
        if isinstance(row, dict) and isinstance(row.get("report"), dict):
            return row
    return {}


def _positive(value: float | None) -> float | None:
    return None if value is None else abs(value)


def _sum_present(*values: float | None) -> float | None:
    present = [value for value in values if value is not None]
    return sum(present) if present else None


def _clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, value))


def _score_growth(revenue_growth: float | None) -> float:
    if revenue_growth is None:
        return 50.0
    return _clamp(50.0 + revenue_growth * 1.5)


def _score_quality(net_margin: float | None, roe: float | None, fcf: float | None) -> float:
    score = 50.0
    if net_margin is not None:
        score += _clamp(net_margin, -25, 30) * 0.8
    if roe is not None:
        score += _clamp(roe, -30, 40) * 0.45
    if fcf is not None:
        score += 8 if fcf > 0 else -12
    return _clamp(score)


def _score_valuation(pe: float | None, ps: float | None, ev_ebitda: float | None) -> float:
    scores: list[float] = []
    if pe is not None and pe > 0:
        scores.append(_clamp(100 - pe * 2.0))
    if ps is not None and ps > 0:
        scores.append(_clamp(100 - ps * 7.0))
    if ev_ebitda is not None and ev_ebitda > 0:
        scores.append(_clamp(100 - ev_ebitda * 3.0))
    return sum(scores) / len(scores) if scores else 50.0


def _score_health(
    debt_to_equity: float | None,
    current_ratio: float | None,
    operating_cash_flow: float | None,
) -> float:
    score = 55.0
    if debt_to_equity is not None:
        score += _clamp(1.5 - debt_to_equity, -1.5, 1.5) * 15
    if current_ratio is not None:
        score += _clamp(current_ratio - 1.0, -1.0, 1.5) * 12
    if operating_cash_flow is not None:
        score += 8 if operating_cash_flow > 0 else -15
    return _clamp(score)


def _money(value: float | None, currency: str) -> str:
    if value is None:
        return "不适用"
    absolute = abs(value)
    if absolute >= 1_000_000_000_000:
        return f"{currency} {value / 1_000_000_000_000:.2f} 万亿"
    if absolute >= 1_000_000_000:
        return f"{currency} {value / 1_000_000_000:.2f} 十亿"
    if absolute >= 1_000_000:
        return f"{currency} {value / 1_000_000:.2f} 百万"
    return f"{currency} {value:,.0f}"


def _pct(value: float | None) -> str:
    return "不适用" if value is None else f"{value:.2f}%"


def build_financial_snapshot(
    *,
    security_type: str,
    currency: str,
    shares_outstanding_millions: float | None,
    existing_market_cap_millions: float | None,
    metric_payload: dict[str, Any] | None,
    reported_payload: dict[str, Any] | None,
    quote_payload: dict[str, Any] | None = None,
    snapshot_date: date,
) -> dict[str, Any]:
    metrics = (metric_payload or {}).get("metric")
    if not isinstance(metrics, dict):
        metrics = {}
    latest = _latest_report(reported_payload or {})
    report = latest.get("report") if isinstance(latest.get("report"), dict) else {}
    shares = (
        shares_outstanding_millions * 1_000_000
        if shares_outstanding_millions is not None and shares_outstanding_millions > 0
        else None
    )
    reported_shares = report_value(
        report,
        "bs",
        ("CommonStockSharesOutstanding", "EntityCommonStockSharesOutstanding"),
        labels=("shares outstanding",),
    )
    reported_shares = reported_shares or report_value(
        report,
        "ic",
        (
            "WeightedAverageNumberOfSharesOutstandingBasic",
            "WeightedAverageNumberOfDilutedSharesOutstanding",
        ),
        labels=("weighted average shares", "basic (in shares)"),
    )
    shares = shares or reported_shares

    market_cap_millions = metric_value(metrics, "marketCapitalization")
    enterprise_value_millions = metric_value(metrics, "enterpriseValue")
    market_cap = market_cap_millions * 1_000_000 if market_cap_millions is not None else None
    if market_cap is None and existing_market_cap_millions is not None:
        market_cap = existing_market_cap_millions * 1_000_000
    enterprise_value = (
        enterprise_value_millions * 1_000_000
        if enterprise_value_millions is not None
        else None
    )
    quote_price = number((quote_payload or {}).get("c"))
    if market_cap is None and quote_price is not None and quote_price > 0 and shares:
        market_cap = quote_price * shares
    revenue_per_share = metric_value(metrics, "revenuePerShareTTM", "revenuePerShareAnnual")
    cash_flow_per_share = metric_value(
        metrics,
        "cashFlowPerShareTTM",
        "cashFlowPerShareAnnual",
    )
    ebitda_per_share = metric_value(metrics, "ebitdPerShareTTM", "ebitdPerShareAnnual")
    cash_per_share = metric_value(
        metrics,
        "cashPerSharePerShareQuarterly",
        "cashPerSharePerShareAnnual",
    )
    book_value_per_share = metric_value(
        metrics,
        "bookValuePerShareQuarterly",
        "bookValuePerShareAnnual",
    )

    revenue = revenue_per_share * shares if revenue_per_share is not None and shares else None
    revenue = revenue or report_value(
        report,
        "ic",
        (
            "RevenueFromContractWithCustomerExcludingAssessedTax",
            "RevenueFromContractWithCustomerIncludingAssessedTax",
            "Revenues",
            "SalesRevenueNet",
            "SalesRevenueGoodsNet",
            "InterestAndDividendIncomeOperating",
        ),
        labels=("total revenue", "net revenue", "net sales", "revenues"),
    )
    gross_margin = metric_value(metrics, "grossMarginTTM", "grossMarginAnnual")
    operating_margin = metric_value(metrics, "operatingMarginTTM", "operatingMarginAnnual")
    net_margin = metric_value(metrics, "netProfitMarginTTM", "netProfitMarginAnnual")
    revenue_growth = metric_value(
        metrics,
        "revenueGrowthTTMYoy",
        "revenueGrowthQuarterlyYoy",
    )
    gross_profit = revenue * gross_margin / 100 if revenue is not None and gross_margin is not None else None
    gross_profit = gross_profit or report_value(
        report,
        "ic",
        ("GrossProfit",),
        labels=("gross profit", "gross margin"),
    )
    operating_income = (
        revenue * operating_margin / 100
        if revenue is not None and operating_margin is not None
        else None
    )
    operating_income = operating_income or report_value(
        report,
        "ic",
        ("OperatingIncomeLoss",),
        labels=("operating income", "income from operations"),
    )
    net_income = revenue * net_margin / 100 if revenue is not None and net_margin is not None else None
    net_income = net_income or report_value(
        report,
        "ic",
        ("NetIncomeLoss", "ProfitLoss", "NetIncomeLossAvailableToCommonStockholdersBasic"),
        labels=("net income", "net earnings", "profit for the year"),
    )
    if gross_margin is None and gross_profit is not None and revenue not in {None, 0}:
        gross_margin = gross_profit / revenue * 100
    if operating_margin is None and operating_income is not None and revenue not in {None, 0}:
        operating_margin = operating_income / revenue * 100
    if net_margin is None and net_income is not None and revenue not in {None, 0}:
        net_margin = net_income / revenue * 100
    ebitda = ebitda_per_share * shares if ebitda_per_share is not None and shares else None
    ebitda = ebitda or report_value(
        report,
        "ic",
        ("EarningsBeforeInterestTaxesDepreciationAndAmortization",),
        labels=("ebitda",),
    )

    operating_cash_flow = (
        cash_flow_per_share * shares
        if cash_flow_per_share is not None and shares
        else None
    )
    operating_cash_flow = operating_cash_flow or report_value(
        report,
        "cf",
        (
            "NetCashProvidedByUsedInOperatingActivities",
            "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations",
        ),
        labels=("operating activities", "operating cash flow"),
    )
    capital_expenditure = _positive(
        report_value(
            report,
            "cf",
            (
                "PaymentsToAcquirePropertyPlantAndEquipment",
                "PaymentsForAdditionsToPropertyPlantAndEquipment",
                "PropertyPlantAndEquipmentAdditions",
            ),
            labels=("capital expenditure", "property, plant and equipment"),
        )
    )
    price_to_fcf = metric_value(metrics, "pfcfShareTTM", "pfcfShareAnnual")
    free_cash_flow = (
        market_cap / price_to_fcf
        if market_cap is not None and price_to_fcf not in {None, 0}
        else None
    )
    if free_cash_flow is None and operating_cash_flow is not None and capital_expenditure is not None:
        free_cash_flow = operating_cash_flow - capital_expenditure
    if free_cash_flow is None and enterprise_value is not None:
        ev_to_fcf = metric_value(
            metrics,
            "currentEv/freeCashFlowTTM",
            "currentEv/freeCashFlowAnnual",
        )
        if ev_to_fcf not in {None, 0}:
            free_cash_flow = enterprise_value / ev_to_fcf

    cash = cash_per_share * shares if cash_per_share is not None and shares else None
    cash = cash or report_value(
        report,
        "bs",
        (
            "CashAndCashEquivalentsAtCarryingValue",
            "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents",
            "CashAndDueFromBanks",
        ),
        labels=("cash and cash equivalents", "cash and due from banks"),
    )
    equity = book_value_per_share * shares if book_value_per_share is not None and shares else None
    equity = equity or report_value(
        report,
        "bs",
        (
            "StockholdersEquity",
            "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",
            "PartnersCapital",
        ),
        labels=("total shareholders", "total stockholders", "total equity"),
    )
    total_assets = report_value(
        report,
        "bs",
        ("Assets",),
        labels=("total assets",),
    )
    total_liabilities = report_value(
        report,
        "bs",
        ("Liabilities",),
        labels=("total liabilities",),
    )
    debt_to_equity = metric_value(
        metrics,
        "totalDebt/totalEquityQuarterly",
        "totalDebt/totalEquityAnnual",
        "longTermDebt/equityQuarterly",
        "longTermDebt/equityAnnual",
    )
    direct_debt = report_value(
        report,
        "bs",
        (
            "LongTermDebtAndFinanceLeaseObligations",
            "LongTermDebtAndCapitalLeaseObligations",
            "DebtAndFinanceLeaseObligations",
        ),
        labels=("total debt",),
    )
    current_debt = report_value(
        report,
        "bs",
        (
            "LongTermDebtAndFinanceLeaseObligationsCurrent",
            "LongTermDebtCurrent",
            "ShortTermBorrowings",
            "ShortTermDebtCurrent",
        ),
        labels=("current portion of long-term debt", "short-term borrowings"),
    )
    noncurrent_debt = report_value(
        report,
        "bs",
        (
            "LongTermDebtAndFinanceLeaseObligationsNoncurrent",
            "LongTermDebtNoncurrent",
            "LongTermDebtAndCapitalLeaseObligationsNoncurrent",
        ),
        labels=("long-term debt",),
    )
    total_debt = direct_debt or _sum_present(current_debt, noncurrent_debt)
    if total_debt is None and equity is not None and debt_to_equity is not None:
        total_debt = equity * debt_to_equity
    if (
        total_debt is None
        and enterprise_value is not None
        and market_cap is not None
        and cash is not None
    ):
        total_debt = max(0.0, enterprise_value - market_cap + cash)
    if total_debt is None and report and total_liabilities is not None:
        total_debt = 0.0

    current_ratio = metric_value(metrics, "currentRatioQuarterly", "currentRatioAnnual")
    roe = metric_value(metrics, "roeTTM", "roeRfy", "roe5Y")
    pe = metric_value(metrics, "peTTM", "peBasicExclExtraTTM", "peAnnual")
    ps = metric_value(metrics, "psTTM", "psAnnual")
    pb = metric_value(metrics, "pbQuarterly", "pb", "pbAnnual")
    ev_to_ebitda = metric_value(metrics, "evEbitdaTTM")
    if pe is None and market_cap is not None and net_income is not None and net_income > 0:
        pe = market_cap / net_income
    if ps is None and market_cap is not None and revenue not in {None, 0}:
        ps = market_cap / revenue
    if pb is None and market_cap is not None and equity not in {None, 0}:
        pb = market_cap / equity
    if enterprise_value is None and market_cap is not None and total_debt is not None:
        enterprise_value = market_cap + total_debt - (cash or 0)
    if ev_to_ebitda is None and enterprise_value is not None and ebitda not in {None, 0}:
        ev_to_ebitda = enterprise_value / ebitda

    is_company = security_type not in {"ETF", "PRE_IPO"}
    categories = {
        "revenue": revenue is not None,
        "profitability": any(value is not None for value in (gross_margin, operating_margin, net_margin)),
        "cash_flow": operating_cash_flow is not None or free_cash_flow is not None,
        "debt": total_debt is not None,
        "valuation": any(value is not None for value in (pe, ps, pb, ev_to_ebitda)),
    }
    if not is_company:
        categories = {key: True for key in categories}
        status = "NOT_APPLICABLE"
        coverage = 100.0
    else:
        coverage = sum(categories.values()) / len(categories) * 100
        status = "COMPLETE" if all(categories.values()) else "PARTIAL"

    fiscal_period_end = None
    raw_period_end = latest.get("endDate")
    if isinstance(raw_period_end, str):
        try:
            fiscal_period_end = date.fromisoformat(raw_period_end[:10])
        except ValueError:
            pass
    snapshot = {
        "snapshot_date": snapshot_date,
        "fiscal_period_end": fiscal_period_end,
        "period_type": "TTM" if metrics else "ANNUAL",
        "currency": currency or "USD",
        "data_status": status,
        "coverage_pct": coverage,
        "revenue_ttm": revenue,
        "revenue_growth_yoy_pct": revenue_growth,
        "gross_profit_ttm": gross_profit,
        "gross_margin_pct": gross_margin,
        "operating_income_ttm": operating_income,
        "operating_margin_pct": operating_margin,
        "net_income_ttm": net_income,
        "net_margin_pct": net_margin,
        "ebitda_ttm": ebitda,
        "operating_cash_flow_ttm": operating_cash_flow,
        "capital_expenditure_ttm": capital_expenditure,
        "free_cash_flow_ttm": free_cash_flow,
        "cash_and_equivalents": cash,
        "total_debt": total_debt,
        "total_assets": total_assets,
        "total_liabilities": total_liabilities,
        "stockholders_equity": equity,
        "current_ratio": current_ratio,
        "debt_to_equity": debt_to_equity,
        "return_on_equity_pct": roe,
        "market_cap": market_cap,
        "enterprise_value": enterprise_value,
        "pe_ratio": pe,
        "price_to_sales_ratio": ps,
        "price_to_book_ratio": pb,
        "ev_to_ebitda": ev_to_ebitda,
        "source": "Finnhub+SEC" if report else "Finnhub",
        "source_url": "https://finnhub.io/docs/api",
        "filing_form": str(latest.get("form") or "")[:32] or None,
        "filing_accession": str(latest.get("accessNumber") or "")[:32] or None,
        "applicable_metrics_json": {
            "categories": categories,
            "not_applicable": [] if is_company else list(categories),
            "classification": security_type,
        },
        "raw_json": {
            "metric": metrics,
            "metric_type": (metric_payload or {}).get("metricType"),
            "latest_report": latest,
            "quote": quote_payload or {},
            "shares_used": shares,
        },
    }
    snapshot["scores"] = {
        "quality": _score_quality(net_margin, roe, free_cash_flow),
        "growth": _score_growth(revenue_growth),
        "valuation": _score_valuation(pe, ps, ev_to_ebitda),
        "financial_health": _score_health(debt_to_equity, current_ratio, operating_cash_flow),
    }
    scores = snapshot["scores"]
    snapshot["scores"]["overall"] = (
        scores["quality"] * 0.30
        + scores["growth"] * 0.20
        + scores["valuation"] * 0.20
        + scores["financial_health"] * 0.30
    )
    snapshot["analysis_text"] = {
        "growth": (
            f"最新 TTM 营收为 {_money(revenue, currency)}，同比增速 {_pct(revenue_growth)}。"
            if is_company
            else "该标的不是经营性公司，营收与公司增长率口径不适用。"
        ),
        "profitability": (
            f"毛利率 {_pct(gross_margin)}、营业利润率 {_pct(operating_margin)}、净利率 {_pct(net_margin)}。"
            if is_company
            else "ETF/Pre-IPO 不采用公司利润率口径。"
        ),
        "valuation": (
            f"市盈率 {pe:.2f}、市销率 {ps:.2f}、市净率 {pb:.2f}、EV/EBITDA {ev_to_ebitda:.2f}。"
            if all(value is not None for value in (pe, ps, pb, ev_to_ebitda))
            else "估值指标按当前可获得的市盈率、市销率、市净率与 EV/EBITDA 入库；不适用项单独标记。"
        ),
        "risk": (
            f"经营现金流 {_money(operating_cash_flow, currency)}，自由现金流 {_money(free_cash_flow, currency)}，总债务 {_money(total_debt, currency)}。"
            if is_company
            else "该标的应按基金结构、杠杆与跟踪误差评估，不能套用公司债务和现金流指标。"
        ),
    }
    return snapshot
