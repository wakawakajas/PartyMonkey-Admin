"""Read-only folders for Pigu: the design libraries, handed to the page.

A browser forgets its permission to a chosen folder every time the page is
closed, and asks again off a tap -- the "Connect the folder" button in Magic
Create. This PC already has the NAS folders on its disk and this agent already
runs on it, so the page can ask the agent for what it would have read from the
folder and never need the permission at all.

It is read-only, completely: there is no route here that writes, renames or
deletes anything, and nothing the page sends is ever turned into a command.
Writing a library (a new design, a saved phrase, library.pc-*.json) still goes
through the folder the browser was given, as it always did; if that is not
connected, the page says so and nothing is written anywhere else.

How a folder is found. The page knows only the folder's *name* -- "Custom Gift
Tag" -- never where it is on this PC. The name is looked for as a folder
directly inside one of `roots` (or two levels down), and nothing is guessed:
a name that matches more than one folder is refused rather than picking one,
because the wrong library on screen is the wrong artwork on somebody's order.

Who may ask. The agent listens on this PC only. On top of that, a request that
names an origin has to name one of `origins` (the Pigu site, or a local page
for testing); anything else is turned away, so another website open in the same
browser cannot read these files. Every path is checked to stay inside the
folder it was asked under, whatever it contains.

Settings live in folders.json beside fiery.json: open it, fill it in, save.
"""
from __future__ import annotations

import json
import mimetypes
import os
import re
import threading
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, Request, Response
from fastapi.responses import FileResponse, JSONResponse

from agent import config

CONFIG_PATH = config.ROOT_DIR / "folders.json"

DEFAULTS: dict[str, Any] = {
    # Switch it off and the page goes back to asking for the folder itself.
    "enabled": True,
    # Where Pigu is served from. http://localhost and http://127.0.0.1 (any
    # port) are always let through, for trying a change before it is pushed.
    "origins": ["https://wakawakajas.github.io"],
    # Folders whose sub-folders (and their sub-folders) the page may ask for by
    # name. As *this* PC sees them -- the same NAS reads differently on others.
    "roots": [r"C:\NAS Folders\Partymonkey NAS\Pigu"],
}

_LOCAL_ORIGIN = re.compile(r"^http://(localhost|127\.0\.0\.1)(:\d+)?$")
_lock = threading.Lock()
_cache: dict[str, Any] = {"stamp": None, "data": dict(DEFAULTS)}


def load() -> dict:
    with _lock:
        try:
            stamp = CONFIG_PATH.stat().st_mtime if CONFIG_PATH.exists() else None
        except OSError:
            stamp = None
        if stamp is not None and stamp == _cache["stamp"]:
            return _cache["data"]
        data = dict(DEFAULTS)
        if stamp is not None:
            try:
                saved = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
                if isinstance(saved, dict):
                    data.update({k: v for k, v in saved.items() if k in DEFAULTS})
            except (OSError, ValueError):
                pass
        _cache["stamp"], _cache["data"] = stamp, data
        return data


def save(patch: dict) -> dict:
    with _lock:
        data = dict(DEFAULTS)
        if CONFIG_PATH.exists():
            try:
                saved = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
                if isinstance(saved, dict):
                    data.update({k: v for k, v in saved.items() if k in DEFAULTS})
            except (OSError, ValueError):
                pass
        data.update({k: v for k, v in patch.items() if k in DEFAULTS})
        CONFIG_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")
        _cache["stamp"] = None
    return data


# ---------------------------------------------------------------- who may ask

def _origin_ok(origin: str) -> bool:
    if _LOCAL_ORIGIN.match(origin):
        return True
    return origin in [str(o).rstrip("/") for o in load().get("origins") or []]


def _host_ok(host: str) -> bool:
    """A page on another site can be made to point its own name at 127.0.0.1;
    the Host it sends then is that name, not ours."""
    return bool(re.match(r"^(localhost|127\.0\.0\.1)(:\d+)?$", (host or "").lower()))


def _gate(request: Request) -> tuple[bool, Optional[str]]:
    """(allowed, origin to echo back)."""
    if not _host_ok(request.headers.get("host", "")):
        return False, None
    origin = (request.headers.get("origin") or "").rstrip("/")
    if origin:
        return (_origin_ok(origin), origin)
    # No Origin: the agent's own pages, or a tool like curl -- unless the
    # browser says another site caused it (an <img> tag, say).
    site = (request.headers.get("sec-fetch-site") or "").lower()
    if site in ("cross-site", "same-site"):
        return False, None
    return True, None


def _headers(origin: Optional[str]) -> dict:
    h = {"Cache-Control": "no-store"}
    if origin:
        h.update({
            "Access-Control-Allow-Origin": origin,
            "Vary": "Origin",
            "Access-Control-Allow-Private-Network": "true",
            "Access-Control-Expose-Headers": "X-Mtime-Ms, Last-Modified, Content-Length",
        })
    return h


def _json(origin: Optional[str], body: dict, status: int = 200) -> JSONResponse:
    return JSONResponse(body, status_code=status, headers=_headers(origin))


# ------------------------------------------------------------ finding a folder

def _roots() -> list[Path]:
    out: list[Path] = []
    for r in load().get("roots") or []:
        p = Path(str(r))
        try:
            if p.is_dir():
                out.append(p)
        except OSError:
            pass
    return out


def _children(p: Path) -> list[Path]:
    try:
        return [Path(e.path) for e in os.scandir(p) if e.is_dir(follow_symlinks=False)]
    except OSError:
        return []


