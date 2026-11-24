"""Pick-up shipping labels, straight out of BigSeller.

Every morning and afternoon the pick-up orders have to be printed. BigSeller
can do it, but only one logistics channel at a time -- ask it for a mixed
batch and it answers "If your orders belong to multiple logistics, we
recommend Auto High-Speed Printing", which needs its printing plugin. So the
button here asks for one PDF per courier and joins them up afterwards:

    Y:\\11 November 2026\\24 November 2026 Morning - Next Day Delivery.pdf
    Y:\\11 November 2026\\24 November 2026 Morning - SPX.pdf

Morning before noon, Afternoon from noon. Press it twice in the same half of
the day and both files get the same number -- "(1)", then "(2)" -- so nothing
on the drive is ever written over.

Three calls do the work, all of them BigSeller's own web API:

    POST /api/v1/order/new/pageList.json        the To Pick Up orders
    POST /api/v1/print/print/selfLabel/many.json    start a batch
    GET  /api/v1/print/print/getOrderPrintProgress.json   ...until fileUrl

BigSeller builds a label in about a quarter of a second and that is nearly
the whole runtime, so each courier is cut into chunks and eight are built at
once: 86 labels in about seven seconds instead of thirty.

Signing in is the one thing this cannot do for itself. BigSeller's session
cookie is HttpOnly, so it has to be copied out of Chrome's devtools once and
pasted into the panel. Whatever BigSeller hands back after that is saved, so
using the button keeps the session alive; when it does lapse the panel says
so rather than quietly downloading nothing.
"""
from __future__ import annotations

import csv
import datetime as dt
import json
import os
import random
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Optional

from agent import config

CONFIG_PATH = config.ROOT_DIR / "bigseller-labels.json"

BASE = "https://www.bigseller.com"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/158.0.0.0 Safari/537.36"
)

# An order joins the first group whose `match` appears in its BigSeller
# carrier name. The last group's empty list makes it the catch-all.
DEFAULT_GROUPS = [
    {"name": "Next Day Delivery", "match": ["doorstep delivery", "next day"]},
    {"name": "SPX", "match": []},
]

DEFAULTS: dict[str, Any] = {
    "cookie": "",
    # The month folder is made inside this one, named like the ones already
    # there: "11 November 2026".
    "out_dir": "Y:\\",
    "groups": DEFAULT_GROUPS,
    # BigSeller's own "General Printing" tops out at 300 a batch; small
    # chunks are what lets them build side by side.
    "chunk_size": 15,
    "workers": 8,
    # Off, so the drive stays PDFs only.
    "write_csv": False,
}

_lock = threading.Lock()
_state: dict[str, Any] = {"running": False, "last_error": "", "last_run_at": "", "last_report": None}


class LabelError(Exception):
    """Something BigSeller said, worth showing the person who pressed."""


class NotLoggedIn(LabelError):
    pass


# ---------------------------------------------------------------- settings
def load() -> dict:
    saved: dict = {}
    if CONFIG_PATH.exists():
        try:
            saved = json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig"))
        except (json.JSONDecodeError, OSError):
            saved = {}
    merged = dict(DEFAULTS)
    merged.update({k: v for k, v in saved.items() if k in DEFAULTS})
    return merged


def save(patch: dict) -> dict:
    with _lock:
        current = load()
        current.update({k: v for k, v in patch.items() if k in DEFAULTS})
        CONFIG_PATH.write_text(json.dumps(current, indent=2), encoding="utf-8")
    return current


def redacted() -> dict:
    """The settings as the panel may see them -- never the cookie itself."""
    cfg = load()
    cfg["cookie"] = "saved" if cfg.get("cookie") else ""
    return cfg


def status() -> dict:
    cfg = load()
    with _lock:
        st = dict(_state)
    st.update({
        "has_cookie": bool(cfg.get("cookie")),
        "out_dir": cfg.get("out_dir", ""),
        "next_name": slot_name(),
        "groups": [g.get("name", "") for g in cfg.get("groups", [])],
    })
    return st


