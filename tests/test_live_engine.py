from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

import pytest

from quantdesk_v2 import live_engine
from quantdesk_v2.binance_client import BinanceAccountClientError


class _Rules:
    min_notional = Decimal("1")

    @staticmethod
    def quantity(value: Decimal) -> Decimal:
        return value

    @staticmethod
    def price(value: Decimal) -> Decimal:
        return value


class _TradingClient:
    @staticmethod
    def symbol_rules(_symbol: str) -> _Rules:
        return _Rules()

    @staticmethod
    def change_leverage(*_args, **_kwargs) -> dict:
        return {}


class _AccountService:
    def __init__(self, orders: tuple[dict, ...] = ()) -> None:
        self.orders = orders

    def open_orders(self, *_args, **_kwargs) -> tuple[dict, ...]:
        return self.orders


def _account(position_mode: str = "hedge") -> dict:
    return {
        "id": 1,
        "user_id": 2,
        "deployment_id": 3,
        "config_json": {
            "position_mode": position_mode,
            "leverage": 3,
            "position_size_pct": 2,
            "margin_cap": 0.2,
        },
    }


def test_hedge_open_and_protection_are_bound_to_long_side(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    market: dict = {}
    protection: dict = {}
    monkeypatch.setattr(live_engine, "_trading_client", _TradingClient())
    monkeypatch.setattr(live_engine, "_exit_levels", lambda *_: (90.0, 110.0))
    monkeypatch.setattr(
        live_engine,
        "_place_market",
        lambda *_args, **kwargs: market.update(kwargs) or {"status": "FILLED", "avgPrice": "100"},
    )
    monkeypatch.setattr(
        live_engine,
        "_place_protection",
        lambda *_args, **kwargs: protection.update(kwargs) or True,
    )
    monkeypatch.setattr(
        live_engine,
        "_account_service",
        SimpleNamespace(
            account=lambda *_args, **_kwargs: SimpleNamespace(
                positions=(
                    {
                        "symbol": "AAPLUSDT",
                        "position_side": "LONG",
                        "side": "long",
                        "amt": 1,
                        "entry_price": 100,
                        "liquidation_price": 50,
                    },
                )
            )
        ),
    )
    monkeypatch.setattr(live_engine.store, "execute", lambda *_args, **_kwargs: 1)

    live_engine._open_position(
        _account(),
        "key",
        "secret",
        SimpleNamespace(
            available_balance=Decimal("1000"),
            wallet_balance=Decimal("1000"),
            positions=(),
        ),
        symbol="AAPLUSDT",
        direction=1,
        price=100.0,
        atr=None,
        signal_time=123,
        signal_key_suffix="manual:11111111-1111-4111-8111-111111111111",
    )

    assert market["side"] == "BUY"
    assert market["position_side"] == "LONG"
    assert market["reduce_only"] is False
    assert market["signal_key"].endswith(
        "manual:11111111-1111-4111-8111-111111111111"
    )
    assert protection["side"] == "SELL"
    assert protection["position_side"] == "LONG"
    assert protection["quantity"] == market["quantity"]
    assert protection["signal_key_suffix"] == (
        "manual:11111111-1111-4111-8111-111111111111"
    )


def test_fixed_copy_total_amount_is_the_live_sizing_base(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict = {}
    account = _account()
    account["config_json"].update(
        {
            "position_size_basis": "copy_total_amount",
            "copy_total_amount": 200,
        }
    )
    monkeypatch.setattr(live_engine, "_trading_client", _TradingClient())
    monkeypatch.setattr(live_engine, "_exit_levels", lambda *_: (90.0, 110.0))
    monkeypatch.setattr(
        live_engine,
        "atr_risk_position_size",
        lambda **kwargs: captured.update(kwargs) or SimpleNamespace(allowed=False),
    )

    live_engine._open_position(
        account,
        "key",
        "secret",
        SimpleNamespace(
            available_balance=Decimal("1000"),
            wallet_balance=Decimal("1000"),
            unrealized_pnl=Decimal("100"),
            positions=(),
        ),
        symbol="AAPLUSDT",
        direction=1,
        price=100.0,
        atr=None,
        signal_time=123,
    )

    assert captured["equity"] == Decimal("200")
    assert captured["available_balance"] == Decimal("40.0")


def test_fixed_copy_total_amount_never_exceeds_real_collateral() -> None:
    capital = live_engine._position_sizing_capital(
        {
            "position_size_basis": "copy_total_amount",
            "copy_total_amount": 5_000,
        },
        wallet=Decimal("1_000"),
        equity=Decimal("800"),
    )

    assert capital.basis == "copy_total_amount"
    assert capital.configured_total_amount == Decimal("5000")
    assert capital.effective_equity == Decimal("800")
    assert capital.margin_equity == Decimal("800")


def test_missing_position_size_basis_preserves_account_equity_behavior() -> None:
    capital = live_engine._position_sizing_capital(
        {},
        wallet=Decimal("1_000"),
        equity=Decimal("1_100"),
    )

    assert capital.basis == "account_equity"
    assert capital.configured_total_amount is None
    assert capital.effective_equity == Decimal("1100")
    assert capital.margin_equity == Decimal("1000")


def test_zero_exchange_liquidation_floor_is_safe_only_for_protected_long() -> None:
    assert live_engine._exchange_liquidation_is_safe(
        entry_price=100,
        stop_price=Decimal("95"),
        liquidation_price=0,
        direction=1,
        min_buffer_pct=0.01,
    )
    assert not live_engine._exchange_liquidation_is_safe(
        entry_price=100,
        stop_price=Decimal("105"),
        liquidation_price=0,
        direction=-1,
        min_buffer_pct=0.01,
    )
    assert not live_engine._exchange_liquidation_is_safe(
        entry_price=100,
        stop_price=Decimal("95"),
        liquidation_price=None,
        direction=1,
        min_buffer_pct=0.01,
    )


def test_post_fill_zero_long_liquidation_price_keeps_protected_position(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    closed: list[str] = []
    failed: list[str] = []
    monkeypatch.setattr(live_engine, "_trading_client", _TradingClient())
    monkeypatch.setattr(live_engine, "_exit_levels", lambda *_: (90.0, 110.0))
    monkeypatch.setattr(
        live_engine,
        "_place_market",
        lambda *_args, **_kwargs: {"status": "FILLED", "avgPrice": "100"},
    )
    monkeypatch.setattr(live_engine, "_place_protection", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(
        live_engine,
        "_account_service",
        SimpleNamespace(
            account=lambda *_args, **_kwargs: SimpleNamespace(
                positions=(
                    {
                        "symbol": "AAPLUSDT",
                        "position_side": "LONG",
                        "side": "long",
                        "amt": 1,
                        "entry_price": 100,
                        "liquidation_price": 0,
                    },
                )
            )
        ),
    )
    monkeypatch.setattr(
        live_engine,
        "_close_position",
        lambda *_args, **_kwargs: closed.append("closed") or True,
    )
    monkeypatch.setattr(
        live_engine,
        "_fail_account",
        lambda _account, reason: failed.append(reason),
    )
    monkeypatch.setattr(live_engine.store, "execute", lambda *_args, **_kwargs: 1)

    opened = live_engine._open_position(
        _account(),
        "key",
        "secret",
        SimpleNamespace(
            available_balance=Decimal("1000"),
            wallet_balance=Decimal("1000"),
            positions=(),
        ),
        symbol="AAPLUSDT",
        direction=1,
        price=100.0,
        atr=None,
        signal_time=123,
    )

    assert opened is True
    assert closed == []
    assert failed == []


def test_periodic_tick_keeps_protected_long_with_zero_liquidation_price(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    position = {
        "symbol": "TXNUSDT",
        "position_side": "LONG",
        "side": "long",
        "amt": 0.33,
        "entry_price": 100,
        "liquidation_price": 0,
    }
    snapshot = SimpleNamespace(
        account_type="UM_FUTURE",
        available_balance=Decimal("900"),
        wallet_balance=Decimal("1000"),
        unrealized_pnl=Decimal("0"),
        positions=(position,),
    )
    managed = {
        ("TXNUSDT", "LONG"): {
            "id": 17,
            "symbol": "TXNUSDT",
            "position_side": "LONG",
            "side": "BUY",
            "quantity": Decimal("0.33"),
            "entry_basis_json": {
                "schema_version": 2,
                "availability": "captured",
                "mode": "live",
            },
        }
    }
    account = _account()
    account["runtime_state_json"] = {}
    account["strategy_snapshot_json"] = {}
    closed: list[str] = []
    failed: list[str] = []

    monkeypatch.setattr(live_engine, "_account_service", SimpleNamespace(account=lambda *_args, **_kwargs: snapshot))
    monkeypatch.setattr(live_engine, "_trading_client", _TradingClient())
    monkeypatch.setattr(live_engine, "_credentials", lambda *_args: ("key", "secret"))
    monkeypatch.setattr(live_engine, "_execution_timeframe_seconds", lambda *_args: 3600)
    monkeypatch.setattr(live_engine, "_cached_position_mode", lambda *_args: "hedge")
    monkeypatch.setattr(live_engine, "_reconcile_intents", lambda *_args: False)
    monkeypatch.setattr(live_engine, "_strategy_universe", lambda *_args: ["TXNUSDT"])
    monkeypatch.setattr(live_engine, "_managed_positions", lambda *_args: managed)
    monkeypatch.setattr(live_engine, "_cancel_orphan_protections", lambda *_args: None)
    monkeypatch.setattr(
        live_engine,
        "_protection_counts",
        lambda *_args: {("TXNUSDT", "LONG"): 2},
    )
    monkeypatch.setattr(
        live_engine,
        "_current_stop_prices",
        lambda *_args: {("TXNUSDT", "LONG"): 95.0},
    )
    monkeypatch.setattr(
        live_engine,
        "_execution_signal",
        lambda *_args, **_kwargs: (0, None, [], None, {}),
    )
    monkeypatch.setattr(live_engine, "_current_open_risk", lambda *_args, **_kwargs: Decimal("0"))
    monkeypatch.setattr(live_engine, "_entry_loss_guard", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(
        live_engine,
        "_close_position",
        lambda *_args, **_kwargs: closed.append(str(_args[-1])) or True,
    )
    monkeypatch.setattr(
        live_engine,
        "_fail_account",
        lambda _account, reason: failed.append(reason),
    )
    monkeypatch.setattr(live_engine.store, "query", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(live_engine.store, "execute", lambda *_args, **_kwargs: 1)

    live_engine._tick_account(account)

    assert closed == []
    assert failed == []


def test_hedge_close_only_uses_the_strategy_managed_quantity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    placed: dict = {}
    canceled: list[tuple[str, str]] = []
    monkeypatch.setattr(live_engine, "_trading_client", _TradingClient())
    monkeypatch.setattr(
        live_engine,
        "_managed_open",
        lambda *_args: {"quantity": Decimal("0.5")},
    )
    monkeypatch.setattr(live_engine, "_pending_market_intent", lambda *_args: False)
    monkeypatch.setattr(
        live_engine,
        "_cancel_protection",
        lambda _account, _key, _secret, symbol, position_side: canceled.append(
            (symbol, position_side)
        ),
    )
    monkeypatch.setattr(
        live_engine,
        "_place_market",
        lambda *_args, **kwargs: placed.update(kwargs) or {"status": "FILLED"},
    )

    live_engine._close_position(
        _account(),
        "key",
        "secret",
        {
            "symbol": "AAPLUSDT",
            "position_side": "LONG",
            "side": "long",
            "amt": 0.8,
        },
        "test",
    )

    assert canceled == [("AAPLUSDT", "LONG")]
    assert placed["side"] == "SELL"
    assert placed["position_side"] == "LONG"
    assert placed["quantity"] == Decimal("0.5")
    assert placed["reduce_only"] is False


def test_post_fill_protection_precedes_account_refresh_and_survives_refresh_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    failures: list[str] = []
    monkeypatch.setattr(live_engine, "_trading_client", _TradingClient())
    monkeypatch.setattr(live_engine, "_exit_levels", lambda *_: (90.0, 110.0))
    monkeypatch.setattr(
        live_engine,
        "build_entry_basis_snapshot",
        lambda *_args, **_kwargs: ({"execution": {}}, None),
    )
    monkeypatch.setattr(
        live_engine,
        "_place_market",
        lambda *_args, **_kwargs: {"status": "FILLED", "avgPrice": "100"},
    )
    monkeypatch.setattr(
        live_engine,
        "_place_protection",
        lambda *_args, **_kwargs: events.append("protect") or True,
    )
    monkeypatch.setattr(
        live_engine,
        "_account_service",
        SimpleNamespace(
            account=lambda *_args, **_kwargs: (
                events.append("refresh"),
                (_ for _ in ()).throw(BinanceAccountClientError("rate_limit")),
            )[1]
        ),
    )
    monkeypatch.setattr(
        live_engine,
        "_close_position",
        lambda *_args, **_kwargs: events.append("close") or True,
    )
    monkeypatch.setattr(
        live_engine,
        "_fail_account",
        lambda _account, reason: failures.append(reason),
    )
    monkeypatch.setattr(live_engine.store, "execute", lambda *_args, **_kwargs: 1)

    live_engine._open_position(
        _account(),
        "key",
        "secret",
        SimpleNamespace(
            available_balance=Decimal("1000"),
            wallet_balance=Decimal("1000"),
            positions=(),
        ),
        symbol="AAPLUSDT",
        direction=1,
        price=100.0,
        atr=None,
        signal_time=123,
    )

    assert events == ["protect", "refresh", "close"]
    assert failures == ["position_state_unverified"]


def test_signal_freshness_requires_closed_bar_and_respects_exact_maximum_age(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    opened = 1_700_000_000
    account = {
        "strategy_snapshot_json": {"strategy_kind": "legacy_signal"},
    }
    policy = live_engine.policy_from_config({"max_signal_age_seconds": 18_000})

    monkeypatch.setattr(live_engine.time, "time", lambda: opened + 14_399)
    assert live_engine._signal_is_fresh(account, opened, policy) is False
    monkeypatch.setattr(live_engine.time, "time", lambda: opened + 14_401)
    assert live_engine._signal_is_fresh(account, opened, policy) is True
    monkeypatch.setattr(live_engine.time, "time", lambda: opened + 18_001)
    assert live_engine._signal_is_fresh(account, opened, policy) is False


def test_live_signal_freshness_uses_frozen_legacy_timeframe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    opened = 1_700_000_000
    account = {
        "strategy_snapshot_json": {
            "strategy_kind": "legacy_signal",
            "timeframe": "1h",
        },
        "config_json": {},
    }
    policy = live_engine.policy_from_config({"max_signal_age_seconds": 18_000})

    monkeypatch.setattr(live_engine.time, "time", lambda: opened + 3_599)
    assert not live_engine._signal_is_fresh(account, opened, policy)
    monkeypatch.setattr(live_engine.time, "time", lambda: opened + 3_601)
    assert live_engine._signal_is_fresh(account, opened, policy)


def test_live_execution_timeframe_uses_full_strategy_trigger_and_fails_closed() -> None:
    account = {
        "strategy_snapshot_json": {
            "strategy_kind": "full_strategy",
            "spec": {"timeframes": {"trigger": "15m"}},
        }
    }

    assert live_engine._execution_timeframe_seconds(account) == 900

    account["strategy_snapshot_json"]["spec"]["timeframes"]["trigger"] = "5m"
    with pytest.raises(live_engine.StrategyEvaluationError):
        live_engine._execution_timeframe_seconds(account)


def test_full_strategy_signal_must_not_outlive_its_decision_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    signal_time = 1_700_000_000
    account = {
        "strategy_snapshot_json": {"strategy_kind": "full_strategy"},
    }
    policy = live_engine.policy_from_config({"max_signal_age_seconds": 18_000})

    monkeypatch.setattr(live_engine.time, "time", lambda: signal_time + 120)
    assert live_engine._signal_is_fresh(
        account,
        signal_time,
        policy,
        {"valid_until": signal_time + 300},
    )
    monkeypatch.setattr(live_engine.time, "time", lambda: signal_time + 301)
    assert not live_engine._signal_is_fresh(
        account,
        signal_time,
        policy,
        {"valid_until": signal_time + 300},
    )


def test_manual_position_or_size_change_blocks_new_entries() -> None:
    managed = {("AAPLUSDT", "LONG"): {"quantity": Decimal("1")}}

    assert not live_engine._has_unmanaged_exposure(
        {("AAPLUSDT", "LONG"): {"amt": 1}},
        managed,
    )
    assert live_engine._has_unmanaged_exposure(
        {("AAPLUSDT", "LONG"): {"amt": 1.5}},
        managed,
    )
    assert live_engine._has_unmanaged_exposure(
        {("MSFTUSDT", "LONG"): {"amt": 1}},
        managed,
    )


def test_current_margin_prefers_exchange_initial_margin_and_falls_back_only_if_missing() -> None:
    positions = (
        {
            "initial_margin": "37.5",
            "notional": "1000",
            "leverage": 20,
        },
        {
            "initial_margin": None,
            "notional": "600",
            "leverage": 3,
        },
        {
            "initial_margin": 0,
            "notional": "500",
            "leverage": 5,
        },
    )

    assert live_engine._current_initial_margin(
        positions,
        fallback_leverage=10,
    ) == Decimal("337.5")


def test_historical_position_is_review_only_and_never_opened_or_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    position = {
        "symbol": "AAPLUSDT",
        "position_side": "LONG",
        "side": "long",
        "amt": 0.5,
        "entry_price": None,
        "liquidation_price": None,
        "notional": 50,
        "leverage": 20,
    }
    historical_open = {
        "id": 20,
        "symbol": "AAPLUSDT",
        "position_side": "LONG",
        "side": "BUY",
        "quantity": Decimal("0.5"),
        "entry_basis_json": None,
        "created_at": "2020-01-01 00:00:00",
    }
    snapshot = SimpleNamespace(
        account_type="UM_FUTURE",
        available_balance=Decimal("1000"),
        wallet_balance=Decimal("1000"),
        unrealized_pnl=Decimal("0"),
        positions=(position,),
    )
    account = _account()
    account["config_json"]["max_holding_bars"] = 12
    account["runtime_state_json"] = {}
    persisted: list[tuple[str, tuple]] = []

    monkeypatch.setattr(
        live_engine,
        "_account_service",
        SimpleNamespace(account=lambda *_args, **_kwargs: snapshot),
    )
    monkeypatch.setattr(live_engine, "_trading_client", _TradingClient())
    monkeypatch.setattr(live_engine, "_credentials", lambda *_args: ("key", "secret"))
    monkeypatch.setattr(live_engine, "_cached_position_mode", lambda *_args: "hedge")
    monkeypatch.setattr(live_engine, "_reconcile_intents", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(live_engine, "_strategy_universe", lambda *_args: ["AAPLUSDT"])
    monkeypatch.setattr(
        live_engine,
        "_managed_positions",
        lambda *_args: {("AAPLUSDT", "LONG"): historical_open},
    )
    monkeypatch.setattr(live_engine, "_protection_counts", lambda *_args: {})
    monkeypatch.setattr(live_engine, "_current_stop_prices", lambda *_args: {})
    monkeypatch.setattr(live_engine, "_cancel_orphan_protections", lambda *_args: None)
    monkeypatch.setattr(
        live_engine,
        "_strategy_signal",
        lambda *_args: (0, None, None, None, {}),
    )
    monkeypatch.setattr(
        live_engine,
        "_close_position",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("historical position must not be auto-closed")
        ),
    )
    monkeypatch.setattr(
        live_engine,
        "_open_position",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("review-required exposure must block new entries")
        ),
    )
    monkeypatch.setattr(
        live_engine,
        "_fail_account",
        lambda *_args: (_ for _ in ()).throw(
            AssertionError("grandfather compatibility must not fail the account")
        ),
    )
    monkeypatch.setattr(live_engine.store, "query", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(
        live_engine.store,
        "execute",
        lambda statement, params=(): persisted.append((statement, params)) or 1,
    )

    live_engine._tick_account(account)

    assert account["runtime_state_json"]["risk_review_required"] is True
    assert account["runtime_state_json"]["entry_block_reasons"] == [
        "risk_review_required"
    ]
    warning = account["runtime_state_json"]["risk_review_warnings"][0]
    assert warning["code"] == "historical_position_review_required"
    assert warning["protection_count"] == 0
    assert any("runtime_state_json" in statement for statement, _params in persisted)
    account_update = next(
        params for statement, params in persisted if "last_tick_at" in statement
    )
    assert account_update[0] == "risk_review_required"


@pytest.mark.parametrize("schema_version", [1, 2])
def test_captured_live_entry_basis_versions_do_not_require_risk_review(
    schema_version: int,
) -> None:
    positions = {
        ("TXNUSDT", "LONG"): {
            "symbol": "TXNUSDT",
            "position_side": "LONG",
            "amt": Decimal("0.33"),
        }
    }
    managed = {
        ("TXNUSDT", "LONG"): {
            "id": 17,
            "symbol": "TXNUSDT",
            "position_side": "LONG",
            "quantity": Decimal("0.33"),
            "entry_basis_json": {
                "schema_version": schema_version,
                "availability": "captured",
                "mode": "live",
            },
        }
    }

    assert live_engine._risk_review_warnings(
        positions,
        managed,
        {("TXNUSDT", "LONG"): 2},
    ) == []


@pytest.mark.parametrize("schema_version", [True, 0, 3, "2"])
def test_unknown_or_malformed_entry_basis_version_stays_review_only(
    schema_version: object,
) -> None:
    assert live_engine._is_grandfathered_open(
        {
            "entry_basis_json": {
                "schema_version": schema_version,
                "availability": "captured",
                "mode": "live",
            }
        }
    )


def test_rate_limit_backoff_grows_and_is_capped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = [1000.0]
    monkeypatch.setattr(live_engine.time, "monotonic", lambda: now[0])
    live_engine._clear_account_backoff(77)

    live_engine._record_account_backoff(77, "rate_limit")
    assert live_engine._account_backoff_active(77)
    assert live_engine._account_backoff[77] == (1060.0, 1)

    now[0] = 1061.0
    live_engine._record_account_backoff(77, "rate_limit")
    assert live_engine._account_backoff[77] == (1181.0, 2)

    for _ in range(10):
        live_engine._record_account_backoff(77, "rate_limit")
    assert live_engine._account_backoff[77][0] == now[0] + 300
    live_engine._clear_account_backoff(77)
    assert not live_engine._account_backoff_active(77)


def test_reconcile_recovers_unknown_market_and_canceled_protection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    updates: list[tuple[int, str, dict]] = []
    intents = [
        {
            "id": 10,
            "user_id": 2,
            "symbol": "AAPLUSDT",
            "action": "open",
            "status": "unknown",
            "client_order_id": "open-10",
        },
        {
            "id": 11,
            "user_id": 2,
            "symbol": "AAPLUSDT",
            "action": "stop",
            "status": "submitted",
            "client_order_id": "stop-11",
        },
    ]

    class Trading(_TradingClient):
        @staticmethod
        def query_order(*_args, **_kwargs) -> dict:
            return {"orderId": 100, "status": "FILLED"}

        @staticmethod
        def query_algo_order(*_args, **_kwargs) -> dict:
            return {"algoId": 101, "algoStatus": "CANCELLED"}

    monkeypatch.setattr(live_engine, "_account_service", _AccountService())
    monkeypatch.setattr(live_engine, "_trading_client", Trading())
    monkeypatch.setattr(live_engine.store, "query", lambda *_args, **_kwargs: intents)
    monkeypatch.setattr(
        live_engine,
        "_update_intent",
        lambda intent_id, _user_id, **kwargs: updates.append((intent_id, kwargs["status"], kwargs)),
    )

    live_engine._reconcile_intents(_account(), "key", "secret", force=True)

    assert [(intent_id, status) for intent_id, status, _ in updates] == [
        (10, "filled"),
        (11, "canceled"),
    ]


def test_reconcile_validates_open_algo_order_without_individual_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    updates: list[dict] = []
    orders = (
        {
            "order_id": "900",
            "client_order_id": "stop-12",
            "symbol": "AAPLUSDT",
            "side": "SELL",
            "position_side": "LONG",
            "type": "STOP_MARKET",
            "status": "NEW",
            "stop_price": 90.0,
            "conditional": True,
        },
    )

    class Trading(_TradingClient):
        @staticmethod
        def query_algo_order(*_args, **_kwargs) -> dict:
            raise AssertionError("an open order must not be queried individually")

    monkeypatch.setattr(live_engine, "_account_service", _AccountService(orders))
    monkeypatch.setattr(live_engine, "_trading_client", Trading())
    monkeypatch.setattr(
        live_engine.store,
        "query",
        lambda *_args, **_kwargs: [
            {
                "id": 12,
                "user_id": 2,
                "symbol": "AAPLUSDT",
                "action": "stop",
                "status": "submitted",
                "client_order_id": "stop-12",
            }
        ],
    )
    monkeypatch.setattr(
        live_engine,
        "_update_intent",
        lambda _intent_id, _user_id, **kwargs: updates.append(kwargs),
    )

    live_engine._reconcile_intents(_account(), "key", "secret", force=True)

    assert updates[0]["status"] == "submitted"
    assert updates[0]["response"]["algoId"] == "900"


def test_reconcile_filled_protection_requires_fresh_position_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    updates: list[dict] = []

    class Trading(_TradingClient):
        @staticmethod
        def query_algo_order(*_args, **_kwargs) -> dict:
            return {"algoId": 901, "algoStatus": "FINISHED"}

    monkeypatch.setattr(live_engine, "_account_service", _AccountService())
    monkeypatch.setattr(live_engine, "_trading_client", Trading())
    monkeypatch.setattr(
        live_engine.store,
        "query",
        lambda *_args, **_kwargs: [
            {
                "id": 13,
                "user_id": 2,
                "symbol": "AAPLUSDT",
                "action": "take_profit",
                "status": "submitted",
                "client_order_id": "take-profit-13",
            }
        ],
    )
    monkeypatch.setattr(
        live_engine,
        "_update_intent",
        lambda _intent_id, _user_id, **kwargs: updates.append(kwargs),
    )

    changed = live_engine._reconcile_intents(_account(), "key", "secret", force=True)

    assert changed is True
    assert updates[0]["status"] == "filled"


def test_protection_count_uses_current_open_generation_and_distinct_actions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    managed = {
        ("AAPLUSDT", "LONG"): {
            "id": 20,
            "symbol": "AAPLUSDT",
            "position_side": "LONG",
        }
    }
    monkeypatch.setattr(
        live_engine.store,
        "query",
        lambda *_args, **_kwargs: [
            {"id": 18, "symbol": "AAPLUSDT", "position_side": "LONG", "action": "stop"},
            {"id": 21, "symbol": "AAPLUSDT", "position_side": "LONG", "action": "stop"},
            {"id": 22, "symbol": "AAPLUSDT", "position_side": "LONG", "action": "stop"},
            {
                "id": 23,
                "symbol": "AAPLUSDT",
                "position_side": "LONG",
                "action": "take_profit",
            },
        ],
    )

    assert live_engine._protection_counts(_account(), managed) == {("AAPLUSDT", "LONG"): 2}


def test_missing_exchange_position_records_reconciled_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    canceled: list[tuple[str, str]] = []
    reconciled: list[int] = []
    managed = {
        ("AAPLUSDT", "LONG"): {
            "id": 20,
            "symbol": "AAPLUSDT",
            "position_side": "LONG",
            "side": "BUY",
            "quantity": Decimal("0.5"),
        }
    }
    snapshot = SimpleNamespace(
        account_type="UM_FUTURE",
        available_balance=Decimal("1000"),
        wallet_balance=Decimal("1000"),
        positions=(),
    )

    class AccountService:
        @staticmethod
        def account(*_args, **_kwargs):
            return snapshot

    class Trading(_TradingClient):
        @staticmethod
        def position_mode(*_args, **_kwargs) -> str:
            return "hedge"

    monkeypatch.setattr(live_engine, "_account_service", AccountService())
    monkeypatch.setattr(live_engine, "_trading_client", Trading())
    monkeypatch.setattr(live_engine, "_credentials", lambda *_args: ("key", "secret"))
    monkeypatch.setattr(live_engine, "_reconcile_intents", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(live_engine, "_strategy_universe", lambda *_args: ["AAPLUSDT"])
    monkeypatch.setattr(live_engine, "_managed_positions", lambda *_args: managed)
    monkeypatch.setattr(live_engine, "_protection_counts", lambda *_args: {})
    monkeypatch.setattr(live_engine, "_cancel_orphan_protections", lambda *_args: None)
    monkeypatch.setattr(live_engine, "_failed_close_keys", lambda *_args: set())
    monkeypatch.setattr(
        live_engine,
        "_cancel_protection",
        lambda _account, _key, _secret, symbol, side: canceled.append((symbol, side)),
    )
    monkeypatch.setattr(
        live_engine,
        "_record_reconciled_close",
        lambda _account, opened: reconciled.append(opened["id"]),
    )
    monkeypatch.setattr(live_engine, "_strategy_signal", lambda *_args: (0, None, None, None, {}))
    monkeypatch.setattr(live_engine.store, "query", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(live_engine.store, "execute", lambda *_args, **_kwargs: 1)

    live_engine._tick_account(_account())

    assert canceled == [("AAPLUSDT", "LONG")]
    assert reconciled == [20]


def test_reconciled_close_copies_the_original_entry_basis(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created: dict = {}
    updates: list[dict] = []
    monkeypatch.setattr(
        live_engine,
        "_create_intent",
        lambda *_args, **kwargs: (
            created.update(kwargs) or {"id": 31, "client_order_id": "reconciled-31"}
        ),
    )
    monkeypatch.setattr(
        live_engine,
        "_update_intent",
        lambda *_args, **kwargs: updates.append(kwargs),
    )

    live_engine._record_reconciled_close(
        _account(),
        {
            "id": 20,
            "symbol": "AAPLUSDT",
            "position_side": "LONG",
            "side": "BUY",
            "quantity": Decimal("0.5"),
            "strategy_signal_id": 91,
            "entry_basis_json": '{"schema_version":1,"reasons":["MA_BULLISH"]}',
        },
    )

    assert created["strategy_signal_id"] == 91
    assert created["entry_basis"]["reasons"] == ["MA_BULLISH"]
    assert updates[0]["status"] == "filled"


def test_execution_fence_blocks_market_and_algo_exchange_writes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    exchange_calls: list[str] = []
    updates: list[dict] = []

    class Trading(_TradingClient):
        @staticmethod
        def change_leverage(*_args, **_kwargs) -> dict:
            exchange_calls.append("leverage")
            return {}

        @staticmethod
        def place_market_order(*_args, **_kwargs) -> dict:
            exchange_calls.append("market")
            return {"status": "FILLED"}

        @staticmethod
        def place_close_trigger(*_args, **_kwargs) -> dict:
            exchange_calls.append("algo")
            return {"algoStatus": "NEW"}

    intent_ids = iter((30, 31))
    monkeypatch.setattr(live_engine, "_trading_client", Trading())
    monkeypatch.setattr(
        live_engine,
        "_create_intent",
        lambda *_args, **_kwargs: {
            "id": next(intent_ids),
            "client_order_id": "client-id",
        },
    )
    monkeypatch.setattr(live_engine, "_execution_enabled", lambda *_args: False)
    monkeypatch.setattr(live_engine, "_safety_write_enabled", lambda *_args: False)
    monkeypatch.setattr(
        live_engine,
        "_update_intent",
        lambda _intent_id, _user_id, **kwargs: updates.append(kwargs),
    )

    market = live_engine._place_market(
        _account(),
        "key",
        "secret",
        signal_key="open",
        symbol="AAPLUSDT",
        action="open",
        side="BUY",
        position_side="LONG",
        quantity=Decimal("0.5"),
        reduce_only=False,
        leverage=10,
    )
    protected = live_engine._place_protection(
        _account(),
        "key",
        "secret",
        symbol="AAPLUSDT",
        side="SELL",
        position_side="LONG",
        quantity=Decimal("0.5"),
        signal_time=123,
        stop=Decimal("90"),
        target=Decimal("110"),
    )

    assert market is None
    assert protected is False
    assert exchange_calls == []
    assert [item["error_code"] for item in updates] == [
        "execution_stopped",
        "execution_stopped",
    ]


def test_paused_account_can_still_send_risk_reducing_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    exchange_calls: list[str] = []

    class Trading(_TradingClient):
        @staticmethod
        def place_market_order(*_args, **_kwargs) -> dict:
            exchange_calls.append("close")
            return {"orderId": 99, "status": "FILLED"}

    monkeypatch.setattr(live_engine, "_trading_client", Trading())
    monkeypatch.setattr(
        live_engine,
        "_create_intent",
        lambda *_args, **_kwargs: {"id": 40, "client_order_id": "close-40"},
    )
    monkeypatch.setattr(
        live_engine,
        "_execution_enabled",
        lambda *_args: (_ for _ in ()).throw(AssertionError("entry fence used for close")),
    )
    monkeypatch.setattr(live_engine, "_safety_write_enabled", lambda *_args: True)
    monkeypatch.setattr(live_engine, "_update_intent", lambda *_args, **_kwargs: None)

    response = live_engine._place_market(
        _account(),
        "key",
        "secret",
        signal_key="close",
        symbol="AAPLUSDT",
        action="close",
        side="SELL",
        position_side="LONG",
        quantity=Decimal("0.5"),
        reduce_only=False,
    )

    assert response == {"orderId": 99, "status": "FILLED"}
    assert exchange_calls == ["close"]


def test_filled_audit_failure_still_returns_fill_and_installs_open_protection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    protected: list[dict] = []
    reconciliation: list[int] = []

    class Trading(_TradingClient):
        @staticmethod
        def place_market_order(*_args, **_kwargs) -> dict:
            return {
                "orderId": 99,
                "clientOrderId": "open-40",
                "status": "FILLED",
                "avgPrice": "100",
            }

    snapshot = SimpleNamespace(
        available_balance=Decimal("1000"),
        wallet_balance=Decimal("1000"),
        unrealized_pnl=Decimal("0"),
        positions=(),
    )
    refreshed = SimpleNamespace(
        positions=(
            {
                "symbol": "AAPLUSDT",
                "position_side": "LONG",
                "side": "long",
                "amt": 0.5,
                "entry_price": 100,
                "liquidation_price": 50,
            },
        )
    )
    account = _account()

    monkeypatch.setattr(live_engine, "_trading_client", Trading())
    monkeypatch.setattr(
        live_engine,
        "_account_service",
        SimpleNamespace(account=lambda *_args, **_kwargs: refreshed),
    )
    monkeypatch.setattr(
        live_engine,
        "_create_intent",
        lambda *_args, **_kwargs: {"id": 40, "client_order_id": "open-40"},
    )
    monkeypatch.setattr(live_engine, "_execution_enabled", lambda *_args: True)
    monkeypatch.setattr(
        live_engine,
        "_update_intent",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("deadlock")),
    )
    monkeypatch.setattr(
        live_engine,
        "_request_reconciliation",
        lambda account_id: reconciliation.append(account_id),
    )
    monkeypatch.setattr(live_engine, "_exit_levels", lambda *_args: (90.0, 110.0))
    monkeypatch.setattr(
        live_engine,
        "_place_protection",
        lambda *_args, **kwargs: protected.append(kwargs) or True,
    )
    monkeypatch.setattr(live_engine.store, "query", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(live_engine.store, "execute", lambda *_args, **_kwargs: 1)

    live_engine._open_position(
        account,
        "key",
        "secret",
        snapshot,
        symbol="AAPLUSDT",
        direction=1,
        price=100.0,
        atr=None,
        signal_time=123,
    )

    assert reconciliation == [1]
    assert account["_local_audit_pending"] is True
    assert protected[0]["stop"] == Decimal("90.0")
    assert protected[0]["target"] == Decimal("110.0")


def test_error_account_recovery_closes_filled_open_without_protection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    closed: list[str] = []
    failed: list[str] = []
    position = {
        "symbol": "AAPLUSDT",
        "position_side": "LONG",
        "side": "long",
        "amt": 0.5,
    }
    snapshot = SimpleNamespace(account_type="UM_FUTURE", positions=(position,))

    class AccountService:
        @staticmethod
        def account(*_args, **_kwargs):
            return snapshot

    monkeypatch.setattr(live_engine, "_account_service", AccountService())
    monkeypatch.setattr(live_engine, "_credentials", lambda *_args: ("key", "secret"))
    monkeypatch.setattr(live_engine, "_reconcile_intents", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        live_engine,
        "_managed_positions",
        lambda *_args: {
            ("AAPLUSDT", "LONG"): {
                "id": 50,
                "symbol": "AAPLUSDT",
                "position_side": "LONG",
                "entry_basis_json": {
                    "schema_version": 1,
                    "availability": "captured",
                    "mode": "live",
                },
            }
        },
    )
    monkeypatch.setattr(live_engine, "_protection_counts", lambda *_args: {})
    monkeypatch.setattr(live_engine, "_failed_close_keys", lambda *_args: set())
    monkeypatch.setattr(live_engine, "_cancel_orphan_protections", lambda *_args: None)
    monkeypatch.setattr(
        live_engine,
        "_close_position",
        lambda _account, _key, _secret, _position, reason: closed.append(reason),
    )
    monkeypatch.setattr(
        live_engine,
        "_fail_account",
        lambda _account, reason: failed.append(reason),
    )

    live_engine._recover_account(_account())

    assert closed == ["recovery_protection_missing"]
    assert failed == ["recovery_protection_missing"]


def test_recovery_keeps_historical_position_and_records_review_warning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    position = {
        "symbol": "AAPLUSDT",
        "position_side": "LONG",
        "side": "long",
        "amt": 0.5,
    }
    snapshot = SimpleNamespace(account_type="UM_FUTURE", positions=(position,))
    account = _account()
    account["runtime_state_json"] = {}

    monkeypatch.setattr(
        live_engine,
        "_account_service",
        SimpleNamespace(account=lambda *_args, **_kwargs: snapshot),
    )
    monkeypatch.setattr(live_engine, "_credentials", lambda *_args: ("key", "secret"))
    monkeypatch.setattr(live_engine, "_reconcile_intents", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(
        live_engine,
        "_managed_positions",
        lambda *_args: {
            ("AAPLUSDT", "LONG"): {
                "id": 50,
                "symbol": "AAPLUSDT",
                "position_side": "LONG",
                "quantity": Decimal("0.5"),
                "entry_basis_json": None,
            }
        },
    )
    monkeypatch.setattr(live_engine, "_protection_counts", lambda *_args: {})
    monkeypatch.setattr(live_engine, "_failed_close_keys", lambda *_args: set())
    monkeypatch.setattr(live_engine, "_cancel_orphan_protections", lambda *_args: None)
    monkeypatch.setattr(
        live_engine,
        "_close_position",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("historical recovery must not market-close exposure")
        ),
    )
    monkeypatch.setattr(live_engine.store, "execute", lambda *_args, **_kwargs: 1)

    live_engine._recover_account(account)

    assert account["runtime_state_json"]["risk_review_required"] is True
    assert account["runtime_state_json"]["risk_review_warnings"][0]["code"] == (
        "historical_position_review_required"
    )


@pytest.mark.parametrize("timeout_then_query", [False, True])
def test_protection_rejects_terminal_post_and_query_status(
    monkeypatch: pytest.MonkeyPatch,
    timeout_then_query: bool,
) -> None:
    updates: list[dict] = []

    class Trading(_TradingClient):
        @staticmethod
        def place_close_trigger(*_args, **_kwargs) -> dict:
            if timeout_then_query:
                raise BinanceAccountClientError("timeout")
            return {"algoId": 70, "algoStatus": "REJECTED"}

        @staticmethod
        def query_algo_order(*_args, **_kwargs) -> dict:
            return {"algoId": 70, "algoStatus": "REJECTED"}

    monkeypatch.setattr(live_engine, "_trading_client", Trading())
    monkeypatch.setattr(
        live_engine,
        "_create_intent",
        lambda *_args, **_kwargs: {"id": 70, "client_order_id": "protect-70"},
    )
    monkeypatch.setattr(live_engine, "_safety_write_enabled", lambda *_args: True)
    monkeypatch.setattr(
        live_engine,
        "_update_intent",
        lambda _intent_id, _user_id, **kwargs: updates.append(kwargs),
    )

    result = live_engine._place_protection(
        _account(),
        "key",
        "secret",
        symbol="AAPLUSDT",
        side="SELL",
        position_side="LONG",
        quantity=Decimal("0.5"),
        signal_time=123,
        stop=Decimal("90"),
        target=Decimal("110"),
    )

    assert result is False
    assert updates[-1]["status"] == "rejected"
    assert all(update["status"] != "submitted" for update in updates)


def test_protection_rollback_failure_is_unknown_and_alerted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    updates: list[tuple[int, dict]] = []
    failures: list[str] = []
    responses = iter(
        (
            {"algoId": 80, "algoStatus": "NEW"},
            {"algoId": 81, "algoStatus": "REJECTED"},
        )
    )
    intent_ids = iter((80, 81))

    class Trading(_TradingClient):
        @staticmethod
        def place_close_trigger(*_args, **_kwargs) -> dict:
            return next(responses)

        @staticmethod
        def cancel_algo_order(*_args, **_kwargs) -> dict:
            raise BinanceAccountClientError("network")

    monkeypatch.setattr(live_engine, "_trading_client", Trading())
    monkeypatch.setattr(
        live_engine,
        "_create_intent",
        lambda *_args, **_kwargs: {
            "id": (intent_id := next(intent_ids)),
            "client_order_id": f"protect-{intent_id}",
        },
    )
    monkeypatch.setattr(live_engine, "_safety_write_enabled", lambda *_args: True)
    monkeypatch.setattr(
        live_engine,
        "_update_intent",
        lambda intent_id, _user_id, **kwargs: updates.append((intent_id, kwargs)),
    )
    monkeypatch.setattr(
        live_engine,
        "_fail_account",
        lambda _account, reason: failures.append(reason),
    )

    result = live_engine._place_protection(
        _account(),
        "key",
        "secret",
        symbol="AAPLUSDT",
        side="SELL",
        position_side="LONG",
        quantity=Decimal("0.5"),
        signal_time=123,
        stop=Decimal("90"),
        target=Decimal("110"),
    )

    assert result is False
    assert any(intent_id == 80 and item["status"] == "unknown" for intent_id, item in updates)
    assert failures == ["protective_cancel_failed"]


def test_submitted_market_reconciliation_keeps_account_fenced(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    account = {**_account(), "id": 99}
    updates: list[dict] = []
    orders = (
        {
            "order_id": "900",
            "client_order_id": "open-900",
            "symbol": "AAPLUSDT",
            "side": "BUY",
            "position_side": "LONG",
            "type": "MARKET",
            "status": "NEW",
            "average_price": 0,
            "conditional": False,
        },
    )
    monkeypatch.setattr(live_engine, "_account_service", _AccountService(orders))
    monkeypatch.setattr(live_engine, "_trading_client", _TradingClient())
    monkeypatch.setattr(
        live_engine.store,
        "query",
        lambda *_args, **_kwargs: [
            {
                "id": 90,
                "user_id": 2,
                "symbol": "AAPLUSDT",
                "action": "open",
                "status": "unknown",
                "client_order_id": "open-900",
            }
        ],
    )
    monkeypatch.setattr(
        live_engine,
        "_update_intent",
        lambda _intent_id, _user_id, **kwargs: updates.append(kwargs),
    )

    with pytest.raises(BinanceAccountClientError) as captured:
        live_engine._reconcile_intents(account, "key", "secret", force=True)

    assert captured.value.category == "order_state_pending"
    assert updates[-1]["status"] == "submitted"
    live_engine._finish_reconciliation(99, successful=True)


def test_pending_close_prevents_duplicate_market_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(live_engine, "_trading_client", _TradingClient())
    monkeypatch.setattr(
        live_engine,
        "_managed_open",
        lambda *_args: {"quantity": Decimal("0.5")},
    )
    monkeypatch.setattr(live_engine, "_pending_market_intent", lambda *_args: True)
    monkeypatch.setattr(
        live_engine,
        "_place_market",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("duplicate close")),
    )

    result = live_engine._close_position(
        _account(),
        "key",
        "secret",
        {
            "symbol": "AAPLUSDT",
            "position_side": "LONG",
            "side": "long",
            "amt": 0.5,
        },
        "recovery",
    )

    assert result is False


def test_orphan_protection_is_canceled_without_managed_generation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    canceled: list[tuple[str, str]] = []
    monkeypatch.setattr(
        live_engine.store,
        "query",
        lambda *_args, **_kwargs: [
            {"symbol": "AAPLUSDT", "position_side": "LONG"},
            {"symbol": "MSFTUSDT", "position_side": "SHORT"},
        ],
    )
    monkeypatch.setattr(
        live_engine,
        "_cancel_protection",
        lambda _account, _key, _secret, symbol, side: canceled.append((symbol, side)),
    )

    live_engine._cancel_orphan_protections(
        _account(),
        "key",
        "secret",
        {("AAPLUSDT", "LONG"): {"id": 1}},
    )

    assert canceled == [("MSFTUSDT", "SHORT")]


def test_preexisting_exchange_position_keeps_its_orphaned_local_protection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    canceled: list[tuple[str, str]] = []
    monkeypatch.setattr(
        live_engine.store,
        "query",
        lambda *_args, **_kwargs: [
            {"symbol": "AAPLUSDT", "position_side": "LONG"},
        ],
    )
    monkeypatch.setattr(
        live_engine,
        "_cancel_protection",
        lambda _account, _key, _secret, symbol, side: canceled.append((symbol, side)),
    )

    live_engine._cancel_orphan_protections(
        _account(),
        "key",
        "secret",
        {},
        {("AAPLUSDT", "LONG")},
    )

    assert canceled == []
    warnings = live_engine._risk_review_warnings(
        {("AAPLUSDT", "LONG"): {"amt": 0.5}},
        {},
    )
    assert warnings[0]["code"] == "unmanaged_exchange_position"


def test_recovery_refreshes_position_snapshot_after_market_state_change(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshots = iter(
        (
            SimpleNamespace(account_type="UM_FUTURE", positions=()),
            SimpleNamespace(
                account_type="UM_FUTURE",
                positions=(
                    {
                        "symbol": "AAPLUSDT",
                        "position_side": "LONG",
                        "side": "long",
                        "amt": 0.5,
                    },
                ),
            ),
        )
    )
    closed: list[str] = []

    class AccountService:
        @staticmethod
        def account(*_args, **_kwargs):
            return next(snapshots)

    monkeypatch.setattr(live_engine, "_account_service", AccountService())
    monkeypatch.setattr(live_engine, "_credentials", lambda *_args: ("key", "secret"))
    monkeypatch.setattr(live_engine, "_reconcile_intents", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(
        live_engine,
        "_managed_positions",
        lambda *_args: {
            ("AAPLUSDT", "LONG"): {
                "id": 100,
                "symbol": "AAPLUSDT",
                "position_side": "LONG",
                "entry_basis_json": {
                    "schema_version": 1,
                    "availability": "captured",
                    "mode": "live",
                },
            }
        },
    )
    monkeypatch.setattr(live_engine, "_cancel_orphan_protections", lambda *_args: None)
    monkeypatch.setattr(live_engine, "_protection_counts", lambda *_args: {})
    monkeypatch.setattr(live_engine, "_failed_close_keys", lambda *_args: set())
    monkeypatch.setattr(
        live_engine,
        "_close_position",
        lambda _account, _key, _secret, _position, reason: closed.append(reason),
    )
    monkeypatch.setattr(live_engine, "_fail_account", lambda *_args: None)

    live_engine._recover_account(_account())

    assert closed == ["recovery_protection_missing"]
