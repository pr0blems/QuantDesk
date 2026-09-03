from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from cryptography.fernet import Fernet
from fastapi import HTTPException
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

from quantdesk_v2 import api, backtest
from quantdesk_v2.backtest import BacktestRepository, BacktestUnavailable
from quantdesk_v2.config import Settings
from quantdesk_v2.database import get_db
from quantdesk_v2.main import create_app
from quantdesk_v2.models import (
    AuditLog,
    BacktestRun,
    BacktestTrade,
    StrategyRevision,
    User,
    UserStrategy,
)
from quantdesk_v2.strategy_catalog import ENGINE_PARAMETER_SCHEMAS


class FakeBacktestRepository:
    def __init__(self) -> None:
        self.configs: list[dict] = []
        self.full_specs: list[dict] = []

    @staticmethod
    def catalog() -> dict:
        return {
            "strategies": [
                {
                    "id": "ma_cross",
                    "name": "均线交叉",
                    "category": "趋势",
                    "description": "测试策略",
                    "params": [
                        {
                            "key": "fast_period",
                            "label": "快线",
                            "type": "integer",
                            "default": 5,
                            "min": 2,
                            "max": 20,
                        },
                        {
                            "key": "slow_period",
                            "label": "慢线",
                            "type": "integer",
                            "default": 20,
                            "min": 3,
                            "max": 200,
                        },
                    ],
                }
            ],
            "symbols": [
                {
                    "symbol": "BTCUSDT",
                    "pair": "BTC/USDT",
                    "available": True,
                    "timeframes": [
                        {
                            "timeframe": "4h",
                            "start_ts": 1_735_689_600,
                            "end_ts": 1_738_281_600,
                            "bars": 438,
                        }
                    ],
                }
            ],
            "limits": {"max_bars": 50_000, "timestamp_unit": "seconds"},
        }

    def run(self, config: dict) -> dict:
        self.configs.append(config)
        return {
            "account": {
                "initial_capital": 10_000.0,
                "final_balance": 10_500.0,
                "final_equity": 10_500.0,
                "net_profit": 500.0,
                "total_fees": 8.25,
                "peak_equity": 10_700.0,
            },
            "metrics": {
                "net_profit": 500.0,
                "total_return_pct": 5.0,
                "max_drawdown_pct": 2.1,
                "win_rate_pct": 100.0,
                "profit_factor": None,
                "sharpe_ratio": None,
                "trade_count": 1,
                "exposure_pct": 18.0,
            },
            "equity_curve": [
                {"ts": 1_735_689_600, "equity": 10_000.0, "drawdown_pct": 0.0},
                {"ts": 1_735_776_000, "equity": 10_500.0, "drawdown_pct": 0.0},
            ],
            "trades": [
                {
                    "symbol": "BTCUSDT",
                    "timeframe": "4h",
                    "side": "long",
                    "entry_ts": 1_735_689_600,
                    "exit_ts": 1_735_776_000,
                    "entry_price": 100.0,
                    "exit_price": 105.0,
                    "quantity": 100.0,
                    "gross_pnl": 500.0,
                    "fees": 8.25,
                    "net_pnl": 491.75,
                    "return_pct": 4.9175,
                    "holding_bars": 6,
                    "exit_reason": "take_profit",
                    "leverage": 2,
                }
            ],
            "data_quality": {
                "bars_used": 438,
                "coverage_pct": 100.0,
                "actual_start_ts": 1_735_689_600,
                "actual_end_ts": 1_735_776_000,
                "trades_total": 1,
                "trades_returned": 1,
                "trades_truncated": False,
                "timestamp_unit": "seconds",
                "warnings": [],
                "assumptions": ["next bar open execution"],
            },
        }

    def run_full_strategy(self, config: dict, spec: dict) -> dict:
        self.full_specs.append(spec)
        return self.run(config)


class FailingBacktestRepository(FakeBacktestRepository):
    def __init__(self, message: str) -> None:
        super().__init__()
        self.message = message

    def run(self, config: dict) -> dict:
        del config
        raise BacktestUnavailable(self.message)


class InvalidResultRepository(FakeBacktestRepository):
    def run(self, config: dict) -> dict:
        result = super().run(config)
        result["trades"][0]["quantity"] = None
        return result


