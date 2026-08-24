from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from quantdesk_v2 import live_engine
from quantdesk_v2.interfaces.api import ai_monitor as ai_monitor_api
from quantdesk_v2.schemas import (
    AiMonitorLiveCopyConfigUpdate,
    AiMonitorLiveCopyUpdate,
    AiMonitorManualFollowRequest,
)

ROOT = Path(__file__).resolve().parents[1]


def _account(*, enabled: bool = True) -> dict:
    return {
        "id": 1,
        "user_id": 7,
        "deployment_id": 11,
        "config_json": {
            "execution_scope": "ai_monitor",
            "signal_source": "ai_monitor",
            "ai_monitor_live_copy_enabled": enabled,
            "ai_monitor_live_copy_enabled_at": "2026-08-17T09:59:00+00:00",
            "ai_monitor_live_signal_max_age_seconds": 300,
            "ai_monitor_live_min_combined_score": 70,
            "ai_monitor_live_regular_session_only": False,
            "risk_per_trade_pct": 0.5,
            "position_size_pct": 2,
            "leverage": 10,
            "risk_max_leverage": 10,
        },
        "strategy_snapshot_json": {"strategy_kind": "legacy_signal"},
    }


def _prediction_row(*, entry_ready: bool = True, direction: str = "long") -> dict:
    predicted_at = datetime(2026, 8, 17, 10, 0, 0)
    return {
        "prediction_public_id": "prediction-public-id",
        "opportunity_public_id": "opportunity-public-id",
        "direction": direction,
        "timeframe": "1h",
        "confidence_score": 82.5,
        "entry_price": 100,
        "readiness_status": "shadow_ready",
        "estimated_cost_bps": 16,
        "expected_edge_lower_bound_bps": 30,
        "predicted_at": predicted_at,
        "due_at": predicted_at + timedelta(hours=4),
        "expires_at": predicted_at + timedelta(hours=2),
        "evidence_json": {
            "virtual_entry_gate": {
                "entry_ready": entry_ready,
                "direction": direction,
                "reference_price": 100,
            },
            "risk_plan": {
                "stop_loss_pct": 1.5,
                "take_profit_pct": 3.0,
                "atr_pct": 1.0,
            },
            "live_readiness": {
                "status": "shadow_ready",
                "minimum_combined_score": 70,
                "safety_margin_bps": 10,
                "required_gross_edge_bps": 26,
                "checks": {
                    "indicator_policy_passed": True,
                    "indicator_strength": True,
                    "combined_score": True,
                    "macro_entry_policy": True,
                    "market_quality": True,
                    "market_flow_available": True,
                    "market_flow_freshness": True,
                    "market_flow_quality": True,
                    "calibration_samples": True,
                    "cost_stress_edge": True,
                },
            },
        },
    }


@pytest.fixture(autouse=True)
def _fresh_order_book_for_live_signal_tests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        live_engine,
        "_current_order_book_gate",
        lambda *_args, **_kwargs: {
            "status": "passed",
            "passed": True,
            "quality_passed": True,
            "direction_clear": True,
            "direction_conflict": False,
            "confirms_direction": True,
        },
    )


def test_live_copy_schema_requires_explicit_true_only_at_endpoint_boundary() -> None:
    disabled = AiMonitorLiveCopyUpdate(enabled=False)

    assert disabled.account_id is None
    with pytest.raises(ValidationError):
        AiMonitorLiveCopyUpdate(enabled=True, unexpected=True)  # type: ignore[call-arg]


