"""Reading buyer messages out of DuoKe, and typing the approved reply back.

DuoKe is the desktop app the shop answers Shopee chat in. It has no API and
no export, so this reads the window itself through UI Automation -- the same
way replay finds a button it was recorded clicking -- and posts what it finds
to the Replies screen in Pigu. Nothing is answered here: a draft is written in
Pigu, somebody reads it and presses Enter, and the approved reply comes back
down this pipe to be typed into the right conversation.

A reply can carry a picture as well as words. A fact in Pigu may have a photo
attached — the size chart, the care label — and when the buyer's question
matches that fact the photo goes out with the reply: fetched from storage, put
on the clipboard as a file, pasted into the chat and sent. That paste is the
one thing here that needs DuoKe in front for a second, because Ctrl+V cannot be
posted to a background window the way a character can; the foreground is handed
straight back. Turn "send_photos" off and the words still go, leaving the
picture to be pasted by hand.

Three things live in this module:

    probe()       dump the window's UIA tree to a file, once, so the shapes
                  below can be filled in for the DuoKe build on this PC
    sync_once()   one pass: read the unread threads, send new messages up,
                  type down any reply that has been approved
    watcher       the loop that calls sync_once every poll_seconds while the
                  agent is running

WHY A PROBE RATHER THAN HARD-CODED NAMES: every chat app names its panes
differently, and DuoKe updates itself. Guessing an automation id here would
mean a sync that silently reads nothing after an update, which is the worst
failure this could have -- a shop believing the messages are being watched.
So the selectors live in duoke.json, the probe is what fills them in, and a
pass that finds nothing says so out loud in the heartbeat instead of looking
like a quiet morning.

WHAT IS SENT WHERE: buyer messages and approved replies go to this shop's own
Supabase project, signed in as a real account with that account's row level
security. Nothing else leaves the machine, and the agent holds no service key
-- it can touch exactly the rows the person whose login it uses could.

THE PASSWORD IS IN duoke.json IN PLAIN TEXT. It is a local file on a single-user
shop PC, the same trade the rest of Macro Studio makes, and it is in
.gitignore. Use an account that has the Replies section and nothing else.
"""
from __future__ import annotations

import hashlib
import json
import platform
import tempfile
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from agent import actions, config, uia, winapi

CONFIG_PATH = config.ROOT_DIR / "duoke.json"
SEEN_PATH = config.ROOT_DIR / "duoke-seen.json"
PROBE_DIR = config.RUNS_DIR

# A conversation is opened, read, and left. Ten in a pass is plenty for a
# morning's unread and keeps one slow pass from holding the window for a
# minute while somebody is trying to use it.
MAX_THREADS_PER_PASS = 10
# How many messages back to look at in an opened conversation. The buyer's
# last message is what is being answered; the two before it are context for
# the draft.
MESSAGE_TAIL = 6
# What "pull chat history" fetches. More than the draft needs, because the
# point of asking is to read the thread yourself.
HISTORY_TAIL = 24
MAX_SEEN = 2000

DEFAULTS: dict[str, Any] = {
    "enabled": False,
    # Supabase — the same project the app uses. URL and anon key are on the
    # project's API settings page; they are not secrets in the sense the
    # password is, they are what every browser running Pigu already has.
    "supabase_url": "",
    "supabase_anon_key": "",
    "email": "",
    "password": "",
    # Which shop these messages belong to, if you run more than one. Shown on
    # the message in Pigu; nothing keys off it.
    "store": "",
    # Which window to read, either way round: a case-insensitive substring of
    # the title, OR of the program's own file name. The file name is the one
    # that holds still -- DuoKe titles itself 多客 in Chinese and renames
    # itself again whenever a conversation is open, while the exe stays
    # Duoke.exe. Leave the title empty to match on the program alone.
    "window_title": "",
    "window_process": "duoke",
    "poll_seconds": 20,
    # Off means read-only: messages come up, nothing is ever typed into DuoKe.
    # Worth leaving off for the first day, to watch what it drafts before it
    # can act.
    "type_back": True,
    # A fact in Pigu can carry a photo -- a size chart, a care label -- and a
    # reply can go out with it. Pasting a picture is the one thing here that
    # needs the window in front for a moment, because Ctrl+V cannot be posted
    # to a window in the background the way a character can. Off means the
    # words go and the picture waits to be pasted by hand.
    "send_photos": True,
    # Where reply photos live. The same private bucket the rest of the app
    # uses; the path is what the row carries.
    "photo_bucket": "shipment-photos",
    # Opening an unread conversation is a click, and a click needs the window.
    # Off means only the conversation already open is read.
    "open_unread": True,
    # Filled in from a probe. Each is matched by automation_id first, then
    # class_name, then name-contains; x_band is the fallback -- the left/right
    # split of a two-column chat window, in pixels from the window's left edge.
    "reader": {
        "chat_list": {"automation_id": "", "class_name": "", "name": "", "x_band": [0, 340]},
        "messages": {"automation_id": "", "class_name": "", "name": "", "x_band": [340, 99999]},
        "input": {"automation_id": "", "class_name": "", "name": ""},
        # What an unread thread looks like in the list. DuoKe shows a count;
        # a thread whose name matches this is opened and read.
        "unread_pattern": r"\((\d+)\)|\b(\d+)\s*条|·\s*(\d+)$",
        # A message bubble shorter than this is a sticker, a timestamp or a
        # read receipt, not a question.
        "min_message_chars": 2,
    },
}

