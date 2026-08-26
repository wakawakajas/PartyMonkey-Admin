"""How the macro list is arranged: categories, and what sits in each.

Kept apart from the macros themselves on purpose. A macro is a thing you
can hand to a colleague on its own -- copy the JSON into their macros/
folder and it runs. Which drawer you happen to keep it in is a fact about
your desk, not about the macro, and shouldn't travel with it or collide
when two people file the same macro differently.

So the arrangement lives in one small file, and a macro this file has
never heard of simply shows up as uncategorised. That also means deleting
this file loses nothing but the tidying.
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
import uuid
from pathlib import Path

from agent import config

_lock = threading.RLock()
LIBRARY_PATH: Path = config.ROOT_DIR / "library.json"

# The drawer everything starts in. It is not stored -- it is what's left
# once the named categories have taken their share, so it can't drift out
# of step with what actually exists.
UNCATEGORISED = ""


def _blank() -> dict:
    return {"categories": [], "placement": {}}


def _read() -> dict:
    with _lock:
        try:
            data = json.loads(LIBRARY_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return _blank()
        if not isinstance(data, dict):
            return _blank()
        cats = [c for c in (data.get("categories") or []) if isinstance(c, dict) and c.get("id")]
        placement = data.get("placement") or {}
        if not isinstance(placement, dict):
            placement = {}
        return {"categories": cats, "placement": placement}


def _write(data: dict) -> None:
    with _lock:
        handle, tmp = tempfile.mkstemp(dir=str(LIBRARY_PATH.parent), suffix=".tmp")
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            os.replace(tmp, LIBRARY_PATH)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise


def get_layout(known_ids: list[str]) -> dict:
    """The arrangement, reconciled against the macros that actually exist.

    Reconciled rather than trusted: a macro deleted from another window,
    or one dropped into macros/ by hand, must not leave the list showing
    something that isn't there or hiding something that is."""
    data = _read()
    known = list(known_ids)
    valid_cats = {c["id"] for c in data["categories"]}

    placement = {}
    for macro_id in known:
        entry = data["placement"].get(macro_id) or {}
        category = entry.get("category") or UNCATEGORISED
        if category not in valid_cats:
            category = UNCATEGORISED
        placement[macro_id] = {"category": category, "order": int(entry.get("order") or 0)}

    return {
        "categories": [{"id": c["id"], "name": c.get("name") or "Untitled",
                        "collapsed": bool(c.get("collapsed", True))}
                       for c in data["categories"]],
        "placement": placement,
    }


def save_layout(categories: list[dict], placement: dict, known_ids: list[str]) -> dict:
    """Takes the whole arrangement at once.

    One call rather than an endpoint per action: a drag can rename
    nothing and still move three things, and sending the finished picture
    means the file can never hold half of a rearrangement."""
    clean_cats, seen = [], set()
    for cat in categories or []:
        if not isinstance(cat, dict):
            continue
        name = str(cat.get("name") or "").strip()
        if not name:
            continue
        cat_id = str(cat.get("id") or "").strip() or uuid.uuid4().hex
        if cat_id in seen:
            continue
        seen.add(cat_id)
        clean_cats.append({"id": cat_id, "name": name[:60],
                           "collapsed": bool(cat.get("collapsed", True))})

    known = set(known_ids)
    clean_placement = {}
    for macro_id, entry in (placement or {}).items():
        if macro_id not in known or not isinstance(entry, dict):
            continue
        category = str(entry.get("category") or "")
        if category not in seen:
            category = UNCATEGORISED
        try:
            order = int(entry.get("order") or 0)
        except (TypeError, ValueError):
            order = 0
        clean_placement[macro_id] = {"category": category, "order": order}

    _write({"categories": clean_cats, "placement": clean_placement})
    return get_layout(known_ids)
