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
What the page is given back is a random token for that one folder; every other
route takes the token, so a page can read inside the folders it opened and
nowhere else under `roots`. A token lasts until the agent restarts; asked with
one that is gone, the answer is 410 and the page opens the folder again.

Who may ask. The agent listens on this PC only. On top of that, a request that
names an origin has to name one of `origins`; anything else is turned away, so
another website open in the same browser cannot read these files. Every path is
checked to stay inside the folder it was asked under, whatever it contains --
including through a junction or shortcut that leads somewhere else.

Settings live in folders.json beside fiery.json: open it, fill it in, save. A
file that cannot be read switches the feature off rather than back to its
defaults.
"""
from __future__ import annotations

import json
import mimetypes
import os
import re
import secrets
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
    # Where Pigu is served from.
    "origins": ["https://wakawakajas.github.io"],
    # Folders whose sub-folders (and their sub-folders) the page may ask for by
    # name. Full paths, as *this* PC sees them -- the same NAS reads differently
    # on others.
    "roots": [r"C:\NAS Folders\Partymonkey NAS\Pigu"],
    # Let pages on http://localhost / http://127.0.0.1 (any port) ask too. Off:
    # it is only for trying a change to the page before it is pushed, and any
    # program on this PC that serves a web page could otherwise read these files.
    "dev_origins": False,
}

# Python's table is thin on what a design folder holds.
for _ext, _type in ((".webp", "image/webp"), (".ttf", "font/ttf"), (".otf", "font/otf"),
                    (".woff", "font/woff"), (".woff2", "font/woff2"), (".svg", "image/svg+xml")):
    mimetypes.add_type(_type, _ext)

_LOCAL_ORIGIN = re.compile(r"^http://(localhost|127\.0\.0\.1)(:\d+)?$")
_lock = threading.Lock()
_cache: dict[str, Any] = {"stamp": None, "data": dict(DEFAULTS)}


def _read_saved() -> Optional[dict]:
    """The file's own settings, {} for none, None for one that cannot be read."""
    if not CONFIG_PATH.exists():
        return {}
    try:
        saved = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return saved if isinstance(saved, dict) else None


def load() -> dict:
    with _lock:
        try:
            stamp = CONFIG_PATH.stat().st_mtime if CONFIG_PATH.exists() else None
        except OSError:
            stamp = None
        if stamp == _cache["stamp"] and _cache.get("loaded"):
            return _cache["data"]
        data = dict(DEFAULTS)
        saved = _read_saved()
        if saved is None:
            # a typo in the file is not a reason to start serving folders
            data["enabled"] = False
        else:
            data.update({k: v for k, v in saved.items() if k in DEFAULTS})
        _cache.update({"stamp": stamp, "data": data, "loaded": True})
        return data


def save(patch: dict) -> dict:
    with _lock:
        data = dict(DEFAULTS)
        saved = _read_saved()
        if saved is None:
            # leave an unreadable file for somebody to look at rather than
            # overwriting what they typed
            raise ValueError("folders.json cannot be read; fix it by hand first")
        data.update({k: v for k, v in saved.items() if k in DEFAULTS})
        data.update({k: v for k, v in patch.items() if k in DEFAULTS})
        CONFIG_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")
        _cache["loaded"] = False
    return data


# ---------------------------------------------------------------- who may ask

def _origin_ok(origin: str) -> bool:
    cfg = load()
    if cfg.get("dev_origins") and _LOCAL_ORIGIN.match(origin):
        return True
    return origin in [str(o).rstrip("/") for o in cfg.get("origins") or []]


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
            # a relative path would be read from wherever the agent was started
            if p.is_absolute() and p.is_dir():
                out.append(p)
        except OSError:
            pass
    return out


def _within(base: Path | str, target: Path | str) -> bool:
    try:
        b, t = os.path.realpath(base), os.path.realpath(target)
        return os.path.commonpath([os.path.normcase(b), os.path.normcase(t)]) == os.path.normcase(b)
    except (ValueError, OSError):
        return False


def _children(p: Path, root: Path) -> list[Path]:
    """Folders directly inside p that really are inside root (a junction that
    leads out of it does not count)."""
    out: list[Path] = []
    try:
        with os.scandir(p) as it:
            for e in it:
                try:
                    if e.is_dir() and _within(root, e.path):
                        out.append(Path(e.path))
                except OSError:
                    continue
    except OSError:
        pass
    return out


