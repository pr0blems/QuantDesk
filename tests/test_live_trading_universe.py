from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from quantdesk_v2.api import _live_risk_config, _lock_and_revalidate_live_arm
from quantdesk_v2.live_engine import (
    _position_key,
    _strategy_position_side,
    _strategy_universe,
)
from quantdesk_v2.market_config import tradfi_symbols
from quantdesk_v2.schemas import (
    LiveAccountCreateRequest,
    LiveAccountStatusUpdate,
    LiveAccountStrategyUpdate,
)


def test_live_account_request_does_not_accept_a_client_owned_symbol_list() -> None:
    payload = {
        "name": "股票实盘",
        "strategy_id": "00000000-0000-0000-0000-000000000001",
        "leverage": 3,
        "max_positions": 20,
        "position_size_pct": 2,
        "margin_cap": 0.2,
    }

    request = LiveAccountCreateRequest.model_validate(payload)
    assert not hasattr(request, "symbols")
    assert request.risk_per_trade_pct == 0.5
    assert request.max_total_risk_pct == 4
    assert request.risk_max_leverage == 10
    assert request.block_high_risk_products is True

    with pytest.raises(ValidationError):
        LiveAccountCreateRequest.model_validate({**payload, "symbols": ["BTCUSDT"]})

    with pytest.raises(ValidationError):
        LiveAccountCreateRequest.model_validate({**payload, "max_positions": 21})


def test_live_strategy_adjustment_supports_up_to_twenty_positions() -> None:
    payload = {
        "strategy_id": "00000000-0000-0000-0000-000000000001",
        "leverage": 3,
        "max_positions": 20,
        "position_size_pct": 2,
        "margin_cap": 0.2,
    }

    update = LiveAccountStrategyUpdate.model_validate(payload)

    assert update.max_positions == 20
    assert update.risk_per_trade_pct is None
    assert update.max_total_risk_pct is None
    with pytest.raises(ValidationError):
        LiveAccountStrategyUpdate.model_validate({**payload, "max_positions": 21})


def test_strategy_update_preserves_existing_new_risk_controls() -> None:
    update = LiveAccountStrategyUpdate.model_validate(
        {
            "strategy_id": "00000000-0000-0000-0000-000000000001",
            "leverage": 3,
            "max_positions": 20,
            "position_size_pct": 2,
            "margin_cap": 0.2,
        }
    )

    config = _live_risk_config(
        update,
        {"risk_per_trade_pct": 0.75, "round_trip_cost_bps": 22},
    )

    assert config["risk_per_trade_pct"] == 0.75
    assert config["round_trip_cost_bps"] == 22
    assert config["risk_max_leverage"] == 10


def test_live_frontend_exposes_strategy_adjustment_and_twenty_position_limit() -> None:
    source = (
        Path(__file__).parents[1] / "web" / "src" / "controllers" / "live.js"
    ).read_text(encoding="utf-8")

    assert 'id="live-adjust"' in source
    assert 'id="live-max-positions" type="number" min="1" max="20"' in source
    assert 'id="live-risk-per-trade"' in source
    assert 'id="live-total-risk"' in source
    assert 'id="live-liq-buffer"' in source
    assert 'id="live-ticker-age"' in source
    assert 'id="live-signal-age"' in source
    assert 'id="live-block-high-risk"' in source
    assert 'margin_cap: Number(this.q("#live-cap").value) / 100' in source
    assert 'method: "PUT"' in source
    assert "/strategy`" in source


def test_live_account_update_accepts_rename_and_rejects_empty_change() -> None:
    update = LiveAccountStatusUpdate.model_validate({"name": "新的实盘名称"})

    assert update.name == "新的实盘名称"
    assert update.status is None
    with pytest.raises(ValidationError):
        LiveAccountStatusUpdate.model_validate({})


def test_live_arm_rechecks_competing_account_after_preflight_under_lock() -> None:
    ciphertext = "encrypted-value"
    locked_user = SimpleNamespace(
        binance_credentials_configured=True,
        binance_permissions={"requested": ["READ", "TRADE"]},
        binance_key_version=4,
        binance_api_key_encrypted="encrypted-key",
        binance_api_secret_encrypted=ciphertext,
    )
    target = SimpleNamespace(id=11, name="股票实盘")

    class FakeSession:
        def __init__(self) -> None:
            self.responses = iter((locked_user, target, 12))
            self.statements: list[object] = []

        def scalar(self, statement):
            self.statements.append(statement)
            return next(self.responses)

    db = FakeSession()

    with pytest.raises(HTTPException, match="pause the other live deployment first") as error:
        _lock_and_revalidate_live_arm(
            db,  # type: ignore[arg-type]
            user_id=7,
            account_id="00000000-0000-0000-0000-000000000011",
            confirmation_name="股票实盘",
            credential_version=4,
            encrypted_key="encrypted-key",
            encrypted_secret=ciphertext,
        )

    assert error.value.status_code == 409
    assert "FOR UPDATE" in str(db.statements[0])
    assert "FOR UPDATE" in str(db.statements[1])


def test_live_engine_uses_the_full_paper_universe_not_five_symbols() -> None:
    universe = tradfi_symbols()

    assert len(universe) == 150
    assert "BTCUSDT" not in universe
    assert "ETHUSDT" not in universe
    assert _strategy_universe({"symbols": ["BTCUSDT"]}) == universe


def test_live_engine_only_applies_server_preflight_eligibility() -> None:
    universe = tradfi_symbols()
    selected = [universe[7], universe[2]]

    assert _strategy_universe(
        {"eligible_symbols": [selected[0], "BTCUSDT", selected[1]]}
    ) == [universe[2], universe[7]]


def test_hedge_mode_keeps_long_and_short_position_keys_separate() -> None:
    assert _strategy_position_side("one_way", 1) == "BOTH"
    assert _strategy_position_side("hedge", 1) == "LONG"
    assert _strategy_position_side("hedge", -1) == "SHORT"
    assert _position_key({"symbol": "AAPLUSDT", "position_side": "LONG"}) == (
        "AAPLUSDT",
        "LONG",
    )
    assert _position_key({"symbol": "AAPLUSDT", "position_side": "SHORT"}) == (
        "AAPLUSDT",
        "SHORT",
    )
