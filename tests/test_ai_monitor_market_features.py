from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

from quantdesk_v2.application.ai_monitor.market_features import (
    realtime_feature_payload,
)
from quantdesk_v2.infrastructure.persistence.ai_monitor_market_features import (
    latest_realtime_feature_snapshots,
    load_market_flow_input_maps,
)


class _RowsResult:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def all(self) -> list[Any]:
        return self._rows


class _FeatureSession:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def scalars(self, _statement: Any) -> _RowsResult:
        return _RowsResult(self._rows)


class _ProfileSession:
    def execute(self, _statement: Any) -> _RowsResult:
        return _RowsResult(
            [
                (
                    "aapl",
                    Decimal("3100000000000"),
                    Decimal("15100000000"),
                    "finnhub",
                    "Technology",
                    "Consumer Electronics",
                )
            ]
        )


class _MarketFlowRepository:
    def market_flow_input_rows(self) -> dict[str, list[dict[str, Any]]]:
        return {
            "depth": [{"symbol": "aaplusdt", "book_imbalance": Decimal("0.25")}],
            "positioning": [{"symbol": "AAPLUSDT", "long_short_ratio": Decimal("1.2")}],
            "ticker": [{"symbol": "aaplusdt", "price": Decimal("210.50")}],
            "underlying": [{"contract_symbol": "aaplusdt", "price": Decimal("210.25")}],
        }


def test_realtime_feature_payload_preserves_the_existing_api_shape() -> None:
    bucket_at = datetime(2026, 8, 31, 14, 5)
    captured_at = datetime(2026, 8, 31, 14, 5, 1)
    snapshot = SimpleNamespace(
        id=17,
        symbol="AAPL",
        bucket_at=bucket_at,
        captured_at=captured_at,
        quote_snapshot_json={"source": "finnhub"},
        last_price=Decimal("210.25"),
        bid=Decimal("210.20"),
        ask=Decimal("210.30"),
        spread_bps=Decimal("4.75"),
        quote_age_ms=240,
        size_imbalance=Decimal("0.125"),
        market_session="regular",
        option_flow_snapshot_json={"call_put_ratio": 1.3},
        gex_snapshot_json={"regime": "positive"},
        institutional_flow_snapshot_json={"direction": "long"},
        halt_status="clear",
        data_coverage=Decimal("0.7500"),
        stale_fields_json=["lit_flow"],
        quality_json={"status": "partial"},
        feature_version="uw_features_v2",
    )

    payload = realtime_feature_payload(snapshot)

    assert payload == {
        "id": 17,
        "symbol": "AAPL",
        "bucket_at": bucket_at.isoformat(),
        "captured_at": captured_at.isoformat(),
        "quote": {
            "source": "finnhub",
            "last_price": 210.25,
            "bid": 210.2,
            "ask": 210.3,
            "spread_bps": 4.75,
            "quote_age_ms": 240,
            "size_imbalance": 0.125,
            "market_session": "regular",
        },
        "option_flow": {"call_put_ratio": 1.3},
        "gex": {"regime": "positive"},
        "institutional_flow": {"direction": "long"},
        "halt_status": "clear",
        "data_coverage": 0.75,
        "stale_fields": ["lit_flow"],
        "quality": {"status": "partial"},
        "feature_version": "uw_features_v2",
    }


def test_latest_realtime_feature_snapshots_keeps_the_newest_row_per_symbol() -> None:
    rows = [
        SimpleNamespace(symbol="aapl", marker="new"),
        SimpleNamespace(symbol="AAPL", marker="old"),
        SimpleNamespace(symbol="msft", marker="new"),
    ]

    result = latest_realtime_feature_snapshots(
        _FeatureSession(rows),  # type: ignore[arg-type]
        [" aapl ", "MSFT", "aapl", ""],
    )

    assert result["AAPL"].marker == "new"
    assert result["MSFT"].marker == "new"
    assert len(result) == 2


def test_market_flow_input_maps_use_the_public_repository_boundary() -> None:
    result = load_market_flow_input_maps(
        _ProfileSession(),  # type: ignore[arg-type]
        _MarketFlowRepository(),  # type: ignore[arg-type]
    )

    assert result["depth"]["AAPLUSDT"]["book_imbalance"] == Decimal("0.25")
    assert result["positioning"]["AAPLUSDT"]["long_short_ratio"] == Decimal("1.2")
    assert result["ticker"]["AAPLUSDT"]["price"] == Decimal("210.50")
    assert result["underlying"]["AAPLUSDT"]["price"] == Decimal("210.25")
    assert result["profile"]["AAPL"] == {
        "market_cap": Decimal("3100000000000"),
        "shares_outstanding": Decimal("15100000000"),
        "source": "finnhub",
        "sector": "Technology",
        "industry": "Consumer Electronics",
    }
