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
import re
import subprocess
import tempfile
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
CATALOG_PATH = config.ROOT_DIR / "duoke-catalog.json"
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
    # A minimised DuoKe cannot be read at all: minimising it makes Chromium
    # tear down the accessibility tree, and restoring it from here does not
    # build it again -- only a real click does, because our process has no
    # claim on the foreground and Windows refuses to hand it over. So this
    # restores the window and LEAVES IT UP, which is the state it has to be in
    # anyway. It may sit behind everything else; it may not sit in the taskbar.
    # Off means a minimised DuoKe is left alone and the heartbeat says why
    # nothing was read.
    "restore_if_minimized": True,
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
    # A photo can only go in through the clipboard, and a clipboard paste needs
    # the real keyboard, which needs the window in front for a second -- so
    # sending a photo takes the screen and the mouse briefly. Words do not:
    # they are posted. Off means photos wait to be pasted by hand and nothing
    # ever jumps in front of what somebody is doing.
    "photos_may_take_screen": True,
    # A reply can carry a product card -- the same one DuoKe's own Product tab
    # sends, with the picture, the price and the link in it. Off means the
    # words go and the product does not.
    "send_products": True,
    # How often the shop's own listings are read out of the Product tab into
    # Pigu, so the Replies screen can search them instead of somebody typing
    # names in by hand. Pressing Sync products on the screen asks for one
    # straight away; this is the "and anyway, every so often" number.
    "catalog_hours": 12,
    # The catalogue read moves the Product tab about, so it waits for a quiet
    # moment: nothing unread, nothing approved and still to type. A shop mid
    # morning never has one -- which is right, because a buyer waiting matters
    # more than a listing being a few hours stale.
    "catalog_when_quiet": True,
    # Answering a buyer in DuoKe by hand should take them off the list in
    # Pigu, and the only way to know it happened is to look: a conversation
    # whose last line is now OURS has been dealt with. That costs a click, so
    # it happens to one conversation at a time, no more often than this, and
    # only when nothing is waiting to go out.
    "hand_check_seconds": 60,
    # And not straight away: a buyer who sends two lines ten seconds apart
    # would otherwise have the first one checked while they are still typing
    # the second.
    "hand_check_after": 45,
    # How many conversations one look may try before giving up for this minute.
    # A row that can never be found -- a renamed buyer, a deleted chat -- would
    # otherwise block the check for good.
    "hand_check_tries": 3,
    # Where reply photos live. The same private bucket the rest of the app
    # uses; the path is what the row carries.
    "photo_bucket": "shipment-photos",
    # Opening an unread conversation is a click, and a click needs the window.
    # Off means only the conversation already open is read.
    "open_unread": True,
    # Where the three things are. Everything here is empty on purpose: the
    # window is measured rather than described, from the one element that can
    # be found without being told anything -- the reply box, which is the
    # widest text field at the bottom and is exactly as wide as the
    # conversation above it. Everything else is relative to that.
    #
    # Fill a selector or an x_band in from a probe only where the measuring
    # gets it wrong on a particular build: a number written down here is true
    # until somebody drags the window to another screen, and measuring is true
    # afterwards as well.
    "reader": {
        "chat_list": {"automation_id": "", "class_name": "", "name": "", "x_band": []},
        "messages": {"automation_id": "", "class_name": "", "name": "", "x_band": []},
        "input": {"automation_id": "", "class_name": "", "name": ""},
        # how far left of the conversation the thread list reaches. Beyond it
        # is the shop switcher, whose entries are text too and are not people.
        "list_width": 560,
        # The Product tab on the right-hand panel: its tab, its search box, the
        # Send button on a row, and how tall a row is. Names as the app writes
        # them in English; a build in another language needs these changed and
        # nothing else.
        # DuoKe's own search, top middle: what finds a buyer whose
        # conversation has scrolled out of the list. Its results appear as a
        # panel under it, headed "Buyer".
        "chat_search": "Search",
        "product_tab": "Product",
        "product_search": "Search Product Name",
        "product_send": "Send",
        "product_row_height": 252,
        # how many screenfuls of listings to scroll through in one read, and
        # how far a notch of the wheel goes. Four hundred listings is a big
        # shop and this covers it; a shop with more gets the rest next time.
        "catalog_pages": 3,
        "catalog_scroll_notches": 5,
        # What to search the shop for when reading the catalogue.
        #
        # The Product tab's own list is not the catalogue: it is that buyer's
        # recent inquiries, half a dozen rows that do not scroll. The search
        # box is what reaches the whole shop, so the read sweeps it -- and the
        # terms are mostly single vowels, because every title has an "a" or an
        # "e" in it somewhere and the point is coverage rather than relevance.
        # The words after them are here for titles that a vowel search buries.
        "catalog_terms": ["a", "e", "i", "o", "u", "card", "sticker", "balloon",
                          "tag", "box", "gift", "party"],
        # How many of those terms to search in ONE pass. One. Twelve of them
        # back to back is a minute and a half of the Product tab scrolling
        # itself in front of whoever is using the PC; one is a flicker, and the
        # sweep finishes over the next few passes instead. Where it got to is
        # remembered in duoke-catalog.json.
        "catalog_terms_per_pass": 1,
        # where the right-hand panel starts, in pixels from the window's left
        # edge. Only the panel is searched for product rows: the conversation
        # has titles in it too, in the strip above the messages.
        "panel_left": 4250,
        # the strip above the conversation: the buyer's name, the product, the
        # order number. Text, in the same column, said by nobody.
        "header_height": 300,
        # and where that strip starts, below the window's own toolbar
        "header_top": 150,
        # a row in the conversation list, and where the list begins under its
        # tabs and its Sort control
        "row_height": 100,
        "list_top": 150,
        # What an unread thread looks like in the list. DuoKe shows a count;
        # a thread whose name matches this is opened and read.
        "unread_pattern": r"\((\d+)\)|\b(\d+)\s*条|·\s*(\d+)$",
        # Narrower than this at the bottom of the window is not the reply box:
        # it is a search field, or the Select on an order panel.
        "min_input_width": 700,
        # A message bubble shorter than this is a sticker, a timestamp or a
        # read receipt, not a question.
        "min_message_chars": 2,
        # The same again for lines that carry a name or a number in them, so a
        # whole-line match cannot catch them: "partymonkeysg:main has joined
        # the conversation" was landing in Pigu as the thing a buyer said.
        "ignore_patterns": [
            r"has joined the conversation",
            r"has left the conversation",
            r"(?:已|已经)(?:加入|离开)(?:会话|对话)",
            r"^(?:assigned|transferred) to ",
            r"^you (?:have been )?assigned",
        ],
        # DuoKe's own furniture, sitting in the conversation looking like
        # things somebody said: the label on an auto-reply, the heading over a
        # translation, the order panel's field names. Matched as a
        # case-insensitive whole line, so a buyer who writes "more" is still
        # heard.
        "ignore_lines": [
            "more", "seller note", "translate", "original text", "translation preview",
            "default translation", "duoke order note", "auto invite to follow",
            "incoming buyer reception", "logistic information", "shipping provider",
            "tracking number", "order id", "total amount", "logistics status",
            "abnormal reason", "completed time", "no result found", "sort",
        ],
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


_last_hand_check = [0.0]


def _sweep_at() -> int:
    """Which search term the catalogue sweep is up to."""
    try:
        return int(json.loads(CATALOG_PATH.read_text(encoding="utf-8")).get("term", 0))
    except (json.JSONDecodeError, OSError, TypeError, ValueError):
        return 0


def _sweep_to(term: int) -> None:
    try:
        CATALOG_PATH.write_text(json.dumps({"term": term}), encoding="utf-8")
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
            "/reply_messages?select=id,chat_key,reply,store,photos,products"
            "&status=eq.answered&typed_at=is.null&reply=neq."
            "&order=answered_at.asc&limit=20"
        )
        out = self.rest("GET", query)
        return out if isinstance(out, list) else []

    def catalog_due(self, every_hours: int) -> bool:
        """Whether to read the catalogue this pass: asked for since it was last
        done, or simply older than the interval."""
        out = self.rest("GET", "/reply_sync?select=catalog_wanted_at,catalog_at&id=eq.true")
        row = (out or [{}])[0] if isinstance(out, list) else {}
        asked, done = row.get("catalog_wanted_at"), row.get("catalog_at")
        if asked and (not done or str(done) < str(asked)):
            return True
        if not done:
            return True
        try:
            when = datetime.fromisoformat(str(done).replace("Z", "+00:00"))
        except ValueError:
            return True
        return (datetime.now(timezone.utc) - when).total_seconds() > max(1, every_hours) * 3600

    def save_catalog(self, rows: list[dict], shop: str, finished: bool = True) -> int:
        """Write the listings: insert what is new, refresh what is known.

        NOT an on_conflict upsert. PostgREST resolves on_conflict against a
        unique constraint on the named COLUMNS, and the catalogue's key is an
        expression -- lower(btrim(title)) -- so the whole write came back
        42P10, "no unique or exclusion constraint matching". Reading what is
        already there and splitting the write in two needs no change to the
        table, which matters because the table is already live.

        The refresh is one request for the many and one for the few: every
        known row's seen_at moves together (that is what says a listing is
        still on the panel), and only a row whose price, SKU or stock has
        actually changed is written on its own.
        """
        if not rows:
            return 0
        now = datetime.now(timezone.utc).isoformat()
        where_shop = f"&shop=eq.{urllib.parse.quote(shop or '')}"
        known = self.rest(
            "GET", "/reply_catalog?select=id,title,price,sku,stock&limit=2000" + where_shop)
        by_title = {}
        for row in (known if isinstance(known, list) else []):
            by_title[str(row.get("title", "")).strip().lower()] = row

        fresh, again, changed = [], [], []
        for r in rows:
            title = r["title"][:300]
            shaped = {
                "price": r.get("price", "")[:60],
                "sku": r.get("sku", "")[:80],
                "stock": r.get("stock", "")[:20],
            }
            have = by_title.get(title.strip().lower())
            if not have:
                fresh.append({"user_id": self.user_id, "shop": shop or "",
                              "title": title, "seen_at": now, **shaped})
                continue
            again.append(str(have.get("id")))
            if any(str(have.get(k) or "") != v for k, v in shaped.items()):
                changed.append((str(have.get("id")), shaped))

        for at in range(0, len(fresh), 50):
            self.rest("POST", "/reply_catalog", fresh[at:at + 50], prefer="return=minimal")
        for at in range(0, len(again), 100):
            ids = ",".join(again[at:at + 100])
            self.rest("PATCH", f"/reply_catalog?id=in.({ids})", {"seen_at": now},
                      prefer="return=minimal")
        for row_id, shaped in changed[:80]:
            self.rest("PATCH", f"/reply_catalog?id=eq.{urllib.parse.quote(row_id)}",
                      {**shaped, "seen_at": now}, prefer="return=minimal")

        # catalog_at is "when the catalogue was last read THROUGH", so a slice
        # of the sweep moves the count and the seen_at stamps but not the clock
        # that decides when to read again.
        beat: dict[str, Any] = {"catalog_count": len(by_title) + len(fresh)}
        if finished:
            beat["catalog_at"] = now
        self.rest("PATCH", "/reply_sync?id=eq.true", beat, prefer="return=minimal")
        return len(fresh) + len(again)

    def pending_chats(self) -> list[dict]:
        """The conversations Pigu is still showing as unanswered, oldest first,
        as [{chat_key, buyer, ids, newest}]."""
        out = self.rest(
            "GET",
            "/reply_messages?select=id,chat_key,buyer,received_at&status=eq.new"
            "&order=received_at.asc&limit=200",
        )
        rows = out if isinstance(out, list) else []
        chats: dict[str, dict] = {}
        for row in rows:
            key = (row.get("chat_key") or row.get("buyer") or "").strip()
            if not key:
                continue
            seat = chats.setdefault(key, {"chat_key": key, "buyer": row.get("buyer") or key,
                                          "ids": [], "newest": ""})
            seat["ids"].append(str(row.get("id")))
            stamp = str(row.get("received_at") or "")
            if stamp > seat["newest"]:
                seat["newest"] = stamp
        return sorted(chats.values(), key=lambda c: c["newest"])

    def answered_by_hand(self, ids: list[str]) -> None:
        """Off the list, without a reply of its own: somebody typed one in
        DuoKe. Skipped is exactly what that means here -- dealt with, and
        nothing for the shop PC to send."""
        if not ids:
            return
        joined = ",".join(urllib.parse.quote(i) for i in ids)
        self.rest("PATCH", f"/reply_messages?id=in.({joined})",
                  {"status": "skipped"}, prefer="return=minimal")

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
        # A minimised window reports a placeholder 314x50 strip, which the
        # size test below would throw out -- and then nothing would ever
        # restore it, which is how "DuoKe is not open" came to mean "DuoKe is
        # sitting in the taskbar". Size can only be judged once it is up.
        if winapi.is_minimized(hwnd):
            best = hwnd
            break
        left, top, right, bottom = winapi.window_rect(hwnd)
        if right - left < 400 or bottom - top < 300:
            continue
        best = hwnd
        break
    return best


