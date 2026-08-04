from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime

import pytest

from quantdesk_v2 import paper_engine as paper
from quantdesk_v2.strategy_catalog import SYSTEM_STRATEGY_DEFINITIONS
from quantdesk_v2.strategy_runtime import StrategyDecision, build_trend_pullback_spec


def _account() -> dict:
    return {
        "id": 11,
        "public_id": "00000000-0000-0000-0000-000000000011",
        "user_id": 7,
        "name": "ATR 模拟盘",
        "status": "active",
        "initial_balance": 10_000.0,
        "balance": 10_000.0,
        "started_at": datetime(2026, 8, 3, tzinfo=UTC),
        "config_json": {
            "leverage": 20,
            "max_positions": 15,
            "margin_cap": 0.8,
            "position_size_pct": 10,
            "fee_bps": 5,
            "slippage_bps": 3,
            "stop_loss_pct": 3,
            "take_profit_pct": 5,
            "max_holding_bars": 12,
        },
        "strategy_snapshot_json": {
            "public_id": "00000000-0000-0000-0000-000000000019",
            "name": "AI 模拟盘 ATR 趋势",
            "engine_key": "multi_factor",
            "parameters": {
                "fast_period": 20,
                "slow_period": 50,
                "rsi_period": 14,
                "threshold": 3,
            },
        },
    }


@pytest.mark.parametrize(
    ("side", "expected_stop", "expected_target"),
    [(1, 97.0, 105.0), (-1, 103.0, 95.0)],
)
def test_exit_levels_use_symmetric_atr_stop_and_take_profit(
    side: int, expected_stop: float, expected_target: float
) -> None:
    config = paper._config(_account())

    stop, target = paper._exit_levels(100, side, 2, config)

    assert stop == pytest.approx(expected_stop)
    assert target == pytest.approx(expected_target)
    assert (100 - stop) * side > 0
    assert (target - 100) * side > 0


def test_exit_levels_fall_back_to_configured_percentages_without_atr() -> None:
    config = paper._config(_account())

    stop, target = paper._exit_levels(100, 1, None, config)

    assert stop == pytest.approx(97)
    assert target == pytest.approx(105)


def test_full_strategy_signal_reads_declared_timeframes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    account = _account()
    account["strategy_snapshot_json"] = {
        "name": "多周期趋势回踩延续",
        "strategy_kind": "full_strategy",
        "spec": build_trend_pullback_spec(),
    }
    requested: list[tuple[str, str, int]] = []

    def get_klines(symbol: str, timeframe: str, limit: int) -> list[dict]:
        requested.append((symbol, timeframe, limit))
        return [{"open_time": 100}]

    monkeypatch.setattr(paper.store, "get_klines", get_klines)
    recorded: list[tuple[str, str]] = []
    monkeypatch.setattr(
        paper,
        "_record_full_strategy_decision",
        lambda account, symbol, spec, decision: recorded.append(
            (symbol, decision.decision)
        )
        or True,
    )
    monkeypatch.setattr(
        paper,
        "evaluate_strategy",
        lambda spec, market: StrategyDecision(
            decision="LONG_ENTRY",
            signal_time=100,
            valid_until=200,
            confidence=0.75,
            reason_codes=("REGIME_UP", "BREAKOUT_UP"),
            evidence={"setup": {"atr": 2.5}},
            risk_proposal={},
        ),
    )

    direction, atr, basis, signal_time = paper._strategy_signal(account, "TESTUSDT")

    assert set(requested) == {
        ("TESTUSDT", "4h", 600),
        ("TESTUSDT", "1h", 600),
        ("TESTUSDT", "15m", 600),
    }
    assert direction == 1
    assert atr == pytest.approx(2.5)
    assert signal_time == 100
    assert "类型：完整策略" in basis
    assert "依据：REGIME_UP / BREAKOUT_UP" in basis
    assert recorded == [("TESTUSDT", "LONG_ENTRY")]