# ---------------------------------------------------------------- naming
def slot_name(now: Optional[dt.datetime] = None) -> str:
    """'24 November 2026 Morning' before noon, 'Afternoon' from noon on."""
    now = now or dt.datetime.now()
    part = "Morning" if now.hour < 12 else "Afternoon"
    return f"{now.day} {now:%B %Y} {part}"


def month_dir(root: Path, now: Optional[dt.datetime] = None) -> Path:
    """Y:\\11 November 2026 -- the folders already on the drive."""
    now = now or dt.datetime.now()
    return root / f"{now:%m %B %Y}"


def run_paths(folder: Path, base: str, suffixes: list[str]) -> list[Path]:
    """One free set of names, all sharing a single (1)/(2) counter, so a
    second run in the same half-day keeps its files numbered together."""
    n = 0
    while True:
        tag = f" ({n})" if n else ""
        paths = [folder / f"{base}{tag}{s}.pdf" for s in suffixes]
        if not any(p.exists() for p in paths):
            return paths
        n += 1


# ---------------------------------------------------------------- http
class Bs:
    """BigSeller over its own web API, holding the cookie the way a browser
    would -- so a refreshed session is kept rather than thrown away."""

    def __init__(self, cookie: str) -> None:
        self.jar: dict[str, str] = {}
        self._jar_lock = threading.Lock()
        for part in cookie.split(";"):
            part = part.strip()
            if "=" in part:
                name, _, value = part.partition("=")
                self.jar[name.strip()] = value.strip()

    def cookie_header(self) -> str:
        with self._jar_lock:
            return "; ".join(f"{k}={v}" for k, v in self.jar.items())

    def _absorb(self, res: Any) -> None:
        with self._jar_lock:
            for line in res.headers.get_all("Set-Cookie") or []:
                pair = line.split(";", 1)[0].strip()
                if "=" in pair:
                    name, _, value = pair.partition("=")
                    if value:  # a blank value is the server clearing it
                        self.jar[name.strip()] = value.strip()

    def raw(self, method: str, url: str, data: Optional[bytes] = None,
            content_type: str = "", timeout: int = 60) -> bytes:
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("User-Agent", UA)
        req.add_header("Accept", "application/json, text/plain, */*")
        if url.startswith(BASE):
            req.add_header("Cookie", self.cookie_header())
            req.add_header("Origin", BASE)
            req.add_header("Referer", f"{BASE}/web/order/index.htm?status=pickup")
            req.add_header("X-Requested-With", "XMLHttpRequest")
        if content_type:
            req.add_header("Content-Type", content_type)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as res:
                body = res.read()
                if url.startswith(BASE):
                    self._absorb(res)
                return body
        except urllib.error.HTTPError as exc:
            raise LabelError(f"BigSeller answered {exc.code} for {url.split('?')[0]}") from exc
        except urllib.error.URLError as exc:
            raise LabelError(f"Could not reach BigSeller: {exc.reason}") from exc

    def api(self, method: str, path: str, *, json_body: Optional[dict] = None,
            form: Optional[dict] = None) -> dict:
        data, ctype = None, ""
        if json_body is not None:
            data = json.dumps(json_body).encode()
            ctype = "application/json"
        elif form is not None:
            data = urllib.parse.urlencode(form).encode()
            ctype = "application/x-www-form-urlencoded"
        raw = self.raw(method, BASE + path, data, ctype)
        try:
            j = json.loads(raw.decode("utf-8", "replace"))
        except json.JSONDecodeError:
            raise NotLoggedIn("BigSeller sent a page instead of data -- the cookie has expired.")
        if j.get("code") not in (0, None):
            msg = re.sub(r"<[^>]+>", "", str(j.get("msg") or "")).strip()
            raise LabelError(msg or f"{path} failed")
        return j

    def check_login(self) -> None:
        if not self.api("GET", "/api/v1/isLogin.json").get("data"):
            raise NotLoggedIn("BigSeller says you are not signed in -- paste a fresh cookie.")


