"""Hierarchical, persistent trading-control helpers.

The live worker consults this module immediately before every risk-increasing
write.  Risk-reducing closes and protective orders deliberately remain
available while a latch is engaged.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from typing import Any

CONTROL_SCOPE_TYPES = frozenset(
    {
        "global",
        "account",
        "strategy_revision",
        "symbol",
        "data_source",
        "broker_connection",
    }
)

_PUBLIC_ID = re.compile(r"[0-9a-fA-F-]{36}")
_SYMBOL = re.compile(r"[A-Z0-9][A-Z0-9._-]{1,31}")
_SERVICE_KEY = re.compile(r"[a-z0-9][a-z0-9._:/-]{0,63}")


def user_owner_scope(user_id: int) -> str:
    if isinstance(user_id, bool) or not isinstance(user_id, int) or user_id < 1:
        raise ValueError("user_id must be a positive integer")
    return f"user:{user_id}"


def strategy_revision_scope_key(strategy_public_id: str, version: int) -> str:
    public_id = str(strategy_public_id).strip().lower()
    if not _PUBLIC_ID.fullmatch(public_id):
        raise ValueError("invalid strategy public id")
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        raise ValueError("strategy revision version must be positive")
    return f"{public_id}@{version}"


def account_symbol_scope_key(account_public_id: str, symbol: str) -> str:
    account_id = str(account_public_id).strip().lower()
    normalized_symbol = str(symbol).strip().upper()
    if not _PUBLIC_ID.fullmatch(account_id):
        raise ValueError("invalid account public id")
    if not _SYMBOL.fullmatch(normalized_symbol):
        raise ValueError("invalid symbol")
    return f"{account_id}:{normalized_symbol}"


def normalize_service_key(value: str) -> str:
    normalized = str(value).strip().lower()
    if not _SERVICE_KEY.fullmatch(normalized):
        raise ValueError("invalid service control key")
    return normalized


def normalize_symbol(value: str) -> str:
    normalized = str(value).strip().upper()
    if not _SYMBOL.fullmatch(normalized):
        raise ValueError("invalid symbol")
    return normalized


def effective_control_blockers(
    query: Callable[[str, Sequence[Any]], Sequence[Mapping[str, Any]]],
    *,
    owner_user_id: int,
    account_public_id: str,
    strategy_public_id: str,
    strategy_revision_version: int,
    symbol: str,
    data_source: str = "market_data",
    broker_connection: str = "binance-usdm",
) -> list[dict[str, Any]]:
    """Return every engaged latch that blocks a new exposure decision."""

    owner_scope = user_owner_scope(owner_user_id)
    account_id = str(account_public_id).strip().lower()
    if not _PUBLIC_ID.fullmatch(account_id):
        raise ValueError("invalid account public id")
    revision_key = strategy_revision_scope_key(
        strategy_public_id,
        strategy_revision_version,
    )
    normalized_symbol = normalize_symbol(symbol)
    symbol_key = account_symbol_scope_key(account_id, normalized_symbol)
    global_symbol_key = f"*:{normalized_symbol}"
    source_key = normalize_service_key(data_source)
    broker_key = normalize_service_key(broker_connection)
    rows = query(
        """SELECT public_id,owner_scope,scope_type,scope_key,reason_code,
                  reason_text,version,changed_at
           FROM trading_control_latches
           WHERE engaged=1
             AND (
                 (owner_scope='global' AND (
                     (scope_type='global' AND scope_key='*')
                     OR (scope_type='symbol' AND scope_key=?)
                     OR (scope_type='data_source' AND scope_key=?)
                     OR (scope_type='broker_connection' AND scope_key=?)
                 ))
                 OR (owner_scope=? AND (
                     (scope_type='account' AND scope_key=?)
                     OR (scope_type='strategy_revision' AND scope_key=?)
                     OR (scope_type='symbol' AND scope_key=?)
                     OR (scope_type='data_source' AND scope_key=?)
                     OR (scope_type='broker_connection' AND scope_key=?)
                 ))
             )
           ORDER BY FIELD(scope_type,'global','broker_connection','data_source',
                          'account','strategy_revision','symbol'),id""",
        (
            global_symbol_key,
            source_key,
            broker_key,
            owner_scope,
            account_id,
            revision_key,
            symbol_key,
            source_key,
            broker_key,
        ),
    )
    return [dict(row) for row in rows]