def test_full_strategy_signal_persistence_is_tenant_scoped_and_idempotent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    account = _account()
    writes: list[tuple[str, tuple]] = []
    def query(sql: str, params: tuple = ()) -> list[dict]:
        del params
        if "strategy_deployments" in sql:
            return [{"id": 51, "strategy_revision_id": 61}]
        if "market_opportunities" in sql:
            return [{"id": 71}]
        return []

    monkeypatch.setattr(paper.store, "query", query)
    monkeypatch.setattr(
        paper.store,
        "execute",
        lambda sql, params=(): writes.append((sql, tuple(params))) or 1,
    )
    decision = StrategyDecision(
        decision="LONG_ENTRY",
        signal_time=1_700_000_000_000,
        valid_until=1_700_001_800_000,
        confidence=0.8,
        reason_codes=("REGIME_UP",),
        evidence={"setup": {"atr": 2.0}},
        risk_proposal={"stop_distance": 3.0},
    )

    persisted = paper._record_full_strategy_decision(
        account, "TESTUSDT", build_trend_pullback_spec(), decision
    )

    assert persisted is True
    insert_sql, insert_params = writes[0]
    assert "INSERT IGNORE INTO strategy_signals" in insert_sql
    assert insert_params[1:4] == (account["user_id"], 51, 61)
    assert insert_params[4] == 71
    assert insert_params[10] == 1_700_001_800
    assert insert_params[-1].startswith("paper:51:61:TESTUSDT:15m:")
    assert writes[1][1] == (decision.signal_time, 51, account["user_id"])


def test_short_exit_levels_never_return_a_nonpositive_target() -> None:
    account = _account()
    account["config_json"]["take_profit_pct"] = 500
    config = paper._config(account)

    stop, target = paper._exit_levels(100, -1, 100, config)

    assert stop is not None and stop > 100
    assert target == pytest.approx(0.1)
    assert (target - 100) * -1 > 0


@pytest.mark.parametrize("side", [1, -1])
def test_open_position_always_persists_take_profit(
    monkeypatch: pytest.MonkeyPatch, side: int
) -> None:
    account = _account()
    writes: list[tuple[str, tuple]] = []
    monkeypatch.setattr(paper, "_prices", lambda: {"TESTUSDT": 100.0})
    monkeypatch.setattr(
        paper, "_equity", lambda account, prices, positions: (10_000.0, 0.0)
    )
    monkeypatch.setattr(
        paper,
        "_set_balance",
        lambda account, balance: account.update(balance=balance),
    )
    monkeypatch.setattr(
        paper.store,
        "execute",
        lambda sql, params=(): writes.append((sql, tuple(params))),
    )

    opened = paper._open_position(
        account, "TESTUSDT", side, 100.0, 2.0, ["4h signal"], [], 1_000
    )

    assert opened is True
    insert_params = next(params for sql, params in writes if "INSERT INTO paper_positions" in sql)
    entry, stop, target = insert_params[5], insert_params[8], insert_params[9]
    assert (entry - stop) * side > 0
    assert (target - entry) * side > 0
    assert target == pytest.approx(entry + side * 5)