def _within(base: Path, target: Path) -> bool:
    try:
        b, t = os.path.realpath(base), os.path.realpath(target)
        return os.path.commonpath([b, t]) == b
    except (ValueError, OSError):
        return False


def _find(name: str) -> tuple[list[tuple[int, str]], bool]:
    """Every folder called `name` -- the root itself, a child, or a grandchild --
    as (root index, path under that root)."""
    want = name.strip().lower()
    hits: list[tuple[int, str]] = []
    seen: set[str] = set()
    if not want:
        return hits, False
    for i, root in enumerate(_roots()):
        cands: list[tuple[Path, str]] = [(root, "")]
        for c in _children(root):
            cands.append((c, c.name))
            for g in _children(c):
                cands.append((g, c.name + "/" + g.name))
        for p, rel in cands:
            if p.name.lower() != want:
                continue
            real = os.path.normcase(os.path.realpath(p))
            if real in seen:
                continue
            seen.add(real)
            hits.append((i, rel))
    return hits, len(hits) > 1


_BAD_PART = re.compile(r"[\\:\x00*?\"<>|]")


def _parts(path: str) -> Optional[list[str]]:
    out: list[str] = []
    for part in str(path or "").split("/"):
        if part in ("", "."):
            continue
        if part == ".." or _BAD_PART.search(part) or part.endswith((" ", ".")):
            return None
        out.append(part)
    return out


def _target(folder_id: str, path: str) -> Optional[Path]:
    """The real path for `path` inside the folder `folder_id` names -- or None
    for anything that is not inside it."""
    m = re.match(r"^(\d+)\|(.*)$", str(folder_id or ""))
    if not m:
        return None
    roots = _roots()
    i = int(m.group(1))
    if i >= len(roots):
        return None
    base_parts, sub_parts = _parts(m.group(2)), _parts(path)
    if base_parts is None or sub_parts is None:
        return None
    base = roots[i].joinpath(*base_parts)
    target = base.joinpath(*sub_parts)
    if not _within(roots[i], base) or not _within(base, target):
        return None
    return target


# ------------------------------------------------------------------- the routes

router = APIRouter(prefix="/api/folders")


@router.options("/{rest:path}")
def preflight(rest: str, request: Request) -> Response:
    ok, origin = _gate(request)
    if not ok or not origin or not load().get("enabled"):
        return Response(status_code=403)
    h = _headers(origin)
    h.update({"Access-Control-Allow-Methods": "GET, OPTIONS",
              "Access-Control-Allow-Headers": request.headers.get("access-control-request-headers", "*"),
              "Access-Control-Max-Age": "600"})
    return Response(status_code=204, headers=h)


def _check(request: Request) -> tuple[Optional[JSONResponse], Optional[str]]:
    ok, origin = _gate(request)
    if not ok:
        return JSONResponse({"ok": False, "error": "not allowed"}, status_code=403), None
    if not load().get("enabled"):
        return _json(origin, {"ok": False, "error": "folders are switched off in folders.json"}, 503), origin
    return None, origin


@router.get("/ping")
def ping(request: Request) -> Response:
    refused, origin = _check(request)
    if refused:
        return refused
    return _json(origin, {"ok": True, "roots": len(_roots())})


@router.get("/open")
def open_folder(request: Request, name: str = "") -> Response:
    """Which folder is called `name`: an id for the other routes, or why not."""
    refused, origin = _check(request)
    if refused:
        return refused
    hits, ambiguous = _find(name)
    if ambiguous:
        return _json(origin, {"ok": False, "error": "more than one folder is called that"}, 409)
    if not hits:
        return _json(origin, {"ok": False, "error": "no folder of that name"}, 404)
    i, rel = hits[0]
    return _json(origin, {"ok": True, "id": f"{i}|{rel}", "name": name.strip()})


@router.get("/list")
def list_folder(request: Request, id: str = "", path: str = "") -> Response:
    refused, origin = _check(request)
    if refused:
        return refused
    target = _target(id, path)
    if target is None:
        return _json(origin, {"ok": False, "error": "not a folder here"}, 404)
    entries = []
    try:
        with os.scandir(target) as it:
            for e in it:
                try:
                    if e.is_dir(follow_symlinks=False):
                        entries.append({"name": e.name, "kind": "directory", "size": 0,
                                        "mtime": int(e.stat().st_mtime * 1000)})
                    elif e.is_file(follow_symlinks=False):
                        st = e.stat()
                        entries.append({"name": e.name, "kind": "file", "size": st.st_size,
                                        "mtime": int(st.st_mtime * 1000)})
                except OSError:
                    continue
    except (FileNotFoundError, NotADirectoryError):
        return _json(origin, {"ok": False, "error": "not a folder here"}, 404)
    except OSError as exc:
        return _json(origin, {"ok": False, "error": f"could not read the folder: {exc}"}, 500)
    return _json(origin, {"ok": True, "entries": entries})


@router.get("/file")
def read_file(request: Request, id: str = "", path: str = "") -> Response:
    refused, origin = _check(request)
    if refused:
        return refused
    target = _target(id, path)
    if target is None or not target.is_file() or target.is_symlink():
        return _json(origin, {"ok": False, "error": "no such file"}, 404)
    try:
        mtime = int(target.stat().st_mtime * 1000)
    except OSError:
        return _json(origin, {"ok": False, "error": "no such file"}, 404)
    kind = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
    h = _headers(origin)
    h["X-Mtime-Ms"] = str(mtime)
    return FileResponse(target, media_type=kind, headers=h)