def test_manual_follow_schema_binds_one_exact_signal_and_normalizes_symbol() -> None:
    payload = AiMonitorManualFollowRequest(
        account_id="a" * 36,
        opportunity_id="o" * 36,
        prediction_id="p" * 36,
        manual_attempt_id="11111111-1111-4111-8111-111111111111",
        expected_contract_symbol="aaplusdt",
        expected_direction="long",
        acknowledge_real_funds=True,
    )

    assert payload.expected_contract_symbol == "AAPLUSDT"
    assert payload.acknowledge_real_funds is True
    pending_evaluation = AiMonitorManualFollowRequest(
        account_id="a" * 36,
        opportunity_id="o" * 36,
        manual_attempt_id="22222222-2222-4222-8222-222222222222",
        expected_contract_symbol="nvdadusdt",
        expected_direction="short",
        acknowledge_real_funds=True,
    )
    assert pending_evaluation.prediction_id is None
    assert pending_evaluation.expected_contract_symbol == "NVDADUSDT"
    with pytest.raises(ValidationError):
        AiMonitorManualFollowRequest(
            account_id="a" * 36,
            opportunity_id="o" * 36,
            prediction_id="p" * 36,
            manual_attempt_id="33333333-3333-4333-8333-333333333333",
            expected_contract_symbol="AAPLUSDT",
            expected_direction="long",
            acknowledge_real_funds=True,
            unexpected=True,
        )


def test_manual_follow_history_uses_binance_realized_and_unrealized_pnl() -> None:
    opened_at = datetime(2026, 8, 20, 15, 3, 33, tzinfo=UTC)
    closed_at = datetime(2026, 8, 20, 15, 14, 56, tzinfo=UTC)
    basis = {
        "execution": {"entry_price": 185.63},
        "signal": {
            "evidence": {
                "manual_follow": True,
                "manual_attempt_id": "11111111-1111-4111-8111-111111111111",
            }
        },
    }
    intents = [
        {
            "id": 17,
            "public_id": "open-txn",
            "symbol": "TXNUSDT",
            "action": "open",
            "position_side": "LONG",
            "quantity": Decimal("0.33"),
            "entry_basis_json": basis,
            "response_json": {"avgPrice": "185.63"},
            "submitted_at": opened_at,
            "created_at": opened_at,
        },
        {
            "id": 20,
            "public_id": "close-txn",
            "symbol": "TXNUSDT",
            "action": "close",
            "position_side": "LONG",
            "signal_key": "live:1:TXNUSDT:LONG:close:position_state_unverified:123",
            "entry_basis_json": basis,
            "response_json": {"avgPrice": "184.64"},
            "submitted_at": closed_at,
            "created_at": closed_at,
        },
    ]
    income = (
        SimpleNamespace(
            symbol="TXNUSDT",
            income_type="REALIZED_PNL",
            income=Decimal("-0.328"),
            time_ms=int(closed_at.timestamp() * 1_000),
        ),
        SimpleNamespace(
            symbol="TXNUSDT",
            income_type="COMMISSION",
            income=Decimal("-0.0707792"),
            time_ms=int(closed_at.timestamp() * 1_000),
        ),
    )

    result = ai_monitor_api._manual_follow_history_out(
        intents,
        income_records=income,
        generated_at=datetime(2026, 8, 20, 16, tzinfo=UTC),
        history_status="available",
    )

    assert result["summary"]["total"] == 1
    assert result["summary"]["closed_count"] == 1
    assert result["summary"]["losses"] == 1
    assert result["summary"]["net_pnl"] == pytest.approx(-0.3987792)
    assert result["records"][0]["close_reason"] == "position_state_unverified"
    assert result["records"][0]["exit_price"] == pytest.approx(184.64)


def test_live_copy_config_validates_direction_and_risk_boundaries() -> None:
    policy = AiMonitorLiveCopyConfigUpdate(position_mode="hedge", allow_short=True)

    assert policy.position_mode == "hedge"
    assert policy.position_size_basis == "account_equity"
    assert policy.copy_total_amount == 1_000
    assert policy.minimum_combined_score == 70
    fixed_amount = AiMonitorLiveCopyConfigUpdate(
        position_size_basis="copy_total_amount",
        copy_total_amount=25_000,
    )
    assert fixed_amount.position_size_basis == "copy_total_amount"
    assert fixed_amount.copy_total_amount == 25_000
    with pytest.raises(ValidationError):
        AiMonitorLiveCopyConfigUpdate(allow_long=False, allow_short=False)
    with pytest.raises(ValidationError):
        AiMonitorLiveCopyConfigUpdate(risk_per_trade_pct=4, max_total_risk_pct=2)
    with pytest.raises(ValidationError):
        AiMonitorLiveCopyConfigUpdate(copy_total_amount=0)
    with pytest.raises(ValidationError):
        AiMonitorLiveCopyConfigUpdate(position_size_basis="unknown")  # type: ignore[arg-type]


