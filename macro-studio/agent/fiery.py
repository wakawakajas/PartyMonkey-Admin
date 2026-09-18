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
import platform
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from agent import config, duoke
from agent.duoke import Cloud, CloudError

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
}

_lock = threading.RLock()
_cache: Optional[dict] = None
_cache_stamp: float = -1.0

# One job at a time: two uploads racing each other is two half-finished jobs.
_send_lock = threading.Lock()

MAX_PER_PASS = 20


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
              data: Optional[bytes] = None, content_type: str = "",
              timeout: int = 30) -> Any:
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            content_type = "application/json; charset=utf-8"
        req = urllib.request.Request(self.base + path, data=data, method=method)
        if content_type:
            req.add_header("Content-Type", content_type)
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

    def upload(self, name: str, pdf: bytes, preset_id: str) -> str:
        """The job's id on the Fiery. The upload's own answer is read first;
        where it does not say, the job is found as the one that was not there
        a moment ago."""
        before = self.job_ids()
        boundary = "----pigu" + uuid.uuid4().hex
        buf = io.BytesIO()

        def field(key: str, value: str) -> None:
            buf.write(f"--{boundary}\r\nContent-Disposition: form-data; "
                      f'name="{key}"\r\n\r\n{value}\r\n'.encode("utf-8"))

        if preset_id:
            field("preset", preset_id)
        safe = name.replace('"', "'")
        buf.write(f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; "
                  f'filename="{safe}"\r\nContent-Type: application/pdf\r\n\r\n'
                  .encode("utf-8"))
        buf.write(pdf)
        buf.write(f"\r\n--{boundary}--\r\n".encode("utf-8"))
        query = "?preset=" + urllib.parse.quote(preset_id) if preset_id else ""
        out = self._call("POST", "/jobs" + query, data=buf.getvalue(),
                         content_type=f"multipart/form-data; boundary={boundary}",
                         timeout=600)
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

    def set_copies(self, job_id: str, copies: int) -> None:
        self._call("PUT", "/jobs/" + urllib.parse.quote(job_id),
                   {"attributes": {"numcopies": str(int(copies))}})

    def print_job(self, job_id: str) -> None:
        """Print once the Fiery has finished taking the file in. A print asked
        for while it is still spooling is refused, so it is asked again."""
        last: Optional[Exception] = None
        for _ in range(12):
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
        cloud = Cloud(*_credentials())
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


def send_one(fiery: Fiery, row: dict) -> str:
    """One row to the Fiery. Returns the Fiery's job id."""
    path = _find_file(row)
    if path is None:
        raise FieryError(f"{row.get('file_name')} is not in {load().get('folder')}.")
    pdf = _only_pages(_as_pdf(path), str(row.get("pages") or ""))
    name = path.name if path.suffix.lower() == ".pdf" else path.stem + ".pdf"
    job_id = fiery.upload(name, pdf, str(row.get("preset_id") or ""))
    copies = max(1, int(row.get("copies") or 1))
    try:
        fiery.set_copies(job_id, copies)
    except FieryError as exc:
        # Never printed with the wrong count: the job stays held, and says so.
        raise FieryError(f"The job is on the Fiery, held -- its copies could not "
                         f"be set to {copies}: {exc}") from exc
    if row.get("action") == "print":
        fiery.print_job(job_id)
    return job_id


def sync_once() -> dict:
    """One pass: every queued job whose file is in the folder, sent."""
    global _presets_due
    cfg = load()
    report: dict[str, Any] = {"sent": 0, "failed": 0, "waiting": 0, "notes": []}
    cloud = Cloud(*_credentials())
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
        try:
            for row in todo:
                try:
                    job_id = send_one(fiery, row)
                    _finish(cloud, row["id"], True, job_id=job_id)
                    report["sent"] += 1
                    _state["sent_total"] += 1
                    _state["last_sent"] = str(row.get("file_name") or "")
                except Exception as exc:     # one bad file must not stop the rest
                    _finish(cloud, row["id"], False, str(exc))
                    report["failed"] += 1
                    report["notes"].append(f"{row.get('file_name')}: {exc}")
                    _state["last_error"] = str(exc)
        finally:
            fiery.logout()
    if report["sent"] and not report["failed"]:
        _state["last_error"] = ""
    _state["last_pass_at"] = _now()
    return report


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
        while not self._stop.is_set():
            cfg = load()
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