class TinyQuantityRepository(FakeBacktestRepository):
    def run(self, config: dict) -> dict:
        result = super().run(config)
        result["trades"][0]["quantity"] = 8.333333333333333e-13
        return result


class FullTradeCollectionRepository(FakeBacktestRepository):
    def run(self, config: dict) -> dict:
        result = super().run(config)
        latest = result["trades"][0]
        earlier = {
            **latest,
            "entry_ts": latest["entry_ts"] - 86_400,
            "exit_ts": latest["exit_ts"] - 86_400,
            "net_pnl": -10.0,
            "gross_pnl": -8.0,
            "return_pct": -0.1,
            "exit_reason": "stop_loss",
        }
        result["_all_trades"] = [earlier, latest]
        result["metrics"]["trade_count"] = 2
        result["data_quality"].update(
            {"trades_total": 2, "trades_returned": 1, "trades_truncated": True}
        )
        return result


def build_test_client(mysql_test_engine: Engine):
    settings = Settings(
        _env_file=None,
        app_env="test",
        database_url=mysql_test_engine.url.render_as_string(hide_password=False),
        jwt_secret=SecretStr("test-jwt-secret-that-is-long-enough-123456"),
        credential_master_key=SecretStr(Fernet.generate_key().decode("ascii")),
        app_cookie_secure=False,
        app_allowed_hosts="testserver",
        app_allowed_origins="http://testserver",
    )
    test_session = sessionmaker(
        bind=mysql_test_engine, autoflush=False, expire_on_commit=False
    )
    app = create_app(settings)
    app.state.database_engine.dispose()
    app.state.database_engine = mysql_test_engine

    def override_db():
        db = test_session()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_db
    return TestClient(app), test_session


def register_and_login(client: TestClient, username: str) -> dict[str, str]:
    registered = client.post(
        "/api/v2/auth/register",
        json={"username": username, "password": "correct horse battery staple"},
    )
    assert registered.status_code == 201
    login = client.post(
        "/api/v2/auth/login",
        json={
            "username": username,
            "password": "correct horse battery staple",
            "client_type": "web",
        },
    )
    assert login.status_code == 200
    return {"Authorization": f"Bearer {login.json()['access_token']}"}


def backtest_payload() -> dict:
    return {
        "strategy_id": "ma_cross",
        "symbol": "btcusdt",
        "timeframe": "4h",
        "start_date": "2025-01-01",
        "end_date": "2025-01-02",
        "initial_capital": 10_000,
        "position_size_pct": 10,
        "leverage": 2,
        "fee_bps": 4,
        "slippage_bps": 2,
        "stop_loss_pct": 5,
        "take_profit_pct": 10,
        "max_holding_bars": 120,
        "params": {"fast_period": 5},
    }


def test_builtin_indicator_strategy_is_a_manual_backtest_choice() -> None:
    strategy = UserStrategy(
        status="active",
        version=1,
        engine_key="ma_cross",
        strategy_kind="builtin_strategy",
        lifecycle_status="published",
        parameter_schema_json=[{"key": "fast_period"}, {"key": "slow_period"}],
    )
    revision = StrategyRevision(version=1, lifecycle_status="published")

    assert api._strategy_is_backtest_compatible(strategy, revision) is True


def test_live_ai_policy_is_not_misrepresented_as_a_kline_backtest() -> None:
    strategy = UserStrategy(
        status="active",
        version=11,
        engine_key="multi_factor",
        strategy_kind="builtin_strategy",
        lifecycle_status="published",
        parameter_schema_json=[
            {"key": "minimum_news_score"},
            {"key": "minimum_combined_score"},
        ],
    )
    revision = StrategyRevision(version=11, lifecycle_status="published")

    assert api._strategy_is_backtest_compatible(strategy, revision) is False


def test_validated_published_full_strategy_remains_backtestable() -> None:
    strategy = UserStrategy(
        status="active",
        version=1,
        engine_key="strategy_dsl",
        strategy_kind="full_strategy",
        lifecycle_status="published",
        spec_json={"schema_version": 1},
        spec_hash="same-spec",
    )
    revision = StrategyRevision(
        version=1,
        lifecycle_status="published",
        spec_hash="same-spec",
        validation_json={"valid": True},
    )

    assert api._strategy_is_backtest_compatible(strategy, revision) is True


