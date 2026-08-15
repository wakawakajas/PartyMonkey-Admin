"""Macro persistence: one JSON file per macro under macros/, so they can
be backed up, hand-edited, or put under version control directly. The
last MAX_MACRO_VERSIONS versions of each macro are kept under
macros/versions/<id>/ so a bad rename/edit can be undone by hand.

Every write goes through a temp-file + os.replace swap so a crash or
power loss mid-write can't leave a macro file half-written/corrupted.
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from agent import config

_lock = threading.RLock()


class MacroNotFoundError(KeyError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _macro_path(macro_id: str) -> Path:
    return config.MACROS_DIR / f"{macro_id}.json"


def _versions_dir(macro_id: str) -> Path:
    d = config.MACRO_VERSIONS_DIR / macro_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def _atomic_write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _snapshot_version(macro_id: str) -> None:
    """Copies the CURRENT on-disk macro into its version history before
    it gets overwritten by an edit/rename, then prunes to the newest
    MAX_MACRO_VERSIONS. A no-op for a macro that doesn't exist yet."""
    path = _macro_path(macro_id)
    if not path.exists():
        return
    versions_dir = _versions_dir(macro_id)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
    try:
        (versions_dir / f"{ts}.json").write_bytes(path.read_bytes())
    except OSError:
        return
    versions = sorted(versions_dir.glob("*.json"))
    for old in versions[: max(len(versions) - config.MAX_MACRO_VERSIONS, 0)]:
        try:
            old.unlink()
        except OSError:
            pass


def _read(macro_id: str) -> dict:
    path = _macro_path(macro_id)
    if not path.exists():
        raise MacroNotFoundError(macro_id)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise ValueError(f"Macro file for {macro_id} is corrupted: {exc}") from exc


def list_macros() -> list[dict]:
    with _lock:
        summaries = []
        for path in config.MACROS_DIR.glob("*.json"):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue  # corrupted file -- skip rather than crash the whole library
            summaries.append({
                "id": data.get("id", path.stem),
                "name": data.get("name", "(unnamed)"),
                "step_count": len(data.get("steps", [])),
                "last_run": data.get("last_run"),
                "last_result": data.get("last_result"),
                "created_at": data.get("created_at"),
                "updated_at": data.get("updated_at"),
            })
        summaries.sort(key=lambda m: m.get("updated_at") or "", reverse=True)
        return summaries


def get_macro(macro_id: str) -> dict:
    with _lock:
        return _read(macro_id)


def create_macro(name: str, steps: list[dict]) -> dict:
    name = name.strip()
    if not name:
        raise ValueError("Macro name can't be empty.")
    with _lock:
        macro_id = uuid.uuid4().hex
        data = {
            "id": macro_id,
            "name": name,
            "steps": steps,
            "created_at": _now(),
            "updated_at": _now(),
            "last_run": None,
            "last_result": None,
        }
        _atomic_write(_macro_path(macro_id), data)
        return data


def rename_macro(macro_id: str, new_name: str) -> dict:
    new_name = new_name.strip()
    if not new_name:
        raise ValueError("Macro name can't be empty.")
    with _lock:
        data = _read(macro_id)
        _snapshot_version(macro_id)
        data["name"] = new_name
        data["updated_at"] = _now()
        _atomic_write(_macro_path(macro_id), data)
        return data


def update_macro_steps(macro_id: str, steps: list[dict]) -> dict:
    """Not called yet -- wired up for Phase 6's step editor to use."""
    with _lock:
        data = _read(macro_id)
        _snapshot_version(macro_id)
        data["steps"] = steps
        data["updated_at"] = _now()
        _atomic_write(_macro_path(macro_id), data)
        return data


def update_video_settings(macro_id: str, video: dict) -> dict:
    """Not a content edit (no version snapshot) -- same treatment as
    last_run/last_result, just a per-macro setting."""
    with _lock:
        data = _read(macro_id)
        data["video"] = video
        data["updated_at"] = _now()
        _atomic_write(_macro_path(macro_id), data)
        return data


def duplicate_macro(macro_id: str, new_name: Optional[str] = None) -> dict:
    with _lock:
        source = _read(macro_id)
        name = (new_name or f"{source['name']} (copy)").strip()
        return create_macro(name, source.get("steps", []))


def delete_macro(macro_id: str) -> None:
    with _lock:
        path = _macro_path(macro_id)
        if not path.exists():
            raise MacroNotFoundError(macro_id)
        path.unlink()
        versions_dir = config.MACRO_VERSIONS_DIR / macro_id
        if versions_dir.exists():
            for f in versions_dir.glob("*.json"):
                try:
                    f.unlink()
                except OSError:
                    pass
            try:
                versions_dir.rmdir()
            except OSError:
                pass


def delete_macros(macro_ids: list[str]) -> int:
    count = 0
    with _lock:
        for mid in macro_ids:
            try:
                delete_macro(mid)
                count += 1
            except MacroNotFoundError:
                continue
    return count


def delete_all_macros() -> int:
    with _lock:
        return delete_macros([p.stem for p in config.MACROS_DIR.glob("*.json")])


def record_run_result(macro_id: str, summary: dict) -> dict:
    """Updates last_run/last_result only -- not a content edit, so this
    doesn't churn version history."""
    with _lock:
        data = _read(macro_id)
        data["last_run"] = _now()
        data["last_result"] = summary
        _atomic_write(_macro_path(macro_id), data)
        return data
