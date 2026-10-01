"""Store Pick Up's "From BigSeller" button.

Pigu is a web page: it cannot see BigSeller or DuoKe. This PC has both, so
the button writes a row in `pickup_sync_jobs` and this picks it up:

  queued  -> reading   New orders in BigSeller whose logistics is Seller Store
                       Pick Up, and each buyer's DuoKe chat read for when they
                       are collecting (the reply-draft function turns the chat
                       into a one-line note)
          -> found     the list goes back to Pigu, which adds the pick ups
                       itself as the person who pressed the button
  shipping -> sending  the ones Pigu now has are shipped in BigSeller -- the
                       same /v1/order/ship.json the Ship button calls
          -> done      with what happened to each

BigSeller is read through Macro Studio's own Chrome (the debugging one, see
open-cdp-chrome.bat), which has to be signed in to BigSeller once. DuoKe is
read through its API without opening any chat -- nothing is typed or sent
there. Settings live in pickup-sync.json: {"enabled": true} is all it needs;
the Supabase login is borrowed from duoke.json.
"""
from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from agent import cdp, config, duoke, duoke_web, realtime
from agent.duoke import Cloud, CloudError, shared_cloud

CONFIG_PATH = config.ROOT_DIR / "pickup-sync.json"

DEFAULTS: dict[str, Any] = {
    # Off until switched on. Only one PC needs it: the one signed in to
    # BigSeller in Macro Studio's Chrome, with DuoKe open.
    "enabled": False,
    "poll_seconds": 4,
    "cdp_port": cdp.DEFAULT_PORT,
    # Blank borrows the Replies sync login from duoke.json.
    "supabase_url": "",
    "supabase_anon_key": "",
    "email": "",
    "password": "",
}

BIGSELLER_URL = "https://www.bigseller.com/web/order/index.htm?status=new"
PICKUP_CARRIER = "seller store pick up"
SGT = timezone(timedelta(hours=8))

_lock = threading.Lock()
_state: dict[str, Any] = {"running": False, "last_error": "", "last_pass_at": "", "jobs_done": 0}


# ---------------------------------------------------------------- settings
def load() -> dict:
    saved: dict = {}
    if CONFIG_PATH.exists():
        try:
            saved = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            saved = {}
    merged = dict(DEFAULTS)
    merged.update({k: v for k, v in saved.items() if k in DEFAULTS})
    return merged


def save(patch: dict) -> dict:
    with _lock:
        data = load()
        data.update({k: v for k, v in (patch or {}).items() if k in DEFAULTS and v is not None})
        CONFIG_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")
        return data


def _cloud() -> Cloud:
    cfg, d = load(), duoke.load()
    return shared_cloud(cfg.get("supabase_url") or d.get("supabase_url", ""),
                        cfg.get("supabase_anon_key") or d.get("supabase_anon_key", ""),
                        cfg.get("email") or d.get("email", ""),
                        cfg.get("password") or d.get("password", ""))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _patch(cloud: Cloud, job_id: str, only_from: str, fields: dict) -> bool:
    """Moves a job on, but only if it is still where we left it -- two PCs
    running this never both take the same press of the button."""
    got = cloud.rest("PATCH", f"/pickup_sync_jobs?id=eq.{job_id}&status=eq.{only_from}",
                     fields | {"updated_at": _now()}, prefer="return=representation")
    return bool(got)


# ---------------------------------------------------------------- BigSeller
def _bigseller_page(port: int) -> dict:
    if not cdp.is_running(port):
        cdp.launch(port, url=BIGSELLER_URL)
        try:
            cdp.park_offscreen(port)
        except Exception:
            pass
    try:
        page = cdp.find_page(port, "bigseller.com", timeout_ms=15000)
    except RuntimeError:
        page = cdp.open_tab(port, BIGSELLER_URL)
        page = cdp.find_page(port, "bigseller.com", timeout_ms=15000)
    cdp.wait_loaded(page, timeout_ms=30000)
    return page


def _bs_call(page: dict, js: str) -> Any:
    raw = cdp.evaluate(page, js, timeout=60)
    try:
        return json.loads(raw) if isinstance(raw, str) else raw
    except json.JSONDecodeError:
        return None


_LIST_JS = r"""
(async () => {
  const r = await fetch('/api/v1/order/new/pageList.json', {method: 'POST',
    headers: {'Content-Type': 'application/json'}, credentials: 'include',
    body: JSON.stringify({status: 'new', timeType: 1, inquireType: 0, searchType: 'commoditySku',
                          desc: 1, orderBy: 'expireTime', pageNo: 1, pageSize: 300})});
  const text = await r.text();
  let j; try { j = JSON.parse(text); } catch (e) { return JSON.stringify({signin: true}); }
  if (j.code !== 0) return JSON.stringify({error: j.msg || ('code ' + j.code)});
  return JSON.stringify({rows: ((j.data && j.data.page && j.data.page.rows) || []).map(o => ({
    bs_id: String(o.id), order_id: o.platformOrderId || '', buyer: o.buyerUsername || '',
    recipient: o.contactPerson || '', carrier: o.shippingCarrierName || o.shipmentProvider || '',
    shop: o.shopName || '', paid: o.payTimeStr || ''}))});
})()
"""


