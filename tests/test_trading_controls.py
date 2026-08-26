from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import pytest

from quantdesk_v2.trading_controls import (
    account_symbol_scope_key,
    effective_control_blockers,
    strategy_revision_scope_key,
    user_owner_scope,
)


def test_scope_keys_are_canonical_and_tenant_bound() -> None:
    assert user_owner_scope(7) == "user:7"
    assert (
        strategy_revision_scope_key(
            "AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA",
            12,
        )
        == "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa@12"
    )
    assert (
        account_symbol_scope_key(
            "BBBBBBBB-BBBB-4BBB-8BBB-BBBBBBBBBBBB",
            "aaplusdt",
        )
        == "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb:AAPLUSDT"
    )


@pytest.mark.parametrize(
    ("user_id", "strategy_id", "version", "account_id", "symbol"),
    [
        (0, "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa", 1, "b" * 36, "AAPLUSDT"),
        (1, "not-a-uuid", 1, "b" * 36, "AAPLUSDT"),
        (1, "a" * 36, 0, "b" * 36, "AAPLUSDT"),
        (1, "a" * 36, 1, "not-a-uuid", "AAPLUSDT"),
        (1, "a" * 36, 1, "b" * 36, "bad symbol"),
    ],
)
def test_invalid_scope_bindings_fail_closed(
    user_id: int,
    strategy_id: str,
    version: int,
    account_id: str,
    symbol: str,
) -> None:
    with pytest.raises(ValueError):
        effective_control_blockers(
            lambda *_args: [],
            owner_user_id=user_id,
            account_public_id=account_id,
            strategy_public_id=strategy_id,
            strategy_revision_version=version,
            symbol=symbol,
        )


def test_effective_control_query_includes_every_hierarchical_scope() -> None:
    captured: dict[str, Any] = {}

    def query(sql: str, params: Sequence[Any]) -> list[dict[str, Any]]:
        captured["sql"] = sql
        captured["params"] = tuple(params)
        return [{"scope_type": "account", "scope_key": params[4]}]

    blockers = effective_control_blockers(
        query,
        owner_user_id=9,
        account_public_id="11111111-1111-4111-8111-111111111111",
        strategy_public_id="22222222-2222-4222-8222-222222222222",
        strategy_revision_version=3,
        symbol="AAPLUSDT",
    )

    assert blockers == [
        {
            "scope_type": "account",
            "scope_key": "11111111-1111-4111-8111-111111111111",
        }
    ]
    assert "scope_type='global'" in captured["sql"]
    assert "scope_type='strategy_revision'" in captured["sql"]
    assert "scope_type='broker_connection'" in captured["sql"]
    assert captured["params"] == (
        "*:AAPLUSDT",
        "market_data",
        "binance-usdm",
        "user:9",
        "11111111-1111-4111-8111-111111111111",
        "22222222-2222-4222-8222-222222222222@3",
        "11111111-1111-4111-8111-111111111111:AAPLUSDT",
        "market_data",
        "binance-usdm",
    )