# How long to wait for a Chromium app to build its accessibility tree.
#
# DuoKe is Electron, and an Electron window hands out nothing but empty panes
# until an accessibility client asks -- the FIRST probe of this window returned
# 22 nodes, the second 1476. So the tree is touched, given a moment, and only
# then walked. Nothing is a bug about this; it is how Chromium avoids building
# a tree nobody reads.
TREE_WARM_SECONDS = 0.9
# Below this many named elements the window is assumed to be still cold rather
# than genuinely empty, and it is asked again.
TREE_COLD_NAMES = 12


def warm_tree(hwnd: int) -> list[dict]:
    """The window's elements, after however long it takes them to exist.

    Asking is what makes Chromium build the tree, so this asks repeatedly
    rather than waiting once: a window just restored from the taskbar has been
    seen to take three or four seconds to answer with anything at all, and the
    only way to find out is to keep asking.
    """
    nodes: list[dict] = []
    for attempt in range(6):
        nodes = _walk(uia.element_from_handle(hwnd), max_depth=16, budget=[9000])
        named = sum(1 for n in nodes if (n.get("name") or "").strip())
        if named >= TREE_COLD_NAMES:
            return nodes
        time.sleep(TREE_WARM_SECONDS)
    return nodes


def _show_for_reading(hwnd: int) -> bool:
    """Bring a minimised window back up, never to the front.

    Returns True when this call is what restored it -- which is now only worth
    knowing so the pass can say so. It is deliberately NOT put back
    afterwards: minimising the window is what kills the tree, and it stays
    killed until somebody clicks the app, so a sync that tidied up after
    itself would break every pass after the first.
    """
    if not winapi.is_minimized(hwnd):
        return False
    if not load().get("restore_if_minimized"):
        return False
    winapi.restore_without_focus(hwnd)
    # Layout first, then the accessibility tree on top of it. Restoring and
    # walking in the same breath reads a window that has not been laid out.
    time.sleep(1.2)
    return True


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
        cfg = load()
        looked = cfg.get("window_title") or cfg.get("window_process") or "duoke"
        return {"ok": False, "error": f"no open window belongs to \"{looked}\""}
    probe_restored = _show_for_reading(hwnd)
    root = uia.element_from_handle(hwnd)
    if root is None:
        return {"ok": False, "error": "Windows would not hand over that window's elements"}
    window = winapi.window_rect(hwnd)
    nodes = warm_tree(hwnd)

    # Panes ranked by how much text they contain, which is what tells the
    # conversation from the chrome around it.
    panes: list[dict] = []
    for node in nodes:
        rect0 = node.get("rect")
        if rect0 and (rect0[2] - rect0[0] <= 1 or rect0[3] - rect0[1] <= 1):
            continue                      # built but not shown; see _texts
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
        # A panel that is built but not shown -- an order sidebar, a terms
        # dialog, the tab nobody is looking at -- reports a rect with no width
        # or height at all. Its text is real text and belongs to no column, so
        # without this the conversation fills up with a migration letter.
        if rect[2] - rect[0] <= 1 or rect[3] - rect[1] <= 1:
            continue
        out.append({"text": name, "rect": rect, "_el": node["_el"],
                    "control_type": node["control_type"]})
    out.sort(key=lambda n: (n["rect"][1], n["rect"][0]))
    return out


_TIME_ONLY = re.compile(r"^[\d\s:./\-]+$")
# An icon font puts its glyphs in Unicode's private use area, so a chat list
# full of \ue22c is full of buttons, not people.
_GLYPHS = re.compile(r"^[\ue000-\uf8ff\s]+$")
# The unread badge: a small number on its own, at the left of a row.
_BADGE = re.compile(r"^\d{1,3}$")
# When a row says something happened: a clock, a date, or an age. Sits on the
# top line beside the name and the shop, and is neither of those.
_WHEN = re.compile(r"^\d{1,2}[:/.]\d{2}(?::\d{2})?$|^\d+\s*(?:m|min|mins|h|hr|d|day|days)$"
                   r"|^(?:just now|now)$", re.IGNORECASE)