def test_validated_martingale_basket_strategy_is_a_manual_backtest_choice() -> None:
    strategy = UserStrategy(
        status="active",
        version=2,
        engine_key="martingale_tp4",
        strategy_kind="basket_strategy",
        lifecycle_status="validated",
        parameters_json={
            item["key"]: item["default"] for item in ENGINE_PARAMETER_SCHEMAS["martingale_tp4"]
        },
    )
    revision = StrategyRevision(
        version=2,
        lifecycle_status="validated",
        validation_json={"valid": True},
    )

    assert api._strategy_is_backtest_compatible(strategy, revision) is True


def test_martingale_replay_is_normalized_for_standard_backtest_persistence() -> None:
    normalized = api._normalize_martingale_backtest_result(
        {
            "manifest_id": "manifest-1",
            "market_data_source": "binance_fapi",
            "market_data_quality": {"source_fallback_reason": "not_configured"},
            "result": {
                "signal_bar_count": 3,
                "metrics": {
                    "initial_capital": "10000",
                    "final_equity": "10010",
                    "net_profit": "10",
                    "return_pct": "0.1",
                    "cycle_count": 1,
                    "win_rate_pct": "100",
                    "profit_factor": None,
                    "total_fees": "2",
                    "maximum_drawdown_pct": "0.05",
                    "leverage": "10",
                },
                "cycles": [
                    {
                        "sequence": 1,
                        "opened_at": 1_000_000,
                        "closed_at": 1_120_000,
                        "mode": "auto",
                        "leg_count": 1,
                        "realized_pnl": "10",
                        "fees": "2",
                        "exit_reason": "basket_take_profit",
                        "leverage": 10,
                        "peak_initial_margin": "10",
                        "minimum_available_balance": "9990",
                        "available_balance_after_close": "10010",
                        "long_leg_count": 1,
                        "short_leg_count": 0,
                    }
                ],
                "fills": [
                    {
                        "bar_open_time": 1_000_000,
                        "action": "open",
                        "direction": "buy",
                        "quantity": "1",
                        "price": "100",
                    },
                    {
                        "bar_open_time": 1_120_000,
                        "action": "close_all",
                        "direction": "buy",
                        "quantity": "1",
                        "price": "112",
                    },
                ],
                "equity_curve": [
                    {
                        "bar_open_time": 1_000_000,
                        "equity": "10000",
                        "drawdown_pct": "0",
                        "open_legs": 1,
                    },
                    {
                        "bar_open_time": 1_060_000,
                        "equity": "10005",
                        "drawdown_pct": "0",
                        "open_legs": 1,
                    },
                    {
                        "bar_open_time": 1_120_000,
                        "equity": "10010",
                        "drawdown_pct": "0",
                        "open_legs": 0,
                    },
                ],
                "warnings": ["tiger_reference_prices_are_not_binance_execution_prices"],
            },
        },
        initial_capital=Decimal("10000"),
    )

    assert normalized["account"]["final_equity"] == 10010.0
    assert normalized["metrics"]["total_return_pct"] == 0.1
    assert normalized["metrics"]["max_drawdown_pct"] == 0.05
    assert normalized["metrics"]["trade_count"] == 1
    assert normalized["trades"][0]["side"] == "long"
    assert normalized["trades"][0]["holding_bars"] == 3
    assert normalized["trades"][0]["exit_reason"] == "basket_take_profit"
    assert normalized["trades"][0]["initial_margin"] == 10.0
    assert normalized["trades"][0]["leverage"] == 10
    assert normalized["trades"][0]["available_balance_after_close"] == 10010.0
    assert normalized["trades"][0]["account_return_pct"] == 0.1
    assert normalized["trades"][0]["margin_return_pct"] == 100.0
    assert normalized["trades"][0]["executions"] == [
        {
            "sequence": 1,
            "timestamp": 1000,
            "phase": "entry",
            "action": "open",
            "position_side": "long",
            "order_side": "buy",
            "quantity": 1.0,
            "price": 100.0,
            "fee": 0.0,
            "gross_pnl": 0.0,
            "net_pnl": 0.0,
            "reason_code": "",
            "leg_indices": [],
        },
        {
            "sequence": 2,
            "timestamp": 1120,
            "phase": "exit",
            "action": "close_all",
            "position_side": "long",
            "order_side": "sell",
            "quantity": 1.0,
            "price": 112.0,
            "fee": 0.0,
            "gross_pnl": 0.0,
            "net_pnl": 0.0,
            "reason_code": "",
            "leg_indices": [],
        },
    ]
    assert normalized["equity_curve"][0]["timestamp"] == 1000
    assert normalized["data_quality"]["manifest_id"] == "manifest-1"
    assert normalized["data_quality"]["source"] == "binance_fapi"
    assert any("Binance 映射合约" in item for item in normalized["data_quality"]["warnings"])