def test_ai_monitor_live_signal_is_fresh_bounded_and_carries_protection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2026, 8, 17, 10, 1, tzinfo=UTC).timestamp()
    captured: dict = {}

    def query(statement: str, params: tuple) -> list[dict]:
        captured["statement"] = statement
        captured["params"] = params
        return [_prediction_row()]

    monkeypatch.setattr(live_engine.store, "query", query)
    monkeypatch.setattr(live_engine.time, "time", lambda: now)

    direction, atr, basis, signal_time, evidence = live_engine._ai_monitor_signal(
        _account(),
        "AAPLUSDT",
        price=102,
    )

    assert direction == 1
    assert atr == pytest.approx(1.02)
    assert signal_time == int(datetime(2026, 8, 17, 10, 0, tzinfo=UTC).timestamp())
    assert evidence["source"] == "ai_monitor_live_copy_v1"
    assert evidence["risk_proposal"]["stop_distance"] == pytest.approx(1.53)
    assert evidence["risk_proposal"]["take_profit_distance"] == pytest.approx(3.06)
    assert evidence["risk_proposal"]["max_leverage"] == 10
    assert evidence["valid_until"] == signal_time + 300
    assert "p.predicted_at>=" in captured["statement"]
    assert "o.expires_at>?" in captured["statement"]
    assert "CURRENT_TIMESTAMP" not in captured["statement"]
    assert captured["params"][0:2] == (7, "AAPLUSDT")
    assert captured["params"][2] == datetime(2026, 8, 17, 10, 1)
    assert captured["params"][3] == datetime(2026, 8, 17, 9, 59)
    assert any("组合评分" in item for item in basis)
    policy = live_engine.policy_from_config(_account()["config_json"])
    assert live_engine._signal_is_fresh(_account(), signal_time, policy, evidence)


def test_ai_monitor_live_signal_applies_frozen_macro_position_multiplier(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2026, 8, 17, 10, 1, tzinfo=UTC).timestamp()
    row = _prediction_row()
    row["evidence_json"]["macro_entry_policy"] = {
        "entry_allowed": True,
        "position_multiplier": 0.25,
        "state": "rate_liquidity_shock",
    }
    row["evidence_json"]["live_readiness"]["minimum_combined_score"] = 80
    monkeypatch.setattr(live_engine.store, "query", lambda *_args, **_kwargs: [row])
    monkeypatch.setattr(live_engine.time, "time", lambda: now)

    direction, _atr, _basis, _signal_time, evidence = live_engine._ai_monitor_signal(
        _account(), "AAPLUSDT", price=102
    )

    assert direction == 1
    assert evidence["risk_proposal"]["risk_per_trade_pct"] == pytest.approx(0.125)
    assert evidence["risk_proposal"]["max_margin_pct"] == pytest.approx(0.5)
    assert evidence["risk_proposal"]["macro_position_multiplier"] == 0.25