@pytest.mark.parametrize(
    ("side", "market_price", "expected_target"),
    [(1, 106.0, 105.0), (-1, 94.0, 95.0)],
)
def test_zero_target_is_repaired_and_closes_on_same_tick(
    monkeypatch: pytest.MonkeyPatch,
    side: int,
    market_price: float,
    expected_target: float,
) -> None:
    account = _account()
    position = {
        "id": 31,
        "paper_account_id": account["id"],
        "user_id": account["user_id"],
        "symbol": "TESTUSDT",
        "side": side,
        "qty": 10.0,
        "avg_entry": 100.0,
        "margin": 50.0,
        "stop": 97.0 if side > 0 else 103.0,
        "target": 0,
        "atr_entry": 2.0,
        "liq_price": None,
        "funding_acc": 0,
        "opened_ts": 900,
    }
    writes: list[tuple[str, tuple]] = []
    closes: list[tuple[float, str]] = []
    monkeypatch.setattr(paper, "_positions", lambda account: [position])
    monkeypatch.setattr(
        paper, "_strategy_signal", lambda account, symbol: (0, 2.0, [], 800)
    )
    monkeypatch.setattr(paper, "tradfi_symbols", lambda: [])
    monkeypatch.setattr(
        paper.store,
        "execute",
        lambda sql, params=(): writes.append((sql, tuple(params))),
    )
    monkeypatch.setattr(
        paper,
        "_close_position",
        lambda account, position, price, reason, now: closes.append((price, reason)),
    )

    paper._tick_account(account, {"TESTUSDT": market_price}, 1_000)

    assert position["target"] == pytest.approx(expected_target)
    update_params = next(params for sql, params in writes if "SET target" in sql)
    assert update_params == (expected_target, 31, 11, 7)
    assert closes == [(market_price, "take_profit")]


def test_signal_failure_cannot_block_existing_take_profit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    account = _account()
    position = {
        "id": 32,
        "symbol": "TESTUSDT",
        "side": 1,
        "qty": 10,
        "avg_entry": 100,
        "margin": 50,
        "stop": 97,
        "target": 105,
        "atr_entry": 2,
        "liq_price": None,
        "funding_acc": 0,
        "opened_ts": 900,
    }
    closes: list[str] = []
    monkeypatch.setattr(paper, "_positions", lambda account: [position])
    monkeypatch.setattr(
        paper,
        "_strategy_signal",
        lambda account, symbol: (_ for _ in ()).throw(ValueError("bad candle")),
    )
    monkeypatch.setattr(paper, "tradfi_symbols", lambda: [])
    monkeypatch.setattr(paper.store, "execute", lambda sql, params=(): 1)
    monkeypatch.setattr(
        paper,
        "_close_position",
        lambda account, position, price, reason, now: closes.append(reason) or True,
    )

    paper._tick_account(account, {"TESTUSDT": 106}, 1_000)

    assert closes == ["take_profit"]


