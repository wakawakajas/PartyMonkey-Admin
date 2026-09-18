"""Magic Create's Download & Print: a PDF in the Working Folder, sent to the
Fiery with the server preset, copies and pages it was given.

The app saves the file into the Working Folder the way it always has, then
writes a row in `fiery_jobs` saying which file and what to do with it. It
cannot go further itself -- it is served over https, and a browser will not
let an https page talk to the Fiery on the LAN. This can: it sees the row,
reads the file out of the same folder, and hands it to the Fiery API.

What goes to the Fiery is the order Command WorkStation would be given by
hand: upload with the server preset applied, then the copies, then print --
or leave it held, for a job somebody wants to look at on the RIP first. A
page range is taken out of the PDF here rather than sent as a Fiery setting:
the job that arrives is then exactly the pages that print, whatever the
preset says about page ranges.

The presets go the other way. They are listed off the Fiery every few
minutes and written to `fiery_presets`, so the pop-up in the app offers what
Command WorkStation shows, under the same names, and nothing that has been
deleted since.

Settings live in fiery.json beside labels.json: open it, fill it in, save.
The Fiery API key is free from developer.fiery.com and is activated on the
Fiery itself; the user is a Fiery operator (not guest).
"""
from __future__ import annotations

import http.cookiejar
import io
import json
import os
import platform
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import hashlib
import uuid
import zlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from agent import config, duoke
from agent.duoke import Cloud, CloudError, shared_cloud

CONFIG_PATH = config.ROOT_DIR / "fiery.json"

DEFAULTS: dict[str, Any] = {
    # Off until somebody fills this in.
    "enabled": False,
    # The Fiery itself: its IP address or name as Command WorkStation shows
    # it, e.g. 192.168.1.50. https is assumed; the Fiery's own certificate is
    # self-signed, so it is not checked.
    "fiery_host": "",
    # A Fiery operator login (not guest), and the Fiery API key.
    "fiery_username": "operator",
    "fiery_password": "",
    "api_key": "",
    # Where the app saves Magic Create's PDFs, as this PC sees that folder.
    "folder": r"C:\NAS Folders\Partymonkey NAS\Working Folder",
    # Supabase -- leave all four blank to use duoke.json's.
    "supabase_url": "",
    "supabase_anon_key": "",
    "email": "",
    "password": "",
    # How often to look for jobs, and how often to re-read the presets.
    "poll_seconds": 4,
    "presets_minutes": 5,
    # A file the NAS has not delivered yet is waited for, up to this long.
    # A job older than this is failed rather than sent -- a PC switched on in
    # the afternoon must not print the morning at somebody.
    "stale_minutes": 30,
    # How long a job waits for the one before it in the same press to reach
    # the press, before it is sent anyway.
    "order_wait_minutes": 20,
}

_lock = threading.RLock()
_cache: Optional[dict] = None
_cache_stamp: float = -1.0

# One job at a time: two uploads racing each other is two half-finished jobs.
_send_lock = threading.Lock()

# a whole morning's press in one pass, so a run is not split across two
MAX_PER_PASS = 60


class FieryError(RuntimeError):
    """Something the person at the machine can act on."""


# ---------------------------------------------------------------- settings


def load() -> dict:
    """The settings as the file has them now -- it is edited by hand, so its
    timestamp decides whether the cache still stands (see labels.load)."""
    global _cache, _cache_stamp
    with _lock:
        try:
            stamp = CONFIG_PATH.stat().st_mtime
        except OSError:
            stamp = 0.0
        if _cache is not None and stamp == _cache_stamp:
            return _cache
        saved: dict = {}
        if CONFIG_PATH.exists():
            try:
                saved = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                saved = {}
        merged = dict(DEFAULTS)
        merged.update({k: v for k, v in saved.items() if k in DEFAULTS})
        _cache, _cache_stamp = merged, stamp
        return _cache


def save(patch: dict) -> dict:
    global _cache, _cache_stamp
    with _lock:
        data = dict(load())
        data.update({k: v for k, v in (patch or {}).items()
                     if k in DEFAULTS and v is not None})
        CONFIG_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")
        _cache = data
        try:
            _cache_stamp = CONFIG_PATH.stat().st_mtime
        except OSError:
            _cache_stamp = 0.0
        return data


