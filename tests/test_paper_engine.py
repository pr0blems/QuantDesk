from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest

from quantdesk_v2 import paper_engine as paper
from quantdesk_v2.domain.runtime import build_decision_envelope
from quantdesk_v2.strategy_catalog import SYSTEM_STRATEGY_DEFINITIONS
from quantdesk_v2.strategy_runtime import StrategyDecision, build_trend_pullback_spec


def _account() -> dict:
    return {
        "id": 11,
        "public_id": "00000000-0000-0000-0000-000000000011",
        "user_id": 7,
        "deployment_id": 51,
        "strategy_revision_id": 61,
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
            "funding_rate_8h_bps": 1,
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


def _fresh_prices(symbol: str, price: float, timestamp: int) -> paper._PriceSnapshot:
    snapshot = paper._PriceSnapshot()
    snapshot[symbol] = price
    snapshot.timestamps[symbol] = timestamp
    return snapshot


def _mock_open_transaction(
    monkeypatch: pytest.MonkeyPatch,
    account: dict,
    writes: list[tuple[str, tuple]],
    *,
    fail_on_insert: bool = False,
) -> dict[str, bool]:
    state = {"rolled_back": False}

    class FakeTransaction:
        def query(self, sql: str, params=()):
            raise AssertionError(f"fill append must not read a projection table: {sql}")

        def execute(self, sql: str, params=()):
            writes.append((sql, tuple(params)))
            if fail_on_insert and "INSERT INTO paper_order_executions" in sql:
                raise RuntimeError("simulated fill append failure")
            return 1

    @contextmanager
    def transaction():
        try:
            yield FakeTransaction()
        except Exception:
            state["rolled_back"] = True
            raise

    monkeypatch.setattr(paper.store, "transaction", transaction)
    monkeypatch.setattr(paper.store, "add_alert", lambda *args, **kwargs: None)
    monkeypatch.setattr(paper, "_reconcile_paper_account", lambda target: True)
    _mock_execution(monkeypatch)
    return state


def _mock_execution(monkeypatch: pytest.MonkeyPatch) -> None:
    def execute(account, positions, intent, submit):
        del account, positions
        try:
            submit(
                paper.MarketOrder(
                    symbol=intent.symbol,
                    side=intent.side,
                    quantity=intent.quantity,
                    client_order_id="paper-test-order",
                    position_side=intent.position_side,
                    reduce_only=intent.reduce_only,
                )
            )
        except paper.BrokerError as exc:
            return SimpleNamespace(
                state=paper.ExecutionState.BROKER_REJECTED,
                error_code=f"broker_{exc.category}",
            )
        return SimpleNamespace(state=paper.ExecutionState.FILLED, error_code=None)

    monkeypatch.setattr(paper, "_paper_execute", execute)


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


@pytest.mark.parametrize(("side", "expected"), [(1, 0.04), (-1, -0.04)])
def test_estimated_funding_accrues_on_utc_boundaries_once(
    monkeypatch: pytest.MonkeyPatch, side: int, expected: float
) -> None:
    account = _account()
    position = {
        "id": 21,
        "paper_account_id": account["id"],
        "user_id": account["user_id"],
        "symbol": "TESTUSDT",
        "side": side,
        "qty": 2.0,
        "avg_entry": 100.0,
        "opened_ts": 1,
        "funding_acc": 0.0,
        "funding_ts": 0,
    }
    writes: list[tuple[str, tuple]] = []
    monkeypatch.setattr(
        paper.store,
        "execute",
        lambda sql, params=(): writes.append((sql, tuple(params))) or 1,
    )

    delta = paper._accrue_estimated_funding(
        account, position, 100.0, 2 * paper.FUNDING_INTERVAL_SECONDS, paper._config(account)
    )
    duplicate = paper._accrue_estimated_funding(
        account, position, 100.0, 2 * paper.FUNDING_INTERVAL_SECONDS, paper._config(account)
    )

    assert delta == pytest.approx(expected)
    assert duplicate == 0
    assert position["funding_acc"] == pytest.approx(expected)
    assert position["funding_ts"] == 2 * paper.FUNDING_INTERVAL_SECONDS
    assert len(writes) == 1
    assert writes[0][1] == pytest.approx(
        (expected, 2 * paper.FUNDING_INTERVAL_SECONDS, 21, 11, 7, 0)
    )


def test_open_equity_includes_accrued_funding_cost() -> None:
    account = _account()
    account["balance"] = 9_900
    position = {
        "symbol": "TESTUSDT",
        "side": 1,
        "qty": 1,
        "avg_entry": 100,
        "margin": 100,
        "funding_acc": 2,
    }

    equity, unrealized = paper._equity(account, {"TESTUSDT": 105}, [position])

    assert unrealized == pytest.approx(3)
    assert equity == pytest.approx(10_003)


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
        lambda account, symbol, spec, decision, snapshot=None, envelope=None: recorded.append(
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

    direction, atr, basis, signal_time, evidence = paper._strategy_signal(
        account, "TESTUSDT"
    )

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
    assert evidence["reason_codes"] == ["REGIME_UP", "BREAKOUT_UP"]
    assert evidence["valid_until"] == 200
    assert recorded == [("TESTUSDT", "LONG_ENTRY")]


def test_legacy_signal_reads_its_frozen_timeframe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    account = _account()
    account["strategy_snapshot_json"]["timeframe"] = "1h"
    requested: list[tuple[str, str, int]] = []

    def get_klines(symbol: str, timeframe: str, limit: int) -> list[dict]:
        requested.append((symbol, timeframe, limit))
        return [
            {
                "open_time": index * 3_600,
                "open": 100 + index,
                "high": 101 + index,
                "low": 99 + index,
                "close": 100 + index,
                "volume": 10,
            }
            for index in range(3)
        ]

    monkeypatch.setattr(paper.store, "get_klines", get_klines)

    direction, atr, basis, signal_time, _ = paper._strategy_signal(account, "TESTUSDT")

    assert requested == [("TESTUSDT", "1h", 600)]
    assert direction == 0
    assert atr is None
    assert signal_time == 2 * 3_600
    assert "周期：1h" in basis


@pytest.mark.parametrize(
    ("directions", "expected_direction", "expected_matched"),
    [
        ((1, 1), 1, 2),
        ((-1, -1), -1, 2),
        ((1, 0), 0, 1),
        ((1, -1), 0, 1),
    ],
)
def test_combined_paper_strategies_require_all_same_direction(
    monkeypatch: pytest.MonkeyPatch,
    directions: tuple[int, int],
    expected_direction: int,
    expected_matched: int,
) -> None:
    account = _account()
    snapshots = [
        {
            "public_id": f"00000000-0000-0000-0000-0000000000{index}",
            "name": f"策略 {index}",
            "engine_key": "multi_factor",
            "timeframe": "1h",
        }
        for index in (21, 22)
    ]
    account["strategy_snapshot_json"] = {
        **snapshots[0],
        "combination_mode": "all",
        "strategy_snapshots": snapshots,
    }

    def strategy_signal(account, symbol, snapshot=None):
        del account, symbol
        index = snapshots.index(snapshot)
        return (
            directions[index],
            2.0 + index,
            [f"策略：{snapshot['name']}"],
            1_700_000_000 + index * 60,
            {"score": index + 1},
        )

    monkeypatch.setattr(paper, "_strategy_signal", strategy_signal)

    direction, atr, basis, signal_time, evidence = paper._paper_strategy_signal(
        account, "TESTUSDT"
    )

    assert direction == expected_direction
    assert atr == pytest.approx(2.0)
    assert signal_time == 1_700_000_060
    assert evidence["combination_mode"] == "all"
    assert evidence["required_count"] == 2
    assert evidence["matched_count"] == expected_matched
    assert [item["direction"] for item in evidence["strategy_signals"]] == list(
        directions
    )
    assert evidence["combination_key"].count("|") == 1
    assert basis[0] == f"组合条件：{expected_matched}/2 策略同向满足（全部满足才开仓）"


def test_combined_strategy_freshness_requires_every_component() -> None:
    account = _account()
    snapshots = [
        {
            "public_id": f"00000000-0000-0000-0000-0000000000{index}",
            "name": f"策略 {index}",
            "engine_key": "multi_factor",
            "timeframe": "1h",
        }
        for index in (31, 32)
    ]
    account["strategy_snapshot_json"] = {
        **snapshots[0],
        "combination_mode": "all",
        "strategy_snapshots": snapshots,
    }
    now = 1_700_010_000
    recent_bar = now - 90 * 60
    policy = paper._paper_risk_policy(account)
    evidence = {
        "strategy_signals": [
            {"direction": 1, "signal_time": recent_bar, "evidence": {}},
            {"direction": 1, "signal_time": recent_bar, "evidence": {}},
        ]
    }

    assert paper._signal_is_fresh(account, recent_bar, evidence, now, policy)

    evidence["strategy_signals"][1]["signal_time"] = now - 8 * 60 * 60
    assert not paper._signal_is_fresh(account, recent_bar, evidence, now, policy)


def test_legacy_paper_config_is_forced_through_strategy_event_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    account = _account()
    account["config_json"]["signal_mode"] = "legacy_score_v1"
    expected = (1, 2.0, ["统一策略"], 1_700_000_000, {"source": "strategy_event_v2"})
    monkeypatch.setattr(
        paper,
        "_strategy_signal",
        lambda *_args, **_kwargs: expected,
    )

    result = paper._paper_strategy_signal(account, "TESTUSDT", 125)

    assert paper._paper_signal_mode(account) == paper.STRATEGY_EVENT_SIGNAL_MODE
    assert result == expected


def test_paper_ticker_freshness_tolerates_database_clock_drift() -> None:
    account = _account()
    policy = paper._paper_risk_policy(account)
    prices = _fresh_prices("TESTUSDT", 100, 1_700_000_008)

    assert paper._price_is_fresh(prices, "TESTUSDT", 1_700_000_000, policy)


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
    event_time = datetime.fromtimestamp(decision.signal_time / 1_000, tz=UTC)
    envelope = build_decision_envelope(
        revision_fingerprint="strategy-revision:61",
        event_id="paper-test-closed-bar",
        symbol="TESTUSDT",
        timeframe="15m",
        event_time=event_time,
        decision=decision.decision,
        confidence=Decimal(str(decision.confidence)),
        reason_codes=tuple(decision.reason_codes),
        evidence=decision.evidence,
        risk_proposal=decision.risk_proposal,
        valid_until=datetime.fromtimestamp(decision.valid_until / 1_000, tz=UTC),
    )

    persisted = paper._record_full_strategy_decision(
        account, "TESTUSDT", build_trend_pullback_spec(), decision, None, envelope
    )

    assert persisted is True
    insert_sql, insert_params = writes[0]
    assert "INSERT IGNORE INTO strategy_signals" in insert_sql
    assert insert_params[1:4] == (account["user_id"], 51, 61)
    assert insert_params[4] == 71
    assert insert_params[10] == 1_700_001_800
    # The persisted key is a bounded hash of mode/deployment plus the stable
    # cross-runtime decision id.  This avoids index-length drift while keeping
    # replay/paper/shadow decisions comparable.
    assert len(insert_params[-1]) == 64
    assert set(insert_params[-1]) <= set("0123456789abcdef")
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
    _mock_open_transaction(monkeypatch, account, writes)

    opened = paper._open_position(
        account, "TESTUSDT", side, 100.0, 2.0, ["4h signal"], [], 1_000
    )

    assert opened is True
    insert_params = next(
        params for sql, params in writes if "INSERT INTO paper_order_executions" in sql
    )
    projection = json.loads(insert_params[-1])
    projected_position = projection["position"]
    entry = projected_position["avg_entry"]
    stop = projected_position["stop"]
    target = projected_position["target"]
    assert (entry - stop) * side > 0
    assert (target - entry) * side > 0
    assert target == pytest.approx(entry + side * 5)
    assert projected_position["funding_ts"] == 1_000
    entry_basis = projected_position["basis"]
    assert entry_basis["availability"] == "captured"
    assert entry_basis["reasons"] == ["4h signal"]
    assert entry_basis["execution"]["entry_price"] == pytest.approx(entry)
    assert entry_basis["exit_policy"] == {
        "version": "unified_exit_v1",
        "source": "atr",
        "entry_price": pytest.approx(entry),
        "direction": side,
        "stop": pytest.approx(stop),
        "target": pytest.approx(target),
        "risk_distance": pytest.approx(abs(entry - stop)),
        "reward_distance": pytest.approx(abs(target - entry)),
    }
    assert entry_basis["execution_policy"] == {
        "trigger_timeframe": "4h",
        "timeframe_seconds": 4 * 3_600,
        "max_holding_bars": 12,
        "max_holding_seconds": 12 * 4 * 3_600,
    }


def test_open_position_does_not_mutate_projection_when_fill_append_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    account = _account()
    initial_balance = account["balance"]
    writes: list[tuple[str, tuple]] = []
    monkeypatch.setattr(paper, "_prices", lambda: {"TESTUSDT": 100.0})
    monkeypatch.setattr(
        paper, "_equity", lambda account, prices, positions: (10_000.0, 0.0)
    )
    state = _mock_open_transaction(
        monkeypatch,
        account,
        writes,
        fail_on_insert=True,
    )

    with pytest.raises(RuntimeError, match="simulated fill append failure"):
        paper._open_position(
            account,
            "TESTUSDT",
            1,
            100.0,
            2.0,
            ["4h signal"],
            [],
            1_000,
        )

    assert state["rolled_back"] is True
    assert account["balance"] == initial_balance
    assert len(writes) == 1
    assert "INSERT INTO paper_order_executions" in writes[0][0]
    assert "paper_positions" not in writes[0][0]


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


def test_max_holding_exit_includes_funding_accrued_on_the_same_tick(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    account = _account()
    now = 6 * paper.FUNDING_INTERVAL_SECONDS
    position = {
        "id": 35,
        "paper_account_id": account["id"],
        "user_id": account["user_id"],
        "symbol": "TESTUSDT",
        "side": 1,
        "qty": 10,
        "avg_entry": 100,
        "margin": 50,
        "stop": None,
        "target": None,
        "atr_entry": None,
        "liq_price": None,
        "funding_acc": 0,
        "funding_ts": 0,
        "opened_ts": 0,
    }
    closes: list[tuple[str, float]] = []
    monkeypatch.setattr(paper, "_positions", lambda account: [position])
    monkeypatch.setattr(
        paper, "_strategy_signal", lambda account, symbol: (0, None, [], now)
    )
    monkeypatch.setattr(paper, "tradfi_symbols", lambda: [])
    monkeypatch.setattr(paper.store, "execute", lambda sql, params=(): 1)
    monkeypatch.setattr(paper, "_record_equity", lambda *args: None)
    monkeypatch.setattr(
        paper,
        "_close_position",
        lambda account, position, price, reason, now: closes.append(
            (reason, position["funding_acc"])
        )
        or True,
    )

    paper._tick_account(account, {"TESTUSDT": 100}, now)

    assert closes == [("max_holding_bars", pytest.approx(0.6))]


def test_max_holding_exit_uses_policy_captured_when_position_was_opened(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    account = _account()
    account["strategy_snapshot_json"]["timeframe"] = "4h"
    account["config_json"]["max_holding_bars"] = 12
    now = 2 * 3_600
    position = {
        "id": 37,
        "paper_account_id": account["id"],
        "user_id": account["user_id"],
        "symbol": "TESTUSDT",
        "side": 1,
        "qty": 1,
        "avg_entry": 100,
        "margin": 50,
        "stop": None,
        "target": None,
        "atr_entry": None,
        "liq_price": None,
        "funding_acc": 0,
        "funding_ts": now,
        "opened_ts": 0,
        "basis": json.dumps(
            {
                "execution_policy": {
                    "trigger_timeframe": "1h",
                    "timeframe_seconds": 3_600,
                    "max_holding_bars": 2,
                }
            }
        ),
    }
    closes: list[str] = []
    monkeypatch.setattr(paper, "_positions", lambda account: [position])
    monkeypatch.setattr(
        paper,
        "_paper_strategy_signal",
        lambda *_args: (0, None, [], now, {}),
    )
    monkeypatch.setattr(paper, "tradfi_symbols", lambda: [])
    monkeypatch.setattr(paper.store, "execute", lambda sql, params=(): 1)
    monkeypatch.setattr(paper, "_record_equity", lambda *args: None)
    monkeypatch.setattr(
        paper,
        "_close_position",
        lambda account, position, price, reason, now: closes.append(reason) or True,
    )

    paper._tick_account(account, {"TESTUSDT": 100}, now)

    assert closes == ["max_holding_bars"]


def test_close_position_deducts_accrued_funding_from_balance_and_trade(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    account = _account()
    account["balance"] = 9_950
    account["config_json"]["fee_bps"] = 0
    account["config_json"]["slippage_bps"] = 0
    position = {
        "id": 36,
        "symbol": "TESTUSDT",
        "side": 1,
        "qty": 1,
        "avg_entry": 100,
        "margin": 50,
        "funding_acc": 2,
        "open_score": 100,
        "basis": '{"schema_version":1,"reasons":["entry signal"]}',
        "opened_ts": 900,
    }
    statements: list[tuple[str, tuple]] = []

    class Transaction:
        @staticmethod
        def query(sql, params=()):
            return [{"balance": 9_950}]

        @staticmethod
        def execute(sql, params=()):
            statements.append((sql, tuple(params)))
            return 1

    @contextmanager
    def transaction():
        yield Transaction()

    monkeypatch.setattr(paper.store, "transaction", transaction)
    monkeypatch.setattr(paper.store, "add_alert", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        paper,
        "_reconcile_paper_account",
        lambda target: target.update(balance=9_998) is None,
    )
    _mock_execution(monkeypatch)

    closed = paper._close_position(account, position, 100, "max_holding_bars", 1_000)

    assert closed is True
    fact_params = next(
        params for sql, params in statements if "INSERT INTO paper_order_executions" in sql
    )
    projection = json.loads(fact_params[-1])
    trade = projection["trade"]
    assert projection["balance_credit"] == pytest.approx(48)
    assert trade["pnl"] == pytest.approx(-2)
    assert trade["fee"] == 0
    assert trade["funding"] == 2
    trade_basis = trade["entry_basis"]
    assert trade_basis["schema_version"] == 1
    assert trade_basis["exit_decision"] == {
        "version": "unified_exit_decision_v1",
        "policy_version": "unified_exit_v1",
        "mode": "paper",
        "reason": "max_holding_bars",
        "source": "holding_policy",
        "priority": 60,
        "trigger_price": 100.0,
        "observed_at": 1_000,
        "execution_price": 100.0,
    }
    assert account["balance"] == pytest.approx(9_998)


def test_close_position_requires_successful_fact_projection(
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

    class FillTransaction:
        @staticmethod
        def execute(sql, params=()):
            statements.append(sql)
            return 1

    @contextmanager
    def transaction():
        yield FillTransaction()

    monkeypatch.setattr(paper.store, "transaction", transaction)
    monkeypatch.setattr(
        paper.store,
        "add_alert",
        lambda *args, **kwargs: pytest.fail("lost close ownership must not alert"),
    )
    monkeypatch.setattr(paper, "_reconcile_paper_account", lambda account: False)
    _mock_execution(monkeypatch)

    closed = paper._close_position(account, position, 105, "take_profit", 1_000)

    assert closed is False
    assert len(statements) == 1
    assert "INSERT INTO paper_order_executions" in statements[0]


def test_system_default_contains_the_fixed_paper_strategy() -> None:
    strategy = next(
        item
        for item in SYSTEM_STRATEGY_DEFINITIONS
        if item["template_key"] == "paper_multifactor_atr_v1"
    )

    assert strategy["name"] == "AI 模拟盘 ATR 趋势"
    assert strategy["version"] == 3
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
    monkeypatch.setattr(paper, "_reconcile_paper_account", lambda account: True)
    monkeypatch.setattr(paper.store, "query", lambda sql, params=(): [])

    data = paper.api_data(account["user_id"], account["id"])

    assert "2.5×ATR 止盈" in data["rules"]["exits"]
    assert "资金费率估算 1.0 bps/8h" in data["rules"]["costs"]


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
    monkeypatch.setattr(paper, "_reconcile_paper_account", lambda account: True)
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


def test_projection_failure_isolates_only_the_affected_paper_account(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    broken = {**_account(), "id": 11, "status": "active"}
    healthy = {**_account(), "id": 12, "status": "active"}
    ticked: list[int] = []

    @contextmanager
    def advisory_lock(*args, **kwargs):
        yield True

    monkeypatch.setattr(paper.store, "advisory_lock", advisory_lock)
    monkeypatch.setattr(paper, "_tracked_accounts", lambda account_id=None: [broken, healthy])
    monkeypatch.setattr(paper, "_prices", lambda: {"TESTUSDT": 100.0})
    monkeypatch.setattr(
        paper,
        "_reconcile_paper_account",
        lambda account: account["id"] != broken["id"],
    )
    monkeypatch.setattr(
        paper, "_tick_account", lambda account, prices, now: ticked.append(account["id"])
    )

    paper.tick()

    assert ticked == [healthy["id"]]


def test_paper_max_positions_is_hard_capped_at_twenty() -> None:
    account = _account()
    account["config_json"]["max_positions"] = 50

    assert paper._config(account)["max_positions"] == 20


def test_paper_leverage_is_hard_capped_at_twenty() -> None:
    account = _account()
    account["config_json"]["leverage"] = 50

    assert paper._config(account)["leverage"] == 20


def test_paper_visible_leverage_is_the_policy_ceiling_for_legacy_accounts() -> None:
    account = _account()
    account["config_json"]["risk_max_leverage"] = 10

    assert paper._paper_risk_policy(account).max_leverage == 20


def test_legacy_signal_requires_a_closed_recent_four_hour_bar() -> None:
    account = _account()
    policy = paper._paper_risk_policy(account)
    bar_open = 1_700_000_000

    assert not paper._signal_is_fresh(
        account, bar_open, {}, bar_open + 4 * 3600 - 1, policy
    )
    assert paper._signal_is_fresh(
        account, bar_open, {}, bar_open + 4 * 3600 + 30 * 60, policy
    )
    assert not paper._signal_is_fresh(
        account, bar_open, {}, bar_open + 5 * 3600 + 1, policy
    )


def test_legacy_signal_freshness_uses_its_frozen_timeframe() -> None:
    account = _account()
    account["strategy_snapshot_json"]["timeframe"] = "1h"
    policy = paper._paper_risk_policy(account)
    bar_open = 1_700_000_000

    assert not paper._signal_is_fresh(
        account, bar_open, {}, bar_open + 3600 - 1, policy
    )
    assert paper._signal_is_fresh(
        account, bar_open, {}, bar_open + 3600 + 30, policy
    )


def test_full_strategy_signal_honors_its_valid_until() -> None:
    account = _account()
    account["strategy_snapshot_json"]["strategy_kind"] = "full_strategy"
    policy = paper._paper_risk_policy(account)
    now = 1_700_010_000

    assert paper._signal_is_fresh(
        account,
        now - 60,
        {"valid_until": (now + 1) * 1000},
        now,
        policy,
    )
    assert not paper._signal_is_fresh(
        account,
        now - 60,
        {"valid_until": (now - 1) * 1000},
        now,
        policy,
    )
    assert not paper._signal_is_fresh(account, now - 60, {}, now, policy)


def test_full_strategy_risk_proposal_keeps_fixed_paper_leverage() -> None:
    account = _account()
    account["strategy_snapshot_json"]["strategy_kind"] = "full_strategy"
    base = paper._paper_risk_policy(account)

    looser = paper._paper_risk_policy(
        account,
        {
            "risk_proposal": {
                "risk_per_trade_pct": 5,
                "max_margin_pct": 100,
                "max_leverage": 20,
            }
        },
    )
    tighter = paper._paper_risk_policy(
        account,
        {
            "risk_proposal": {
                "risk_per_trade_pct": 0.2,
                "max_margin_pct": 1,
                "max_leverage": 3,
            }
        },
    )

    assert looser.risk_per_trade_pct == base.risk_per_trade_pct
    assert looser.max_margin_per_trade_pct == base.max_margin_per_trade_pct
    assert looser.max_leverage == base.max_leverage
    assert tighter.risk_per_trade_pct == paper.Decimal("0.2")
    assert tighter.max_margin_per_trade_pct == paper.Decimal("1")
    assert tighter.max_leverage == base.max_leverage


def test_full_strategy_uses_its_fixed_stop_and_target_distances() -> None:
    config = paper._config(_account())
    evidence = {
        "risk_proposal": {
            "stop_distance": 7.5,
            "take_profit_distance": 12.5,
        }
    }

    assert paper._signal_exit_levels(100, 1, 2, config, evidence) == (92.5, 112.5)
    assert paper._signal_exit_levels(100, -1, 2, config, evidence) == (107.5, 87.5)


def test_incomplete_full_strategy_risk_proposal_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    account = _account()
    account["strategy_snapshot_json"]["strategy_kind"] = "full_strategy"
    monkeypatch.setattr(
        paper,
        "_prices",
        lambda: pytest.fail("invalid proposal must fail before market sizing"),
    )

    assert not paper._open_position(
        account,
        "AAPLUSDT",
        1,
        100,
        2,
        ["full strategy"],
        [],
        1_700_020_000,
        signal_evidence={"risk_proposal": {"max_leverage": 3}},
    )


def test_stale_ticker_blocks_a_new_paper_entry(monkeypatch: pytest.MonkeyPatch) -> None:
    account = _account()
    now = 1_700_030_000
    prices = _fresh_prices("AAPLUSDT", 100, now - 121)
    signal_calls: list[str] = []
    monkeypatch.setattr(paper, "_positions", lambda account: [])
    monkeypatch.setattr(paper, "tradfi_symbols", lambda: ["AAPLUSDT"])
    monkeypatch.setattr(paper, "_entry_loss_guard", lambda *args: True)
    monkeypatch.setattr(paper, "_record_equity", lambda *args: None)
    monkeypatch.setattr(
        paper,
        "_strategy_signal",
        lambda account, symbol: signal_calls.append(symbol),
    )

    paper._tick_account(account, prices, now)

    assert signal_calls == []


@pytest.mark.parametrize("symbol", ["UVXYUSDT", "TQQQUSDT"])
def test_high_risk_product_is_rejected_before_signal_evaluation(
    monkeypatch: pytest.MonkeyPatch, symbol: str
) -> None:
    account = _account()
    now = 1_700_040_000
    prices = _fresh_prices(symbol, 100, now)
    monkeypatch.setattr(paper, "_positions", lambda account: [])
    monkeypatch.setattr(paper, "tradfi_symbols", lambda: [symbol])
    monkeypatch.setattr(paper, "_entry_loss_guard", lambda *args: True)
    monkeypatch.setattr(paper, "_record_equity", lambda *args: None)
    monkeypatch.setattr(
        paper,
        "_strategy_signal",
        lambda *args: pytest.fail("blocked product must not evaluate a signal"),
    )

    paper._tick_account(account, prices, now)


def test_correlated_group_cap_is_applied_to_paper_entries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    account = _account()
    now = 1_700_050_000
    positions = [
        {
            "id": index,
            "paper_account_id": account["id"],
            "user_id": account["user_id"],
            "symbol": symbol,
            "side": 1,
            "qty": 1,
            "avg_entry": 100,
            "margin": 10,
            "stop": 97,
            "target": 105,
            "funding_acc": 0,
            "funding_ts": now,
            "opened_ts": now,
        }
        for index, symbol in enumerate(("AMDUSDT", "NVDAUSDT"), start=1)
    ]
    prices = _fresh_prices("QCOMUSDT", 100, now)
    monkeypatch.setattr(paper, "_positions", lambda account: positions)
    monkeypatch.setattr(paper, "tradfi_symbols", lambda: ["QCOMUSDT"])
    monkeypatch.setattr(paper, "_entry_loss_guard", lambda *args: True)
    monkeypatch.setattr(paper, "_record_equity", lambda *args: None)
    monkeypatch.setattr(
        paper,
        "_strategy_signal",
        lambda *args: pytest.fail("third correlated position must be blocked"),
    )

    paper._tick_account(account, prices, now)


def test_atr_risk_sizing_keeps_configured_leverage_and_limits_risk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    account = _account()
    account["config_json"].update({"fee_bps": 0, "slippage_bps": 0})
    writes: list[tuple[str, tuple]] = []
    monkeypatch.setattr(paper, "_prices", lambda: {"AAPLUSDT": 100.0})
    monkeypatch.setattr(
        paper, "_equity", lambda account, prices, positions: (10_000.0, 0.0)
    )
    _mock_open_transaction(monkeypatch, account, writes)

    opened = paper._open_position(
        account, "AAPLUSDT", 1, 100, 10, ["closed 4h signal"], [], 1_700_060_000
    )

    assert opened is True
    params = next(
        params for sql, params in writes if "INSERT INTO paper_order_executions" in sql
    )
    position = json.loads(params[-1])["position"]
    quantity = position["qty"]
    margin = position["margin"]
    leverage = position["leverage"]
    stop = position["stop"]
    assert leverage == 20
    assert stop == pytest.approx(85)
    assert quantity * (100 - stop) == pytest.approx(50)
    assert margin == pytest.approx(16.6666667)
    assert margin < 10_000 * 0.10


def test_existing_position_risk_consumes_portfolio_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    account = _account()
    account["config_json"].update({"fee_bps": 0, "slippage_bps": 0})
    existing = {
        "symbol": "MUUUSDT",
        "side": -1,
        "qty": 100,
        "avg_entry": 100,
        "margin": 500,
        "stop": 110,
        "atr_entry": 6.6666667,
    }
    monkeypatch.setattr(paper, "_prices", lambda: {"AAPLUSDT": 100.0})
    monkeypatch.setattr(
        paper, "_equity", lambda account, prices, positions: (10_000.0, 0.0)
    )
    monkeypatch.setattr(
        paper.store,
        "execute",
        lambda *args, **kwargs: pytest.fail("risk cap must block the insert"),
    )

    opened = paper._open_position(
        account, "AAPLUSDT", 1, 100, 2, ["closed 4h signal"], [existing], 1_700_070_000
    )

    assert opened is False


@pytest.mark.parametrize(
    ("current_equity", "day_start", "high_watermark"),
    [(9_700, 10_000, 10_000), (9_300, 9_300, 10_000)],
)
def test_daily_loss_or_drawdown_blocks_new_entries(
    monkeypatch: pytest.MonkeyPatch,
    current_equity: float,
    day_start: float,
    high_watermark: float,
) -> None:
    account = _account()

    def query(sql: str, params=()):
        if "MAX(equity)" in sql:
            return [{"high_watermark": high_watermark}]
        return [{"equity": day_start}]

    monkeypatch.setattr(paper.store, "query", query)

    assert not paper._entry_loss_guard(
        account,
        current_equity,
        1_700_080_000,
        paper._paper_risk_policy(account),
    )


def test_signal_is_consumed_only_after_position_open_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    account = _account()
    now = 1_700_090_000
    bar_open = now - 4 * 3600 - 30 * 60
    prices = _fresh_prices("AAPLUSDT", 100, now)
    consumed: list[tuple[int, str, int]] = []
    monkeypatch.setattr(paper, "_positions", lambda account: [])
    monkeypatch.setattr(paper, "tradfi_symbols", lambda: ["AAPLUSDT"])
    monkeypatch.setattr(paper, "_entry_loss_guard", lambda *args: True)
    monkeypatch.setattr(paper, "_record_equity", lambda *args: None)
    monkeypatch.setattr(
        paper,
        "_strategy_signal",
        lambda *args: (1, 2.0, ["closed"], bar_open, {}),
    )
    monkeypatch.setattr(paper.store, "user_state_get", lambda *args: None)
    monkeypatch.setattr(
        paper.store,
        "user_state_set",
        lambda user_id, key, value: consumed.append((user_id, key, value)),
    )
    monkeypatch.setattr(paper, "_open_position", lambda *args: False)

    paper._tick_account(account, prices, now)
    assert consumed == []

    monkeypatch.setattr(paper, "_open_position", lambda *args: True)
    paper._tick_account(account, prices, now)
    assert consumed == [
        (account["user_id"], f"paper:{account['id']}:signal:AAPLUSDT", bar_open)
    ]


def test_api_trade_statistics_use_net_pnl_after_funding_and_both_fees(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    account = _account()
    trades = [
        {
            "id": 1,
            "qty": 10,
            "entry_price": 100,
            "exit_price": 101,
            "side": 1,
            "margin": 1_000,
            "pnl": 8,
            "fee": 0.505,
            "funding": 2,
            "entry_basis_json": None,
        },
        {
            "id": 2,
            "qty": 1,
            "entry_price": 100,
            "exit_price": 100.05,
            "side": 1,
            "margin": 100,
            "pnl": 0.05,
            "fee": 0.050025,
            "funding": 0,
            "entry_basis_json": None,
        },
    ]
    monkeypatch.setattr(paper, "_account", lambda *args: account)
    monkeypatch.setattr(paper, "_prices", lambda: {})
    monkeypatch.setattr(paper, "_positions", lambda account: [])
    monkeypatch.setattr(paper, "_today_pnl", lambda *args: 0)

    def query(sql: str, params=()):
        return trades if "FROM paper_trades" in sql else []

    monkeypatch.setattr(paper.store, "query", query)

    data = paper.api_data(account["user_id"], account["id"])

    first, second = data["trades"]
    assert first["entry_fee"] == pytest.approx(0.5)
    assert first["exit_fee"] == pytest.approx(0.505)
    assert first["fee"] == pytest.approx(1.005)
    assert first["net_pnl"] == pytest.approx(6.995)
    assert second["pnl"] > 0
    assert second["net_pnl"] < 0
    assert data["stats"]["wins"] == 1
    assert data["stats"]["losses"] == 1
    assert data["stats"]["win_rate"] == 50
    assert data["stats"]["realized"] == pytest.approx(6.94)