def test_ai_monitor_auto_signal_rechecks_current_book_but_manual_follow_bypasses_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2026, 8, 17, 10, 1, tzinfo=UTC).timestamp()
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(live_engine.time, "time", lambda: now)
    monkeypatch.setattr(
        live_engine.store,
        "query",
        lambda *_args, **_kwargs: [_prediction_row()],
    )
    monkeypatch.setattr(
        live_engine,
        "_current_order_book_gate",
        lambda symbol, direction: calls.append((symbol, direction))
        or {
            "status": "direction_conflict",
            "passed": False,
            "quality_passed": True,
            "direction_clear": False,
            "direction_conflict": True,
        },
    )

    assert live_engine._ai_monitor_signal(_account(), "AAPLUSDT", price=102)[0] == 0
    manual = live_engine._ai_monitor_signal(
        _account(),
        "AAPLUSDT",
        price=102,
        prediction_public_id="prediction-public-id",
        opportunity_public_id="opportunity-public-id",
    )

    assert manual[0] == 1
    assert manual[4]["execution_order_book"] is None
    assert calls == [("AAPLUSDT", "long")]


def test_ai_monitor_auto_requires_positive_order_book_confirmation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2026, 8, 17, 10, 1, tzinfo=UTC).timestamp()
    monkeypatch.setattr(live_engine.time, "time", lambda: now)
    monkeypatch.setattr(
        live_engine.store,
        "query",
        lambda *_args, **_kwargs: [_prediction_row()],
    )
    monkeypatch.setattr(
        live_engine,
        "_current_order_book_gate",
        lambda *_args, **_kwargs: {
            "status": "passed",
            "passed": True,
            "quality_passed": True,
            "direction_clear": True,
            "direction_conflict": False,
            "confirms_direction": False,
        },
    )

    assert live_engine._ai_monitor_signal(_account(), "AAPLUSDT", price=102)[0] == 0


def test_ai_monitor_auto_requires_shadow_readiness_but_manual_follow_bypasses_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2026, 8, 17, 10, 1, tzinfo=UTC).timestamp()
    row = _prediction_row()
    row["evidence_json"]["live_readiness"] = {
        "status": "research_only",
        "minimum_combined_score": 70,
        "checks": {
            "calibration_samples": False,
            "cost_stress_edge": False,
        },
    }
    monkeypatch.setattr(live_engine.store, "query", lambda *_args, **_kwargs: [row])
    monkeypatch.setattr(live_engine.time, "time", lambda: now)

    assert live_engine._ai_monitor_signal(_account(), "AAPLUSDT", price=102)[0] == 0
    manual = live_engine._ai_monitor_signal(
        _account(),
        "AAPLUSDT",
        price=102,
        prediction_public_id="prediction-public-id",
        opportunity_public_id="opportunity-public-id",
    )

    assert manual[0] == 1
    assert manual[4]["manual_gate_override"] is True
    assert manual[4]["automatic_readiness_passed"] is False


def test_ai_monitor_auto_requires_persisted_positive_edge_lower_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2026, 8, 17, 10, 1, tzinfo=UTC).timestamp()
    row = _prediction_row()
    row["expected_edge_lower_bound_bps"] = 26
    monkeypatch.setattr(live_engine.store, "query", lambda *_args, **_kwargs: [row])
    monkeypatch.setattr(live_engine.time, "time", lambda: now)

    assert live_engine._ai_monitor_signal(_account(), "AAPLUSDT", price=102)[0] == 0


def test_ai_monitor_manual_signal_query_is_bound_to_clicked_prediction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2026, 8, 17, 10, 1, tzinfo=UTC).timestamp()
    captured: dict = {}

    def query(statement: str, params: tuple) -> list[dict]:
        captured["statement"] = statement
        captured["params"] = params
        return [_prediction_row()]

    monkeypatch.setattr(live_engine.store, "query", query)
    monkeypatch.setattr(live_engine.time, "time", lambda: now)

    direction, *_ = live_engine._ai_monitor_signal(
        _account(),
        "AAPLUSDT",
        price=102,
        prediction_public_id="prediction-public-id",
        opportunity_public_id="opportunity-public-id",
    )

    assert direction == 1
    assert "p.public_id=? AND o.public_id=?" in captured["statement"]
    assert "p.predicted_at>=" not in captured["statement"]
    assert "o.expires_at>?" not in captured["statement"]
    assert "CURRENT_TIMESTAMP" not in captured["statement"]
    assert captured["params"][2:4] == (
        "prediction-public-id",
        "opportunity-public-id",
    )
    assert len(captured["params"]) == 4