_NOISE = re.compile(
    r"^(yesterday|today|kemarin|hari ini|昨天|今天|已读|未读|read|unread|sent|delivered)$",
    re.IGNORECASE,
)


def _patterns(reader: dict) -> list:
    """The ignore_patterns, compiled, with a broken one dropped rather than
    taking the whole pass down with it."""
    out = []
    for raw in (reader.get("ignore_patterns") or []):
        try:
            out.append(re.compile(str(raw), re.IGNORECASE))
        except re.error:
            continue
    return out


def _is_noise(text: str) -> bool:
    t = text.strip()
    return (not t or bool(_TIME_ONLY.match(t)) or bool(_NOISE.match(t))
            or bool(_GLYPHS.match(t)))


def message_band(nodes: list[dict], window: tuple[int, int, int, int],
                 reader: dict) -> Optional[list[float]]:
    """Where the conversation is, in pixels from the window's left edge.

    Taken from the reply box, because the reply box is exactly as wide as the
    conversation above it -- which is true of this app at any window size and
    on any monitor, where a number typed into a config file is true only until
    somebody drags the window onto a different screen. An explicit x_band in
    duoke.json still wins, for a build where it is not true.
    """
    told = list((reader.get("messages") or {}).get("x_band") or [])
    box = _input_box(nodes, reader)
    rect = box.get("rect") if box else None
    # Measured first, told second -- but only when what was measured can
    # plausibly be a reply box. With no conversation open there is no reply box
    # at all, and the widest field at the bottom is then a 476px search box,
    # which would put the conversation in the wrong half of the window.
    if rect:
        left = rect[0] - window[0]
        right = rect[2] - window[0]
        if (right - left) >= float(reader.get("min_input_width") or 700):
            # a little slack each side: a bubble can be indented past the edge
            return [max(0, left - 40), right + 40]
    return told or None


def read_open_who(nodes: list[dict], window: tuple[int, int, int, int],
                  reader: dict, band: list[float]) -> Optional[str]:
    """Who the open conversation is with.

    The window title cannot say: DuoKe titles itself after itself. The name is
    written above the conversation instead -- the leftmost piece of text in the
    conversation column's own header strip -- which is where a chat app has put
    it since chat apps existed.
    """
    header = int(reader.get("header_height") or 300)
    # Below the window's own toolbar, whose stats ("Online Duration") sit in
    # the same column and read as a name to anything looking only at x.
    above = int(reader.get("header_top") or 150)
    lines = [
        l for l in _texts([n for n in nodes if _in_band(n, band, window)], 2)
        if above <= (l["rect"][1] - window[1]) < header and not _is_noise(l["text"])
    ]
    if not lines:
        return None
    lines.sort(key=lambda l: (l["rect"][0], l["rect"][1]))
    return lines[0]["text"].strip() or None


def read_open_conversation(nodes: list[dict], window: tuple[int, int, int, int],
                           reader: dict, tail: int = MESSAGE_TAIL) -> list[dict]:
    """The tail of the conversation on screen, each line marked inbound or not.

    Which side of the pane a bubble sits on is what says who wrote it. Every
    chat app ever built puts the other person on the left and you on the
    right, and unlike a colour or an automation id that is still true after an
    update -- so the split is measured from the pane the bubbles are actually
    in, not assumed.
    """
    band = message_band(nodes, window, reader)
    region = ([n for n in nodes if _in_band(n, band, window)] if band
              else _find_region(nodes, reader.get("messages") or {}, "x_band", window))
    header = int(reader.get("header_height") or 300)
    skip = {str(x).strip().lower().rstrip(":：") for x in (reader.get("ignore_lines") or [])}
    junk = _patterns(reader)
    lines = [l for l in _texts(region, int(reader.get("min_message_chars") or 2))
             if l["text"].strip().lower().rstrip(":：") not in skip
             and not any(rx.search(l["text"]) for rx in junk)]
    # The strip above the conversation carries the buyer's name, the product and
    # the order number. All of it is text in the same column and none of it was
    # said by anybody.
    lines = [l for l in lines
             if not _is_noise(l["text"]) and (l["rect"][1] - window[1]) >= header]
    if not lines:
        return []
    left = min(l["rect"][0] for l in lines)
    right = max(l["rect"][2] for l in lines)
    middle = (left + right) / 2
    out: list[dict] = []
    for line in lines[-tail:]:
        centre = (line["rect"][0] + line["rect"][2]) / 2
        text = line["text"]
        # DuoKe draws its own translation under each message, so every line
        # arrives twice in a row when the languages match -- which is what made
        # one sent reply look like two sent replies.
        if out and out[-1]["text"] == text:
            continue
        out.append({"text": text, "inbound": centre < middle})
    return out


def read_threads(nodes: list[dict], window: tuple[int, int, int, int],
                 reader: dict) -> list[dict]:
    """The conversation list, a row at a time.

    A row in this list is not one element and not one line of text: it is a
    badge, a name, the shop it came through, a time and a preview of the last
    message, all laid out side by side. Read line by line it produces five
    "threads" per conversation, one of them called "12:29". So the texts in the
    column are grouped by how far down the window they sit -- anything within a
    row's height of each other is one conversation -- and inside a row the name
    is the widest thing on the top line, which is what a chat list always makes
    it.

    Unread is the small number to the left of the name. Nothing else in a row
    is a bare one-to-three-digit number: a time has a colon in it.
    """
    named = reader.get("chat_list") or {}
    if named.get("automation_id") or named.get("class_name") or named.get("name") \
            or named.get("x_band"):
        region = _find_region(nodes, named, "x_band", window)
    else:
        # Immediately left of the conversation, and no wider than a list of
        # names needs to be: further left than that is the shop switcher, whose
        # entries are also plain text and are not conversations.
        band = message_band(nodes, window, reader)
        width = float(named.get("list_width") or 560)
        region = ([n for n in nodes if _in_band(n, [max(0, band[0] - width), band[0]], window)]
                  if band else [])
        if not region:
            # No conversation open, so nothing measured the columns. The list
            # is still there to be read, and its own band is the one thing
            # worth writing down in duoke.json for this build.
            told = list(named.get("x_band") or [])
            if told:
                region = [n for n in nodes if _in_band(n, told, window)]
    pattern = reader.get("unread_pattern") or ""
    rx = None
    if pattern:
        try:
            rx = re.compile(pattern)
        except re.error:
            rx = None
    row_height = float(reader.get("row_height") or 100)
    top = float(reader.get("list_top") or 230)

    # everything in the column, below its own tabs -- and not the column's own
    # controls, which sit among the rows and are not people
    skip = {str(x).strip().lower().rstrip(":：") for x in (reader.get("ignore_lines") or [])}
    junk = _patterns(reader)
    items = [i for i in _texts(region, 1)
             if (i["rect"][1] - window[1]) >= top
             and i["text"].strip().lower().rstrip(":：") not in skip
             and not any(rx.search(i["text"]) for rx in junk)]
    if not items:
        return []
    items.sort(key=lambda i: (i["rect"][1], i["rect"][0]))

    rows: list[list[dict]] = []
    for item in items:
        y = item["rect"][1]
        if rows and (y - rows[-1][0]["rect"][1]) < row_height:
            rows[-1].append(item)
        else:
            rows.append([item])

    out = []
    seen: set[str] = set()
    for row in rows:
        badges = [i for i in row if _BADGE.match(i["text"].strip())]
        words = [i for i in row if not _is_noise(i["text"]) and i not in badges]
        if not words:
            continue
        first_y = min(i["rect"][1] for i in words)
        # the top line of the row: the name, the shop, the time. The preview
        # sits below it and is not what the conversation is called.
        top_line = [i for i in words if i["rect"][1] - first_y <= 30]
        # A conversation row is a name with something said underneath it.
        # Without this, a DuoKe that has come up on its dashboard hands over
        # "Number of consultations" and "Guide Buyers" as though they were
        # people waiting for an answer -- which is how a statistics panel
        # nearly got messages posted about it.
        if len(top_line) >= len(words):
            continue
        # Leftmost, not widest. Widest was the obvious rule and it picked the
        # shop a conversation came through over the person in it: "bnair" is
        # 69 pixels wide and "PartyMonkey" beside it is 177. The buyer's name
        # is the first thing on the line, at the same left edge in every row.
        named = min(top_line or words, key=lambda i: i["rect"][0])
        name = named["text"].strip()
        # Everything else on the top line is the shop and the time. One PC
        # answers for three shops here, and which one a buyer wrote to changes
        # what is true about postage and what the reply should sound like --
        # so it travels with the message rather than being guessed later.
        shop = ""
        for other in sorted((i for i in top_line if i is not named),
                            key=lambda i: i["rect"][0]):
            text = other["text"].strip()
            if not text or _WHEN.match(text) or _BADGE.match(text):
                continue
            shop = text
            break
        if not name or name in seen:
            continue
        seen.add(name)
        left = min(i["rect"][0] for i in row)
        right = max(i["rect"][2] for i in row)
        out.append({
            "name": name,
            "shop": shop,
            "unread": bool(badges) or bool(rx and rx.search(name)),
            # the whole row is the click target, but the name is the element
            # most likely to answer an invoke
            "rect": (left, first_y, right, max(i["rect"][3] for i in row)),
            "_el": named["_el"],
        })
    return out