def redacted() -> dict:
    data = dict(load())
    for key in ("password", "fiery_password"):
        data[key] = "*" * len(data.get(key) or "")
    for key in ("supabase_anon_key", "api_key"):
        data[key] = (data.get(key) or "")[:6] + "…" if data.get(key) else ""
    return data


def _credentials() -> tuple[str, str, str, str]:
    """Ours if they are filled in, DuoKe's if they are not."""
    cfg = load()
    mine = (cfg.get("supabase_url", ""), cfg.get("supabase_anon_key", ""),
            cfg.get("email", ""), cfg.get("password", ""))
    if all(mine):
        return mine
    d = duoke.load()
    return (cfg.get("supabase_url") or d.get("supabase_url", ""),
            cfg.get("supabase_anon_key") or d.get("supabase_anon_key", ""),
            cfg.get("email") or d.get("email", ""),
            cfg.get("password") or d.get("password", ""))


# ---------------------------------------------------------------- the Fiery


def _items(obj: Any) -> list[dict]:
    """The list of things in a Fiery answer, wherever it has been put.

    v5 wraps lists as {"data": {"items": [...]}}, older answers are the bare
    list; both are accepted rather than one being bet on."""
    if isinstance(obj, list):
        return [x for x in obj if isinstance(x, dict)]
    if isinstance(obj, dict):
        for key in ("items", "data", "presets", "jobs"):
            if key in obj:
                found = _items(obj[key])
                if found:
                    return found
    return []


def _first(d: dict, *keys: str) -> str:
    for key in keys:
        value = d.get(key)
        if value not in (None, ""):
            return str(value)
    return ""