_lock = threading.RLock()
_cache: Optional[dict] = None


# ---------------------------------------------------------------- config


def _merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for key, value in (over or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


def load() -> dict:
    global _cache
    with _lock:
        if _cache is not None:
            return _cache
        saved: dict = {}
        if CONFIG_PATH.exists():
            try:
                saved = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                saved = {}
        _cache = _merge(DEFAULTS, saved)
        return _cache


def save(patch: dict) -> dict:
    """Merge a patch into duoke.json. Returns the settings as they now are."""
    global _cache
    with _lock:
        data = _merge(load(), patch or {})
        CONFIG_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")
        _cache = data
        return data


def redacted() -> dict:
    """The settings as the web UI may see them -- the password's length, not
    the password. A field that came back empty every time would look unset."""
    data = dict(load())
    data["password"] = "*" * len(data.get("password") or "")
    data["supabase_anon_key"] = (data.get("supabase_anon_key") or "")[:12] + "…" \
        if data.get("supabase_anon_key") else ""
    return data


def _seen() -> set[str]:
    try:
        return set(json.loads(SEEN_PATH.read_text(encoding="utf-8")))
    except (json.JSONDecodeError, OSError, TypeError):
        return set()


def _remember(fingerprints: set[str]) -> None:
    """What has already been sent up. The database refuses a duplicate anyway
    -- this is so a pass over a busy morning does not make forty rejected
    requests to find that out."""
    keep = list(_seen() | fingerprints)[-MAX_SEEN:]
    try:
        SEEN_PATH.write_text(json.dumps(keep), encoding="utf-8")
    except OSError:
        pass


# ---------------------------------------------------------------- Supabase


class CloudError(RuntimeError):
    pass


class Cloud:
    """The smallest Supabase client that will do: sign in with a password,
    keep the token, retry once when it has aged out.

    urllib rather than a library, because adding a dependency to
    requirements.txt means every PC that has Macro Studio installed has to
    reinstall before the sync works, and this is four calls."""

    def __init__(self, url: str, anon: str, email: str, password: str):
        self.url = (url or "").rstrip("/")
        self.anon = anon or ""
        self.email = email or ""
        self.password = password or ""
        self.token: Optional[str] = None
        self.refresh_token: Optional[str] = None
        self.user_id: Optional[str] = None

    # -- plumbing
    def _call(self, method: str, path: str, body: Any = None, headers: Optional[dict] = None) -> Any:
        if not self.url or not self.anon:
            raise CloudError("Supabase URL and anon key are not set")
        data = None if body is None else json.dumps(body).encode("utf-8")
        req = urllib.request.Request(self.url + path, data=data, method=method)
        req.add_header("apikey", self.anon)
        req.add_header("Content-Type", "application/json")
        for key, value in (headers or {}).items():
            req.add_header(key, value)
        try:
            with urllib.request.urlopen(req, timeout=25) as res:
                raw = res.read().decode("utf-8") or ""
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8")[:300]
            except Exception:
                pass
            raise CloudError(f"{method} {path} -> {exc.code} {detail}") from exc
        except urllib.error.URLError as exc:
            raise CloudError(f"cannot reach Supabase: {exc.reason}") from exc
        if not raw:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return raw

    def sign_in(self) -> None:
        if not self.email or not self.password:
            raise CloudError("no email and password set for the sync account")
        out = self._call(
            "POST",
            "/auth/v1/token?grant_type=password",
            {"email": self.email, "password": self.password},
        )
        self.token = (out or {}).get("access_token")
        self.refresh_token = (out or {}).get("refresh_token")
        self.user_id = ((out or {}).get("user") or {}).get("id")
        if not self.token or not self.user_id:
            raise CloudError("signed in but got no token back")

    def _auth(self) -> dict:
        if not self.token:
            self.sign_in()
        return {"Authorization": "Bearer " + str(self.token)}

    def rest(self, method: str, path: str, body: Any = None, prefer: str = "") -> Any:
        headers = self._auth()
        if prefer:
            headers["Prefer"] = prefer
        try:
            return self._call(method, "/rest/v1" + path, body, headers)
        except CloudError as exc:
            # An hour-old token is the one failure worth retrying by itself:
            # the agent is meant to run all day and signing in again is free.
            if "401" not in str(exc) and "403" not in str(exc):
                raise
            self.token = None
            return self._call(method, "/rest/v1" + path, body, self._auth() | (
                {"Prefer": prefer} if prefer else {}))

    # -- the four things this actually does
    def send_message(self, row: dict) -> bool:
        """Insert one buyer message. A fingerprint the table has already seen
        comes back 409 and is not an error -- it is the duplicate guard doing
        its job."""
        try:
            self.rest("POST", "/reply_messages", row, prefer="return=minimal")
            return True
        except CloudError as exc:
            if "409" in str(exc) or "duplicate key" in str(exc):
                return False
            raise

    def object_bytes(self, bucket: str, path: str) -> bytes:
        """One file out of storage, as the signed-in account. Not through
        /rest/v1 like everything else here -- storage is its own API, and what
        comes back is the file rather than JSON."""
        if not self.url or not path:
            raise CloudError("no photo to fetch")
        safe = "/".join(urllib.parse.quote(part) for part in path.split("/"))
        req = urllib.request.Request(f"{self.url}/storage/v1/object/{bucket}/{safe}")
        req.add_header("apikey", self.anon)
        for key, value in self._auth().items():
            req.add_header(key, value)
        try:
            with urllib.request.urlopen(req, timeout=30) as res:
                return res.read()
        except urllib.error.HTTPError as exc:
            raise CloudError(f"photo {path} -> {exc.code}") from exc
        except urllib.error.URLError as exc:
            raise CloudError(f"cannot reach storage: {exc.reason}") from exc

    def replies_to_type(self) -> list[dict]:
        query = (
            "/reply_messages?select=id,chat_key,reply,store,photos"
            "&status=eq.answered&typed_at=is.null&reply=neq."
            "&order=answered_at.asc&limit=20"
        )
        out = self.rest("GET", query)
        return out if isinstance(out, list) else []

    def history_wanted(self) -> list[dict]:
        """Rows somebody has pressed Pull chat history on since they were last
        read. Ordered oldest ask first, so a queue of them is served in the
        order people asked."""
        query = (
            "/reply_messages?select=id,chat_key,history_at,history_wanted_at"
            "&history_wanted_at=not.is.null&status=eq.new"
            "&order=history_wanted_at.asc&limit=5"
        )
        out = self.rest("GET", query)
        rows = out if isinstance(out, list) else []
        fresh = []
        for row in rows:
            read_at = row.get("history_at")
            asked_at = row.get("history_wanted_at")
            # already answered after it was asked -- nothing to do
            if read_at and asked_at and str(read_at) >= str(asked_at):
                continue
            fresh.append(row)
        return fresh

    def save_history(self, row_id: str, lines: list[dict]) -> None:
        self.rest(
            "PATCH",
            f"/reply_messages?id=eq.{urllib.parse.quote(row_id)}",
            {"history": lines, "history_at": datetime.now(timezone.utc).isoformat()},
            prefer="return=minimal",
        )

    def mark_typed(self, row_id: str, note: str = "") -> None:
        body: dict[str, Any] = {"typed_at": datetime.now(timezone.utc).isoformat()}
        if note:
            body["draft"] = note
        self.rest("PATCH", f"/reply_messages?id=eq.{urllib.parse.quote(row_id)}", body,
                  prefer="return=minimal")

    def beat(self, device: str, window_found: bool, threads: int, note: str) -> None:
        self.rest(
            "PATCH",
            "/reply_sync?id=eq.true",
            {
                "device": device[:60],
                "window_found": window_found,
                "threads": threads,
                "note": note[:300],
                "beat_at": datetime.now(timezone.utc).isoformat(),
            },
            prefer="return=minimal",
        )


# ---------------------------------------------------------------- the window


def find_window() -> Optional[int]:
    cfg = load()
    want = (cfg.get("window_title") or "").strip().lower()
    want_exe = (cfg.get("window_process") or "").strip().lower()
    if not want and not want_exe:
        return None
    best: Optional[int] = None
    for hwnd in winapi.enum_top_level_windows():
        title = winapi.window_title(hwnd) or ""
        by_title = bool(want) and want in title.lower()
        by_exe = False
        if want_exe and not by_title:
            by_exe = want_exe in (winapi.process_name(winapi.window_pid(hwnd)) or "")
        if not (by_title or by_exe):
            continue
        # Our own browser tab showing the Replies screen has "DuoKe" in its
        # title the moment somebody types the word. A window with no size is
        # not the app either.
        if config.WEB_WINDOW_TITLE_HINT.lower() in title.lower():
            continue
        left, top, right, bottom = winapi.window_rect(hwnd)
        if right - left < 400 or bottom - top < 300:
            continue
        best = hwnd
        break
    return best


def _rect(element) -> Optional[tuple[int, int, int, int]]:
    try:
        r = element.CurrentBoundingRectangle
        return int(r.left), int(r.top), int(r.right), int(r.bottom)
    except Exception:
        return None


def _walk(element, depth: int = 0, max_depth: int = 14, budget: Optional[list[int]] = None) -> list[dict]:
    """Flat list of the tree under `element`, each node with where it is.

    Flat rather than nested on purpose: everything this module wants to know
    -- which column a bubble is in, which list item is which -- is a question
    about position, and a flat list sorted by y is the shape that answers it.
    """
    if budget is None:
        budget = [4000]
    out: list[dict] = []
    if element is None or budget[0] <= 0:
        return out
    try:
        walker = uia._automation().RawViewWalker
    except Exception:
        return out
    queue = [(element, depth)]
    while queue and budget[0] > 0:
        node, node_depth = queue.pop(0)
        budget[0] -= 1
        desc = uia._describe(node)
        desc["depth"] = node_depth
        desc["rect"] = _rect(node)
        desc["_el"] = node
        out.append(desc)
        if node_depth >= max_depth:
            continue
        try:
            child = walker.GetFirstChildElement(node)
            while child is not None:
                queue.append((child, node_depth + 1))
                child = walker.GetNextSiblingElement(child)
        except Exception:
            pass
    return out


def _matches(node: dict, want: dict) -> bool:
    aid = (want.get("automation_id") or "").strip()
    cls = (want.get("class_name") or "").strip()
    name = (want.get("name") or "").strip()
    if aid and node.get("automation_id") == aid:
        return True
    if cls and node.get("class_name") == cls:
        return True
    if name and name.lower() in (node.get("name") or "").lower():
        return True
    return False


def _in_band(node: dict, band: list, window: tuple[int, int, int, int]) -> bool:
    rect = node.get("rect")
    if not rect or not band or len(band) != 2:
        return False
    x = rect[0] - window[0]
    return float(band[0]) <= x < float(band[1])


def probe() -> dict:
    """Dump the DuoKe window's tree to runs/ and say what looks like what.

    Run this once, with DuoKe open and a conversation showing. The file it
    writes is the thing to read when filling in duoke.json's `reader`: every
    element, its automation id, its class and where it sits.
    """
    hwnd = find_window()
    if not hwnd:
        return {"ok": False, "error": f"no window whose title contains "
                                     f"\"{load().get('window_title')}\" is open"}
    root = uia.element_from_handle(hwnd)
    if root is None:
        return {"ok": False, "error": "Windows would not hand over that window's elements"}
    window = winapi.window_rect(hwnd)
    nodes = _walk(root, max_depth=16, budget=[6000])

    # Panes ranked by how much text they contain, which is what tells the
    # conversation from the chrome around it.
    panes: list[dict] = []
    for node in nodes:
        if node["control_type"] not in ("Pane", "List", "Group", "Document", "Custom", "Table"):
            continue
        rect = node.get("rect")
        if not rect:
            continue
        inside = [
            n for n in nodes
            if n.get("rect") and n["control_type"] in ("Text", "ListItem", "DataItem", "Edit")
            and rect[0] <= n["rect"][0] and n["rect"][2] <= rect[2]
            and rect[1] <= n["rect"][1] and n["rect"][3] <= rect[3]
            and (n.get("name") or "").strip()
        ]
        if len(inside) < 3:
            continue
        panes.append({
            "automation_id": node["automation_id"],
            "class_name": node["class_name"],
            "control_type": node["control_type"],
            "name": node["name"][:60],
            "x_from_window_left": rect[0] - window[0],
            "width": rect[2] - rect[0],
            "text_inside": len(inside),
            "sample": [(n.get("name") or "")[:70] for n in inside[:6]],
        })
    panes.sort(key=lambda p: p["text_inside"], reverse=True)

    edits = [
        {
            "automation_id": n["automation_id"],
            "class_name": n["class_name"],
            "name": n["name"][:60],
            "x_from_window_left": (n["rect"][0] - window[0]) if n.get("rect") else None,
            "y_from_window_top": (n["rect"][1] - window[1]) if n.get("rect") else None,
        }
        for n in nodes if n["control_type"] == "Edit"
    ]

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = PROBE_DIR / f"duoke-probe-{stamp}.json"
    dump = {
        "taken_at": datetime.now(timezone.utc).isoformat(),
        "window": {"title": winapi.window_title(hwnd), "rect": list(window)},
        "candidate_panes": panes[:12],
        "edit_boxes": edits,
        "tree": [
            {k: v for k, v in n.items() if k != "_el"}
            for n in nodes
        ],
    }
    try:
        path.write_text(json.dumps(dump, indent=2, ensure_ascii=False), encoding="utf-8")
    except OSError as exc:
        return {"ok": False, "error": f"could not write the probe file: {exc}"}

    return {
        "ok": True,
        "file": str(path),
        "window_title": winapi.window_title(hwnd),
        "nodes": len(nodes),
        "candidate_panes": panes[:6],
        "edit_boxes": edits[:6],
    }


# ---------------------------------------------------------------- reading


def _find_region(nodes: list[dict], want: dict, band_key: str,
                 window: tuple[int, int, int, int]) -> list[dict]:
    """The nodes belonging to one region of the window.

    A named selector wins: if duoke.json says the chat list is a particular
    automation id, that pane's contents are the answer and its position is
    irrelevant. With nothing named, the x band decides -- which is why the
    defaults are a left column and a right column, the shape every chat
    window has ever had.
    """
    named = [n for n in nodes if _matches(n, want)]
    if named:
        host = max(named, key=lambda n: ((n["rect"][2] - n["rect"][0]) if n.get("rect") else 0))
        rect = host.get("rect")
        if rect:
            return [
                n for n in nodes
                if n is not host and n.get("rect")
                and rect[0] <= n["rect"][0] and n["rect"][2] <= rect[2] + 2
                and rect[1] <= n["rect"][1] and n["rect"][3] <= rect[3] + 2
            ]
    band = want.get(band_key) or want.get("x_band") or []
    return [n for n in nodes if _in_band(n, band, window)]


def _texts(nodes: list[dict], min_chars: int) -> list[dict]:
    out = []
    for node in nodes:
        if node["control_type"] not in ("Text", "ListItem", "DataItem", "Document", "Custom"):
            continue
        name = (node.get("name") or "").strip()
        if len(name) < min_chars:
            continue
        rect = node.get("rect")
        if not rect:
            continue
        out.append({"text": name, "rect": rect, "_el": node["_el"],
                    "control_type": node["control_type"]})
    out.sort(key=lambda n: (n["rect"][1], n["rect"][0]))
    return out


_TIME_ONLY = re.compile(r"^[\d\s:./\-]+$")
_NOISE = re.compile(
    r"^(yesterday|today|kemarin|hari ini|昨天|今天|已读|未读|read|unread|sent|delivered)$",
    re.IGNORECASE,
)


def _is_noise(text: str) -> bool:
    t = text.strip()
    return not t or bool(_TIME_ONLY.match(t)) or bool(_NOISE.match(t))


def read_open_conversation(nodes: list[dict], window: tuple[int, int, int, int],
                           reader: dict, tail: int = MESSAGE_TAIL) -> list[dict]:
    """The tail of the conversation on screen, each line marked inbound or not.

    Which side of the pane a bubble sits on is what says who wrote it. Every
    chat app ever built puts the other person on the left and you on the
    right, and unlike a colour or an automation id that is still true after an
    update -- so the split is measured from the pane the bubbles are actually
    in, not assumed.
    """
    region = _find_region(nodes, reader.get("messages") or {}, "x_band", window)
    lines = _texts(region, int(reader.get("min_message_chars") or 2))
    lines = [l for l in lines if not _is_noise(l["text"])]
    if not lines:
        return []
    left = min(l["rect"][0] for l in lines)
    right = max(l["rect"][2] for l in lines)
    middle = (left + right) / 2
    out = []
    for line in lines[-tail:]:
        centre = (line["rect"][0] + line["rect"][2]) / 2
        out.append({"text": line["text"], "inbound": centre < middle})
    return out


def read_threads(nodes: list[dict], window: tuple[int, int, int, int],
                 reader: dict) -> list[dict]:
    """The conversation list: what each thread is called, and whether it is
    unread. The list's own items only -- a thread is a ListItem or a Text
    block in the left column, and its name is what has to be matched again
    later to type a reply into it."""
    region = _find_region(nodes, reader.get("chat_list") or {}, "x_band", window)
    pattern = reader.get("unread_pattern") or ""
    rx = None
    if pattern:
        try:
            rx = re.compile(pattern)
        except re.error:
            rx = None
    out = []
    seen_names: set[str] = set()
    for item in _texts(region, 1):
        name = item["text"]
        if _is_noise(name) or name in seen_names:
            continue
        seen_names.add(name)
        unread = bool(rx and rx.search(name))
        out.append({
            "name": name,
            "unread": unread,
            "rect": item["rect"],
            "_el": item["_el"],
        })
    return out


def _chat_key(name: str) -> str:
    """A thread's name with the unread count taken off it, so the same
    conversation is the same key whether or not it had two waiting when it was
    read."""
    return re.sub(r"\s*[\(·]\s*\d+\s*\)?\s*$", "", name).strip()


def _open_thread(thread: dict, hwnd: int, allow_click: bool) -> bool:
    element = thread.get("_el")
    if element is not None:
        if uia.try_select(element) or uia.try_invoke(element):
            time.sleep(0.6)
            return True
    if not allow_click:
        return False
    rect = thread.get("rect")
    if not rect:
        return False
    x, y = (rect[0] + rect[2]) // 2, (rect[1] + rect[3]) // 2
    left, top, _r, _b = winapi.window_rect(hwnd)
    cx, cy = winapi.screen_to_client(hwnd, x, y)
    child = winapi.child_window_from_point(hwnd, cx, cy)
    if child:
        winapi.post_click(child, cx, cy)
        time.sleep(0.6)
        return True
    return False


def open_thread_name(threads: list[dict], hwnd: int) -> Optional[str]:
    """Which conversation is the one on screen.

    There is no reliable "selected" flag to read: a chat list draws its own
    selection and does not always tell UIA about it. What DuoKe does say is
    its window title, which carries the open conversation's name -- so a
    thread whose name appears in the title is the one being looked at.

    None rather than a guess when nothing matches. A message filed under the
    wrong conversation would be answered into the wrong conversation, which is
    worse than not reading it at all.
    """
    title = (winapi.window_title(hwnd) or "").lower()
    if not title:
        return None
    best = None
    for thread in threads:
        name = _chat_key(thread["name"])
        if len(name) >= 3 and name.lower() in title:
            if best is None or len(name) > len(best):
                best = name
    return best


def _fingerprint(chat_key: str, text: str) -> str:
    # The hour is in it so the same question asked again tomorrow is a new
    # message, while the same question still on screen at the next pass four
    # minutes later is not.
    hour = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H")
    raw = f"{chat_key}|{text}|{hour}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------- writing back


def _input_box(nodes: list[dict], reader: dict) -> Optional[dict]:
    """DuoKe's message box. Named in duoke.json where a probe has named it,
    otherwise the edit control lowest in the window: the search box sits at the
    top of the list column, the reply box at the bottom of the conversation."""
    want = reader.get("input") or {}
    if want.get("automation_id") or want.get("class_name") or want.get("name"):
        named = [n for n in nodes if n["control_type"] == "Edit" and _matches(n, want)]
        if named:
            return named[0]
    edits = [n for n in nodes if n["control_type"] == "Edit" and n.get("rect")]
    if not edits:
        return None
    return max(edits, key=lambda n: n["rect"][1])


def type_reply(nodes: list[dict], window: tuple[int, int, int, int], reader: dict,
               hwnd: int, text: str) -> tuple[bool, str]:
    """Put `text` in DuoKe's message box and press Enter.

    SetValue first, because it is the one route that does not care whether the
    window has focus -- the shop PC is being used for something else while
    this runs. Where the box has no ValuePattern (a rich editor usually does
    not) the characters are posted to it one at a time instead, which still
    does not need the window in front.
    """
    box = _input_box(nodes, reader)
    if box is None:
        return False, "no text box found in the window"
    element = box.get("_el")

    if element is not None and uia.try_set_value(element, text):
        value = uia.get_current_value(element) or ""
        if text[:40] not in value:
            return False, "the text box took the reply but did not keep it"
    else:
        handle = None
        rect = box.get("rect")
        if rect:
            cx, cy = winapi.screen_to_client(hwnd, (rect[0] + rect[2]) // 2,
                                             (rect[1] + rect[3]) // 2)
            handle = winapi.child_window_from_point(hwnd, cx, cy)
        if not handle:
            return False, "the text box would not take the reply"
        for ch in text:
            winapi.post_char(handle, ch)
        time.sleep(0.2)
        vk = winapi.vk_for("enter")
        if vk:
            winapi.post_key_down(handle, vk)
            winapi.post_key_up(handle, vk)
        return True, "typed character by character"

    # Enter, to the control that holds the text.
    handle = None
    rect = box.get("rect")
    if rect:
        cx, cy = winapi.screen_to_client(hwnd, (rect[0] + rect[2]) // 2,
                                         (rect[1] + rect[3]) // 2)
        handle = winapi.child_window_from_point(hwnd, cx, cy)
    vk = winapi.vk_for("enter")
    if handle and vk:
        winapi.post_key_down(handle, vk)
        winapi.post_key_up(handle, vk)
        return True, "sent"
    return False, "the reply is in the box but Enter could not be delivered — send it by hand"


def paste_photo(nodes: list[dict], window: tuple[int, int, int, int], reader: dict,
                hwnd: int, image: bytes) -> tuple[bool, str]:
    """Put one picture in the chat: on the clipboard, then Ctrl+V, then Enter.

    A chat box takes a pasted image; there is no other way in from outside the
    app, since DuoKe has no "attach this file" that can be driven blind. The
    file is written to the temp folder because Set-Clipboard copies a FILE,
    which is what a chat box understands as an image to send.

    This is the one place that takes the foreground. It is given back to
    whatever had it as soon as the paste has gone in -- a shop PC is usually
    being used for something else, and stealing the window for a second is
    tolerable where stealing it for a minute is not.
    """
    if not image:
        return False, "there was nothing to paste"
    stamp = datetime.now().strftime("%H%M%S%f")
    temp = Path(tempfile.gettempdir()) / f"duoke-reply-{stamp}.jpg"
    try:
        temp.write_bytes(image)
    except OSError as exc:
        return False, f"could not write the photo out: {exc}"

    was = winapi.get_foreground_window()
    try:
        proc = actions._powershell(f"Set-Clipboard -LiteralPath '{temp}'")
        if proc.returncode != 0:
            return False, "the photo would not go on the clipboard"

        # Click the message box first: a paste lands wherever the caret is, and
        # after the words went in the caret is where it should be -- but a
        # window just brought forward may have put it somewhere else.
        if not winapi.set_foreground(hwnd):
            return False, "Windows would not bring DuoKe to the front, so the photo was not pasted"
        time.sleep(0.35)
        box = _input_box(nodes, reader)
        if box and box.get("rect"):
            rect = box["rect"]
            winapi.physical_move_and_click((rect[0] + rect[2]) // 2, (rect[1] + rect[3]) // 2)
            time.sleep(0.2)

        ctrl, v, enter = winapi.vk_for("ctrl"), winapi.vk_for("v"), winapi.vk_for("enter")
        if not (ctrl and v and enter):
            return False, "this machine reports no Ctrl, V or Enter key"
        winapi.physical_key(ctrl)
        winapi.physical_key(v)
        winapi.physical_key(v, key_up=True)
        winapi.physical_key(ctrl, key_up=True)
        # A pasted picture takes a moment to become an attachment; Enter before
        # that sends an empty message and loses the picture.
        time.sleep(1.2)
        winapi.physical_key(enter)
        winapi.physical_key(enter, key_up=True)
        time.sleep(0.4)
        return True, "pasted"
    finally:
        if was and was != hwnd:
            winapi.set_foreground(was)
        try:
            temp.unlink()
        except OSError:
            pass


# ---------------------------------------------------------------- one pass


_state: dict[str, Any] = {
    "running": False,
    "last_pass_at": None,
    "last_error": "",
    "last_report": {},
}


def status() -> dict:
    cfg = load()
    return {
        "configured": bool(cfg.get("supabase_url") and cfg.get("supabase_anon_key")
                           and cfg.get("email") and cfg.get("password")),
        "enabled": bool(cfg.get("enabled")),
        "watching": bool(_state["running"]),
        "poll_seconds": int(cfg.get("poll_seconds") or 20),
        "type_back": bool(cfg.get("type_back")),
        "window_title": cfg.get("window_title"),
        "window_process": cfg.get("window_process"),
        "window_found": find_window() is not None,
        "last_pass_at": _state["last_pass_at"],
        "last_error": _state["last_error"],
        "last_report": _state["last_report"],
        "config_file": str(CONFIG_PATH),
    }


def sync_once() -> dict:
    """One pass. Reads, sends, types back, stamps the heartbeat, and returns a
    report of exactly what it did -- which is what the web UI shows and what
    the loop writes into the heartbeat note."""
    cfg = load()
    report: dict[str, Any] = {"sent": 0, "threads": 0, "typed": 0, "photos": 0,
                              "history": 0, "skipped": 0, "notes": []}
    cloud = Cloud(cfg.get("supabase_url", ""), cfg.get("supabase_anon_key", ""),
                  cfg.get("email", ""), cfg.get("password", ""))
    device = (platform.node() or "shop PC")[:60]

    hwnd = find_window()
    if not hwnd:
        report["notes"].append("DuoKe is not open")
        try:
            cloud.beat(device, False, 0, "DuoKe is not open on this PC")
        except CloudError as exc:
            report["notes"].append(str(exc))
        _state["last_report"] = report
        _state["last_pass_at"] = datetime.now(timezone.utc).isoformat()
        return report

    root = uia.element_from_handle(hwnd)
    if root is None:
        report["notes"].append("Windows would not hand over DuoKe's elements")
        _state["last_report"] = report
        return report

    window = winapi.window_rect(hwnd)
    reader = cfg.get("reader") or {}
    nodes = _walk(root, max_depth=16, budget=[6000])
    threads = read_threads(nodes, window, reader)
    report["threads"] = len(threads)

    seen = _seen()
    fresh: set[str] = set()

    def harvest(chat_name: str, tree: list[dict]) -> None:
        key = _chat_key(chat_name)
        lines = read_open_conversation(tree, window, reader)
        inbound = [l["text"] for l in lines if l["inbound"]]
        if not inbound:
            return
        # Only the last thing they said is answered. The lines before it went
        # up on an earlier pass, or were answered by hand; sending them again
        # would put three rows on the screen for one conversation.
        text = inbound[-1].strip()
        if not text:
            return
        mark = _fingerprint(key, text)
        if mark in seen or mark in fresh:
            report["skipped"] += 1
            return
        row = {
            "user_id": cloud.user_id,
            "store": cfg.get("store") or "",
            "chat_key": key,
            "buyer": key,
            "message": text[:4000],
            "fingerprint": mark,
            "source": "duoke",
            # The lines around it, since they were read anyway getting here.
            # A draft written without them answers "so tomorrow?" confidently
            # and wrongly.
            "history": [{"inbound": bool(l["inbound"]), "text": l["text"][:400]}
                        for l in lines[:-1][-MESSAGE_TAIL:]],
        }
        try:
            if cloud.send_message(row):
                report["sent"] += 1
            else:
                report["skipped"] += 1
            fresh.add(mark)
        except CloudError as exc:
            report["notes"].append(str(exc))

    try:
        cloud.sign_in()
    except CloudError as exc:
        _state["last_error"] = str(exc)
        report["notes"].append(str(exc))
        _state["last_report"] = report
        return report

    # The conversation already open is read first, because reading it costs no
    # clicks -- but only when the window title says which one it is. An
    # unnamed conversation is skipped rather than filed under a guess.
    open_name = open_thread_name(threads, hwnd)
    if open_name:
        harvest(open_name, nodes)
    else:
        report["notes"].append("could not tell which conversation is open — read the unread ones only")

    if cfg.get("open_unread"):
        for thread in [t for t in threads if t["unread"]][:MAX_THREADS_PER_PASS]:
            if not _open_thread(thread, hwnd, allow_click=True):
                report["notes"].append(f"could not open {thread['name'][:30]}")
                continue
            fresh_root = uia.element_from_handle(hwnd)
            harvest(thread["name"], _walk(fresh_root, max_depth=16, budget=[6000]))

    # ---- somebody on a phone asked to see a thread
    #
    # Served before the replies are typed, because the person waiting for it is
    # looking at the screen right now, while a reply that has been approved has
    # already been decided and can wait another twenty seconds.
    try:
        wanted = cloud.history_wanted()
    except CloudError as exc:
        wanted = []
        report["notes"].append(str(exc))
    for row in wanted:
        key = (row.get("chat_key") or "").strip()
        tree = _walk(uia.element_from_handle(hwnd), max_depth=16, budget=[6000])
        here = read_threads(tree, window, reader)
        target = next((t for t in here if _chat_key(t["name"]) == key), None)
        if not target:
            report["notes"].append(f"{key[:30]} is not in the list to read")
            continue
        if not _open_thread(target, hwnd, allow_click=True):
            report["notes"].append(f"could not open {key[:30]} to read it")
            continue
        tree = _walk(uia.element_from_handle(hwnd), max_depth=16, budget=[6000])
        lines = read_open_conversation(tree, window, reader, tail=HISTORY_TAIL)
        try:
            cloud.save_history(str(row.get("id")),
                               [{"inbound": bool(l["inbound"]), "text": l["text"][:400]}
                                for l in lines])
            report["history"] += 1
        except CloudError as exc:
            report["notes"].append(str(exc))

    # ---- and the other direction
    if cfg.get("type_back"):
        try:
            pending = cloud.replies_to_type()
        except CloudError as exc:
            pending = []
            report["notes"].append(str(exc))
        for row in pending:
            key = (row.get("chat_key") or "").strip()
            text = (row.get("reply") or "").strip()
            if not text:
                continue
            tree = _walk(uia.element_from_handle(hwnd), max_depth=16, budget=[6000])
            here = read_threads(tree, window, reader)
            target = next((t for t in here if _chat_key(t["name"]) == key), None)
            if target and not _open_thread(target, hwnd, allow_click=True):
                report["notes"].append(f"could not open {key[:30]} to reply")
                continue
            if not target:
                report["notes"].append(f"{key[:30]} is not in the list any more")
                continue
            tree = _walk(uia.element_from_handle(hwnd), max_depth=16, budget=[6000])
            ok, how = type_reply(tree, window, reader, hwnd, text)
            if not ok:
                report["notes"].append(f"{key[:30]}: {how}")
                continue
            report["typed"] += 1
            # The words have gone. A photo that will not paste must not undo
            # that: the reply is marked typed either way, and the picture is
            # reported as the one thing still to do by hand.
            paths = row.get("photos") or []
            if cfg.get("send_photos") and isinstance(paths, list):
                for path in [str(p) for p in paths if p][:3]:
                    try:
                        image = cloud.object_bytes(str(cfg.get("photo_bucket")), path)
                    except CloudError as exc:
                        report["notes"].append(f"{key[:30]}: {exc}")
                        continue
                    fresh = _walk(uia.element_from_handle(hwnd), max_depth=16, budget=[6000])
                    sent, why = paste_photo(fresh, window, reader, hwnd, image)
                    if sent:
                        report["photos"] += 1
                    else:
                        report["notes"].append(f"{key[:30]} photo: {why}")
            try:
                cloud.mark_typed(str(row.get("id")))
            except CloudError as exc:
                report["notes"].append(str(exc))

    _remember(fresh)
    note = (f"read {report['threads']} threads, sent {report['sent']}, typed {report['typed']}"
            + (f", {report['photos']} photos" if report["photos"] else "")
            + ("; " + "; ".join(report["notes"][:3]) if report["notes"] else ""))
    try:
        cloud.beat(device, True, report["threads"], note)
    except CloudError as exc:
        report["notes"].append(str(exc))

    _state["last_pass_at"] = datetime.now(timezone.utc).isoformat()
    _state["last_report"] = report
    _state["last_error"] = "" if not report["notes"] else report["notes"][0]
    return report


# ---------------------------------------------------------------- the loop


class Watcher:
    """Calls sync_once on a timer for as long as the agent is up.

    One thread, and it holds no lock: a pass that hangs on a COM call must not
    take the web UI down with it, so the only thing shared with the rest of
    the agent is the _state dict it writes at the end of each pass.
    """

    def __init__(self) -> None:
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> bool:
        if self._thread and self._thread.is_alive():
            return False
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="duoke-watcher", daemon=True)
        self._thread.start()
        _state["running"] = True
        return True

    def stop(self) -> None:
        self._stop.set()
        _state["running"] = False

    def _run(self) -> None:
        # A first pass straight away would run while the agent is still
        # starting up and DuoKe may not be back yet after a reboot.
        self._stop.wait(8)
        while not self._stop.is_set():
            cfg = load()
            if cfg.get("enabled"):
                try:
                    sync_once()
                except Exception as exc:  # a pass must never kill the loop
                    _state["last_error"] = f"{type(exc).__name__}: {exc}"
            self._stop.wait(max(5, int(cfg.get("poll_seconds") or 20)))
        _state["running"] = False


watcher = Watcher()