def pickup_orders(port: int) -> list[dict]:
    page = _bigseller_page(port)
    out = _bs_call(page, _LIST_JS) or {}
    if out.get("signin"):
        raise RuntimeError("BigSeller is not signed in in Macro Studio's Chrome -- "
                           "open it (open-cdp-chrome.bat), sign in, and press the button again")
    if out.get("error"):
        raise RuntimeError("BigSeller: " + str(out["error"]))
    return [r for r in out.get("rows") or [] if PICKUP_CARRIER in r["carrier"].lower()]


def ship(port: int, bs_id: str) -> tuple[bool, str]:
    """The order's Pack button, NOT Ship -- the user packs pick ups and never
    wants them shipped from here: POST /v1/order/pack.json, flag 0 = single."""
    page = _bigseller_page(port)
    js = ("(async () => { const r = await fetch('/api/v1/order/pack.json', {method: 'POST', "
          "credentials: 'include', headers: {'Content-Type': 'application/x-www-form-urlencoded'}, "
          f"body: 'orderId=' + encodeURIComponent({json.dumps(bs_id)}) + '&flag=0'}}); "
          "return await r.text(); })()")
    raw = cdp.evaluate(page, js, timeout=60)
    try:
        j = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return False, "BigSeller gave no answer"
    if j.get("code") == 0:
        return True, "packed"
    return False, str(j.get("msg") or j.get("errorMsg") or f"code {j.get('code')}")


# ---------------------------------------------------------------- DuoKe
def _chat(buyer: str, sessions: list[dict]) -> list[dict]:
    row = next((s for s in sessions if s["name"].lower() == buyer.lower()), None)
    if not row:
        return []
    raw = duoke_web._run(f"({duoke_web._MESSAGES})({json.dumps(row['shop_id'])}, "
                         f"{json.dumps(row['conversation_id'])})")
    data = duoke_web._json(raw)
    lines = []
    for l in (data.get("lines") or [])[-30:]:
        ts = int(l.get("ts") or 0)
        when = datetime.fromtimestamp(ts / 1000, SGT).strftime("%a %d/%m %H:%M") if ts else ""
        lines.append({"inbound": bool(l.get("inbound")), "text": str(l.get("text") or "")[:300],
                      "when": when})
    return lines


def _remarks(cloud: Cloud, order: dict, lines: list[dict]) -> str:
    if not lines:
        return ""
    body = {"pickup_when": True, "history": lines, "order": order["order_id"],
            "now": datetime.now(SGT).strftime("%a %d/%m/%Y %H:%M")}
    try:
        out = cloud._call("POST", "/functions/v1/reply-draft", body, cloud._auth())
    except CloudError as exc:
        if "401" not in str(exc):
            return ""
        cloud.token = None
        out = cloud._call("POST", "/functions/v1/reply-draft", body, cloud._auth())
    return str((out or {}).get("remarks") or "").strip() if isinstance(out, dict) else ""


# ---------------------------------------------------------------- one pass
def _name(o: dict) -> str:
    # BigSeller shows only the end of the recipient's name, e.g. "Yiming)"
    who = o["recipient"].strip().rstrip(")").strip()
    if not who or "*" in who:
        return o["buyer"]
    return f"{who} ({o['buyer']})" if o["buyer"] else who


def read_job(cloud: Cloud, job: dict, port: int) -> None:
    orders = pickup_orders(port)
    sessions = duoke_web.all_sessions(5) if orders and duoke_web.available() else []
    found = []
    for o in orders:
        remarks = ""
        try:
            remarks = _remarks(cloud, o, _chat(o["buyer"], sessions))
        except Exception:
            pass  # a note we could not write is a blank note, not a failed press
        found.append({"bs_id": o["bs_id"], "order_id": o["order_id"], "name": _name(o),
                      "buyer": o["buyer"], "remarks": remarks, "shop": o["shop"]})
    _patch(cloud, job["id"], "reading", {"status": "found", "found": found})


def ship_job(cloud: Cloud, job: dict, port: int) -> None:
    by_id = {str(f.get("bs_id")): f for f in job.get("found") or []}
    result = []
    for bs_id in [str(x) for x in job.get("ship_ids") or []]:
        if bs_id not in by_id:
            continue  # only what this job itself found is ever shipped
        ok, note = ship(port, bs_id)
        result.append({"bs_id": bs_id, "order_id": by_id[bs_id].get("order_id", ""),
                       "ok": ok, "note": note[:200]})
    _patch(cloud, job["id"], "sending", {"status": "done", "result": result})


