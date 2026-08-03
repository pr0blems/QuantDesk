"""配置加载（优先读 APP_DIR/config 用户配置，缺失时回退打包内默认配置）"""
import json, os
from .paths import CONFIG_DIR, DEFAULT_CONFIG_DIR

def _load(name, default=None):
    for d in (CONFIG_DIR, DEFAULT_CONFIG_DIR):
        p = os.path.join(d, name)
        try:
            with open(p, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            continue
    return default

settings = _load("settings.json", {})
api_keys = _load("api_keys.json", {})
symbols_meta = _load("tradfi_symbols.json", {"symbols": []})

def tradfi_symbols():
    return [s["symbol"] for s in symbols_meta.get("symbols", [])]

def reload_all():
    global settings, api_keys, symbols_meta
    settings = _load("settings.json", {})
    api_keys = _load("api_keys.json", {})
    symbols_meta = _load("tradfi_symbols.json", {"symbols": []})
