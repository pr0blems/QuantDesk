from __future__ import annotations

from datetime import date

from quantdesk_v2.fundamentals import build_financial_snapshot


def test_company_snapshot_covers_required_financial_categories() -> None:
    result = build_financial_snapshot(
        security_type="COMMON_STOCK",
        currency="USD",
        shares_outstanding_millions=100,
        existing_market_cap_millions=None,
        metric_payload={
            "metric": {
                "marketCapitalization": 5_000,
                "enterpriseValue": 5_400,
                "revenuePerShareTTM": 10,
                "revenueGrowthTTMYoy": 12,
                "grossMarginTTM": 55,
                "operatingMarginTTM": 25,
                "netProfitMarginTTM": 20,
                "cashFlowPerShareTTM": 2.2,
                "pfcfShareTTM": 25,
                "cashPerSharePerShareQuarterly": 1.5,
                "bookValuePerShareQuarterly": 4,
                "totalDebt/totalEquityQuarterly": 0.5,
                "currentRatioQuarterly": 1.8,
                "roeTTM": 30,
                "peTTM": 25,
                "psTTM": 5,
                "pbQuarterly": 12.5,
                "evEbitdaTTM": 18,
            }
        },
        reported_payload={},
        snapshot_date=date(2026, 8, 10),
    )

    assert result["data_status"] == "COMPLETE"
    assert result["coverage_pct"] == 100
    assert result["revenue_ttm"] == 1_000_000_000
    assert round(result["operating_cash_flow_ttm"] or 0) == 220_000_000
    assert result["free_cash_flow_ttm"] == 200_000_000
    assert result["total_debt"] == 200_000_000
    assert all(result["applicable_metrics_json"]["categories"].values())


def test_etf_snapshot_marks_company_metrics_not_applicable_instead_of_missing() -> None:
    result = build_financial_snapshot(
        security_type="ETF",
        currency="USD",
        shares_outstanding_millions=None,
        existing_market_cap_millions=500,
        metric_payload={"metric": {}},
        reported_payload={},
        snapshot_date=date(2026, 8, 10),
    )

    assert result["data_status"] == "NOT_APPLICABLE"
    assert result["coverage_pct"] == 100
    assert set(result["applicable_metrics_json"]["not_applicable"]) == {
        "revenue",
        "profitability",
        "cash_flow",
        "debt",
        "valuation",
    }


def test_reported_financials_and_market_cap_derive_missing_ratios() -> None:
    result = build_financial_snapshot(
        security_type="COMMON_STOCK",
        currency="USD",
        shares_outstanding_millions=None,
        existing_market_cap_millions=1_000,
        metric_payload={"metric": {}},
        reported_payload={
            "data": [
                {
                    "endDate": "2025-12-31 00:00:00",
                    "form": "10-K",
                    "accessNumber": "0001",
                    "report": {
                        "bs": [
                            {"concept": "Assets", "label": "Total assets", "value": 900_000_000},
                            {"concept": "Liabilities", "label": "Total liabilities", "value": 400_000_000},
                            {"concept": "StockholdersEquity", "label": "Total equity", "value": 500_000_000},
                            {"concept": "LongTermDebtNoncurrent", "label": "Long-term debt", "value": 100_000_000},
                            {"concept": "CashAndCashEquivalentsAtCarryingValue", "label": "Cash and cash equivalents", "value": 50_000_000},
                        ],
                        "ic": [
                            {"concept": "Revenues", "label": "Revenues", "value": 500_000_000},
                            {"concept": "GrossProfit", "label": "Gross profit", "value": 200_000_000},
                            {"concept": "OperatingIncomeLoss", "label": "Operating income", "value": 100_000_000},
                            {"concept": "NetIncomeLoss", "label": "Net income", "value": 50_000_000},
                        ],
                        "cf": [
                            {"concept": "NetCashProvidedByUsedInOperatingActivities", "label": "Operating activities", "value": 80_000_000},
                            {"concept": "PaymentsToAcquirePropertyPlantAndEquipment", "label": "Capital expenditure", "value": 20_000_000},
                        ],
                    },
                }
            ]
        },
        snapshot_date=date(2026, 8, 10),
    )

    assert result["data_status"] == "COMPLETE"
    assert result["gross_margin_pct"] == 40
    assert result["net_margin_pct"] == 10
    assert result["free_cash_flow_ttm"] == 60_000_000
    assert result["pe_ratio"] == 20
    assert result["price_to_sales_ratio"] == 2
    assert result["price_to_book_ratio"] == 2