class Fiery:
    """A signed-in session with one Fiery. Used for one pass and logged out,
    so a Fiery rebooted in between is never talked to with a dead cookie."""

    def __init__(self, cfg: dict):
        host = (cfg.get("fiery_host") or "").strip().rstrip("/")
        if not host:
            raise FieryError("No Fiery address in fiery.json (fiery_host).")
        if not host.startswith(("http://", "https://")):
            host = "https://" + host
        self.base = host + "/live/api/v5"
        self.cfg = cfg
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()),
            urllib.request.HTTPSHandler(context=context))

    def _call(self, method: str, path: str, body: Any = None,
              data: Any = None, content_type: str = "",
              timeout: int = 30, length: int = 0) -> Any:
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            content_type = "application/json; charset=utf-8"
        req = urllib.request.Request(self.base + path, data=data, method=method)
        if content_type:
            req.add_header("Content-Type", content_type)
        if length:
            # a body handed over in pieces has to say how long it is up front
            req.add_header("Content-Length", str(length))
        req.add_header("Accept", "application/json")
        try:
            with self.opener.open(req, timeout=timeout) as res:
                raw = res.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", "replace")[:300]
            except Exception:
                pass
            if exc.code in (401, 403) and path == "/login":
                # the Fiery says which half it did not like; the two are fixed
                # in different places, so the message names the right one
                if "accessrights" in detail:
                    raise FieryError("The Fiery turned the API key down -- check "
                                     "api_key in fiery.json.") from exc
                raise FieryError("The Fiery turned the user or password down -- "
                                 "check fiery_username and fiery_password in "
                                 "fiery.json.") from exc
            raise FieryError(f"Fiery {method} {path} -> {exc.code} {detail}".strip()) from exc
        except urllib.error.URLError as exc:
            raise FieryError(f"Cannot reach the Fiery at {self.base}: {exc.reason}") from exc
        except TimeoutError as exc:
            raise FieryError(f"The Fiery did not answer {method} {path} in time.") from exc
        if not raw:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return raw

    def login(self) -> None:
        cfg = self.cfg
        if not cfg.get("api_key"):
            raise FieryError("No Fiery API key in fiery.json (api_key). A free one "
                             "comes from developer.fiery.com.")
        # A blank password is not a mistake: a Fiery whose admin account was
        # never given one logs in with none.
        out = self._call("POST", "/login", {
            "username": cfg.get("fiery_username") or "operator",
            "password": cfg.get("fiery_password"),
            "apikey": cfg.get("api_key"),
        })
        if isinstance(out, dict) and out.get("authenticated") is False:
            raise FieryError("The Fiery turned the login down -- check the user "
                             "and password in fiery.json.")
        if isinstance(out, dict) and str(out.get("accessrights", "")).lower() in ("no", "false"):
            raise FieryError("The Fiery accepted the login but not the API key -- "
                             "check api_key in fiery.json, and that its licence "
                             "is activated on the Fiery.")

    def logout(self) -> None:
        try:
            self._call("POST", "/logout", timeout=10)
        except FieryError:
            pass

    def presets(self) -> list[dict]:
        """[{id, name}], as Command WorkStation lists them."""
        out: list[dict] = []
        raw_items = _items(self._call("GET", "/presets"))
        _state["preset_keys"] = sorted(raw_items[0].keys())[:40] if raw_items else []
        for item in raw_items:
            pid = _first(item, "id", "uuid", "presetid", "preset id")
            name = _first(item, "name", "title", "presetname", "preset name",
                          "displayname", "display name") or pid
            if pid:
                out.append({"id": pid, "name": name})
        return out

    def job_ids(self) -> set[str]:
        try:
            return {_first(j, "id", "uuid") for j in _items(self._call(
                "GET", "/jobs?key[]=id&key[]=title"))} - {""}
        except FieryError:
            return set()

    def upload(self, name: str, pdf: Any, preset_id: str) -> str:
        """The job's id on the Fiery. The upload's own answer is read first;
        where it does not say, the job is found as the one that was not there
        a moment ago.

        `pdf` is the file's bytes, or the Path of a file to send as it is. A
        Path is read off the disk a piece at a time as it goes out rather than
        held whole in memory first -- a gift wrapper is 500 MB, and reading all
        of it before sending a byte was most of the wait."""
        before = self.job_ids()
        boundary = "----pigu" + uuid.uuid4().hex
        head = b""
        if preset_id:
            head += (f"--{boundary}\r\nContent-Disposition: form-data; "
                     f'name="preset"\r\n\r\n{preset_id}\r\n').encode("utf-8")
        safe = name.replace('"', "'")
        head += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; "
                 f'filename="{safe}"\r\nContent-Type: application/pdf\r\n\r\n').encode("utf-8")
        tail = f"\r\n--{boundary}--\r\n".encode("utf-8")
        if isinstance(pdf, Path):
            size = pdf.stat().st_size

            def pieces():
                yield head
                with open(pdf, "rb") as fh:
                    while True:
                        chunk = fh.read(1 << 20)
                        if not chunk:
                            break
                        yield chunk
                yield tail
            data: Any = pieces()
            length = len(head) + size + len(tail)
        else:
            data = head + pdf + tail
            length = 0
        # long enough for the biggest file over a slow link, a minute a 100 MB
        timeout = max(600, int(length / (100 << 20) * 60))
        query = "?preset=" + urllib.parse.quote(preset_id) if preset_id else ""
        out = self._call("POST", "/jobs" + query, data=data,
                         content_type=f"multipart/form-data; boundary={boundary}",
                         timeout=timeout, length=length)
        job_id = ""
        if isinstance(out, str):
            job_id = out.strip().strip('"')
        elif isinstance(out, dict):
            data = out.get("data") if isinstance(out.get("data"), dict) else out
            item = data.get("item") if isinstance(data.get("item"), dict) else data
            job_id = _first(item, "id", "uuid", "jobid", "job id") if isinstance(item, dict) else ""
            if not job_id and isinstance(data.get("item"), str):
                job_id = data["item"]
        if job_id:
            return job_id
        for _ in range(10):
            fresh = self.job_ids() - before
            if len(fresh) == 1:
                return fresh.pop()
            time.sleep(2)
        raise FieryError("The Fiery took the file but did not say which job it "
                         "became -- it is in Command WorkStation, with its copies "
                         "not set.")

    def rip(self, job_id: str) -> None:
        """Start the Fiery processing a held job now, so it is ready to go the
        moment it is told to print rather than only starting then."""
        self._call("PUT", "/jobs/" + urllib.parse.quote(job_id) + "/rip")

    def started(self, job_id: str) -> bool:
        """Whether a job has got as far as the press -- printing, printed, or
        finished with some other way (cancelled, gone). Until it has, a job
        sent to print after it can overtake it: the Fiery prints whichever
        job is ready first, and a 2 MB file is ready long before a 500 MB one."""
        try:
            out = self._call("GET", "/jobs/" + urllib.parse.quote(job_id))
        except FieryError as exc:
            return "404" in str(exc)            # gone: nothing left to wait for
        job = out
        if isinstance(out, dict) and isinstance(out.get("data"), dict):
            job = out["data"].get("item") if isinstance(out["data"].get("item"), dict) else out["data"]
        if not isinstance(job, dict):
            return True
        status = str(job.get("status") or "").lower()
        state = str(job.get("state") or "").lower()
        if "print" in status or state in ("completed", "canceled", "cancelled", "error",
                                          "printed", "done printing"):
            return True
        stamp = str(job.get("timestamp dedicated to print") or "").strip()
        return bool(stamp) and stamp not in ("0", "0:0")

    def wait_started(self, job_id: str, minutes: int) -> bool:
        deadline = time.time() + 60 * max(1, minutes)
        while time.time() < deadline:
            if self.started(job_id):
                return True
            time.sleep(5)
        return False

    def set_copies(self, job_id: str, copies: int) -> None:
        self._call("PUT", "/jobs/" + urllib.parse.quote(job_id),
                   {"attributes": {"numcopies": str(int(copies))}})

    def print_job(self, job_id: str, size: int = 0) -> None:
        """Print once the Fiery has finished taking the file in. A print asked
        for while it is still spooling is refused, so it is asked again -- for
        three minutes, and longer for a big file, which the Fiery takes longer
        to take in and process: a minute more for every 100 MB."""
        last: Optional[Exception] = None
        tries = 36 + int(size / (100 << 20) * 12)
        for _ in range(tries):
            try:
                out = self._call("PUT", "/jobs/" + urllib.parse.quote(job_id) + "/print")
                if out is False or (isinstance(out, dict) and out.get("error")):
                    raise FieryError(f"print refused: {str(out)[:200]}")
                return
            except FieryError as exc:
                last = exc
                time.sleep(5)
        raise FieryError(f"The job is on the Fiery, held -- it would not print: {last}")


