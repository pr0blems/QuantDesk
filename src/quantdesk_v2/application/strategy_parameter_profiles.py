"""Resolve mutable strategy runtime parameters without changing revisions."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from typing import Any

QueryRows = Callable[[str, tuple[Any, ...]], Sequence[Mapping[str, Any]]]

EXECUTION_PARAMETER_KEYS = frozenset(
    {
        "position_size_pct",
        "leverage",
        "margin_mode",
        "fee_bps",
        "slippage_bps",
        "stop_loss_pct",
        "take_profit_pct",
        "max_holding_bars",
    }
)


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            return {}
        return dict(parsed) if isinstance(parsed, Mapping) else {}
    return {}


def load_effective_parameter_profile(
    *,
    query: QueryRows,
    user_id: int,
    strategy_id: int | None,
    strategy_public_id: str | None,
    symbol: str,
) -> dict[str, Any]:
    """Merge strategy default and exact-symbol profiles, exact symbol last."""

    normalized_symbol = str(symbol or "").strip().upper()
    if not normalized_symbol or (strategy_id is None and not strategy_public_id):
        return {"parameters": {}, "execution": {}, "scopes": []}
    if strategy_public_id:
        rows = query(
            """SELECT p.scope_key,p.parameters_json,p.execution_json
                 FROM strategy_parameter_profiles p
                 JOIN user_strategies s
                   ON s.id=p.strategy_id AND s.user_id=p.user_id
                WHERE p.user_id=? AND s.public_id=?
                  AND p.scope_key IN ('*',?)
                ORDER BY CASE WHEN p.scope_key='*' THEN 0 ELSE 1 END,p.id""",
            (user_id, strategy_public_id, normalized_symbol),
        )
    else:
        rows = query(
            """SELECT scope_key,parameters_json,execution_json
                 FROM strategy_parameter_profiles
                WHERE user_id=? AND strategy_id=?
                  AND scope_key IN ('*',?)
                ORDER BY CASE WHEN scope_key='*' THEN 0 ELSE 1 END,id""",
            (user_id, strategy_id, normalized_symbol),
        )
    parameters: dict[str, Any] = {}
    execution: dict[str, Any] = {}
    scopes: list[str] = []
    for row in rows:
        scope_key = str(row.get("scope_key") or "")
        parameters.update(_json_object(row.get("parameters_json")))
        execution.update(
            {
                key: value
                for key, value in _json_object(row.get("execution_json")).items()
                if key in EXECUTION_PARAMETER_KEYS
            }
        )
        if scope_key:
            scopes.append("default" if scope_key == "*" else scope_key)
    return {"parameters": parameters, "execution": execution, "scopes": scopes}


def apply_parameter_profile(
    account: Mapping[str, Any],
    snapshot: Mapping[str, Any],
    symbol: str,
    *,
    query: QueryRows,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Return copied account/snapshot values with the effective profile applied."""

    profile = load_effective_parameter_profile(
        query=query,
        user_id=int(account["user_id"]),
        strategy_id=(int(account["strategy_id"]) if account.get("strategy_id") else None),
        strategy_public_id=(str(snapshot.get("public_id")) if snapshot.get("public_id") else None),
        symbol=symbol,
    )
    selected = dict(snapshot)
    selected_parameters = _json_object(selected.get("parameters"))
    selected_parameters.update(profile["parameters"])
    selected["parameters"] = selected_parameters

    effective_account = dict(account)
    effective_config = _json_object(account.get("config_json"))
    effective_config.update(profile["execution"])
    effective_account["config_json"] = effective_config
    return effective_account, selected, profile


def effective_execution_config(
    account: Mapping[str, Any],
    symbol: str | None,
    *,
    query: QueryRows,
) -> dict[str, Any]:
    """Return account config overlaid by default and optional symbol settings."""

    config = _json_object(account.get("config_json"))
    normalized_symbol = str(symbol or "").strip().upper()
    if not account.get("strategy_id"):
        return config
    # A default-only lookup still needs a non-empty comparison value. The value
    # cannot collide with a valid exchange symbol, so only '*' is selected.
    profile = load_effective_parameter_profile(
        query=query,
        user_id=int(account["user_id"]),
        strategy_id=int(account["strategy_id"]),
        strategy_public_id=None,
        symbol=normalized_symbol or "__DEFAULT_ONLY__",
    )
    config.update(profile["execution"])
    return config
