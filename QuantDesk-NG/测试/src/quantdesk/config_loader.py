"""Load the shared, non-secret market collector configuration."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = (
    Path(os.environ.get("QUANTDESK_CONFIG_DIR", PROJECT_ROOT / "config")).expanduser().resolve()
)


def _load(name: str, default: Any) -> Any:
    path = CONFIG_DIR / name
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return default


settings = _load("settings.json", {})
symbols_meta = _load("tradfi_symbols.json", {"symbols": []})


def tradfi_symbols() -> list[str]:
    return [item["symbol"] for item in symbols_meta.get("symbols", [])]


def reload_all() -> None:
    global settings, symbols_meta
    settings = _load("settings.json", {})
    symbols_meta = _load("tradfi_symbols.json", {"symbols": []})