# ---------------------------------------------------------------- the PDF


def _page_list(spec: str, count: int) -> list[int]:
    """'1-3,5' -> [0, 1, 2, 4]. Blank is every page."""
    spec = (spec or "").replace(" ", "")
    if not spec or spec.lower() == "all":
        return list(range(count))
    pages: list[int] = []
    for part in spec.split(","):
        if not part:
            continue
        if "-" in part:
            a, _, b = part.partition("-")
            lo = int(a) if a else 1
            hi = int(b) if b else count
        else:
            lo = hi = int(part)
        if lo < 1 or hi > count or lo > hi:
            raise FieryError(f"Pages {spec} -- the file has {count} page"
                             f"{'' if count == 1 else 's'}.")
        pages.extend(range(lo - 1, hi))
    if not pages:
        raise FieryError(f"Pages {spec} names no page.")
    return pages


def _only_pages(pdf: bytes, spec: str) -> bytes:
    if not (spec or "").strip() or spec.strip().lower() == "all":
        return pdf
    from pypdf import PdfReader, PdfWriter
    reader = PdfReader(io.BytesIO(pdf))
    pages = _page_list(spec, len(reader.pages))
    if pages == list(range(len(reader.pages))):
        return pdf
    writer = PdfWriter()
    for i in pages:
        writer.add_page(reader.pages[i])
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


def _plain(name: str) -> bool:
    return bool(name) and "/" not in name and "\\" not in name and name not in (".", "..")


# ---------------------------------------------------------------- slimming
# A PDF saved from Illustrator with "Preserve Illustrator Editing
# Capabilities" carries the whole .ai file inside it, as thousands of
# AIPDFPrivateData blocks hung off the page's /PieceInfo. The press never
# reads a byte of it -- it is Illustrator's, for opening the file again -- and
# it is most of the file: a 436 MB gift wrapper is 54 MB of picture and 380 MB
# of that. The picture itself is stored uncompressed, so it is squeezed too,
# losslessly: the pixels that arrive are the pixels that left.
#
# What goes to the Fiery is a slim copy, made here and kept, so a design
# printed every morning is slimmed once. The file in the folder is never
# touched. Anything about the copy that does not check out -- a page's
# content not byte-for-byte the same, a different page count, a picture that
# decodes differently -- and the original is sent instead.

CACHE_DIR = config.ROOT_DIR / "fiery-cache"
CACHE_DAYS = 14