def open_by_search(hwnd: int, window: tuple[int, int, int, int], reader: dict,
                   key: str) -> bool:
    """Open a conversation that is no longer in the list, by searching for it.

    The list holds the recent and the unread; a buyer answered an hour ago has
    dropped off the bottom of it, and a reply approved for them then had
    nowhere to go -- "not in the list any more" was three real replies sitting
    unsent. DuoKe's own search finds them, and its results are rows under a
    "Buyer" heading in the middle of the window, which is neither the list
    column nor the conversation.

    The search box is emptied afterwards: leaving somebody else's name in it
    would leave the app showing a list nobody asked for.
    """
    want = (key or "").strip()
    if not want:
        return False
    nodes = warm_tree(hwnd)
    name = str(reader.get("chat_search") or "Search")
    box = next((n for n in nodes
                if n["control_type"] == "Edit" and (n.get("name") or "").strip() == name), None)
    if not box:
        return False
    if not _post_type(hwnd, box, want):
        return False
    _post_key(hwnd, box, "enter")
    time.sleep(2.0)

    band = message_band(warm_tree(hwnd), window, reader)
    list_right = (band[0] if band else 967)
    hits = []
    for node in warm_tree(hwnd):
        rect = node.get("rect")
        if not rect or rect[2] - rect[0] <= 1:
            continue
        text = (node.get("name") or "").strip()
        if not text or node["control_type"] not in ("Text", "ListItem"):
            continue
        # a result, not the list row it may also still be in, and not the
        # conversation header of whoever is open
        if (rect[0] - window[0]) < list_right:
            continue
        if _chat_key(text).lower() != want.lower():
            continue
        hits.append((rect[1], node))
    hits.sort(key=lambda h: h[0])

    opened = False
    for _y, node in hits[:3]:
        if not _post_click_element(hwnd, node):
            continue
        time.sleep(1.4)
        tree = warm_tree(hwnd)
        who = read_open_who(tree, window, reader, message_band(tree, window, reader) or [])
        if who and _chat_key(who).lower() == want.lower():
            opened = True
            break

    # tidy up whether or not it worked
    box2 = next((n for n in warm_tree(hwnd)
                 if n["control_type"] == "Edit" and (n.get("name") or "").strip() == name), None)
    if box2:
        _post_type(hwnd, box2, "")
        _post_key(hwnd, box2, "enter")
        time.sleep(0.6)
    return opened


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
    """DuoKe's message box: the WIDEST edit control in the bottom half.

    "Lowest in the window" was the obvious rule and it was wrong on the real
    thing -- an order panel on the far right has a little Select box below the
    reply box, so lowest picked a 185px dropdown over a 3356px message field.
    Widest-at-the-bottom is what actually describes a chat's reply box, and it
    has the useful side effect of measuring the conversation column: the reply
    box spans it exactly, which is what `message_band` uses.
    """
    want = reader.get("input") or {}
    if want.get("automation_id") or want.get("class_name") or want.get("name"):
        named = [n for n in nodes if n["control_type"] == "Edit" and _matches(n, want)]
        if named:
            return named[0]
    edits = [n for n in nodes if n["control_type"] == "Edit" and n.get("rect")
             and n["rect"][2] - n["rect"][0] > 1]
    if not edits:
        return None
    bottom = max(n["rect"][3] for n in edits)
    top = min(n["rect"][1] for n in edits)
    half = top + (bottom - top) / 2
    low = [n for n in edits if n["rect"][1] >= half] or edits
    return max(low, key=lambda n: n["rect"][2] - n["rect"][0])


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

    # TYPED, NOT PASTED. The characters are posted to the window, which needs
    # neither the foreground nor the mouse -- so a reply goes out without
    # anything jumping in front of whoever is using the PC. This was the last
    # thing here still taking the screen, and it did not need to: the box
    # accepts posted characters perfectly well, which was worth ten seconds of
    # trying before assuming a chat box could only be pasted into.
    if _post_type(hwnd, box, text):
        fresh = _input_box(warm_tree(hwnd), reader) or box
        value = uia.get_current_value(fresh.get("_el"))
        landed = value is None or text[:40] in value
        if landed and _post_key(hwnd, fresh, "enter"):
            time.sleep(0.5)
            return True, "typed and sent in the background"
        if value is not None and text[:40] not in value:
            # something is in the box and it is not the reply: do not press
            # Enter on it
            _post_type(hwnd, fresh, "")

    # A build where posting does not reach the editor: the clipboard still
    # does, at the cost of the screen for a second.
    if not load().get("photos_may_take_screen"):
        return False, ("the reply would not type into the box, and pasting is switched off "
                       "(photos_may_take_screen)")
    was = winapi.get_foreground_window()
    borrowed = clip_save()
    try:
        actions.clipboard_write(text)
    except RuntimeError as exc:
        clip_restore(borrowed)
        return False, f"the reply would not go on the clipboard: {exc}"
    try:
        return _paste_and_send(box, hwnd, verify=True)
    finally:
        if not clip_restore(borrowed):
            _state["clipboard_kept"] = borrowed.get("kind", "unknown")
        # Whatever had the screen gets it back. The click that sent the reply
        # took it legitimately; keeping it would mean the next thing somebody
        # types goes into DuoKe.
        if was and was != hwnd:
            winapi.set_foreground(was)


# ---------------------------------------------------------------- clipboard

# Sending a photo has to go through the clipboard -- a file on the clipboard is
# the only thing a chat box built out of HTML will accept as an attachment --
# and the clipboard belongs to whoever is using the PC. Taking it and not
# giving it back means somebody's copied paragraph, spreadsheet row or
# screenshot is gone the next time they press Ctrl+V, which is a far worse
# thing to do to a person's morning than a slow reply.
#
# So it is borrowed: what is on it is saved first and put back afterwards, in
# the format it was in. Text, files and an image are the three that matter;
# anything more exotic (an Excel range, HTML with formatting) cannot be
# round-tripped through here, and the note says so rather than pretending.
_CLIP_READ = r"""
Add-Type -AssemblyName System.Windows.Forms,System.Drawing | Out-Null
$out = @{ kind = 'empty' }
try {
  if ([Windows.Forms.Clipboard]::ContainsFileDropList()) {
    $out = @{ kind = 'files'; files = @([Windows.Forms.Clipboard]::GetFileDropList()) }
  } elseif ([Windows.Forms.Clipboard]::ContainsImage()) {
    $p = Join-Path $env:TEMP ('duoke-clip-' + [guid]::NewGuid().ToString('N') + '.png')
    [Windows.Forms.Clipboard]::GetImage().Save($p, [System.Drawing.Imaging.ImageFormat]::Png)
    $out = @{ kind = 'image'; path = $p }
  } elseif ([Windows.Forms.Clipboard]::ContainsText()) {
    $bytes = [Text.Encoding]::UTF8.GetBytes([Windows.Forms.Clipboard]::GetText())
    $out = @{ kind = 'text'; b64 = [Convert]::ToBase64String($bytes) }
  }
} catch { $out = @{ kind = 'unknown' } }
$out | ConvertTo-Json -Compress
"""


def _powershell_sta(script: str) -> subprocess.CompletedProcess:
    """PowerShell in a single-threaded apartment, which the clipboard APIs
    require. actions._powershell cannot be used for this: its cmdlets are fine
    but Windows.Forms.Clipboard refuses to run MTA."""
    return subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-STA", "-Command", script],
        capture_output=True, text=True, encoding="ascii", errors="replace", timeout=20,
    )