# ---------------------------------------------------------------- orders
def fetch_pickup_orders(bs: Bs) -> list[dict]:
    rows, page = [], 1
    while True:
        pg = bs.api("POST", "/api/v1/order/new/pageList.json", json_body={
            "pageNo": page, "pageSize": 100, "status": "pickup", "platform": "shopee",
        })["data"]["page"]
        rows.extend(pg["rows"])
        if page >= pg["totalPage"] or not pg["rows"]:
            return rows
        page += 1


def group_orders(rows: list[dict], groups: list[dict]) -> "dict[str, dict]":
    out: dict[str, dict] = {g["name"]: {} for g in groups}
    fallback = groups[-1]["name"]
    for o in rows:
        carrier = o.get("shippingCarrierName") or o.get("buyerShippingCarrier") or "Unknown"
        low = carrier.lower()
        target = fallback
        for g in groups:
            if any(m.lower() in low for m in g.get("match", [])):
                target = g["name"]
                break
        out[target].setdefault(carrier, []).append(o)
    return out


# ---------------------------------------------------------------- printing
def _mark() -> str:
    return f"{int(time.time() * 1000)}{random.randint(0, 999)}"


def build_pdf(bs: Bs, order_ids: list[str], timeout: int = 300) -> str:
    """One PDF for these orders -- all on one courier -- and its URL.

    BigSeller builds at roughly 0.25s a label, so wait about that long before
    asking, poll quickly, then ease off.
    """
    mark = _mark()
    bs.api("POST", "/api/v1/print/print/selfLabel/many.json",
           form={"orderIds": ",".join(order_ids), "mark": mark, "printSource": "2"})

    deadline = time.time() + timeout
    wait, last = 0.25, ""
    time.sleep(min(0.2 * len(order_ids), 3.0))
    while time.time() < deadline:
        d = bs.api("GET", f"/api/v1/print/print/getOrderPrintProgress.json?mark={mark}"
                          "&type=lable").get("data") or {}
        last = d.get("status") or ""
        if d.get("fileUrl"):
            return d["fileUrl"]
        if last in ("build_failed", "fail", "error"):
            raise LabelError(f"BigSeller could not build the labels: {d.get('error') or last}")
        time.sleep(wait)
        wait = min(wait * 1.4, 2.0)
    raise LabelError(f"Timed out waiting for labels (last status: {last or 'none'})")


def merge_pdfs(parts: list[Path], dest: Path) -> int:
    from pypdf import PdfReader, PdfWriter

    writer = PdfWriter()
    pages = 0
    for p in parts:
        for page in PdfReader(str(p)).pages:
            writer.add_page(page)
            pages += 1
    with open(dest, "wb") as f:
        writer.write(f)
    return pages


# ---------------------------------------------------------------- the button
def preview() -> dict:
    """What is waiting, without downloading anything."""
    cfg = load()
    if not cfg.get("cookie"):
        raise NotLoggedIn("No BigSeller cookie saved yet.")
    bs = Bs(cfg["cookie"])
    bs.check_login()
    rows = fetch_pickup_orders(bs)
    grouped = group_orders(rows, cfg["groups"])
    save({"cookie": bs.cookie_header()})
    return {
        "total": len(rows),
        "name": slot_name(),
        "groups": [
            {"name": g, "count": sum(len(v) for v in c.values()),
             "carriers": [{"name": k, "count": len(v)} for k, v in c.items()]}
            for g, c in grouped.items()
        ],
    }


def download_now() -> dict:
    """Fetch, build, merge, write. The report is what the panel shows."""
    with _lock:
        if _state["running"]:
            raise LabelError("A download is already running.")
        _state["running"] = True
        _state["last_error"] = ""
    started = time.time()
    try:
        report = _download()
        with _lock:
            _state["last_report"] = report
            _state["last_run_at"] = dt.datetime.now().isoformat(timespec="seconds")
        return report
    except Exception as exc:
        with _lock:
            _state["last_error"] = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        with _lock:
            _state["running"] = False
        del started