def _find(name: str) -> list[Path]:
    """Every folder called `name` -- a root itself, a child, or a grandchild --
    as real paths, each once."""
    want = name.strip().lower()
    hits: list[Path] = []
    seen: set[str] = set()
    if not want:
        return hits
    for root in _roots():
        cands: list[Path] = [root]
        for c in _children(root, root):
            cands.append(c)
            cands.extend(_children(c, root))
        for p in cands:
            if p.name.lower() != want:
                continue
            real = os.path.realpath(p)
            key = os.path.normcase(real)
            if key in seen:
                continue
            seen.add(key)
            hits.append(Path(real))
    return hits


# What a token stands for: one real folder, found by name and never by a path
# the page made up. Bounded, oldest dropped; the same folder keeps its token.
_tok_lock = threading.Lock()
_tokens: dict[str, Path] = {}
_token_of: dict[str, str] = {}
_MAX_TOKENS = 256


def _token_for(folder: Path) -> str:
    key = os.path.normcase(str(folder))
    with _tok_lock:
        tok = _token_of.get(key)
        if tok and tok in _tokens:
            return tok
        while len(_tokens) >= _MAX_TOKENS:
            old = next(iter(_tokens))
            _token_of.pop(os.path.normcase(str(_tokens.pop(old))), None)
        tok = secrets.token_urlsafe(12)
        _tokens[tok] = folder
        _token_of[key] = tok
        return tok


def _folder(token: str) -> Optional[Path]:
    """The folder a token stands for, if it is still there and still under a
    root (settings can change while the agent runs)."""
    with _tok_lock:
        p = _tokens.get(str(token or ""))
    if p is None:
        return None
    try:
        if not p.is_dir():
            return None
    except OSError:
        return None
    return p if any(_within(r, p) for r in _roots()) else None


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


def _inside(base: Path, path: str) -> Optional[Path]:
    """`path` below `base`, or None for anything that is not inside it."""
    parts = _parts(path)
    if parts is None:
        return None
    target = base.joinpath(*parts)
    return target if _within(base, target) else None


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
    """Which folder is called `name`: a token for the other routes, or why not."""
    refused, origin = _check(request)
    if refused:
        return refused
    hits = _find(name)
    if len(hits) > 1:
        return _json(origin, {"ok": False, "error": "more than one folder is called that"}, 409)
    if not hits:
        return _json(origin, {"ok": False, "error": "no folder of that name"}, 404)
    return _json(origin, {"ok": True, "id": _token_for(hits[0]), "name": name.strip()})


def _resolve(origin: Optional[str], folder_id: str, path: str):
    """(target, None) or (None, the response to send instead)."""
    base = _folder(folder_id)
    if base is None:
        return None, _json(origin, {"ok": False, "error": "unknown folder, open it again"}, 410)
    target = _inside(base, path)
    if target is None:
        return None, _json(origin, {"ok": False, "error": "not here"}, 404)
    return target, None


@router.get("/list")
def list_folder(request: Request, id: str = "", path: str = "") -> Response:
    refused, origin = _check(request)
    if refused:
        return refused
    target, bad = _resolve(origin, id, path)
    if bad:
        return bad
    base = _folder(id)
    entries = []
    try:
        with os.scandir(target) as it:
            for e in it:
                try:
                    # A junction or shortcut is followed, as a real folder
                    # handle would -- but only while it stays inside this
                    # folder; one that leads out is not listed at all.
                    if e.is_dir():
                        if not _within(base, e.path):
                            continue
                        entries.append({"name": e.name, "kind": "directory", "size": 0,
                                        "mtime": int(e.stat().st_mtime * 1000)})
                    elif e.is_file():
                        if not _within(base, e.path):
                            continue
                        st = e.stat()
                        entries.append({"name": e.name, "kind": "file", "size": st.st_size,
                                        "mtime": int(st.st_mtime * 1000)})
                except OSError:
                    continue
    except (FileNotFoundError, NotADirectoryError):
        return _json(origin, {"ok": False, "error": "not a folder here"}, 404)
    except OSError:
        return _json(origin, {"ok": False, "error": "could not read the folder"}, 500)
    return _json(origin, {"ok": True, "entries": entries})


@router.get("/file")
def read_file(request: Request, id: str = "", path: str = "") -> Response:
    refused, origin = _check(request)
    if refused:
        return refused
    target, bad = _resolve(origin, id, path)
    if bad:
        return bad
    try:
        if not target.is_file():
            return _json(origin, {"ok": False, "error": "no such file"}, 404)
        mtime = int(target.stat().st_mtime * 1000)
    except OSError:
        return _json(origin, {"ok": False, "error": "no such file"}, 404)
    kind = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
    h = _headers(origin)
    h["X-Mtime-Ms"] = str(mtime)
    return FileResponse(target, media_type=kind, headers=h)