def test_martingale_mixed_cycle_is_not_mislabelled_as_one_long_trade() -> None:
    normalized = api._normalize_martingale_backtest_result(
        {
            "result": {
                "signal_bar_count": 2,
                "metrics": {
                    "initial_capital": "1000",
                    "final_equity": "900",
                    "return_pct": "-10",
                    "total_fees": "0",
                    "maximum_drawdown_pct": "10",
                    "leverage": "10",
                },
                "cycles": [
                    {
                        "sequence": 1,
                        "opened_at": 1_000_000,
                        "closed_at": 1_060_000,
                        "mode": "grid",
                        "leg_count": 2,
                        "realized_pnl": "-100",
                        "fees": "0",
                        "exit_reason": "end_of_data",
                        "leverage": 10,
                        "peak_initial_margin": "30",
                        "minimum_available_balance": "970",
                        "available_balance_after_close": "900",
                        "long_leg_count": 1,
                        "short_leg_count": 1,
                    }
                ],
                "fills": [
                    {"bar_open_time": 1_000_000, "action": "open", "direction": "buy", "quantity": "1", "price": "100"},
                    {"bar_open_time": 1_000_000, "action": "add", "direction": "sell", "quantity": "2", "price": "100"},
                    {"bar_open_time": 1_060_000, "action": "close_all", "direction": "buy", "quantity": "1", "price": "110"},
                    {"bar_open_time": 1_060_000, "action": "close_all", "direction": "sell", "quantity": "2", "price": "110"},
                ],
                "equity_curve": [
                    {"bar_open_time": 1_000_000, "equity": "1000", "drawdown_pct": "0", "open_legs": 2},
                    {"bar_open_time": 1_060_000, "equity": "900", "drawdown_pct": "10", "open_legs": 0},
                ],
                "warnings": [],
            }
        },
        initial_capital=Decimal("1000"),
    )

    trade = normalized["trades"][0]
    assert trade["side"] == "long"
    assert trade["is_mixed_basket"] is True
    assert trade["position_structure"] == "mixed_basket"
    assert trade["long_quantity"] == 1.0
    assert trade["short_quantity"] == 2.0
    assert trade["long_entry_price"] == 100.0
    assert trade["short_exit_price"] == 110.0
    assert trade["initial_margin"] == 30.0
    assert trade["available_balance_after_close"] == 900.0
    assert trade["return_pct"] == -10.0
    assert [item["order_side"] for item in trade["executions"]] == [
        "buy",
        "sell",
        "sell",
        "buy",
    ]
    assert [item["position_side"] for item in trade["executions"]] == [
        "long",
        "short",
        "long",
        "short",
    ]


def test_catalog_keeps_symbols_without_local_history_for_on_demand_fetch() -> None:
    catalog = api._catalog_response(
        {
            "strategies": [],
            "symbols": [
                {
                    "symbol": "AAPLUSDT",
                    "available": False,
                    "timeframes": [],
                }
            ],
            "timeframes": ["15m", "1h", "4h"],
            "limits": {},
        }
    )

    assert catalog["symbols"] == [
        {
            "symbol": "AAPLUSDT",
            "available": False,
            "timeframes": [],
        }
    ]
    assert [item["value"] for item in catalog["timeframes"]] == ["15m", "1h", "4h"]


def test_strategy_trigger_timeframe_reads_current_and_legacy_validation_shapes() -> None:
    assert api._strategy_trigger_timeframe(
        {
            "strategy_kind": "source_strategy",
            "source_validation": {"trigger_timeframe": "1h"},
        }
    ) == "1h"
    assert api._strategy_trigger_timeframe(
        {
            "strategy_kind": "source_strategy",
            "source_validation": {
                "data_requirements": {"trigger_timeframe": "4h"}
            },
        }
    ) == "4h"
    assert api._strategy_trigger_timeframe(
        {
            "strategy_kind": "full_strategy",
            "spec": {"timeframes": {"trigger": "15m"}},
        }
    ) == "15m"
    assert api._strategy_trigger_timeframe(
        {
            "strategy_kind": "source_strategy",
            "source_validation": {"trigger_timeframe": "2h"},
        }
    ) is None