# pypdf refuses streams over 75 MB by default, as a guard against hostile
# files. These are the shop's own artwork, and a 160 MB picture is ordinary.
try:
    import pypdf.filters as _pdf_filters
    for _name in dir(_pdf_filters):
        if (_name.startswith(("MAX_", "ZLIB_MAX", "LZW_MAX", "RUN_LENGTH_MAX"))
                or _name == "FLATE_MAX_BUFFER_SIZE") and isinstance(getattr(_pdf_filters, _name), int):
            setattr(_pdf_filters, _name, 4 << 30)
except Exception:
    pass


def _slim(path: Path) -> Optional[Path]:
    """A slim copy of `path` to send in its place, or None to send it as is."""
    try:
        st = path.stat()
        key = hashlib.sha1(f"{path.name}|{st.st_size}|{st.st_mtime_ns}".encode()).hexdigest()[:20]
        CACHE_DIR.mkdir(exist_ok=True)
        done = CACHE_DIR / f"{key}.pdf"
        if done.exists():
            return done
        skip = CACHE_DIR / f"{key}.as-is"
        if skip.exists():
            return None
        if st.st_size < (20 << 20):            # small already: not worth the look
            return None
        from pypdf import PdfReader, PdfWriter
        from pypdf.generic import NameObject
        reader = PdfReader(str(path))
        writer = PdfWriter()
        for page in reader.pages:
            if "/PieceInfo" in page:
                del page[NameObject("/PieceInfo")]
            writer.add_page(page)
        root = reader.trailer["/Root"]
        for k in ("/OutputIntents", "/OCProperties"):    # colour and layers, as they were
            if k in root:
                writer._root_object[NameObject(k)] = root[k].clone(writer)
        pictures: list[tuple[str, int, str]] = []
        seen: set[int] = set()
        for n, page in enumerate(writer.pages):
            res = page.get("/Resources")
            xo = res.get_object().get("/XObject") if res else None
            if not xo:
                continue
            for name, ref in xo.get_object().items():
                img = ref.get_object()
                if img.get("/Subtype") != "/Image" or "/Filter" in img or id(img) in seen:
                    continue
                seen.add(id(img))
                pictures.append((str(name), n, hashlib.md5(img._data).hexdigest()))
                img._data = zlib.compress(img._data, 6)
                img[NameObject("/Filter")] = NameObject("/FlateDecode")
        out = io.BytesIO()
        writer.write(out)
        data = out.getvalue()
        # the checks: same pages, same drawing, same pixels
        check = PdfReader(io.BytesIO(data))
        if len(check.pages) != len(reader.pages):
            raise ValueError("page count changed")
        for a, b in zip(reader.pages, check.pages):
            if a.get_contents() is not None and a.get_contents().get_data() != b.get_contents().get_data():
                raise ValueError("page content changed")
        for name, n, digest in pictures:
            img = check.pages[n]["/Resources"]["/XObject"][name].get_object()
            if hashlib.md5(img.get_data()).hexdigest() != digest:
                raise ValueError("picture changed")
        if len(data) > st.st_size * 0.8:      # little to gain: send the original
            skip.write_text("")
            return None
        tmp = done.with_suffix(".part")
        tmp.write_bytes(data)
        tmp.replace(done)
        _state["slimmed"] = f"{path.name}: {st.st_size >> 20} MB -> {max(1, len(data) >> 20)} MB"
        return done
    except Exception as exc:
        _state["last_slim_error"] = f"{path.name}: {type(exc).__name__}: {exc}"[:300]
        return None


def _slim_tidy() -> None:
    """Slim copies not used for a fortnight are thrown away."""
    try:
        cutoff = time.time() - CACHE_DAYS * 86400
        for f in CACHE_DIR.glob("*"):
            if f.stat().st_mtime < cutoff:
                f.unlink(missing_ok=True)
    except Exception:
        pass


def _find_file(row: dict) -> Optional[Path]:
    """The row's file: in the folder itself, or in the folder inside it that
    the app wrote it to -- Copy File puts its files in Pigu Today PRINT, which
    sits inside the Working Folder."""
    folder = Path(load().get("folder") or "")
    name = str(row.get("file_name") or "")
    if not _plain(name):
        return None
    where = [folder / name]
    sub = str(row.get("folder") or "")
    if _plain(sub) and sub != folder.name:
        where.append(folder / sub / name)
    for path in where:
        if path.is_file():
            return path
    return None


_IMAGES = {".png", ".jpg", ".jpeg", ".tif", ".tiff"}


