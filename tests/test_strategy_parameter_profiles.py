from __future__ import annotations

import json

from quantdesk_v2.application.strategy_parameter_profiles import (
    apply_parameter_profile,
    effective_execution_config,
    load_effective_parameter_profile,
)


def _query(_sql: str, params: tuple[object, ...]) -> list[dict[str, object]]:
    symbol = str(params[-1])
    rows: list[dict[str, object]] = [
        {
            "scope_key": "*",
            "parameters_json": json.dumps({"period": 21, "threshold": 1.5}),
            "execution_json": json.dumps({"leverage": 3, "position_size_pct": 8}),
        }
    ]
    if symbol == "AAPLUSDT":
        rows.append(
            {
                "scope_key": "AAPLUSDT",
                "parameters_json": {"threshold": 2.25},
                "execution_json": {"leverage": 7, "take_profit_pct": 12},
            }
        )
    return rows


def test_exact_symbol_profile_overrides_strategy_default() -> None:
    profile = load_effective_parameter_profile(
        query=_query,
        user_id=7,
        strategy_id=11,
        strategy_public_id=None,
        symbol="aaplusdt",
    )

    assert profile == {
        "parameters": {"period": 21, "threshold": 2.25},
        "execution": {"leverage": 7, "position_size_pct": 8, "take_profit_pct": 12},
        "scopes": ["default", "AAPLUSDT"],
    }


def test_default_profile_is_used_when_symbol_has_no_override() -> None:
    profile = load_effective_parameter_profile(
        query=_query,
        user_id=7,
        strategy_id=11,
        strategy_public_id=None,
        symbol="MSFTUSDT",
    )

    assert profile["parameters"] == {"period": 21, "threshold": 1.5}
    assert profile["execution"] == {"leverage": 3, "position_size_pct": 8}
    assert profile["scopes"] == ["default"]


def test_strategy_snapshot_and_account_config_receive_same_effective_profile() -> None:
    account = {
        "user_id": 7,
        "strategy_id": 11,
        "config_json": {"leverage": 1, "fee_bps": 4},
    }
    snapshot = {
        "public_id": "strategy-public-id",
        "parameters": {"period": 14, "threshold": 1},
    }

    effective_account, effective_snapshot, profile = apply_parameter_profile(
        account,
        snapshot,
        "AAPLUSDT",
        query=_query,
    )

    assert effective_snapshot["parameters"] == {"period": 21, "threshold": 2.25}
    assert effective_account["config_json"] == {
        "leverage": 7,
        "fee_bps": 4,
        "position_size_pct": 8,
        "take_profit_pct": 12,
    }
    assert profile["scopes"] == ["default", "AAPLUSDT"]
    assert account["config_json"] == {"leverage": 1, "fee_bps": 4}
    assert snapshot["parameters"] == {"period": 14, "threshold": 1}


def test_execution_config_uses_default_then_symbol_override() -> None:
    account = {
        "user_id": 7,
        "strategy_id": 11,
        "config_json": {"leverage": 1, "fee_bps": 4},
    }

    assert effective_execution_config(account, "AAPLUSDT", query=_query) == {
        "leverage": 7,
        "fee_bps": 4,
        "position_size_pct": 8,
        "take_profit_pct": 12,
    }
    assert effective_execution_config(account, "MSFTUSDT", query=_query) == {
        "leverage": 3,
        "fee_bps": 4,
        "position_size_pct": 8,
    }


def test_execution_config_without_deployed_strategy_is_unchanged() -> None:
    account = {
        "user_id": 7,
        "strategy_id": None,
        "config_json": {"leverage": 2},
    }

    assert effective_execution_config(account, "AAPLUSDT", query=_query) == {"leverage": 2}