def test_repository_fetches_and_persists_an_empty_local_range() -> None:
    repository = object.__new__(BacktestRepository)
    repository.symbol_set = {"AAPLUSDT"}
    query_results = iter(
        [
            [{"bars": 0, "end_time": None}],
            [{"bars": 0}],
        ]
    )
    repository._query = lambda *_: next(query_results)
    fetched_calls: list[tuple[str, str, int, int]] = []
    repository.kline_fetcher = lambda symbol, timeframe, start_ms, end_ms: (
        fetched_calls.append((symbol, timeframe, start_ms, end_ms))
        or [(start_ms, 10.0, 12.0, 9.0, 11.0, 100.0)]
    )
    persisted: list[tuple[str, str, list[tuple]]] = []
    repository._upsert_binance_klines = (
        lambda symbol, timeframe, rows: persisted.append((symbol, timeframe, rows))
    )

    result = repository._ensure_klines_if_missing(
        "AAPLUSDT", "15m", 1_700_000_000, 1_700_003_600
    )

    assert fetched_calls == [
        ("AAPLUSDT", "15m", 1_700_000_000_000, 1_700_003_600_000)
    ]
    assert persisted and persisted[0][:2] == ("AAPLUSDT", "15m")
    assert result == {
        "source": "binance_fapi",
        "symbol": "AAPLUSDT",
        "timeframe": "15m",
        "requested_start_ts": 1_700_000_000,
        "requested_end_ts": 1_700_003_600,
        "bars_fetched": 1,
    }


def test_repository_refetches_a_partial_local_range(monkeypatch) -> None:
    repository = object.__new__(BacktestRepository)
    repository.symbol_set = {"AAPLUSDT"}
    start_ts = 1_700_000_000
    end_ts = start_ts + 3_600
    query_results = iter(
        [
            [{"bars": 2, "end_time": (start_ts + 900) * 1_000}],
            [
                {
                    "bars": 2,
                    "start_time": start_ts * 1_000,
                    "end_time": (start_ts + 900) * 1_000,
                }
            ],
        ]
    )
    repository._query = lambda *_: next(query_results)
    fetched_calls: list[tuple[str, str, int, int]] = []
    repository.kline_fetcher = lambda symbol, timeframe, start_ms, end_ms: (
        fetched_calls.append((symbol, timeframe, start_ms, end_ms))
        or [(start_ms, 10.0, 12.0, 9.0, 11.0, 100.0)]
    )
    repository._upsert_binance_klines = lambda *_: None
    monkeypatch.setattr(backtest.time, "time", lambda: end_ts + 10_000)

    result = repository._ensure_klines_if_missing(
        "AAPLUSDT", "15m", start_ts, end_ts
    )

    assert fetched_calls == [
        ("AAPLUSDT", "15m", start_ts * 1_000, end_ts * 1_000)
    ]
    assert result is not None
    assert result["bars_fetched"] == 1


def test_backtest_endpoints_require_authentication(mysql_test_engine: Engine) -> None:
    client, _ = build_test_client(mysql_test_engine)
    with client:
        assert client.get("/api/v2/backtests/catalog").status_code == 401
        assert (
            client.get("/api/v2/backtests/position-calculator?symbol=BTCUSDT").status_code
            == 401
        )
        assert client.get("/api/v2/backtests").status_code == 401
        assert client.get("/api/v2/backtests/1").status_code == 401
        assert client.post("/api/v2/backtests", json=backtest_payload()).status_code == 401


