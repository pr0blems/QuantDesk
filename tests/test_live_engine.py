from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

import pytest

from quantdesk_v2 import live_engine


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

    assert market["side"] == "BUY"
    assert market["position_side"] == "LONG"
    assert market["reduce_only"] is False
    assert protection["side"] == "SELL"
    assert protection["position_side"] == "LONG"
    assert protection["quantity"] == market["quantity"]


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
