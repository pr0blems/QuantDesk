from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from quantdesk_v2 import underlying_quotes
from quantdesk_v2.monitor import (
    build_collected_underlying_quote,
    build_underlying_quote,
)


def _quote(symbol: str, *, stale: bool = False) -> dict[str, object]:
    return {
        "symbol": symbol,
        "available": True,
        "price": 100.0,
        "change_percent": 1.25,
        "previous_close": 98.75,
        "day_open": 99.0,
        "day_high": 101.0,
        "day_low": 98.0,
        "volume": 12_345,
        "source_timestamp": 2_000_000_000,
        "fetched_at": datetime.fromtimestamp(2_000_000_000, UTC),
        "stale": stale,
    }


def test_underlying_quote_calculates_only_time_aligned_basis() -> None:
    result = build_underlying_quote(
        "TESTUSDT",
        {"underlyingType": "EQUITY"},
        101.5,
        2_000_000_000,
        {"TEST": _quote("TEST")},
        {"available": True, "is_open": True, "session": "regular"},
    )

    assert result["quote_symbol"] == "TEST"
    assert result["alignment_status"] == "aligned"
    assert result["basis_bps"] == 150.0
    assert result["spread_alert"] == "strong"


def test_underlying_quote_displays_latest_basis_but_disables_stale_alert() -> None:
    result = build_underlying_quote(
        "TESTUSDT",
        {"underlyingType": "EQUITY"},
        101.5,
        2_000_000_120,
        {"TEST": _quote("TEST", stale=True)},
        {"available": True, "is_open": True, "session": "regular"},
    )

    assert result["alignment_status"] == "stale"
    assert result["basis_comparable"] is True
    assert result["basis_bps"] == 150.0
    assert result["spread_alert"] == "disabled"


def test_underlying_quote_applies_reviewed_symbol_aliases() -> None:
    result = build_underlying_quote(
        "PAYPUSDT",
        {"underlyingType": "EQUITY"},
        100.1,
        2_000_000_000,
        {"PYPL": _quote("PYPL")},
        {"available": True, "is_open": True, "session": "regular"},
    )

    assert result["quote_symbol"] == "PYPL"
    assert result["relation"] == "alias"
    assert result["basis_comparable"] is True


def test_unlisted_contract_is_explicitly_unsupported() -> None:
    result = build_underlying_quote(
        "ANTHROPICUSDT",
        {"underlyingType": "EQUITY"},
        1_450.0,
        2_000_000_000,
        {},
        {"available": True, "is_open": True, "session": "regular"},
    )

    assert result["quote_symbol"] is None
    assert result["relation"] == "unlisted"
    assert result["status"] == "unsupported"
    assert result["basis_comparable"] is False


def test_ng_mapping_covers_native_benchmark_and_unlisted_contracts() -> None:
    mappings = {item.contract_symbol: item for item in underlying_quotes.contract_mappings()}

    assert mappings["AAPLUSDT"].quote_symbol == "AAPL"
    assert mappings["BRKBUSDT"].quote_symbol == "BRK-B"
    assert mappings["XAUUSDT"].quote_symbol == "GC=F"
    assert mappings["XAUUSDT"].relation == "benchmark"
    assert mappings["HK0700USDT"].quote_symbol == "0700.HK"
    assert mappings["HK0700USDT"].relation == "native"
    assert mappings["OPENAIUSDT"].quote_symbol is None
    assert mappings["OPENAIUSDT"].relation == "unlisted"


def test_yahoo_chart_payload_uses_latest_extended_hours_bar() -> None:
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
    assert quote["market_time_ms"] == 1_800_000


def test_persisted_quote_is_preferred_for_aligned_basis() -> None:
    result = build_collected_underlying_quote(
        {
            "quote_symbol": "TEST",
            "relation": "direct",
            "instrument_type": "us_equity",
            "display_name": "Test Corp",
            "source": "yahoo_chart",
            "status": "ok",
            "market_state": "regular",
            "currency": "USD",
            "exchange_name": "NMS",
            "price": 100,
            "change_pct": 1.2,
            "market_time_ms": 2_000_000_000_000,
            "received_at_ms": 2_000_000_001_000,
        },
        100.3,
        2_000_000_000,
    )

    assert result["source"] == "yahoo_chart"
    assert result["alignment_status"] == "aligned"
    assert result["basis_bps"] == 30.0
    assert result["spread_alert"] == "watch"


def test_monitor_frontend_renders_pair_row_and_spread_alert() -> None:
    root = Path(__file__).resolve().parents[1]
    script = (root / "src/quantdesk_v2/static/monitor.js").read_text(encoding="utf-8")
    styles = (root / "src/quantdesk_v2/static/monitor.css").read_text(encoding="utf-8")

    assert 'class="underlying-row' in script
    assert "maybeNotifySpreadAlert(item)" in script
    assert "仅比较最新价，提醒暂停" not in script
    assert "underlying-inline-basis" in script
    assert 'data-matrix-sort="basis"' in script
    assert 'if (key === "basis")' in script
    assert ".underlying-inline-basis.premium { color: var(--m-down) !important; }" in styles
    assert ".underlying-inline-basis.discount { color: var(--m-up) !important; }" in styles
    assert 'class="underlying-price-cell"' in script
    assert 'colspan="17"' not in script
    assert ".underlying-price-cell" in styles
    assert ".underlying-basis.strong" in styles
