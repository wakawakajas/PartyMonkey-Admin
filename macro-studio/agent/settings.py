"""Small persisted settings file. Currently just the global stop hotkey."""
from __future__ import annotations

import json
import threading

from agent import config

SETTINGS_PATH = config.ROOT_DIR / "settings.json"
DEFAULT_STOP_HOTKEY = ["ctrl", "alt", "r"]

_lock = threading.RLock()
_cache: dict | None = None


def _load() -> dict:
    global _cache
    with _lock:
        if _cache is not None:
            return _cache
        data: dict = {}
        if SETTINGS_PATH.exists():
            try:
                data = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                data = {}
        data.setdefault("stop_hotkey", list(DEFAULT_STOP_HOTKEY))
        _cache = data
        return _cache


def get_stop_hotkey() -> list[str]:
    return list(_load()["stop_hotkey"])


def set_stop_hotkey(keys: list[str]) -> list[str]:
    global _cache
    if not keys:
        raise ValueError("Hotkey must have at least one key.")
    normalized = [k.strip().lower() for k in keys if k.strip()]
    with _lock:
        data = _load()
        data["stop_hotkey"] = normalized
        SETTINGS_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")
        _cache = data
    return normalized


def matches_stop_hotkey(pressed_combo: list[str]) -> bool:
    return sorted(pressed_combo) == sorted(get_stop_hotkey())