def test_ai_monitor_manual_signal_keeps_exact_stale_evidence_without_expiring(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2026, 8, 17, 10, 6, tzinfo=UTC).timestamp()
    monkeypatch.setattr(live_engine.store, "query", lambda *_args, **_kwargs: [_prediction_row()])
    monkeypatch.setattr(live_engine.time, "time", lambda: now)

    account = _account()
    direction, _atr, _basis, signal_time, evidence = live_engine._ai_monitor_signal(
        account,
        "AAPLUSDT",
        price=102,
        prediction_public_id="prediction-public-id",
        opportunity_public_id="opportunity-public-id",
    )

    assert direction == 1
    assert evidence["manual_selection"] is True
    assert signal_time is not None
    assert evidence["valid_until"] < now


def test_ai_monitor_manual_pending_opportunity_overrides_research_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2026, 8, 17, 10, 6, tzinfo=UTC).timestamp()
    row = _prediction_row(entry_ready=False)
    row["prediction_public_id"] = None
    row["confidence_score"] = 55
    captured: dict = {}

    def query(statement: str, params: tuple) -> list[dict]:
        captured["statement"] = statement
        captured["params"] = params
        return [row]

    monkeypatch.setattr(live_engine.store, "query", query)
    monkeypatch.setattr(live_engine.time, "time", lambda: now)

    direction, _atr, basis, signal_time, evidence = live_engine._ai_monitor_signal(
        _account(),
        "AAPLUSDT",
        price=102,
        opportunity_public_id="opportunity-public-id",
    )

    assert direction == 1
    assert signal_time is not None
    assert "FROM ai_monitor_opportunities o" in captured["statement"]
    assert "ai_monitor_predictions" not in captured["statement"]
    assert captured["params"] == (7, "AAPLUSDT", "opportunity-public-id")
    assert evidence["manual_selection"] is True
    assert evidence["manual_gate_override"] is True
    assert "准入：人工确认覆盖研究门槛" in basis