def _as_pdf(path: Path) -> bytes:
    """A picture made into a one-page PDF at its own resolution, so it prints
    the size it was drawn. The Fiery takes PDFs; a copied PNG is not one."""
    if path.suffix.lower() not in _IMAGES:
        return path.read_bytes()
    from PIL import Image
    with Image.open(path) as img:
        dpi = img.info.get("dpi") or (300, 300)
        res = float(dpi[0] or 300)
        page = img.convert("RGB") if img.mode not in ("RGB", "L", "CMYK") else img.copy()
    out = io.BytesIO()
    page.save(out, "PDF", resolution=res)
    return out.getvalue()


# ---------------------------------------------------------------- the loop


_state: dict[str, Any] = {
    "running": False,
    "last_pass_at": None,
    "last_error": "",
    "sent_total": 0,
    "last_sent": "",
    "presets": 0,
    "presets_at": None,
    "preset_keys": [],
}
_presets_due = 0.0
# the last job each run sent to print, so the next one waits for it
_run_last: dict[str, str] = {}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _age_minutes(row: dict) -> float:
    try:
        made = datetime.fromisoformat(str(row.get("created_at")).replace("Z", "+00:00"))
    except ValueError:
        return 0.0
    return (datetime.now(timezone.utc) - made).total_seconds() / 60


def _finish(cloud: Cloud, row_id: str, ok: bool, note: str = "", job_id: str = "") -> None:
    cloud.rest("PATCH", f"/fiery_jobs?id=eq.{row_id}",
               {"status": "sent" if ok else "failed", "sent_at": _now(),
                "error": note[:300], "fiery_job_id": job_id},
               prefer="return=minimal")


def sync_presets(cloud: Optional[Cloud] = None) -> dict:
    """The Fiery's presets, written to fiery_presets; ones gone from the
    Fiery are taken out."""
    global _presets_due
    cfg = load()
    if cloud is None:
        cloud = shared_cloud(*_credentials())
    fiery = Fiery(cfg)
    fiery.login()
    try:
        presets = fiery.presets()
    finally:
        fiery.logout()
    stamp = _now()
    if presets:
        cloud.rest("POST", "/fiery_presets?on_conflict=id",
                   [{"id": p["id"], "name": p["name"], "synced_at": stamp} for p in presets],
                   prefer="resolution=merge-duplicates,return=minimal")
    keep = {p["id"] for p in presets}
    have = cloud.rest("GET", "/fiery_presets?select=id") or []
    for row in have:
        if row.get("id") not in keep:
            cloud.rest("DELETE", "/fiery_presets?id=eq." + urllib.parse.quote(str(row["id"])),
                       prefer="return=minimal")
    _state["presets"] = len(presets)
    _state["presets_at"] = stamp
    _presets_due = time.time() + 60 * max(1, int(cfg.get("presets_minutes") or 5))
    return {"presets": presets}


def upload_one(fiery: Fiery, row: dict) -> tuple[str, int]:
    """One row's file onto the Fiery, held, preset and copies set. Returns
    the Fiery's job id and how big the file was."""
    path = _find_file(row)
    if path is None:
        raise FieryError(f"{row.get('file_name')} is not in {load().get('folder')}.")
    pages = str(row.get("pages") or "").strip()
    name = path.name if path.suffix.lower() == ".pdf" else path.stem + ".pdf"
    # the same file with Illustrator's editing data left out, where it has any
    send = (_slim(path) if path.suffix.lower() == ".pdf" else None) or path
    if send != path:
        try:
            os.utime(send)              # used today: kept another fortnight
        except OSError:
            pass
    if path.suffix.lower() == ".pdf" and (not pages or pages.lower() == "all"):
        pdf: Any = send                 # sent as it is, straight off the disk
        size = send.stat().st_size
    else:
        pdf = _only_pages(_as_pdf(send), pages)
        size = len(pdf)
    job_id = fiery.upload(name, pdf, str(row.get("preset_id") or ""))
    copies = max(1, int(row.get("copies") or 1))
    try:
        fiery.set_copies(job_id, copies)
    except FieryError as exc:
        # Never printed with the wrong count: the job stays held, and says so.
        raise FieryError(f"The job is on the Fiery, held -- its copies could not "
                         f"be set to {copies}: {exc}") from exc
    return job_id, size


