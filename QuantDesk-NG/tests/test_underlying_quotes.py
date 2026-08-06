from __future__ import annotations

import pytest

from quantdesk import underlying_quotes


def test_contract_mapping_distinguishes_direct_native_benchmark_and_unlisted() -> None:
    mappings = {item.contract_symbol: item for item in underlying_quotes.contract_mappings()}

    assert mappings["AAPLUSDT"].quote_symbol == "AAPL"
    assert mappings["AAPLUSDT"].relation == "direct"
    assert mappings["BRKBUSDT"].quote_symbol == "BRK-B"
    assert mappings["XAUUSDT"].quote_symbol == "GC=F"
    assert mappings["XAUUSDT"].relation == "benchmark"
    assert mappings["HK0700USDT"].quote_symbol == "0700.HK"
    assert mappings["HK0700USDT"].relation == "native"
    assert mappings["MINIMAXUSDT"].quote_symbol == "0100.HK"
    assert mappings["ZHIPUUSDT"].quote_symbol == "2513.HK"
    assert mappings["QNTXUSDT"].quote_symbol is None
    assert mappings["QNTXUSDT"].relation == "unlisted"
    assert mappings["OPENAIUSDT"].quote_symbol is None
    assert mappings["OPENAIUSDT"].relation == "unlisted"


def test_chart_payload_uses_latest_extended_hours_bar_and_regular_reference() -> None:
    payload = {
        "chart": {
            "result": [
                {
                    "meta": {
                        "shortName": "Example Corp",
                        "currency": "USD",
                        "exchangeName": "NMS",
                        "regularMarketPrice": 101,
                        "chartPreviousClose": 100,
                        "regularMarketTime": 1_800,
                        "currentTradingPeriod": {
                            "pre": {"start": 1_000, "end": 1_500},
                            "regular": {"start": 1_500, "end": 2_000},
                            "post": {"start": 2_000, "end": 2_500},
                        },
                    },
                    "timestamp": [1_700, 1_800],
                    "indicators": {
                        "quote": [
                            {
                                "open": [100, 101],
                                "high": [102, 103],
                                "low": [99, 100],
                                "close": [101, 102],
                                "volume": [10, 20],
                            }
                        ]
                    },
                }
            ]
        }
    }

    quote = underlying_quotes.parse_chart_payload(payload, now_ms=1_850_000)

    assert quote is not None
    assert quote["price"] == 102
    assert quote["change_pct"] == pytest.approx(2)
    assert quote["market_state"] == "regular"
    assert quote["status"] == "ok"
    assert quote["volume"] == 30
    assert quote["market_time_ms"] == 1_800_000


def test_chart_payload_marks_active_but_old_quote_stale() -> None:
    payload = {
        "chart": {
            "result": [
                {
                    "meta": {
                        "currency": "USD",
                        "regularMarketPrice": 100,
                        "chartPreviousClose": 99,
                        "currentTradingPeriod": {
                            "regular": {"start": 1_000, "end": 5_000}
                        },
                    },
                    "timestamp": [1_000],
                    "indicators": {"quote": [{"close": [100]}]},
                }
            ]
        }
    }

    quote = underlying_quotes.parse_chart_payload(payload, now_ms=4_000_000)

    assert quote is not None
    assert quote["market_state"] == "regular"
    assert quote["status"] == "stale"