def photo_job(cloud: Cloud, job: dict, port: int) -> None:
    """A card's "Send to buyer": the words, then the photo, into the buyer's
    DuoKe chat in the background. Somebody pressed Send in Pigu for exactly
    this, which is what approved_send() is for."""
    p = job.get("payload") or {}
    buyer, text, url = str(p.get("buyer") or ""), str(p.get("text") or ""), str(p.get("photo_url") or "")
    if not buyer or not url:
        raise RuntimeError("no buyer or no photo on this pick up")
    if not duoke_web.available():
        raise RuntimeError("DuoKe is not open on the shop PC")
    sess = next((s for s in duoke_web.all_sessions(5) if s["name"].lower() == buyer.lower()), None)
    if not sess:
        raise RuntimeError(f"no DuoKe chat with {buyer}")
    import urllib.request
    with urllib.request.urlopen(url, timeout=30) as res:
        image = res.read()
    with duoke.approved_send():
        said = duoke_web.send_text(sess["shop_id"], sess["conversation_id"], " ".join(text.split())) \
            if text.strip() else "sent"
        if not said.startswith("sent"):
            raise RuntimeError("message: " + said)
        got = duoke_web.send_image(sess["shop_id"], sess["conversation_id"], image,
                                   f"pickup-{p.get('order_id') or 'photo'}.jpg")
    if not got.startswith("sent"):
        raise RuntimeError("photo: " + got)
    _patch(cloud, job["id"], "sending", {"status": "done", "result": [{"ok": True, "note": got}]})


def sync_once() -> None:
    cfg = load()
    port = int(cfg.get("cdp_port") or cdp.DEFAULT_PORT)
    cloud = _cloud()
    # a press older than 10 minutes was given up on by the page that made it
    since = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
    jobs = cloud.rest("GET", "/pickup_sync_jobs?select=*&status=in.(queued,shipping)"
                             f"&created_at=gte.{since.replace('+', '%2B')}&order=created_at") or []
    for job in jobs:
        if job.get("kind") == "photo":
            if job["status"] != "queued":
                continue
            step = (photo_job, "sending")
        else:
            step = (read_job, "reading") if job["status"] == "queued" else (ship_job, "sending")
        if not _patch(cloud, job["id"], job["status"], {"status": step[1]}):
            continue  # another PC took it
        try:
            step[0](cloud, job, port)
            _state["jobs_done"] += 1
        except Exception as exc:
            _state["last_error"] = f"{type(exc).__name__}: {exc}"
            _patch(cloud, job["id"], step[1], {"status": "failed", "error": str(exc)[:300]})
    _state["last_pass_at"] = _now()


def status() -> dict:
    cfg = load()
    return {"enabled": bool(cfg.get("enabled")), "watching": bool(_state["running"]),
            "last_pass_at": _state["last_pass_at"], "last_error": _state["last_error"],
            "jobs_done": _state["jobs_done"], "config_file": str(CONFIG_PATH)}


class Watcher:
    def __init__(self) -> None:
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: Optional[threading.Thread] = None
        # queued = a press of From BigSeller; shipping = Pigu has the list and
        # wants those orders shipped. Both are somebody pressing a button.
        self._listener = realtime.Listener(
            lambda: _cloud(), ["pickup_sync_jobs"], self._wake.set,
            name="pickup-sync-jobs", statuses=("queued", "shipping"))

    def start(self) -> bool:
        if self._thread and self._thread.is_alive():
            return False
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="pickup-sync", daemon=True)
        self._thread.start()
        self._listener.start()
        _state["running"] = True
        return True

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        self._listener.stop()
        _state["running"] = False

    def _run(self) -> None:
        """Runs when Supabase says a button was pressed, not on a timer. One
        catch-up pass when the socket (re)connects; slow polling only if the
        socket is down."""
        self._stop.wait(3)
        while not self._stop.is_set():
            cfg = load()
            retry = 0.0
            if cfg.get("enabled"):
                try:
                    sync_once()
                except Exception as exc:  # a pass must never kill the loop
                    _state["last_error"] = f"{type(exc).__name__}: {exc}"
                    if isinstance(exc, CloudError):
                        retry = 10.0
            if not cfg.get("enabled"):
                timeout = 30                    # local config check, no network
            elif retry:
                timeout = retry
            elif self._listener.connected:
                timeout = None                  # nothing to do until pushed
            else:
                timeout = max(2, int(cfg.get("poll_seconds") or 4)) * 4
            self._wake.wait(timeout)
            self._wake.clear()
        _state["running"] = False


watcher = Watcher()