def print_in_turn(fiery: Fiery, row: dict, job_id: str, size: int) -> None:
    """Print one job of a run once the job before it is at the press."""
    run = str(row.get("run_id") or "")
    before = _run_last.get(run) if run else None
    if before and not fiery.wait_started(before, int(load().get("order_wait_minutes") or 20)):
        _state["last_error"] = (f"{row.get('file_name')} was sent to print without "
                                "waiting any longer for the job before it")
    fiery.print_job(job_id, size)
    if run:
        _run_last[run] = job_id


def send_one(fiery: Fiery, row: dict) -> str:
    """One row to the Fiery. Returns the Fiery's job id."""
    job_id, size = upload_one(fiery, row)
    if row.get("action") == "print":
        print_in_turn(fiery, row, job_id, size)
    return job_id


def sync_once() -> dict:
    """One pass: every queued job whose file is in the folder, sent."""
    global _presets_due
    cfg = load()
    if time.time() >= _presets_due:
        _slim_tidy()
    report: dict[str, Any] = {"sent": 0, "failed": 0, "waiting": 0, "notes": []}
    cloud = shared_cloud(*_credentials())
    if time.time() >= _presets_due:
        try:
            sync_presets(cloud)
        except (FieryError, CloudError) as exc:
            # a Fiery that is switched off is asked again in a minute, not
            # logged in to every few seconds
            _presets_due = time.time() + 60
            report["notes"].append(f"presets: {exc}")
            _state["last_error"] = str(exc)

    base = "/fiery_jobs?select=*&status=eq.queued&limit=" + str(MAX_PER_PASS)
    try:
        rows = cloud.rest("GET", base + "&order=created_at.asc,seq.asc") or []
    except CloudError as exc:
        # supabase-migration-FIERY-COPY.sql not run yet: no seq to sort by
        if "seq" not in str(exc):
            raise
        rows = cloud.rest("GET", base + "&order=created_at.asc") or []
    stale = int(cfg.get("stale_minutes") or 30)
    device = (platform.node() or "shop PC")[:60]
    todo: list[dict] = []
    # A run whose next file has not arrived yet waits as a whole: sending the
    # ones after it would print the batch out of order.
    held: set[str] = set()
    for row in rows:
        run = str(row.get("run_id") or "")
        if run and run in held:
            report["waiting"] += 1
            continue
        if _age_minutes(row) > stale:
            _finish(cloud, row["id"], False,
                    "too old to send -- queued while Macro Studio was not running"
                    if _find_file(row)
                    else f"{row.get('file_name')} never arrived in {cfg.get('folder')}")
            report["failed"] += 1
            continue
        if _find_file(row) is None:
            report["waiting"] += 1          # the NAS has not delivered it yet
            if run:
                held.add(run)
            continue
        claimed = cloud.rest("PATCH", f"/fiery_jobs?id=eq.{row['id']}&status=eq.queued",
                             {"status": "sending", "device": device, "claimed_at": _now()},
                             prefer="return=representation")
        if claimed:
            todo.append(row)
    if not todo:
        _state["last_pass_at"] = _now()
        return report

    with _send_lock:
        fiery = Fiery(cfg)
        try:
            fiery.login()
        except FieryError as exc:
            for row in todo:        # back in the queue: the Fiery may just be asleep
                cloud.rest("PATCH", f"/fiery_jobs?id=eq.{row['id']}",
                           {"status": "queued", "error": str(exc)[:300]},
                           prefer="return=minimal")
            _state["last_error"] = str(exc)
            report["notes"].append(str(exc))
            _state["last_pass_at"] = _now()
            return report
        def sent(row: dict, job_id: str) -> None:
            _finish(cloud, row["id"], True, job_id=job_id)
            report["sent"] += 1
            _state["sent_total"] += 1
            _state["last_sent"] = str(row.get("file_name") or "")

        def failed(row: dict, exc: Exception, job_id: str = "") -> None:
            _finish(cloud, row["id"], False, str(exc), job_id=job_id)
            report["failed"] += 1
            report["notes"].append(f"{row.get('file_name')}: {exc}")
            _state["last_error"] = str(exc)

        # A press to print is taken as one: every file of it goes onto the
        # Fiery first, held, and is set processing -- then they are printed in
        # the batch's order. The press is not kept idle waiting on the next
        # upload, and a small file cannot print ahead of a big one before it.
        groups: list[list[dict]] = []
        runs: dict[str, list[dict]] = {}
        for row in todo:
            run = str(row.get("run_id") or "")
            if run and row.get("action") == "print":
                if run not in runs:
                    runs[run] = []
                    groups.append(runs[run])
                runs[run].append(row)
            else:
                groups.append([row])
        try:
            for group in groups:
                first = group[0]
                if not (first.get("run_id") and first.get("action") == "print"):
                    try:
                        sent(first, send_one(fiery, first))
                    except Exception as exc:     # one bad file must not stop the rest
                        failed(first, exc)
                    continue
                ready: list[tuple[dict, str, int]] = []
                for row in group:
                    try:
                        job_id, size = upload_one(fiery, row)
                        cloud.rest("PATCH", f"/fiery_jobs?id=eq.{row['id']}",
                                   {"fiery_job_id": job_id}, prefer="return=minimal")
                        ready.append((row, job_id, size))
                    except Exception as exc:
                        failed(row, exc)
                for _, job_id, _ in ready:
                    try:
                        fiery.rip(job_id)
                    except FieryError:
                        pass                 # it is processed when printed instead
                for row, job_id, size in ready:
                    try:
                        print_in_turn(fiery, row, job_id, size)
                        sent(row, job_id)
                    except Exception as exc:
                        failed(row, exc, job_id)
        finally:
            fiery.logout()
    if report["sent"] and not report["failed"]:
        _state["last_error"] = ""
    _state["last_pass_at"] = _now()
    return report