def clip_save() -> dict:
    """What is on the clipboard now, in a shape clip_restore understands."""
    try:
        proc = _powershell_sta(_CLIP_READ)
        if proc.returncode != 0:
            return {"kind": "unknown"}
        return json.loads((proc.stdout or "").strip() or '{"kind":"empty"}')
    except (json.JSONDecodeError, OSError, subprocess.SubprocessError):
        return {"kind": "unknown"}


def clip_restore(snap: dict) -> bool:
    """Put it back. False means it could not be, which the caller reports --
    never silently."""
    kind = (snap or {}).get("kind", "unknown")
    if kind == "empty":
        return True
    if kind == "text":
        b64 = str(snap.get("b64") or "")
        if not b64:
            return False
        script = ("Add-Type -AssemblyName System.Windows.Forms | Out-Null; "
                  "[Windows.Forms.Clipboard]::SetText("
                  f"[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{b64}')))")
    elif kind == "files":
        files = [str(f) for f in (snap.get("files") or []) if str(f).strip()]
        if not files:
            return False
        listed = ",".join("'" + f.replace("'", "''") + "'" for f in files)
        script = ("Add-Type -AssemblyName System.Windows.Forms | Out-Null; "
                  "$c = New-Object System.Collections.Specialized.StringCollection; "
                  f"@({listed}) | ForEach-Object {{ $c.Add($_) | Out-Null }}; "
                  "[Windows.Forms.Clipboard]::SetFileDropList($c)")
    elif kind == "image":
        path = str(snap.get("path") or "")
        if not path:
            return False
        safe = path.replace("'", "''")
        script = ("Add-Type -AssemblyName System.Windows.Forms,System.Drawing | Out-Null; "
                  f"$i = [System.Drawing.Image]::FromFile('{safe}'); "
                  "[Windows.Forms.Clipboard]::SetImage($i); $i.Dispose(); "
                  f"Remove-Item -LiteralPath '{safe}' -ErrorAction SilentlyContinue")
    else:
        return False
    try:
        return _powershell_sta(script).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _post_target(hwnd: int, x: int, y: int) -> Optional[int]:
    """The child window that will accept posted input for a screen point.

    Chromium draws everything into one Chrome_RenderWidgetHostHWND, and that
    window answers posted mouse and character messages whether or not it is
    visible, focused, or on a monitor anybody is looking at. That is what lets
    all of this happen behind the work somebody is doing -- no raising, no
    stealing the foreground, no moving windows between screens. It was found
    by trying it: a raise is refused to a background process and a click aimed
    at a covered window lands in whatever is drawn on top of it.
    """
    cx, cy = winapi.screen_to_client(hwnd, x, y)
    child = winapi.child_window_from_point(hwnd, cx, cy)
    return child or None


def _post_click_at(hwnd: int, x: int, y: int) -> bool:
    child = _post_target(hwnd, x, y)
    if not child:
        return False
    kx, ky = winapi.screen_to_client(child, x, y)
    winapi.post_click(child, kx, ky)
    time.sleep(0.35)
    return True


