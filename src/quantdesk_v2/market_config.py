"""Load the shared, non-secret market collector configuration."""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = Path(
    os.environ.get("QUANTDESK_CONFIG_DIR", PROJECT_ROOT / "config")
).expanduser().resolve()


def _load(name: str, default: Any) -> Any:
    path = CONFIG_DIR / name
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return default


settings = _load("settings.json", {})
symbols_meta = _load("tradfi_symbols.json", {"symbols": []})
TRADFI_UNIVERSE_KEY = "binance_tradfi_stocks"
_engine: Engine | None = None
_symbols_lock = threading.RLock()
_symbols_refresh_at = 0.0
_symbols_version = 1
_SYMBOL_REFRESH_SECONDS = 15.0


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        try:
            result = json.loads(value)
        except (TypeError, ValueError, json.JSONDecodeError):
            return {}
        return result if isinstance(result, dict) else {}
    return {}


def configure_engine(engine: Engine) -> None:
    """Use the persisted security mappings as the cross-process universe source."""

    global _engine, _symbols_refresh_at, symbols_meta
    with _symbols_lock:
        _engine = engine
        # Avoid leaking one app/test engine's database universe into another
        # engine when the new database is empty or still awaiting migration.
        symbols_meta = _load("tradfi_symbols.json", {"symbols": []})
        _symbols_refresh_at = 0.0
    refresh_symbols(force=True)


def load_persisted_tradfi_metadata(engine: Engine) -> list[dict[str, Any]]:
    """Read the database-backed universe without mutating the process cache."""

    try:
        with engine.connect() as connection:
            rows = list(
                connection.execute(
                    text(
                        """SELECT source_symbol,normalized_symbol,source_status,
                                  contract_type,underlying_type,onboard_date_ms,
                                  monitor_enabled,strategy_enabled,
                                  live_trading_enabled,source_metadata_json
                           FROM security_symbol_mappings
                           WHERE source='binance_tradfi'
                           ORDER BY onboard_date_ms,source_symbol"""
                    )
                ).mappings()
            )
    except SQLAlchemyError:
        return []
    updated_rows = []
    for row in rows:
        metadata = _json_object(row.get("source_metadata_json"))
        metadata.update(
            {
                "symbol": str(row["source_symbol"]),
                "pair": str(metadata.get("pair") or row["source_symbol"]),
                "status": str(row.get("source_status") or "UNKNOWN"),
                "contractType": str(
                    row.get("contract_type") or "TRADIFI_PERPETUAL"
                ),
                "underlyingType": str(row.get("underlying_type") or ""),
                "onboardDate": row.get("onboard_date_ms"),
                "normalizedSymbol": str(row.get("normalized_symbol") or ""),
                "monitorEnabled": bool(row.get("monitor_enabled")),
                "strategyEnabled": bool(row.get("strategy_enabled")),
                "liveTradingEnabled": bool(row.get("live_trading_enabled")),
            }
        )
        updated_rows.append(metadata)
    return updated_rows


def refresh_symbols(*, force: bool = False) -> bool:
    """Refresh the in-process cache; fall back to the packaged JSON during migration."""

    global symbols_meta, _symbols_refresh_at, _symbols_version
    now = time.monotonic()
    with _symbols_lock:
        if not force and now < _symbols_refresh_at:
            return False
        _symbols_refresh_at = now + _SYMBOL_REFRESH_SECONDS
        engine = _engine
    if engine is None:
        return False
    rows = load_persisted_tradfi_metadata(engine)
    if not rows:
        return False
    updated = {"count": len(rows), "source": "database", "symbols": rows}
    with _symbols_lock:
        old_signature = tuple(
            (
                item.get("symbol"),
                item.get("status"),
                item.get("monitorEnabled", True),
                item.get("strategyEnabled", True),
                item.get("liveTradingEnabled", True),
            )
            for item in symbols_meta.get("symbols", [])
        )
        new_signature = tuple(
            (
                item.get("symbol"),
                item.get("status"),
                item.get("monitorEnabled"),
                item.get("strategyEnabled"),
                item.get("liveTradingEnabled"),
            )
            for item in rows
        )
        symbols_meta = updated
        if old_signature != new_signature:
            _symbols_version += 1
            return True
    return False


def tradfi_metadata(*, purpose: str = "monitor") -> list[dict[str, Any]]:
    refresh_symbols()
    flag = {
        "monitor": "monitorEnabled",
        "strategy": "strategyEnabled",
        "live": "liveTradingEnabled",
    }.get(purpose)
    if flag is None:
        raise ValueError("unknown TradFi universe purpose")
    with _symbols_lock:
        rows = [dict(item) for item in symbols_meta.get("symbols", [])]
    return [
        item
        for item in rows
        if str(item.get("status") or "TRADING").upper() == "TRADING"
        and bool(item.get(flag, True))
    ]


def tradfi_symbols(*, purpose: str = "monitor") -> list[str]:
    return [item["symbol"] for item in tradfi_metadata(purpose=purpose)]


def packaged_tradfi_symbols() -> frozenset[str]:
    """Return the reviewed universe shipped before database-backed discovery."""

    payload = _load("tradfi_symbols.json", {"symbols": []})
    return frozenset(
        str(item.get("symbol") or "").strip().upper()
        for item in payload.get("symbols", [])
        if isinstance(item, dict) and item.get("symbol")
    )


def tradfi_strategy_symbols() -> list[str]:
    return tradfi_symbols(purpose="strategy")


def tradfi_live_symbols() -> list[str]:
    return tradfi_symbols(purpose="live")


def symbols_version() -> int:
    refresh_symbols()
    with _symbols_lock:
        return _symbols_version


def reload_all() -> None:
    global settings, symbols_meta, _symbols_refresh_at
    settings = _load("settings.json", {})
    with _symbols_lock:
        symbols_meta = _load("tradfi_symbols.json", {"symbols": []})
        _symbols_refresh_at = 0.0
    refresh_symbols(force=True)