def _download() -> dict:
    cfg = load()
    if not cfg.get("cookie"):
        raise NotLoggedIn("No BigSeller cookie saved yet.")

    bs = Bs(cfg["cookie"])
    bs.check_login()

    rows = fetch_pickup_orders(bs)
    if not rows:
        save({"cookie": bs.cookie_header()})
        return {"total": 0, "files": [], "seconds": 0, "message": "Nothing to pick up."}

    grouped = group_orders(rows, cfg["groups"])

    root = Path(cfg["out_dir"])
    if not root.exists():
        raise LabelError(f"{root} isn't there -- is the drive connected?")
    out_dir = month_dir(root)
    out_dir.mkdir(parents=True, exist_ok=True)

    base = slot_name()
    group_names = [g for g, c in grouped.items() if c]
    dests = run_paths(out_dir, base, [f" - {g}" for g in group_names])

    tmp_dir = Path(os.environ.get("TEMP", str(config.ROOT_DIR))) / f"bslabels_{os.getpid()}_{int(time.time())}"
    tmp_dir.mkdir(parents=True, exist_ok=True)

    chunk_size = max(1, int(cfg.get("chunk_size", 15)))
    workers = max(1, min(16, int(cfg.get("workers", 8))))

    tasks = []  # (group index, order within group, carrier, ids)
    for gidx, gname in enumerate(group_names):
        seq = 0
        for carrier, orders in grouped[gname].items():
            ids = [str(o["id"]) for o in orders]
            for start in range(0, len(ids), chunk_size):
                tasks.append((gidx, seq, carrier, ids[start:start + chunk_size]))
                seq += 1

    def build(task):
        gidx, seq, carrier, ids = task
        url = build_pdf(bs, ids)
        safe = re.sub(r"[^A-Za-z0-9]+", "_", f"{gidx}_{seq}_{carrier}").strip("_")
        path = tmp_dir / f"{safe}.pdf"
        path.write_bytes(bs.raw("GET", url, timeout=120))
        return task, path

    started = time.time()
    try:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            results = list(pool.map(build, tasks))

        files = []
        for gidx, gname in enumerate(group_names):
            parts = [p for (g, _s, _c, _i), p in sorted(results, key=lambda r: r[0][:2]) if g == gidx]
            dest = dests[gidx]
            pages = merge_pdfs(parts, dest)
            files.append({
                "name": dest.name,
                "path": str(dest),
                "group": gname,
                "orders": sum(len(v) for v in grouped[gname].values()),
                "pages": pages,
            })

        if cfg.get("write_csv"):
            csv_path = dests[0].with_name(f"{dests[0].stem.rsplit(' - ', 1)[0]}.csv")
            with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
                w = csv.writer(f)
                w.writerow(["Group", "Carrier", "Shop", "Order No.", "Tracking No.", "Package No."])
                for gname in group_names:
                    for carrier, orders in grouped[gname].items():
                        for o in orders:
                            w.writerow([gname, carrier, o.get("shopName", ""),
                                        o.get("platformOrderId", ""), o.get("trackingNo", ""),
                                        o.get("packageNo", "")])
            files.append({"name": csv_path.name, "path": str(csv_path), "group": "", "orders": len(rows), "pages": 0})
    finally:
        for p in tmp_dir.glob("*.pdf"):
            try:
                p.unlink()
            except OSError:
                pass
        try:
            tmp_dir.rmdir()
        except OSError:
            pass

    save({"cookie": bs.cookie_header()})
    return {
        "total": len(rows),
        "folder": str(out_dir),
        "files": files,
        "batches": len(tasks),
        "seconds": round(time.time() - started, 1),
    }


def open_folder() -> str:
    """Show the month folder in Explorer, on this PC."""
    import subprocess

    cfg = load()
    folder = month_dir(Path(cfg["out_dir"]))
    if not folder.exists():
        raise LabelError(f"{folder} isn't there yet.")
    subprocess.Popen(["explorer", str(folder)])
    return str(folder)