def test_take_profit_does_not_reopen_the_same_symbol_on_the_same_tick(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    account = _account()
    position = {
        "id": 33,
        "symbol": "TESTUSDT",
        "side": 1,
        "qty": 10,
        "avg_entry": 100,
        "margin": 50,
        "stop": 97,
        "target": 105,
        "atr_entry": 2,
        "liq_price": None,
        "funding_acc": 0,
        "opened_ts": 900,
    }
    state = {"closed": False, "open_calls": 0}
    monkeypatch.setattr(
        paper, "_positions", lambda account: [] if state["closed"] else [position]
    )
    monkeypatch.setattr(paper, "tradfi_symbols", lambda: ["TESTUSDT"])
    monkeypatch.setattr(paper.store, "execute", lambda sql, params=(): 1)

    def close(*args) -> bool:
        state["closed"] = True
        return True

    def open_position(*args) -> bool:
        state["open_calls"] += 1
        return True

    monkeypatch.setattr(paper, "_close_position", close)
    monkeypatch.setattr(paper, "_open_position", open_position)

    paper._tick_account(account, {"TESTUSDT": 106}, 1_000)

    assert state == {"closed": True, "open_calls": 0}


def test_close_position_requires_atomic_row_ownership(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    account = _account()
    position = {
        "id": 34,
        "symbol": "TESTUSDT",
        "side": 1,
        "qty": 10,
        "avg_entry": 100,
        "margin": 50,
        "funding_acc": 0,
        "open_score": 100,
        "opened_ts": 900,
    }
    statements: list[str] = []

    class LostOwnershipTransaction:
        @staticmethod
        def query(sql, params=()):
            return [{"balance": 10_000}]

        @staticmethod
        def execute(sql, params=()):
            statements.append(sql)
            return 0 if "DELETE FROM paper_positions" in sql else 1

    @contextmanager
    def transaction():
        yield LostOwnershipTransaction()

    monkeypatch.setattr(paper.store, "transaction", transaction)
    monkeypatch.setattr(
        paper.store,
        "add_alert",
        lambda *args, **kwargs: pytest.fail("lost close ownership must not alert"),
    )

    closed = paper._close_position(account, position, 105, "take_profit", 1_000)

    assert closed is False
    assert len(statements) == 1
    assert "DELETE FROM paper_positions" in statements[0]


def test_system_default_contains_the_fixed_paper_strategy() -> None:
    strategy = next(
        item
        for item in SYSTEM_STRATEGY_DEFINITIONS
        if item["template_key"] == "paper_multifactor_atr_v1"
    )

    assert strategy["name"] == "AI 模拟盘 ATR 趋势"
    assert strategy["version"] == 2
    threshold = next(
        item for item in strategy["parameter_schema_json"] if item["key"] == "threshold"
    )
    assert threshold["default"] == 3
    assert strategy["risk_defaults_json"]["take_profit_pct"] == 5
    assert "2.5×ATR" in strategy["description"]


def test_api_rules_publish_atr_take_profit(monkeypatch: pytest.MonkeyPatch) -> None:
    account = _account()
    monkeypatch.setattr(paper, "_account", lambda user_id, account_id: account)
    monkeypatch.setattr(paper, "_prices", lambda: {})
    monkeypatch.setattr(paper, "_positions", lambda account: [])
    monkeypatch.setattr(paper.store, "query", lambda sql, params=(): [])

    data = paper.api_data(account["user_id"], account["id"])

    assert "2.5×ATR 止盈" in data["rules"]["exits"]


def test_today_pnl_uses_tenant_account_equity_before_local_midnight(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    account = _account()
    now = int(datetime(2026, 8, 4, 12, 0, tzinfo=UTC).timestamp())
    expected_day_start = int(datetime(2026, 8, 3, 16, 0, tzinfo=UTC).timestamp())
    captured: list[tuple[str, tuple]] = []

    def query(sql: str, params=()):
        captured.append((sql, tuple(params)))
        return [{"equity": 10_300.25}]

    monkeypatch.setattr(paper.store, "query", query)

    result = paper._today_pnl(account, 10_425.75, now, 480)

    assert result == pytest.approx(125.5)
    assert captured[0][1] == (
        account["id"],
        account["user_id"],
        expected_day_start,
    )
    assert "paper_account_id=? AND user_id=?" in captured[0][0]
    assert "ts<?" in captured[0][0]


def test_today_pnl_uses_initial_balance_when_account_has_no_prior_day_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    account = _account()
    monkeypatch.setattr(paper.store, "query", lambda sql, params=(): [])

    result = paper._today_pnl(account, 9_875.5, 1_722_772_800, 0)

    assert result == pytest.approx(-124.5)


def test_paused_accounts_keep_recording_equity_without_running_strategy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    active = {**_account(), "id": 11, "status": "active"}
    paused = {**_account(), "id": 12, "status": "paused"}
    ticked: list[int] = []
    recorded: list[int] = []

    @contextmanager
    def advisory_lock(*args, **kwargs):
        yield True

    monkeypatch.setattr(paper.store, "advisory_lock", advisory_lock)
    monkeypatch.setattr(paper, "_prices", lambda: {"TESTUSDT": 100.0})
    monkeypatch.setattr(paper, "_tracked_accounts", lambda account_id=None: [active, paused])
    monkeypatch.setattr(paper, "_positions", lambda account: [])
    monkeypatch.setattr(
        paper, "_tick_account", lambda account, prices, now: ticked.append(account["id"])
    )
    monkeypatch.setattr(
        paper,
        "_record_equity",
        lambda account, prices, positions, now: recorded.append(account["id"]),
    )

    paper.tick()

    assert ticked == [11]
    assert recorded == [12]