def test_backtest_position_calculator_returns_live_price_and_contract_rules(
    mysql_test_engine: Engine,
) -> None:
    client, _ = build_test_client(mysql_test_engine)
    client.app.state.backtest_position_calculator_price_provider = (
        lambda symbol: Decimal("500.25") if symbol == "MUUSDT" else Decimal("0")
    )
    client.app.state.backtest_contract_rules_provider = lambda symbol: {
        "symbol": symbol,
        "tick_size": Decimal("0.01"),
        "market_step_size": Decimal("0.01"),
        "min_quantity": Decimal("0.01"),
        "min_notional": Decimal("5"),
    }

    with client:
        headers = register_and_login(client, "position-calculator-researcher")
        response = client.get(
            "/api/v2/backtests/position-calculator?symbol=MUUSDT",
            headers=headers,
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["symbol"] == "MUUSDT"
    assert payload["price"] == 500.25
    assert payload["source"] == "test_provider"
    assert payload["strategy_point_size"] == 0.01
    assert payload["exchange_tick_size"] == 0.01
    assert payload["quantity_step"] == 0.01
    assert payload["min_quantity"] == 0.01
    assert payload["min_notional"] == 5.0
    assert payload["observed_at"].endswith("Z")


def test_backtest_guard_matches_the_two_worker_browser_contract() -> None:
    api._active_backtest_users.clear()
    api._active_backtest_count = 0
    try:
        api._acquire_backtest_slot(101)
        api._acquire_backtest_slot(101)
        assert api._active_backtest_users == {101: 2}
        assert api._active_backtest_count == 2

        with pytest.raises(HTTPException) as same_user:
            api._acquire_backtest_slot(101)
        assert same_user.value.status_code == 409

        with pytest.raises(HTTPException) as global_capacity:
            api._acquire_backtest_slot(202)
        assert global_capacity.value.status_code == 429
    finally:
        api._release_backtest_slot(101)
        api._release_backtest_slot(101)
        api._active_backtest_users.clear()
        api._active_backtest_count = 0


def test_backtest_run_is_saved_audited_and_isolated_by_user(
    monkeypatch, mysql_test_engine: Engine
) -> None:
    client, test_session = build_test_client(mysql_test_engine)
    repository = FakeBacktestRepository()
    monkeypatch.setattr(api, "_backtest", lambda _: repository)

    with client:
        first_headers = register_and_login(client, "researcher-one")

        catalog = client.get("/api/v2/backtests/catalog", headers=first_headers)
        assert catalog.status_code == 200
        assert catalog.json()["bounds"]["BTCUSDT"]["4h"]["min_date"] == "2025-01-01"
        assert catalog.json()["timeframes"][0]["value"] == "4h"
        assert catalog.json()["limits"]["max_concurrent_backtests"] == 2
        assert catalog.json()["limits"]["max_concurrent_backtests_per_user"] == 2
        assert catalog.json()["limits"]["max_persisted_trades"] == 10_000

        created = client.post("/api/v2/backtests", headers=first_headers, json=backtest_payload())
        assert created.status_code == 201
        body = created.json()
        run_id = body["run"]["id"]
        assert body["run"]["strategy_name"] == "均线交叉"
        assert body["result"]["account"]["final_equity"] == 10_500.0
        assert body["result"]["metrics"]["profit_factor"] is None
        assert body["result"]["trades"][0]["entry_ts"] == 1_735_689_600
        assert body["result"]["trades"][0]["entry_at"].endswith("Z")
        assert body["run"]["start_at"].endswith("Z")
        assert body["run"]["end_at"].endswith("Z")
        assert body["run"]["created_at"].endswith("Z")
        assert body["run"]["completed_at"].endswith("Z")
        assert "NaN" not in created.text

        expected_start = int(datetime(2025, 1, 1, tzinfo=UTC).timestamp())
        expected_end = int(datetime(2025, 1, 2, 23, 59, 59, tzinfo=UTC).timestamp())
        assert repository.configs[0]["start_ts"] == expected_start
        assert repository.configs[0]["end_ts"] == expected_end
        assert repository.configs[0]["initial_capital"] == 10_000.0
        assert repository.configs[0]["params"] == {"fast_period": 5, "slow_period": 20}
        assert isinstance(repository.configs[0]["params"]["fast_period"], int)

        history = client.get("/api/v2/backtests?limit=12", headers=first_headers)
        assert history.status_code == 200
        assert [item["id"] for item in history.json()["items"]] == [run_id]

        detail = client.get(f"/api/v2/backtests/{run_id}", headers=first_headers)
        assert detail.status_code == 200
        assert detail.json()["result"]["data_quality"]["bars_used"] == 438

        second_headers = register_and_login(client, "researcher-two")
        assert client.get("/api/v2/backtests", headers=second_headers).json() == {"items": []}
        hidden = client.get(f"/api/v2/backtests/{run_id}", headers=second_headers)
        assert hidden.status_code == 404

        with test_session() as db:
            first_user = db.scalar(select(User).where(User.username == "researcher-one"))
            saved_run = db.scalar(select(BacktestRun).where(BacktestRun.id == run_id))
            saved_trade = db.scalar(select(BacktestTrade).where(BacktestTrade.run_id == run_id))
            audit = db.scalar(
                select(AuditLog).where(
                    AuditLog.action == "backtest.run.create",
                    AuditLog.resource_id == str(run_id),
                )
            )
            assert first_user is not None
            assert saved_run is not None and saved_run.user_id == first_user.id
            assert saved_run.strategy_name == "均线交叉"
            assert saved_run.config_json["symbol"] == "BTCUSDT"
            assert saved_run.config_json["params"] == {"fast_period": 5, "slow_period": 20}
            assert saved_run.config_json["start_ts"] == expected_start
            assert saved_run.data_quality_json["coverage_pct"] == 100.0
            assert saved_run.metadata_json["app_version"] == api.__version__
            assert saved_run.metadata_json["engine_version"] == api.__version__
            assert len(saved_run.metadata_json["strategy_version"]) == 16
            actual_range = saved_run.metadata_json["actual_data_range"]
            assert actual_range["start_at"].endswith("Z")
            assert actual_range["end_at"].endswith("Z")
            assert actual_range["timestamp_unit"] == "seconds"
            assert saved_trade is not None and saved_trade.user_id == first_user.id
            assert saved_trade.metadata_json["symbol"] == "BTCUSDT"
            assert audit is not None and audit.user_id == first_user.id
            assert audit.resource_type == "backtest_run"


def test_backtest_catalog_exposes_current_users_supported_manual_choices(
    monkeypatch, mysql_test_engine: Engine
) -> None:
    client, test_session = build_test_client(mysql_test_engine)
    repository = FakeBacktestRepository()
    monkeypatch.setattr(api, "_backtest", lambda _: repository)

    with client:
        headers = register_and_login(client, "database-strategy-researcher")
        catalog_response = client.get("/api/v2/backtests/catalog", headers=headers)
        assert catalog_response.status_code == 200
        strategies = catalog_response.json()["strategies"]
        assert len(strategies) > 1
        by_name = {item["name"]: item for item in strategies}
        assert "多周期趋势回踩延续" in by_name
        assert by_name["多周期趋势回踩延续"]["strategy_kind"] == "full_strategy"
        assert "MA 金叉" in by_name
        assert by_name["MA 金叉"]["strategy_kind"] == "builtin_strategy"
        assert "AI 机会决策策略" not in by_name

        with test_session() as db:
            legacy = db.scalar(
                select(UserStrategy).where(UserStrategy.name == "MA 金叉")
            )
            assert legacy is not None

        payload = backtest_payload()
        payload["strategy_id"] = legacy.public_id
        response = client.post("/api/v2/backtests", headers=headers, json=payload)
        assert response.status_code == 201
        assert repository.configs[-1]["strategy_id"] == "ma_cross"


def test_full_strategy_uses_multitimeframe_repository_path(
    monkeypatch, mysql_test_engine: Engine
) -> None:
    client, _ = build_test_client(mysql_test_engine)
    repository = FakeBacktestRepository()
    monkeypatch.setattr(api, "_backtest", lambda _: repository)

    with client:
        headers = register_and_login(client, "full-strategy-researcher")
        catalog = client.get("/api/v2/backtests/catalog", headers=headers).json()
        strategy = next(
            item for item in catalog["strategies"] if item["strategy_kind"] == "full_strategy"
        )
        payload = backtest_payload()
        payload["strategy_id"] = strategy["id"]
        payload["timeframe"] = "4h"
        payload["leverage"] = 5
        payload["params"] = {
            definition["key"]: definition["default"] for definition in strategy["params"]
        }

        response = client.post("/api/v2/backtests", headers=headers, json=payload)

        assert response.status_code == 201
        assert repository.configs[-1]["strategy_id"] == "strategy_dsl"
        assert repository.configs[-1]["timeframe"] == strategy["spec"]["timeframes"]["trigger"]
        assert response.json()["run"]["timeframe"] == strategy["spec"]["timeframes"]["trigger"]
        assert repository.full_specs[-1]["strategy_type"] == "trend_pullback_continuation"

        promoted = client.post(
            f"/api/v2/strategies/{strategy['id']}/promote",
            headers=headers,
            json={
                "expected_version": strategy["version"],
                "target_status": "backtested",
                "confirmed": True,
            },
        )
        assert promoted.status_code == 200
        assert promoted.json()["strategy"]["lifecycle_status"] == "backtested"


def test_backtest_engine_validation_and_service_errors_are_mapped(
    monkeypatch, mysql_test_engine: Engine
) -> None:
    client, _ = build_test_client(mysql_test_engine)
    with client:
        headers = register_and_login(client, "error-researcher")

        monkeypatch.setattr(
            api,
            "_backtest",
            lambda _: FailingBacktestRepository("unknown backtest symbol"),
        )
        invalid = client.post("/api/v2/backtests", headers=headers, json=backtest_payload())
        assert invalid.status_code == 422

        monkeypatch.setattr(
            api,
            "_backtest",
            lambda _: FailingBacktestRepository("backtest market data query failed"),
        )
        unavailable = client.post("/api/v2/backtests", headers=headers, json=backtest_payload())
        assert unavailable.status_code == 503


def test_invalid_engine_result_rolls_back_run_trade_and_audit(
    monkeypatch, mysql_test_engine: Engine
) -> None:
    client, test_session = build_test_client(mysql_test_engine)
    monkeypatch.setattr(api, "_backtest", lambda _: InvalidResultRepository())

    with client:
        headers = register_and_login(client, "atomic-researcher")
        response = client.post("/api/v2/backtests", headers=headers, json=backtest_payload())
        assert response.status_code == 503

        with test_session() as db:
            assert db.scalar(select(BacktestRun.id)) is None
            assert db.scalar(select(BacktestTrade.id)) is None
            assert (
                db.scalar(select(AuditLog.id).where(AuditLog.action == "backtest.run.create"))
                is None
            )


def test_sub_pico_quantity_remains_nonzero_after_persistence(
    monkeypatch, mysql_test_engine: Engine
) -> None:
    client, test_session = build_test_client(mysql_test_engine)
    monkeypatch.setattr(api, "_backtest", lambda _: TinyQuantityRepository())

    with client:
        headers = register_and_login(client, "tiny-quantity-researcher")
        response = client.post("/api/v2/backtests", headers=headers, json=backtest_payload())
        assert response.status_code == 201
        assert 0 < response.json()["result"]["trades"][0]["quantity"] < 1e-12

        with test_session() as db:
            quantity = db.scalar(select(BacktestTrade.quantity))
            assert quantity is not None
            assert Decimal("0") < quantity < Decimal("0.000000000001")


def test_full_trade_collection_is_persisted_but_detail_remains_truncated(
    monkeypatch, mysql_test_engine: Engine
) -> None:
    client, test_session = build_test_client(mysql_test_engine)
    monkeypatch.setattr(api, "_backtest", lambda _: FullTradeCollectionRepository())

    with client:
        headers = register_and_login(client, "full-trades-researcher")
        response = client.post("/api/v2/backtests", headers=headers, json=backtest_payload())
        assert response.status_code == 201
        run_id = response.json()["run"]["id"]
        assert len(response.json()["result"]["trades"]) == 1

        detail = client.get(f"/api/v2/backtests/{run_id}", headers=headers)
        assert detail.status_code == 200
        assert len(detail.json()["result"]["trades"]) == 1

        with test_session() as db:
            saved_trades = db.scalars(
                select(BacktestTrade).where(BacktestTrade.run_id == run_id)
            ).all()
            saved_run = db.get(BacktestRun, run_id)
            assert len(saved_trades) == 2
            assert saved_run is not None
            assert saved_run.metadata_json["persisted_trade_count"] == 2


def test_trade_storage_quota_rejects_run_atomically(
    monkeypatch, mysql_test_engine: Engine
) -> None:
    client, test_session = build_test_client(mysql_test_engine)
    monkeypatch.setattr(api, "_backtest", lambda _: FullTradeCollectionRepository())
    monkeypatch.setattr(api, "MAX_PERSISTED_TRADES", 1)

    with client:
        headers = register_and_login(client, "trade-quota-researcher")
        response = client.post("/api/v2/backtests", headers=headers, json=backtest_payload())
        assert response.status_code == 422

        with test_session() as db:
            assert db.scalar(select(BacktestRun.id)) is None
            assert db.scalar(select(BacktestTrade.id)) is None