def recover() -> int:
    """Jobs this PC had claimed when it was last shut: they are not coming.
    Failed rather than queued again -- one of them may already be on the
    Fiery, and sending it twice prints it twice -- with the error saying
    where to look."""
    cloud = shared_cloud(*_credentials())
    device = (platform.node() or "shop PC")[:60]
    rows = cloud.rest("GET", "/fiery_jobs?select=id&status=eq.sending&device=eq."
                      + urllib.parse.quote(device)) or []
    for row in rows:
        _finish(cloud, row["id"], False,
                "Macro Studio was closed while sending this -- check Command "
                "WorkStation before sending it again")
    return len(rows)


def test() -> dict:
    """Log in and list the presets -- the button to press while setting up."""
    fiery = Fiery(load())
    fiery.login()
    try:
        presets = fiery.presets()
    finally:
        fiery.logout()
    return {"ok": True, "presets": presets, "preset_keys": _state["preset_keys"]}


def status() -> dict:
    cfg = load()
    url, anon, email, password = _credentials()
    folder = Path(cfg.get("folder") or "")
    return {
        "configured": bool(cfg.get("fiery_host") and cfg.get("api_key")),
        "cloud_configured": bool(url and anon and email and password),
        "enabled": bool(cfg.get("enabled")),
        "watching": bool(_state["running"]),
        "fiery_host": cfg.get("fiery_host") or "",
        "folder": str(folder),
        "folder_found": folder.is_dir(),
        "presets": _state["presets"],
        "presets_at": _state["presets_at"],
        "preset_keys": _state["preset_keys"],
        "sent_total": _state["sent_total"],
        "last_sent": _state["last_sent"],
        "last_pass_at": _state["last_pass_at"],
        "last_error": _state["last_error"],
        "slimmed": _state.get("slimmed", ""),
        "last_slim_error": _state.get("last_slim_error", ""),
        "config_file": str(CONFIG_PATH),
    }


class Watcher:
    """sync_once on a timer for as long as the agent is up (see labels)."""

    def __init__(self) -> None:
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> bool:
        if self._thread and self._thread.is_alive():
            return False
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="fiery-watcher", daemon=True)
        self._thread.start()
        _state["running"] = True
        return True

    def stop(self) -> None:
        self._stop.set()
        _state["running"] = False

    def _run(self) -> None:
        self._stop.wait(4)
        recovered = False
        while not self._stop.is_set():
            cfg = load()
            if cfg.get("enabled") and not recovered:
                try:
                    recover()
                    recovered = True
                except Exception as exc:
                    _state["last_error"] = f"{type(exc).__name__}: {exc}"
            if cfg.get("enabled"):
                try:
                    sync_once()
                except Exception as exc:        # a pass must never kill the loop
                    _state["last_error"] = f"{type(exc).__name__}: {exc}"
                    if isinstance(exc, CloudError):
                        self._stop.wait(10)
            self._stop.wait(max(2, int(cfg.get("poll_seconds") or 4)))
        _state["running"] = False


watcher = Watcher()