def test_manual_follow_is_idempotent_per_confirmation_but_allows_a_new_confirmation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    account = _account()
    account["config_json"].update(
        {
            "position_mode": "hedge",
            "max_positions": 10,
        }
    )
    snapshot = SimpleNamespace(
        account_type="UM_FUTURE",
        positions=(),
        wallet_balance=1_000,
        available_balance=1_000,
        unrealized_pnl=0,
    )
    monkeypatch.setattr(live_engine, "_active_accounts", lambda *_args: [account])
    monkeypatch.setattr(live_engine, "_strategy_universe", lambda *_args: ["AAPLUSDT"])
    monkeypatch.setattr(live_engine, "_account_service", SimpleNamespace(account=lambda *_args, **_kwargs: snapshot))
    monkeypatch.setattr(
        live_engine,
        "_trading_client",
        SimpleNamespace(ticker_price=lambda _symbol: Decimal("100")),
    )
    monkeypatch.setattr(live_engine, "_credentials", lambda *_args: ("key", "secret"))
    monkeypatch.setattr(live_engine, "_cached_position_mode", lambda *_args: "hedge")
    monkeypatch.setattr(live_engine, "_reconcile_intents", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(live_engine, "_managed_positions", lambda *_args: {})
    monkeypatch.setattr(live_engine, "_protection_counts", lambda *_args: {})
    monkeypatch.setattr(live_engine, "_risk_review_warnings", lambda *_args: [])
    monkeypatch.setattr(live_engine, "_current_stop_prices", lambda *_args: {})
    monkeypatch.setattr(live_engine, "_current_open_risk", lambda *_args, **_kwargs: 0)
    monkeypatch.setattr(
        live_engine,
        "policy_from_config",
        lambda *_args: SimpleNamespace(max_ticker_age_seconds=120, round_trip_cost_bps=16),
    )
    monkeypatch.setattr(live_engine, "_entry_loss_guard", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(
        live_engine,
        "market_data_freshness",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("manual follow must not inspect cached ticker freshness")
        ),
    )
    monkeypatch.setattr(
        live_engine,
        "symbol_admission",
        lambda *_args, **_kwargs: SimpleNamespace(allowed=True),
    )

    monkeypatch.setattr(
        live_engine,
        "_ai_monitor_signal",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("manual follow must not run automatic signal admission")
        ),
    )
    monkeypatch.setattr(
        live_engine,
        "_signal_is_fresh",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("manual follow must not apply the automatic freshness window")
        ),
    )
    intent_keys: list[str] = []

    def manual_intent(_account: dict, signal_key: str) -> dict | None:
        intent_keys.append(signal_key)
        if signal_key.endswith("manual:11111111-1111-4111-8111-111111111111"):
            return {
                "public_id": "intent-public-id",
                "status": "filled",
                "quantity": 1,
            }
        return None

    monkeypatch.setattr(live_engine, "_manual_intent", manual_intent)
    monkeypatch.setattr(
        live_engine,
        "_open_position",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("filled manual signal must not submit a second order")
        ),
    )

    result = live_engine.execute_ai_monitor_manual_follow(
        user_id=7,
        live_account_id=1,
        opportunity_public_id="opportunity-public-id",
        prediction_public_id="prediction-public-id",
        manual_attempt_id="11111111-1111-4111-8111-111111111111",
        expected_symbol="AAPLUSDT",
        expected_direction="long",
        selected_at=datetime(2026, 8, 17, 10, 0, tzinfo=UTC),
        selected_evidence={"risk_plan": {"stop_loss_pct": 1.5, "take_profit_pct": 3}},
        selected_score=25,
    )

    assert result["status"] == "duplicate"
    assert result["intent"]["id"] == "intent-public-id"
    assert intent_keys[-1].endswith(
        "manual:11111111-1111-4111-8111-111111111111"
    )

    opened: dict = {}

    def open_position(*_args, **kwargs) -> bool:
        opened.update(kwargs)
        return True

    monkeypatch.setattr(live_engine, "_open_position", open_position)
    retried = live_engine.execute_ai_monitor_manual_follow(
        user_id=7,
        live_account_id=1,
        opportunity_public_id="opportunity-public-id",
        prediction_public_id="prediction-public-id",
        manual_attempt_id="22222222-2222-4222-8222-222222222222",
        expected_symbol="AAPLUSDT",
        expected_direction="long",
        selected_at=datetime(2026, 8, 17, 10, 0, tzinfo=UTC),
        selected_evidence={"risk_plan": {"stop_loss_pct": 1.5, "take_profit_pct": 3}},
        selected_score=25,
    )

    assert retried["status"] == "filled"
    assert opened["signal_key_suffix"] == (
        "manual:22222222-2222-4222-8222-222222222222"
    )
    assert opened["signal_evidence"]["manual_attempt_id"] == (
        "22222222-2222-4222-8222-222222222222"
    )
    assert opened["signal_evidence"]["manual_signal_override"] is True
    assert opened["signal_evidence"]["execution_price_source"] == "binance_live_ticker"
    assert "ticker_cache_freshness" in opened["signal_evidence"]["automatic_checks_bypassed"]


def test_ai_monitor_live_signal_fails_closed_when_disabled_or_gate_not_ready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        live_engine.store,
        "query",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("disabled live copy must not query predictions")
        ),
    )
    assert live_engine._ai_monitor_signal(_account(enabled=False), "AAPLUSDT")[0] == 0

    monkeypatch.setattr(live_engine.store, "query", lambda *_args, **_kwargs: [_prediction_row(entry_ready=False)])
    assert live_engine._ai_monitor_signal(_account(), "AAPLUSDT")[0] == 0


