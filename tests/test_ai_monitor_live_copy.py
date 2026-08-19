from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from quantdesk_v2 import live_engine
from quantdesk_v2.schemas import AiMonitorLiveCopyConfigUpdate, AiMonitorLiveCopyUpdate

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
        },
    }


def test_live_copy_schema_requires_explicit_true_only_at_endpoint_boundary() -> None:
    disabled = AiMonitorLiveCopyUpdate(enabled=False)

    assert disabled.account_id is None
    assert disabled.acknowledge_real_funds is False
    with pytest.raises(ValidationError):
        AiMonitorLiveCopyUpdate(enabled=True, unexpected=True)  # type: ignore[call-arg]


def test_live_copy_config_validates_direction_and_risk_boundaries() -> None:
    policy = AiMonitorLiveCopyConfigUpdate(position_mode="hedge", allow_short=True)

    assert policy.position_mode == "hedge"
    assert policy.minimum_combined_score == 70
    with pytest.raises(ValidationError):
        AiMonitorLiveCopyConfigUpdate(allow_long=False, allow_short=False)
    with pytest.raises(ValidationError):
        AiMonitorLiveCopyConfigUpdate(risk_per_trade_pct=4, max_total_risk_pct=2)


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
    assert captured["params"][0:2] == (7, "AAPLUSDT")
    assert any("组合评分" in item for item in basis)
    policy = live_engine.policy_from_config(_account()["config_json"])
    assert live_engine._signal_is_fresh(_account(), signal_time, policy, evidence)


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


def test_ai_monitor_live_copy_ui_requires_modal_confirmation() -> None:
    frontend = (ROOT / "src/quantdesk_v2/static/ai-monitor.js").read_text(encoding="utf-8")
    api = (ROOT / "src/quantdesk_v2/interfaces/api/ai_monitor.py").read_text(encoding="utf-8")

    assert 'id="live-copy-toggle"' in frontend
    assert 'id="live-copy-modal"' in frontend
    assert 'id="live-copy-config-button"' in frontend
    assert 'id="live-copy-config-modal"' in frontend
    assert 'aria-busy="true"' in frontend
    assert "button.disabled = false" in frontend
    assert "void this.loadLiveCopyStatus" in frontend
    assert "data-live-copy-retry" in frontend
    assert "acknowledge_real_funds" in frontend
    assert "只接收开启后生成" in frontend
    assert '@router.put("/live-copy")' in api
    assert '@router.put("/live-copy/config")' in api
    assert "ai_monitor_live_allow_short" in api
    assert "_ensure_ai_monitor_live_account" in api
    assert '"execution_scope": _AI_MONITOR_LIVE_SCOPE' in api
    assert '"ordinary_strategy_switch_independent": True' in api
    assert "LiveOrderIntent.status == \"unknown\"" in api
    assert '"ai_monitor_live_copy_enabled_at": utcnow().isoformat()' in api
    assert '"existing_positions_closed": False' in api
    assert "发现机会独立实盘跟单" in frontend
    assert "不读取、不启停、不改写实盘交易页的其他策略" in frontend
    assert "仅在美股常规交易时段允许新开仓" in frontend
    assert "交易、盈亏和结算价格均以 Binance 映射合约为准" in frontend
    assert 'href="/next/#/live"' not in frontend
