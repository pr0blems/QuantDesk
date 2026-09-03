from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

import quantdesk_v2.interfaces.api.martingale_tp4 as martingale_api
from quantdesk_v2.interfaces.api.martingale_tp4 import router
from quantdesk_v2.strategy_catalog import ENGINE_PARAMETER_SCHEMAS
from quantdesk_v2.tiger_market_data import VerifiedMarketLink


def test_martingale_control_plane_exposes_research_routes_without_trade_route() -> None:
    paths = {route.path for route in router.routes}

    assert "/api/v2/basket-strategies/martingale-tp4/validate" in paths
    assert "/api/v2/basket-strategies/martingale-tp4/mq4/import" in paths
    assert "/api/v2/basket-strategies/martingale-tp4/mq4/csv/import" in paths
    assert "/api/v2/basket-strategies/martingale-tp4/mq4/export" in paths
    assert "/api/v2/basket-strategies/martingale-tp4/backtests" in paths
    assert "/api/v2/basket-strategies/martingale-tp4/tiger-bars/backfill" in paths
    assert "/api/v2/basket-strategies/martingale-tp4/shadow/cycles" in paths
    assert "/api/v2/basket-strategies/martingale-tp4/shadow/cycles/{cycle_id}" in paths
    assert not any(path.endswith(("/execute", "/order", "/arm")) for path in paths)


def test_catalog_backtest_uses_mapped_binance_bars_when_tiger_is_unconfigured(
    monkeypatch,
) -> None:
    link = VerifiedMarketLink(
        security_id=7,
        underlying_symbol="AAOI",
        contract_symbol="AAOIUSDT",
        tiger_mapping_id=11,
        binance_mapping_id=12,
    )
    monkeypatch.setattr(
        martingale_api,
        "resolve_research_contract_market_link",
        lambda db, *, contract_symbol: link,
    )
    captured = {}

    def execute(payload, request, db, user, **kwargs):
        captured.update(kwargs)
        return {"market_data_source": kwargs["source_name"]}

    monkeypatch.setattr(martingale_api, "_execute_martingale_bar_backtest", execute)
    calls = []

    class Repository:
        def load_market_candles(self, symbol, timeframe, start_ts, end_ts, *, max_bars):
            calls.append((symbol, timeframe, max_bars))
            candle = SimpleNamespace(
                ts=start_ts,
                open=100,
                high=101,
                low=99,
                close=100.5,
                volume=10,
            )
            return [candle], {"source": "binance_fapi", "actual_bars": 1}

    settings = SimpleNamespace(
        tiger_openapi_tiger_id="",
        tiger_openapi_account="",
        tiger_openapi_private_key_path=None,
        tiger_openapi_sandbox=False,
    )
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(settings=settings)))
    parameters = {
        item["key"]: item["default"] for item in ENGINE_PARAMETER_SCHEMAS["martingale_tp4"]
    }

    result = martingale_api.run_catalog_martingale_backtest(
        request=request,
        db=object(),
        user=object(),
        strategy_parameters=parameters,
        contract_symbol="AAOIUSDT",
        timeframe="15m",
        begin_at=datetime(2026, 8, 1, tzinfo=UTC),
        end_at=datetime(2026, 8, 3, tzinfo=UTC),
        initial_capital=Decimal("10000"),
        fee_bps=Decimal("4"),
        slippage_bps=Decimal("2"),
        backtest_repository=Repository(),
    )

    assert result["market_data_source"] == "binance_fapi"
    assert calls == [("AAOIUSDT", "15m", 20_000), ("AAOIUSDT", "1d", 1_000)]
    assert captured["source_quality"]["fallback_reason"] == "tiger_openapi_not_configured"
    assert captured["signal_bars_override"][0].source_version == "binance_fapi_mapped_v1"


def test_catalog_backtest_honors_explicit_binance_source(monkeypatch) -> None:
    link = VerifiedMarketLink(
        security_id=7,
        underlying_symbol="AAOI",
        contract_symbol="AAOIUSDT",
        tiger_mapping_id=11,
        binance_mapping_id=12,
    )
    monkeypatch.setattr(
        martingale_api,
        "resolve_research_contract_market_link",
        lambda db, *, contract_symbol: link,
    )
    monkeypatch.setattr(
        martingale_api,
        "build_tiger_quote_api",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("Tiger must not be called")),
    )
    monkeypatch.setattr(
        martingale_api,
        "_execute_martingale_bar_backtest",
        lambda payload, request, db, user, **kwargs: {
            "market_data_source": kwargs["source_name"],
            "source_quality": kwargs["source_quality"],
        },
    )

    class Repository:
        def load_market_candles(self, symbol, timeframe, start_ts, end_ts, *, max_bars):
            candle = SimpleNamespace(
                ts=start_ts,
                open=100,
                high=101,
                low=99,
                close=100.5,
                volume=10,
            )
            return [candle], {"source": "binance_fapi", "actual_bars": 1}

    settings = SimpleNamespace(
        tiger_openapi_tiger_id="configured",
        tiger_openapi_account="configured",
        tiger_openapi_private_key_path="tiger-test-private.key",
        tiger_openapi_sandbox=False,
    )
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(settings=settings)))
    parameters = {
        item["key"]: item["default"] for item in ENGINE_PARAMETER_SCHEMAS["martingale_tp4"]
    }

    result = martingale_api.run_catalog_martingale_backtest(
        request=request,
        db=object(),
        user=object(),
        strategy_parameters=parameters,
        contract_symbol="AAOIUSDT",
        timeframe="15m",
        begin_at=datetime(2026, 8, 1, tzinfo=UTC),
        end_at=datetime(2026, 8, 3, tzinfo=UTC),
        initial_capital=Decimal("10000"),
        fee_bps=Decimal("4"),
        slippage_bps=Decimal("2"),
        market_data_source="binance",
        backtest_repository=Repository(),
    )

    assert result["market_data_source"] == "binance_fapi"
    assert result["source_quality"]["fallback_reason"] == "binance_selected_by_user"