def test_ai_monitor_live_signal_only_enters_during_us_regular_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2026, 8, 17, 10, 1, tzinfo=UTC).timestamp()
    monkeypatch.setattr(live_engine.time, "time", lambda: now)
    monkeypatch.setattr(
        live_engine.store,
        "query",
        lambda *_args, **_kwargs: [_prediction_row()],
    )
    account = _account()
    account["config_json"]["ai_monitor_live_regular_session_only"] = True

    monkeypatch.setattr(
        live_engine.macro_market,
        "us_market_session",
        lambda _now: {"key": "premarket"},
    )
    assert live_engine._ai_monitor_signal(account, "AAPLUSDT", price=102)[0] == 0

    monkeypatch.setattr(
        live_engine.macro_market,
        "us_market_session",
        lambda _now: {"key": "regular"},
    )
    direction, _atr, _basis, _signal_time, evidence = live_engine._ai_monitor_signal(
        account,
        "AAPLUSDT",
        price=102,
    )
    assert direction == 1
    assert evidence["execution_venue"] == "binance_usdm"
    assert evidence["execution_price_source"] == "binance"
    assert evidence["regular_session_only"] is True


def test_ai_monitor_live_signal_honors_independent_direction_switches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2026, 8, 17, 10, 1, tzinfo=UTC).timestamp()
    monkeypatch.setattr(live_engine.time, "time", lambda: now)
    monkeypatch.setattr(
        live_engine.store,
        "query",
        lambda *_args, **_kwargs: [_prediction_row(direction="short")],
    )
    account = _account()
    account["config_json"]["ai_monitor_live_allow_short"] = False
    assert live_engine._ai_monitor_signal(account, "AAPLUSDT", price=100)[0] == 0

    account["config_json"]["ai_monitor_live_allow_short"] = True
    assert live_engine._ai_monitor_signal(account, "AAPLUSDT", price=100)[0] == -1


def test_ai_monitor_live_signal_expires_without_falling_back_to_strategy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(live_engine.store, "query", lambda *_args, **_kwargs: [_prediction_row()])
    expired = datetime(2026, 8, 17, 10, 6, tzinfo=UTC).timestamp()
    monkeypatch.setattr(live_engine.time, "time", lambda: expired)
    monkeypatch.setattr(
        live_engine,
        "_strategy_signal",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("AI source must never fall back to the frozen strategy")
        ),
    )

    assert live_engine._execution_signal(_account(), "AAPLUSDT")[0] == 0


def test_legacy_strategy_account_cannot_consume_ai_monitor_signals(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    legacy = _account()
    legacy["config_json"].pop("execution_scope")
    monkeypatch.setattr(
        live_engine,
        "_ai_monitor_signal",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("ordinary live strategy must not consume AI signals")
        ),
    )
    monkeypatch.setattr(
        live_engine,
        "_strategy_signal",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("legacy AI source must fail closed, not resume strategy")
        ),
    )

    assert live_engine._execution_signal(legacy, "AAPLUSDT") == (0, None, [], None, {})


def test_independent_ai_account_runs_when_ordinary_live_server_is_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = [
        {
            "id": 1,
            "config_json": {"signal_source": "strategy"},
            "strategy_snapshot_json": {},
        },
        {
            "id": 2,
            "config_json": {
                "signal_source": "ai_monitor",
                "execution_scope": "ai_monitor",
            },
            "strategy_snapshot_json": {},
        },
    ]
    monkeypatch.setattr(live_engine.store, "query", lambda *_args, **_kwargs: rows)
    monkeypatch.setattr(
        live_engine,
        "_settings",
        SimpleNamespace(binance_live_trading_enabled=False),
    )

    assert [item["id"] for item in live_engine._active_accounts()] == [2]