def _post_click_element(hwnd: int, node: dict) -> bool:
    rect = (node or {}).get("rect")
    if not rect:
        return False
    return _post_click_at(hwnd, (rect[0] + rect[2]) // 2, (rect[1] + rect[3]) // 2)


def _post_type(hwnd: int, node: dict, text: str, clear: int = 60) -> bool:
    """Put text in a field by posting the keystrokes, caret and all.

    End first, then backspaces: a click lands the caret wherever it lands, and
    a field with yesterday's search still in it returns yesterday's results.
    """
    if not _post_click_element(hwnd, node):
        return False
    rect = node["rect"]
    child = _post_target(hwnd, (rect[0] + rect[2]) // 2, (rect[1] + rect[3]) // 2)
    if not child:
        return False
    end = winapi.vk_for("end")
    bs = winapi.vk_for("backspace")
    if end:
        winapi.post_key_down(child, end)
        winapi.post_key_up(child, end)
    for _ in range(clear):
        if bs:
            winapi.post_key_down(child, bs)
            winapi.post_key_up(child, bs)
    time.sleep(0.4)
    for ch in text:
        winapi.post_char(child, ch)
    time.sleep(0.3)
    return True


def _post_key(hwnd: int, node: dict, key: str) -> bool:
    rect = (node or {}).get("rect")
    vk = winapi.vk_for(key)
    if not rect or not vk:
        return False
    child = _post_target(hwnd, (rect[0] + rect[2]) // 2, (rect[1] + rect[3]) // 2)
    if not child:
        return False
    winapi.post_key_down(child, vk)
    winapi.post_key_up(child, vk)
    return True


def _click_box(box: dict, hwnd: int) -> bool:
    """Put the caret in the message box with a real click.

    A real click is also how the window legitimately comes to the front:
    SetForegroundWindow from here is refused outright, because Windows only
    grants the foreground to a process that has some claim to it, and a
    background agent has none. A synthesised click has one.
    """
    rect = box.get("rect") if box else None
    if not rect:
        return False
    x, y = (rect[0] + rect[2]) // 2, (rect[1] + rect[3]) // 2
    return _click_at(hwnd, x, y)


def _click_at(hwnd: int, x: int, y: int) -> bool:
    """Click a point inside DuoKe, wherever DuoKe happens to be.

    Raised to the top of the pile first, because a click goes to whatever is
    drawn at that point and the window is usually covered by whatever the
    person is actually working in -- a click aimed at DuoKe was landing in
    another app's window entirely. Raising does not move the focus; the first
    click does, and a window that has just been raised takes the second one
    properly, which is why there are two.
    """
    winapi.raise_without_focus(hwnd)
    time.sleep(0.25)
    if winapi.window_from_point(x, y) and             winapi.root_window(winapi.window_from_point(x, y)) != hwnd:
        return False
    winapi.physical_move_and_click(x, y)
    time.sleep(0.3)
    winapi.physical_move_and_click(x, y)
    time.sleep(0.35)
    return True


def _paste_and_send(box: dict, hwnd: int, verify: bool) -> tuple[bool, str]:
    """Ctrl+V into the message box, then Enter.

    Chromium is why. The box is a React-controlled editor: UIA's SetValue
    reports success and the value does not stick, and characters posted to a
    background window never reach the renderer. Pasting is the one route in
    that a chat box built out of HTML cannot tell from a person, which is also
    why it needs the window in front for the second it takes.
    """
    ctrl, v, enter = winapi.vk_for("ctrl"), winapi.vk_for("v"), winapi.vk_for("enter")
    if not (ctrl and v and enter):
        return False, "this machine reports no Ctrl, V or Enter key"
    if not _click_box(box, hwnd):
        return False, "the message box has no place to click"
    winapi.physical_key(ctrl)
    winapi.physical_key(v)
    winapi.physical_key(v, key_up=True)
    winapi.physical_key(ctrl, key_up=True)
    # An attachment takes a moment to become an attachment, and a long reply
    # takes a moment to land; Enter before either is an empty message sent to
    # a customer.
    time.sleep(1.2)
    if verify:
        got = uia.get_current_value(box.get("_el")) if box.get("_el") else None
        if got is not None and not got.strip():
            return False, "the paste did not land in the box, so nothing was sent"
    winapi.physical_key(enter)
    winapi.physical_key(enter, key_up=True)
    time.sleep(0.4)
    return True, "pasted and sent"


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
    borrowed = clip_save()
    try:
        proc = actions._powershell(f"Set-Clipboard -LiteralPath '{temp}'")
        if proc.returncode != 0:
            return False, "the photo would not go on the clipboard"

        box = _input_box(nodes, reader)
        if not box:
            return False, "no message box to paste the photo into"
        # A pasted file cannot be read back out of the box, so there is nothing
        # to verify -- the click and the paste either worked or the picture is
        # still on the clipboard.
        return _paste_and_send(box, hwnd, verify=False)
    finally:
        if was and was != hwnd:
            winapi.set_foreground(was)
        if not clip_restore(borrowed):
            _state["clipboard_kept"] = borrowed.get("kind", "unknown")
        try:
            temp.unlink()
        except OSError:
            pass


def send_product(nodes: list[dict], window: tuple[int, int, int, int], reader: dict,
                 hwnd: int, query: str) -> tuple[bool, str]:
    """Send one product card into the open conversation.

    The same three moves a person makes: open the Product tab, search the
    listing by name, press Send on the row. All of it posted rather than
    clicked for real, so the shop PC can be in use while it happens.

    The row is chosen by how much of the query its title actually contains,
    and a title that matches nothing is not sent: a wrong product card is
    worse to a buyer than no product card, because it reads as an answer.
    """
    want = (query or "").strip()
    if not want:
        return False, "no product named"

    tab_name = str(reader.get("product_tab") or "Product")
    tab = next((n for n in nodes
                if n["control_type"] == "TabItem" and (n.get("name") or "").strip() == tab_name), None)
    if not tab:
        return False, f'no "{tab_name}" tab in the window'
    if not _post_click_element(hwnd, tab):
        return False, "the Product tab would not take a click"
    time.sleep(1.2)

    tree = warm_tree(hwnd)
    search_name = str(reader.get("product_search") or "Search Product Name")
    search = next((n for n in tree
                   if n["control_type"] == "Edit" and search_name in (n.get("name") or "")), None)
    if not search:
        return False, "the product search box is not there"
    if not _post_type(hwnd, search, want):
        return False, "the product search box would not take the name"
    _post_key(hwnd, search, "enter")
    time.sleep(2.0)

    tree = warm_tree(hwnd)
    rows = _product_rows(tree, window, reader)
    if not rows:
        return False, f'no product in the shop matches "{want[:40]}"'
    best = _best_product(rows, want)
    if best is None:
        return False, f'nothing in the list looks like "{want[:40]}", so nothing was sent'
    if not _post_click_element(hwnd, best["send"]):
        return False, "the row's Send button would not take a click"
    time.sleep(1.5)
    return True, f'sent "{best["title"][:50]}"'


def _product_rows(nodes: list[dict], window: tuple[int, int, int, int],
                  reader: dict) -> list[dict]:
    """The listings on screen: each one's title and its own Send button.

    A row is a band of the panel, the same way a conversation is a band of the
    list -- the title, the stock, the price, the SKU and the button are five
    separate elements that only a shared y range ties together.
    """
    band = [float(reader.get("panel_left") or 4250), 99999.0]
    pitch = float(reader.get("product_row_height") or 252)
    send_name = str(reader.get("product_send") or "Send")

    sends = [n for n in nodes
             if n["control_type"] == "Button" and (n.get("name") or "").strip() == send_name
             and n.get("rect") and _in_band(n, band, window)]
    titles = [n for n in nodes
              if n["control_type"] == "Text" and n.get("rect") and _in_band(n, band, window)
              and len((n.get("name") or "").strip()) > 15
              and not _is_noise(n.get("name") or "")]
    out = []
    for send in sends:
        top = send["rect"][1] - pitch
        near = [t for t in titles if top <= t["rect"][1] <= send["rect"][3]]
        if not near:
            continue
        # the title is the first line of the row; the SKU and the price sit
        # under it and are shorter
        near.sort(key=lambda t: t["rect"][1])
        out.append({"title": near[0]["name"].strip(), "send": send})
    return out


def _best_product(rows: list[dict], want: str) -> Optional[dict]:
    asked = set(words(want))
    if not asked:
        return None
    best, score_best = None, 0.0
    for row in rows:
        have = set(words(row["title"]))
        if not have:
            continue
        hit = len(asked & have) / len(asked)
        if hit > score_best:
            best, score_best = row, hit
    # Half the words, or it is a different product. "Kawaii Sticker" must not
    # match "Personalised Gift Tag" because both say PartyMonkey.
    return best if score_best >= 0.5 else None


def words(text: str) -> list[str]:
    """The words worth matching on, shop name and packaging noise dropped."""
    out = []
    for raw in re.split(r"[^\w]+", (text or "").lower()):
        if len(raw) > 2 and raw not in _PRODUCT_STOP:
            out.append(raw)
    return out


_PRODUCT_STOP = {
    "the", "and", "for", "with", "pcs", "pack", "set", "sgd", "sku", "new",
    "ready", "stock", "free", "gift", "shipping",
}


def read_catalog(nodes: list[dict], window: tuple[int, int, int, int], reader: dict,
                 hwnd: int) -> tuple[list[dict], str]:
    """Every listing the Product tab will show, scrolled through a page at a
    time.

    The panel is a virtual list: only what is on screen exists in the tree, so
    reading it means scrolling it, and scrolling it means posting a wheel event
    -- which is also why this can run while somebody is using the PC. Stops
    when a screenful adds nothing new, which is what the bottom of a list
    looks like from here.
    """
    tab_name = str(reader.get("product_tab") or "Product")
    tab = next((n for n in nodes
                if n["control_type"] == "TabItem" and (n.get("name") or "").strip() == tab_name), None)
    if not tab:
        # The right-hand panel, tabs and all, only exists while a conversation
        # is open -- on a freshly started DuoKe there is nothing to read the
        # catalogue out of. Opening the first thread in the list is enough, and
        # it is a thread that is about to be read anyway.
        first = next(iter(read_threads(nodes, window, reader)), None)
        if not first or not _open_thread(first, hwnd, allow_click=True):
            return [], "no conversation is open, so the Product tab is not there to read"
        time.sleep(1.2)
        nodes = warm_tree(hwnd)
        tab = next((n for n in nodes
                    if n["control_type"] == "TabItem"
                    and (n.get("name") or "").strip() == tab_name), None)
        if not tab:
            return [], f'no "{tab_name}" tab in the window'
    # whichever tab the panel was showing, so it can be put back afterwards.
    # There is no "selected" flag to read, so it is the one whose pane is
    # drawn: the Custom element named after a tab.
    panes = {(n.get("name") or "").strip() for n in nodes
             if n["control_type"] == "Custom" and (n.get("name") or "").strip()}
    tabs = {(n.get("name") or "").strip() for n in nodes if n["control_type"] == "TabItem"}
    was_on = next(iter(panes & tabs), "")

    if not _post_click_element(hwnd, tab):
        return [], "the Product tab would not take a click"
    time.sleep(1.2)

    # An old search still in the box would return an old shortlist and call it
    # the catalogue -- but an EMPTY search submitted deliberately is worse: the
    # panel answers it with nothing at all and the whole read comes back zero.
    # So the box is only touched when it has something in it, and the reset is
    # re-opening the tab rather than submitting emptiness.
    tree = warm_tree(hwnd)
    search_name = str(reader.get("product_search") or "Search Product Name")
    search = next((n for n in tree
                   if n["control_type"] == "Edit" and search_name in (n.get("name") or "")), None)
    if search and (uia.get_current_value(search.get("_el")) or "").strip():
        _post_type(hwnd, search, "")
        _post_key(hwnd, search, "enter")
        time.sleep(1.5)

    # A panel showing nothing is usually a panel still holding somebody's
    # search -- including an empty one, which it answers with an empty list and
    # keeps answering that way after the box is cleared. Leaving the tab and
    # coming back is what resets it; nothing typed into the box will.
    if not _catalog_rows(warm_tree(hwnd), window, reader):
        other = next((n for n in warm_tree(hwnd)
                      if n["control_type"] == "TabItem"
                      and (n.get("name") or "").strip() not in ("", tab_name)), None)
        if other:
            _post_click_element(hwnd, other)
            time.sleep(1.0)
        again = next((n for n in warm_tree(hwnd)
                      if n["control_type"] == "TabItem"
                      and (n.get("name") or "").strip() == tab_name), None)
        if again:
            _post_click_element(hwnd, again)
            time.sleep(1.5)

    found: dict[str, dict] = {}
    pages = max(1, int(reader.get("catalog_pages") or 3))
    notches = int(reader.get("catalog_scroll_notches") or 5)
    panel_left = float(reader.get("panel_left") or 4250)
    mid_x = int(window[0] + panel_left + 300)
    mid_y = int((window[1] + window[3]) / 2)
    child = _post_target(hwnd, mid_x, mid_y)

    def harvest_screenfuls() -> None:
        """Whatever is on the panel now, and the next few screenfuls of it.

        Two empty screenfuls end it rather than one: a list scrolled faster
        than it renders shows nothing for a beat, and treating that as the
        bottom of the list once read seven listings out of a shop with
        hundreds."""
        quiet = 0
        for _ in range(pages):
            before = len(found)
            for row in _catalog_rows(warm_tree(hwnd), window, reader):
                key = row["title"].strip().lower()
                if key and key not in found:
                    found[key] = row
            quiet = 0 if len(found) > before else quiet + 1
            if quiet >= 2 or not child:
                break
            winapi.post_wheel(child, mid_x, mid_y, -notches)
            time.sleep(1.1)

    # what the panel offers on its own: this buyer's recent inquiries, which
    # are the listings most likely to be asked about again
    harvest_screenfuls()

    # and then the shop itself, through the one thing that reaches past the
    # shortlist -- a term or two per pass, carrying on from where the last pass
    # stopped, so this is never a minute of somebody's window scrolling itself
    terms = [str(t).strip() for t in (reader.get("catalog_terms") or []) if str(t).strip()]
    per = max(1, int(reader.get("catalog_terms_per_pass") or 1))
    done_all = True
    if search and terms:
        at = _sweep_at() % len(terms)
        for step in range(per):
            word = terms[(at + step) % len(terms)]
            if not _post_type(hwnd, search, word):
                break
            _post_key(hwnd, search, "enter")
            time.sleep(1.6)
            harvest_screenfuls()
        nxt = (at + per) % len(terms)
        _sweep_to(nxt)
        done_all = nxt == 0          # a full circuit of the terms is a full read
        # left as it was found, so the next person to look at the panel is not
        # looking at a search nobody typed
        _post_type(hwnd, search, "")
        _post_key(hwnd, search, "enter")
        time.sleep(0.8)

    # and the panel goes back to the tab it was on: somebody watching an order
    # should not find themselves looking at a product list
    if was_on and was_on != tab_name:
        back = next((n for n in warm_tree(hwnd)
                     if n["control_type"] == "TabItem"
                     and (n.get("name") or "").strip() == was_on), None)
        if back:
            _post_click_element(hwnd, back)

    return list(found.values()), ("" if done_all else "part")


def _catalog_rows(nodes: list[dict], window: tuple[int, int, int, int],
                  reader: dict) -> list[dict]:
    """One listing per Send button, and the lines that belong to it.

    The button is the anchor because it is the only thing in a row that is
    unambiguously one per row. Everything else -- title, price, SKU, stock --
    is read out of the band above it, in the order the panel draws them.
    """
    band = [float(reader.get("panel_left") or 4250), 99999.0]
    pitch = float(reader.get("product_row_height") or 252)
    send_name = str(reader.get("product_send") or "Send")
    sends = [n for n in nodes
             if n["control_type"] == "Button" and (n.get("name") or "").strip() == send_name
             and n.get("rect") and _in_band(n, band, window)]
    texts = [n for n in nodes
             if n["control_type"] == "Text" and n.get("rect") and _in_band(n, band, window)
             and (n.get("name") or "").strip()]
    out = []
    for send in sends:
        top = send["rect"][1] - pitch
        near = sorted((t for t in texts if top <= t["rect"][1] <= send["rect"][3]),
                      key=lambda t: (t["rect"][1], t["rect"][0]))
        lines = [t["name"].strip() for t in near if not _is_noise(t["name"])]
        title = next((l for l in lines if len(l) > 15), "")
        if not title:
            continue
        sku = next((l for l in lines if l.upper().startswith("SKU")), "")
        stock = next((l for l in lines if re.fullmatch(r"x\s*[\d,]+", l, re.IGNORECASE)), "")
        price_bits = [l for l in lines
                      if re.search(r"\d", l) and l is not title and l != sku and l != stock
                      and len(l) < 24]
        out.append({
            "title": title,
            "price": " ".join(price_bits[:3])[:60],
            "sku": sku.split("：", 1)[-1].split(":", 1)[-1].strip()[:80],
            "stock": stock[:20],
        })
    return out


def clear_answered_by_hand(cloud: "Cloud", hwnd: int, window: tuple[int, int, int, int],
                           reader: dict, cfg: dict, report: dict) -> None:
    """Take conversations off Pigu's list that were answered in DuoKe by hand.

    One conversation per look, the one waiting longest, because each one costs
    opening a thread. What decides is the last line in it: if it came from this
    side, the buyer has their answer and the row in Pigu is stale. The reply
    itself is not copied anywhere -- it was typed by a person into DuoKe, which
    is where it belongs; what matters is that the list stops asking for it.

    The lines are written back as history first, so somebody looking at that
    conversation in Pigu sees the reply that was typed rather than watching the
    row vanish with no explanation.
    """
    every = float(cfg.get("hand_check_seconds") or 60)
    if time.time() - _last_hand_check[0] < every:
        return
    try:
        chats = cloud.pending_chats()
    except CloudError as exc:
        report["notes"].append(f"by hand: {exc}")
        return
    if not chats:
        _last_hand_check[0] = time.time()
        return

    wait = float(cfg.get("hand_check_after") or 45)
    most = max(1, int(cfg.get("hand_check_tries") or 3))
    tried = 0
    now = datetime.now(timezone.utc)
    for chat in chats:
        try:
            when = datetime.fromisoformat(str(chat["newest"]).replace("Z", "+00:00"))
        except ValueError:
            continue
        if (now - when).total_seconds() < wait:
            continue                      # they may still be typing
        key = chat["chat_key"]
        tried += 1
        # A candidate that cannot be opened must not use up the look: the next
        # one along may well be answerable, and a row that can never be found
        # would otherwise block the check forever.
        if tried > most:
            break
        tree = warm_tree(hwnd)
        here = read_threads(tree, window, reader)
        target = next((t for t in here if _chat_key(t["name"]) == key), None)
        if target:
            if not _open_thread(target, hwnd, allow_click=True):
                continue
        elif not open_by_search(hwnd, window, reader, key):
            continue
        tree = warm_tree(hwnd)
        who = read_open_who(tree, window, reader, message_band(tree, window, reader) or [])
        if not who or _chat_key(who).lower() != key.lower():
            continue                      # opened something else; do not judge it
        lines = read_open_conversation(tree, window, reader, tail=HISTORY_TAIL)
        _last_hand_check[0] = time.time()
        if not lines or lines[-1]["inbound"]:
            return                        # still theirs; still waiting
        try:
            cloud.save_history(chat["ids"][-1],
                               [{"inbound": bool(l["inbound"]), "text": l["text"][:400]}
                                for l in lines])
            cloud.answered_by_hand(chat["ids"])
            report["by_hand"] = report.get("by_hand", 0) + len(chat["ids"])
        except CloudError as exc:
            report["notes"].append(f"by hand: {exc}")
        return

    _last_hand_check[0] = time.time()


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
                              "products": 0, "history": 0, "catalog": 0,
                              "by_hand": 0, "skipped": 0, "notes": []}
    cloud = Cloud(cfg.get("supabase_url", ""), cfg.get("supabase_anon_key", ""),
                  cfg.get("email", ""), cfg.get("password", ""))
    device = (platform.node() or "shop PC")[:60]

    hwnd = find_window()
    restored = False
    if hwnd:
        restored = _show_for_reading(hwnd)
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
    nodes = warm_tree(hwnd)
    named = sum(1 for n in nodes if (n.get("name") or "").strip())
    if named < TREE_COLD_NAMES:
        # The window is open and its elements are not there. That is one thing
        # and one thing only: Chromium has the renderer switched off for this
        # window, which happens when it has been minimised and not clicked
        # since. Reported as itself rather than as "no messages".
        note = ("DuoKe is open but showing nothing to read — click its window once. "
                "(It was minimised; Chromium does not rebuild a hidden window's "
                "elements until the app is clicked. Launch it with "
                "--force-renderer-accessibility to stop this happening.)")
        report["notes"].append(note)
        try:
            cloud.beat(device, True, 0, note)
        except CloudError:
            pass
        _state["last_report"] = report
        _state["last_pass_at"] = datetime.now(timezone.utc).isoformat()
        return report
    # Is this the chat page at all? A freshly started DuoKe comes up on its
    # dashboard, where there is no conversation, no reply box and no Product
    # tab -- and the left column is full of rows that read like a list of
    # people until you look at them. The reply box is the thing that only
    # exists on the chat page, so it is what decides.
    reply_box = _input_box(nodes, reader)
    reply_rect = reply_box.get("rect") if reply_box else None
    wide_enough = float(reader.get("min_input_width") or 700)
    on_chat_page = bool(reply_rect) and (reply_rect[2] - reply_rect[0]) >= wide_enough
    if not on_chat_page:
        note = ("DuoKe is not showing its chat list — click Chat in DuoKe once. "
                "(A window that has just started opens on the dashboard, where there "
                "is nothing to read.)")
        report["notes"].append(note)
        try:
            cloud.beat(device, True, 0, note)
        except CloudError:
            pass
        _state["last_report"] = report
        _state["last_pass_at"] = datetime.now(timezone.utc).isoformat()
        return report

    threads = read_threads(nodes, window, reader)
    report["threads"] = len(threads)

    # A filter or a search left on in DuoKe empties the conversation list, and
    # an empty list is indistinguishable from a quiet morning from here -- the
    # app says "No Result Found" where the conversations would be, so that is
    # what gets looked for. Reported rather than cleared: which filter somebody
    # is working in is their business.
    if not threads:
        band = message_band(nodes, window, reader)
        left = band[0] if band else 967
        blank = any((n.get("name") or "").strip().lower() in
                    ("no result found", "no results", "no data", "暂无数据")
                    for n in nodes
                    if n.get("rect") and (n["rect"][0] - window[0]) < left)
        if blank:
            note = ("DuoKe's conversation list is showing No Result Found — a filter or a "
                    "search is on in DuoKe, so nothing can be read until it is cleared.")
            report["notes"].append(note)
            try:
                cloud.beat(device, True, 0, note)
            except CloudError:
                pass

    seen = _seen()
    fresh: set[str] = set()

    def harvest(chat_name: str, tree: list[dict], shop: str = "") -> None:
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
            # The shop the buyer wrote to, read off their row in the list.
            # The configured store is only a fallback, for a PC that answers
            # for one shop and whose list therefore does not say.
            "store": shop or cfg.get("store") or "",
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

    # ---- what has been approved goes out FIRST
    #
    # Order is latency. Reading nineteen threads, then the catalogue, then
    # somebody's history request, and only then typing the reply somebody
    # approved a moment ago, is most of a minute of a person watching a
    # screen and wondering whether it worked. Nothing in the reading half
    # is more urgent than a reply that is already written.
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
            tree = warm_tree(hwnd)
            here = read_threads(tree, window, reader)
            target = next((t for t in here if _chat_key(t["name"]) == key), None)
            if target:
                if not _open_thread(target, hwnd, allow_click=True):
                    report["notes"].append(f"could not open {key[:30]} to reply")
                    continue
            elif not open_by_search(hwnd, window, reader, key):
                # searched and still nothing: the buyer has been renamed, or
                # the conversation is gone. Said plainly, because the reply is
                # still sitting there waiting.
                report["notes"].append(
                    f"{key[:30]} is not in the list and the search did not find them")
                continue
            tree = warm_tree(hwnd)
            # The thread was clicked; this is whether the click landed. Typing
            # into the wrong conversation is the one mistake here that reaches
            # a stranger, so it is checked rather than assumed -- and only when
            # the app actually offers a name to check against.
            band = message_band(tree, window, reader)
            who = read_open_who(tree, window, reader, band) if band else None
            if who and _chat_key(who).lower() not in key.lower() \
                    and key.lower() not in _chat_key(who).lower():
                report["notes"].append(
                    f"{key[:30]}: the open conversation looks like {who[:30]}, so nothing was typed")
                continue
            ok, how = type_reply(tree, window, reader, hwnd, text)
            if not ok:
                report["notes"].append(f"{key[:30]}: {how}")
                continue
            report["typed"] += 1
            # The words have gone. A photo that will not paste must not undo
            # that: the reply is marked typed either way, and the picture is
            # reported as the one thing still to do by hand.
            wanted_products = row.get("products") or []
            if cfg.get("send_products") and isinstance(wanted_products, list):
                for item in wanted_products[:2]:
                    query = str((item or {}).get("query") or "").strip() \
                        if isinstance(item, dict) else str(item or "").strip()
                    if not query:
                        continue
                    fresh = warm_tree(hwnd)
                    ok_p, why_p = send_product(fresh, window, reader, hwnd, query)
                    if ok_p:
                        report["products"] += 1
                    else:
                        report["notes"].append(f"{key[:30]} product: {why_p}")
            paths = row.get("photos") or []
            if cfg.get("send_photos") and not cfg.get("photos_may_take_screen") and paths:
                report["notes"].append(
                    f"{key[:30]}: the words went; the photo needs the screen for a second "
                    "and that is switched off, so paste it by hand")
            elif cfg.get("send_photos") and isinstance(paths, list):
                for path in [str(p) for p in paths if p][:3]:
                    try:
                        image = cloud.object_bytes(str(cfg.get("photo_bucket")), path)
                    except CloudError as exc:
                        report["notes"].append(f"{key[:30]}: {exc}")
                        continue
                    fresh = warm_tree(hwnd)
                    sent, why = paste_photo(fresh, window, reader, hwnd, image)
                    if sent:
                        report["photos"] += 1
                    else:
                        report["notes"].append(f"{key[:30]} photo: {why}")
            try:
                cloud.mark_typed(str(row.get("id")))
            except CloudError as exc:
                report["notes"].append(str(exc))


    # THE CONVERSATION THAT IS OPEN IS READ FIRST, every pass.
    #
    # This was left out at first because nothing could say whose it was. Then
    # read_open_who was built -- the name above the messages -- and leaving it
    # out became the reason the screen did not feel live: a buyer replying into
    # the conversation DuoKe currently has open never raises an unread badge,
    # because DuoKe considers a conversation on screen to be read. The agent
    # leaves a conversation open every time it types a reply, so exactly the
    # thread somebody is working is the one whose next message nothing would
    # notice. It cost no clicks to read and it is the whole difference between
    # a screen that updates in seconds and one that waits for the buyer to
    # write twice.
    #
    # The name is checked rather than assumed: an email address is this shop's
    # own account rather than a buyer, and a name that is not a conversation
    # gets nothing filed under it.
    band_now = message_band(nodes, window, reader)
    open_who = read_open_who(nodes, window, reader, band_now) if band_now else None
    if open_who and "@" not in open_who:
        key_open = _chat_key(open_who)
        shop_open = next((t.get("shop", "") for t in threads
                          if _chat_key(t["name"]).lower() == key_open.lower()), "")
        harvest(key_open, nodes, shop_open)

    # Reading the unread comes after sending, and this is the expensive half:
    # a click and a tree walk per thread. A buyer whose message arrives in Pigu
    # four seconds later has lost nothing; a seller watching a reply they have
    # already approved has.
    if cfg.get("open_unread"):
        for thread in [t for t in threads if t["unread"]][:MAX_THREADS_PER_PASS]:
            if not _open_thread(thread, hwnd, allow_click=True):
                report["notes"].append(f"could not open {thread['name'][:30]}")
                continue
            harvest(thread["name"], warm_tree(hwnd), thread.get("shop", ""))

    # ---- the shop's own listings, so the screen can search them
    #
    # After the messages, because a buyer waiting is worth more than a
    # catalogue being a few minutes stale, and before the replies, because a
    # reply may be sending one of these.
    quiet = not any(t["unread"] for t in threads) and not report["typed"]

    # A buyer answered in DuoKe by hand is still on Pigu's list until somebody
    # notices. This is the noticing, and it only happens on a quiet pass for
    # the same reason the catalogue read does: it costs a click in a window
    # somebody may be using.
    if quiet:
        clear_answered_by_hand(cloud, hwnd, window, reader, cfg, report)
    try:
        if not cfg.get("catalog_when_quiet") or quiet:
            if cloud.catalog_due(int(cfg.get("catalog_hours") or 12)):
                listings, part = read_catalog(warm_tree(hwnd), window, reader, hwnd)
                if listings:
                    report["catalog"] = cloud.save_catalog(
                        listings, str(cfg.get("store") or ""),
                        # A partial sweep must not stamp the read as done, or
                        # the next twelve hours pass without the other terms
                        # ever being searched.
                        finished=part != "part")
                elif part and part != "part":
                    report["notes"].append(f"catalogue: {part}")
    except CloudError as exc:
        report["notes"].append(f"catalogue: {exc}")

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
        tree = warm_tree(hwnd)
        here = read_threads(tree, window, reader)
        target = next((t for t in here if _chat_key(t["name"]) == key), None)
        if target:
            if not _open_thread(target, hwnd, allow_click=True):
                report["notes"].append(f"could not open {key[:30]} to read it")
                continue
        elif not open_by_search(hwnd, window, reader, key):
            report["notes"].append(f"{key[:30]} is not in the list and the search missed them")
            continue
        tree = warm_tree(hwnd)
        lines = read_open_conversation(tree, window, reader, tail=HISTORY_TAIL)
        try:
            cloud.save_history(str(row.get("id")),
                               [{"inbound": bool(l["inbound"]), "text": l["text"][:400]}
                                for l in lines])
            report["history"] += 1
        except CloudError as exc:
            report["notes"].append(str(exc))

    _remember(fresh)
    kept = _state.pop("clipboard_kept", "")
    if kept:
        report["notes"].append(
            "a photo was sent and what was on the clipboard before could not be put back"
            + (f" (it was {kept})" if kept != "unknown" else ""))
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
