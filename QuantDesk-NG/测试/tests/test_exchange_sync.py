from __future__ import annotations

from datetime import UTC, datetime

import pytest

from quantdesk import exchange_sync, paper


def test_trading_schedule_fails_closed_and_maps_premarket_to_equity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now_ms = int(datetime(2026, 8, 4, 12, 0, tzinfo=UTC).timestamp() * 1000)
    monkeypatch.setattr(
        exchange_sync.store,
        "system_state_get",
        lambda key, default: {
            "fetched_at_ms": now_ms,
            "payload": {
                "marketSchedules": {
                    "EQUITY": {
                        "sessions": [
                            {
                                "startTime": now_ms - 1_000,
                                "endTime": now_ms + 1_000,
                                "type": "REGULAR",
                            }
                        ]
                    }
                }
            },
        },
    )

    assert exchange_sync.trading_session_open({"underlying_type": "PREMARKET"}, now_ms)
    assert not exchange_sync.trading_session_open(
        {"underlying_type": "PREMARKET"}, now_ms + 2_000
    )


def test_isolated_liquidation_uses_binance_bracket_mmr_and_cum() -> None:
    bracket = {"maint_margin_ratio": 0.01, "cum": 25}

    long_price = paper._isolated_liquidation_price(100, 10, 200, 1, bracket)
    short_price = paper._isolated_liquidation_price(100, 10, 200, -1, bracket)

    assert long_price == pytest.approx((1_000 - 200 - 25) / 9.9)
    assert short_price == pytest.approx((200 + 1_000 + 25) / 10.1)


def test_synced_open_uses_book_taker_fee_and_market_lot_step(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    account = {
        "id": 11,
        "public_id": "paper-id",
        "user_id": 7,
        "name": "strict",
        "balance": 10_000.0,
        "config_json": {
            "leverage": 20,
            "max_positions": 5,
            "margin_cap": 0.8,
            "position_size_pct": 10,
            "fee_bps": 99,
            "slippage_bps": 8,
            "stop_loss_pct": 3,
            "take_profit_pct": 5,
        },
        "strategy_snapshot_json": {"name": "test"},
    }
    environment = {
        "rule": {
            "tick_size": "0.01",
            "market_step_size": "0.01",
            "market_min_qty": "0.01",
            "min_notional": "5",
            "mark_price": "100",
            "rule_updated_at_ms": 123,
        },
        "commission": {"taker_rate": "0.0004"},
        "brackets": [
            {
                "initial_leverage": 5,
                "notional_floor": 0,
                "notional_cap": 100_000,
                "maint_margin_ratio": 0.01,
                "cum": 0,
            }
        ],
    }
    writes: list[tuple[str, tuple]] = []
    monkeypatch.setattr(paper, "_prices", lambda: {"TESTUSDT": 100.0})
    monkeypatch.setattr(paper, "_equity", lambda *args: (10_000.0, 0.0))
    monkeypatch.setattr(paper, "_set_balance", lambda account, balance: account.update(balance=balance))
    monkeypatch.setattr(
        paper.exchange_sync, "execution_readiness", lambda user_id, symbol: (environment, None)
    )
    monkeypatch.setattr(
        paper.store, "execute", lambda sql, params=(): writes.append((sql, tuple(params))) or 1
    )
    monkeypatch.setattr(paper.store, "user_state_set", lambda *args: None)
    monkeypatch.setattr(paper.store, "add_alert", lambda *args, **kwargs: None)

    opened = paper._open_position(
        account,
        "TESTUSDT",
        1,
        100,
        2,
        ["signal"],
        [],
        1_000,
        {"TESTUSDT": {"bid": 99.99, "ask": 100.01, "bid_qty": 100, "ask_qty": 100}},
    )

    assert opened
    sql, params = next(
        (sql, params) for sql, params in writes if "INSERT INTO paper_positions" in sql
    )
    assert sql.count("?") == len(params)
    assert params[4] == pytest.approx(49.99)
    assert params[5] == pytest.approx(100.01)
    assert params[-4] == "binance_synced_v2"
    assert params[-3] == pytest.approx(49.99 * 100.01 * 0.0004)