def test_ai_monitor_live_copy_ui_enables_with_one_confirmation_button() -> None:
    frontend = (ROOT / "src/quantdesk_v2/static/ai-monitor.js").read_text(encoding="utf-8")
    api = (ROOT / "src/quantdesk_v2/interfaces/api/ai_monitor.py").read_text(encoding="utf-8")

    assert 'id="live-copy-toggle"' in frontend
    assert 'id="live-copy-modal"' in frontend
    assert 'id="live-copy-config-button"' in frontend
    assert 'id="live-copy-config-modal"' in frontend
    assert 'id="live-copy-history-button"' in frontend
    assert 'id="live-copy-history-modal"' in frontend
    assert 'aria-busy="true"' in frontend
    assert "button.disabled = false" in frontend
    assert "void this.loadLiveCopyStatus" in frontend
    assert "data-live-copy-retry" in frontend
    assert 'data-live-copy-mode="enable"' in frontend
    assert "确认开启独立实盘跟单" in frontend
    assert 'name="confirmation_name"' not in frontend
    assert "我确认本页 AI 信号会通过独立执行域" not in frontend
    assert "只接收开启后生成" in frontend
    assert '@router.put("/live-copy")' in api
    assert "payload.confirmation_name" not in api
    assert '@router.put("/live-copy/config")' in api
    assert '@router.get("/live-copy/history")' in api
    assert "income_history" in api
    assert "ai_monitor_live_allow_short" in api
    assert '"position_size_basis": payload.position_size_basis' in api
    assert '"copy_total_amount": payload.copy_total_amount' in api
    assert "_ensure_ai_monitor_live_account" in api
    assert '"execution_scope": _AI_MONITOR_LIVE_SCOPE' in api
    assert '"ordinary_strategy_switch_independent": True' in api
    assert "LiveOrderIntent.status == \"unknown\"" in api
    assert '"ai_monitor_live_copy_enabled_at": utcnow().isoformat()' in api
    assert '"existing_positions_closed": False' in api
    assert "发现机会独立实盘跟单" in frontend
    assert "不读取、不启停、不改写实盘交易页的其他策略" in frontend
    assert "仅在美股常规交易时段允许新开仓" in frontend


def test_manual_follow_ui_and_api_require_second_real_funds_confirmation() -> None:
    frontend = (ROOT / "src/quantdesk_v2/static/ai-monitor.js").read_text(encoding="utf-8")
    api = (ROOT / "src/quantdesk_v2/interfaces/api/ai_monitor.py").read_text(encoding="utf-8")

    assert 'data-manual-follow="${this.escape(item.id)}"' in frontend
    assert 'id="manual-follow-modal"' in frontend
    assert "data-manual-follow-form" in frontend
    assert "tickManualFollowCountdown" not in frontend
    assert "跟买已过期" not in frontend
    assert "待评估机会将由人工确认覆盖研究准入门槛" in frontend
    assert 'prediction_id: String(data.get("prediction_id") || "") || null' in frontend
    assert "globalThis.crypto.randomUUID()" in frontend
    assert 'name="manual_attempt_id"' in frontend
    assert 'manual_attempt_id: String(data.get("manual_attempt_id") || "")' in frontend
    assert "当前信号会开空仓，不是买入现货" in frontend
    assert 'this.api("/live-copy/manual-follow"' in frontend
    assert '@router.post("/live-copy/manual-follow")' in api
    assert "if not payload.acknowledge_real_funds" in api
    assert "execute_ai_monitor_manual_follow" in api
    assert "直接使用 Binance 即时合约价格开仓" in frontend
    assert "正在提交 Binance" in frontend
    assert "opportunity.status not in" not in api
    assert "交易、盈亏和结算价格均以 Binance 映射合约为准" in frontend
    assert 'name="position_size_basis"' in frontend
    assert 'value="copy_total_amount"' in frontend
    assert 'name="copy_total_amount"' in frontend
    assert "固定跟单总金额" in frontend
    assert "跟单总金额（USDT）" in frontend
    assert 'href="/next/#/live"' not in frontend
