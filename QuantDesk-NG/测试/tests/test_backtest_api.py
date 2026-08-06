from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

from quantdesk_v2 import api
from quantdesk_v2.backtest import BacktestUnavailable
from quantdesk_v2.config import Settings
from quantdesk_v2.database import get_db
from quantdesk_v2.main import create_app
from quantdesk_v2.models import AuditLog, BacktestRun, BacktestTrade, User


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
    test_session = sessionmaker(bind=mysql_test_engine, autoflush=False, expire_on_commit=False)
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


def test_backtest_endpoints_require_authentication(mysql_test_engine: Engine) -> None:
    client, _ = build_test_client(mysql_test_engine)
    with client:
        assert client.get("/api/v2/backtests/catalog").status_code == 401
        assert client.get("/api/v2/backtests").status_code == 401
        assert client.get("/api/v2/backtests/1").status_code == 401
        assert client.post("/api/v2/backtests", json=backtest_payload()).status_code == 401


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


def test_backtest_catalog_and_execution_use_current_users_database_strategy(
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
        assert len(strategies) == 20
        assert any(item["name"] == "AI 模拟盘 ATR 趋势" for item in strategies)
        strategy = next(item for item in strategies if item["name"] == "MA 金叉")
        assert strategy["engine_key"] == "ma_cross"
        assert strategy["id"] != "ma_cross"

        payload = backtest_payload()
        payload["strategy_id"] = strategy["id"]
        payload["params"] = {
            definition["key"]: definition["default"] for definition in strategy["params"]
        }
        created = client.post("/api/v2/backtests", headers=headers, json=payload)
        assert created.status_code == 201
        assert created.json()["run"]["strategy_id"] == strategy["id"]
        assert created.json()["run"]["strategy_name"] == "MA 金叉"
        assert repository.configs[-1]["strategy_id"] == "ma_cross"

        with test_session() as db:
            saved = db.scalar(select(BacktestRun).where(BacktestRun.strategy_id == strategy["id"]))
            assert saved is not None
            assert saved.config_json["engine_key"] == "ma_cross"
            assert saved.metadata_json["strategy"]["revision"] == 1


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
        payload["timeframe"] = "15m"
        payload["leverage"] = 5
        payload["params"] = {
            definition["key"]: definition["default"] for definition in strategy["params"]
        }

        response = client.post("/api/v2/backtests", headers=headers, json=payload)

        assert response.status_code == 201
        assert repository.configs[-1]["strategy_id"] == "strategy_dsl"
        assert repository.full_specs[-1]["strategy_type"] == "trend_pullback_continuation"


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


def test_trade_storage_quota_rejects_run_atomically(monkeypatch, mysql_test_engine: Engine) -> None:
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
