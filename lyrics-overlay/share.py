"""Live lyrics web page for colleagues on the same network (phone or PC browser).

The overlay calls publish(); colleagues open http://<this-pc>:8765 and see the same song + lyrics in sync.
"""
import json
import os
import re
import socket
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = 8765
BEACON_PORT = 8766            # every running app announces itself here so others can Join it
NAME = socket.gethostname()
ID = uuid.uuid4().hex            # tells this copy of the app apart from others (even on the same PC)
# rev changes on any change; lrev only when the song or its lyrics change (so pollers can skip the lyrics)
DIAG = {}          # how this PC's app is doing (version, video reading) - in the NAS file, for fixing problems
_state = {"title": "", "artist": "", "lines": [], "t0": None, "status": "Waiting for a song…", "via": None,
          "paused": None, "rev": 0, "lrev": 0}
WATCH = False      # True while this PC is Joined to someone: read the NAS folder often
_lock = threading.Lock()
_peers = {}                   # url -> {"name", "url", "title", "artist", "via", "seen"}
_my_url = None
URL_OK = re.compile(r"^http://\d{1,3}(\.\d{1,3}){3}:\d{1,5}$")   # beacons come from the open network: check them


def publish(song, lines, t0, status, via=None, paused=None):
    """via = the PC we're Joined to (we only pass its song on), None if this PC finds songs itself.
    paused = the song position while the player is paused (joiners freeze there), else None.
    Tiny timing wobbles (< 0.5s) don't count as a change, so the NAS is only written when it matters."""
    with _lock:
        new = {"title": song["title"] if song else "", "artist": song["artist"] if song else "",
               "lines": [list(x) for x in lines or []], "t0": t0 if song else None, "status": status, "via": via,
               "paused": round(paused, 1) if (paused is not None and song) else None}
        old_t0 = _state["t0"]
        if new["t0"] is not None and old_t0 is not None and (new["paused"] is not None or abs(new["t0"] - old_t0) < 0.5):
            new["t0"] = old_t0
        if any(new[k] != _state[k] for k in ("title", "artist", "lines")):
            _state["lrev"] += 1
        if any(new[k] != _state[k] for k in new):
            _state.update(new)
            _state["rev"] += 1


def local_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))   # no packet is sent; just picks the LAN interface
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


def url():
    return f"http://{local_ip()}:{PORT}"


# ---------- finding each other on the office network ----------
def _beacon_loop():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    while True:
        with _lock:
            msg = {"app": "lyrics-overlay", "id": ID, "name": NAME, "url": url(), "via": _state["via"],
                   "title": _state["title"], "artist": _state["artist"], "has": bool(_state["lines"])}
        try:
            s.sendto(json.dumps(msg, ensure_ascii=False).encode("utf-8"), ("255.255.255.255", BEACON_PORT))
        except OSError:
            pass
        time.sleep(2)


def _listen_loop():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        s.bind(("", BEACON_PORT))
    except OSError:
        return
    while True:
        try:
            data, _ = s.recvfrom(4096)
            m = json.loads(data.decode("utf-8"))
            if m.get("app") != "lyrics-overlay" or m.get("id") == ID:
                continue
            url_ = str(m.get("url", ""))
            if not URL_OK.match(url_):           # only plain http://<ip>:<port> addresses
                continue
            clean = {"id": str(m.get("id", ""))[:64], "url": url_, "name": str(m.get("name", "?"))[:64],
                     "title": str(m.get("title") or "")[:200], "artist": str(m.get("artist") or "")[:200],
                     "via": (str(m["via"])[:64] if m.get("via") else None), "seen": time.time()}
            with _lock:
                _peers[url_] = clean
        except Exception:
            pass


# ---------- the NAS way: works through firewalls and Wi-Fi / cable splits ----------
# Each PC writes ONLY its own "<PC name>.json" in a shared NAS folder (no file has two writers) and
# reads the others'. Every PC only reaches OUT to the NAS, so no PC needs to accept connections.
# Times are kept in NAS-clock seconds so lyrics stay in sync even if PC clocks differ a little.
NAS_DIRS = (r"\\PARTYMONKEY-NAS\Partymonkey\Partymonkey NAS\Lyrics Overlay\now-playing",
            r"P:\Lyrics Overlay\now-playing")
HEARTBEAT = 60                # write at least this often, so others know the app is still open
NAS_FRESH = 140               # a PC's newest file older than this = that app is closed
SCAN_JOINED, SCAN_IDLE = 2, 20   # seconds between reads of the folder: while Joined / otherwise
_nas = {"dir": None, "off": None, "offs": [], "files": {}, "peers": []}   # off = NAS clock - our clock


def _nas_dir():
    for d in NAS_DIRS:
        try:
            os.makedirs(d, exist_ok=True)
            return d
        except OSError:
            continue
    return None


# Each update is a NEW file "<PC>.<millis>.json" (the writer then deletes its older ones). Replacing one
# fixed file doesn't work on this NAS: other PCs' Windows keeps serving them the old cached copy for
# minutes. A file name nobody has opened before can't be stale. Readers take each PC's newest file.
NAS_FILE = re.compile(r"^(.+)\.(\d{10,})\.json$")
KEEP_OWN = 3                  # keep a few: a reader's folder listing can be up to ~10s old


def _safe_name():
    return re.sub(r"[^\w-]", "_", NAME)


def _nas_write(d):
    with _lock:
        st = dict(_state)
    off = _nas["off"]
    data = {"app": "lyrics-overlay", "id": ID, "name": NAME, "title": st["title"], "artist": st["artist"],
            "lines": st["lines"], "status": st["status"], "via": st["via"], "lrev": st["lrev"],
            "t0_nas": (st["t0"] + off) if (st["t0"] is not None and off is not None) else None,
            "t0_local": st["t0"], "paused": st["paused"], "diag": dict(DIAG)}
    base = _safe_name()
    path = os.path.join(d, f"{base}.{int(time.time() * 1000)}.json")
    tmp = os.path.join(d, f"~{base}.tmp")
    t1 = time.time()
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    t2 = time.time()
    os.rename(tmp, path)                       # a brand-new name, so it never lands on a cached file
    # how far the NAS clock is from ours: the new file's time stamp (NAS clock) vs when we wrote it
    _nas["offs"] = (_nas["offs"] + [os.stat(path).st_mtime - (t1 + t2) / 2])[-7:]
    _nas["off"] = sorted(_nas["offs"])[len(_nas["offs"]) // 2]
    # tidy up our own older files (and the single-file name the previous version used)
    mine = sorted(fn for fn in os.listdir(d) if (m := NAS_FILE.match(fn)) and m.group(1) == base)
    for fn in mine[:-KEEP_OWN] + ([base + ".json"] if os.path.exists(os.path.join(d, base + ".json")) else []):
        try:
            os.remove(os.path.join(d, fn))
        except OSError:
            pass
    return st["rev"]


def _nas_scan(d):
    now_nas = time.time() + (_nas["off"] or 0.0)
    newest = {}                                # PC -> its file names, newest first
    for fn in os.listdir(d):
        m = NAS_FILE.match(fn)
        if m and m.group(1) != _safe_name():
            newest.setdefault(m.group(1), []).append((int(m.group(2)), fn))
    found, keep = [], set()
    for pc, files in newest.items():
        for seq, fn in sorted(files, reverse=True):
            path = os.path.join(d, fn)
            try:
                if now_nas - seq / 1000 > NAS_FRESH:          # newest file is old: that app is closed
                    break
                cached = _nas["files"].get(path)
                if not cached:                                # each name is read once - it never changes
                    with open(path, encoding="utf-8") as f:
                        cached = (seq, json.load(f))
                    _nas["files"][path] = cached
                keep.add(path)
                m = cached[1]
                if m.get("app") == "lyrics-overlay" and m.get("id") != ID:
                    found.append({"id": str(m.get("id", ""))[:64], "name": str(m.get("name", "?"))[:64],
                                  "title": str(m.get("title") or "")[:200],
                                  "artist": str(m.get("artist") or "")[:200],
                                  "via": (str(m["via"])[:64] if m.get("via") else None), "file": path,
                                  "url": None, "seen": time.time()})
                break                                         # got this PC's newest readable file
            except (OSError, ValueError):
                continue                                      # just deleted / half-written: try the older one
    for path in list(_nas["files"]):
        if path not in keep:
            del _nas["files"][path]
    with _lock:
        _nas["peers"] = found


def _nas_loop():
    last_rev, last_write, last_scan, last_try = -1, 0.0, 0.0, 0.0
    while True:
        now = time.time()
        if _nas["dir"] is None and now - last_try > 30:
            last_try = now
            _nas["dir"] = _nas_dir()
        d = _nas["dir"]
        if d:
            try:
                with _lock:
                    rev = _state["rev"]
                if rev != last_rev or now - last_write > HEARTBEAT:   # on a real change + a heartbeat
                    last_rev, last_write = _nas_write(d), now
                if now - last_scan > (SCAN_JOINED if WATCH else SCAN_IDLE) or _nas.get("scan_now"):
                    _nas["scan_now"] = False
                    last_scan = now
                    _nas_scan(d)
            except OSError:
                _nas["dir"] = None                                # NAS gone: look for it again later
        time.sleep(0.5)


def refresh():
    """Read the NAS folder now (e.g. the Join menu is opening). Quick; errors are ignored."""
    d = _nas["dir"]
    if d:
        try:
            _nas_scan(d)
        except OSError:
            pass


def stop():
    """App closing: remove this PC's files so others stop listing it straight away."""
    d = _nas["dir"]
    if not d:
        return
    try:
        for fn in os.listdir(d):
            m = NAS_FILE.match(fn)
            if m and m.group(1) == _safe_name():
                os.remove(os.path.join(d, fn))
    except OSError:
        pass


def nas_ok():
    return _nas["dir"] is not None


def peers(hosts_only=False):
    """Other PCs running the app (found on the NAS or by network announcement). Playing ones first.
    hosts_only: leave out PCs that are themselves Joined to someone (they only pass a song on)."""
    now = time.time()
    with _lock:
        lan = [p for p in _peers.values() if now - p["seen"] < 8]
        nas = list(_nas["peers"])
    by_id = {p["id"]: p for p in lan}
    by_id.update({p["id"]: p for p in nas})           # same app seen both ways: use the NAS route
    live = [p for p in by_id.values() if not (hosts_only and p.get("via"))]
    return sorted(live, key=lambda p: (not p.get("title"), p["name"]))


def fetch_state(host, lrev=None):
    """Read another PC's current song. host = a peers() entry (or a URL). Returns (state, clock_offset)
    where offset = their clock - ours. With the lrev we already have, the lyrics are left out if unchanged."""
    if isinstance(host, dict) and host.get("file"):
        cached = _nas["files"].get(host["file"])
        if not cached:
            raise OSError("not read yet")
        st = dict(cached[1])
        if st.get("t0_nas") is not None and _nas["off"] is not None:
            st["t0"] = st["t0_nas"] - _nas["off"]          # NAS clock -> our clock
        else:
            st["t0"] = st.get("t0_local")                   # no clock reference yet: assume clocks agree
        if lrev is not None and st.get("lrev") == lrev:
            st.pop("lines", None)
        return st, 0.0
    import urllib.request
    host_url = host["url"] if isinstance(host, dict) else host
    t1 = time.time()
    q = f"?lrev={lrev}" if lrev is not None else ""
    with urllib.request.urlopen(host_url + "/state" + q, timeout=3) as r:
        st = json.loads(r.read().decode("utf-8"))
    return st, st["now"] - (t1 + time.time()) / 2


PAGE = r"""<!doctype html><html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Now Playing</title>
<style>
:root{color-scheme:dark}
body{margin:0;background:#0d0f14;color:#fff;font-family:"Microsoft YaHei UI","PingFang SC",system-ui,sans-serif;
 height:100vh;display:flex;flex-direction:column;overflow:hidden}
header{padding:16px 20px;border-bottom:1px solid #222a35;font-size:15px;color:#9fe7ff;display:flex;gap:8px;align-items:center}
header b{color:#fff;font-size:18px}
#wrap{flex:1;overflow:hidden;position:relative}
#list{position:absolute;left:0;right:0;transition:transform .45s ease;padding:0 20px}
.l{font-size:22px;line-height:1.5;padding:8px 0;color:#5d6675;text-align:center;transition:color .3s,font-size .3s}
.l.on{color:#fff;font-size:30px;font-weight:700}
#msg{padding:40px 20px;text-align:center;color:#8a93a3;font-size:20px}
footer{border-top:1px solid #222a35;padding:10px 16px;font-size:14px;color:#8a93a3;display:none}
footer a{display:inline-block;margin:4px 8px 4px 0;padding:6px 10px;border-radius:8px;background:#1b2130;
 color:#cfe9ff;text-decoration:none}
</style></head><body>
<header>♪ <span id="song"><b>…</b></span></header>
<div id="wrap"><div id="list"></div><div id="msg"></div></div>
<footer id="peers"></footer>
<script>
let st=null, lrev=-1, offset=0, cur=-1;
const list=document.getElementById('list'), msg=document.getElementById('msg');
async function poll(){
  try{
    const t=Date.now()/1000, r=await fetch('/state?lrev='+lrev,{cache:'no-store'}), j=await r.json();
    offset=j.now-(t+Date.now()/1000)/2;               // server clock minus ours
    if(j.lrev!==lrev || !st){lrev=j.lrev; st=j; render();}   // new song / lyrics: rebuild the list
    else {st.t0=j.t0; st.status=j.status; if(!st.lines.length) msg.textContent=st.status||'';}  // timing only
  }catch(e){}
  setTimeout(poll,2000);
}
function esc(s){return String(s==null?'':s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
function render(){
  document.getElementById('song').innerHTML = st.title ? '<b>'+esc(st.title)+'</b> — '+esc(st.artist) : '<b>Nothing playing</b>';
  cur=-1;
  list.innerHTML = st.lines.map((l,i)=>'<div class="l" id="l'+i+'">'+(esc(l[1])||'♪')+'</div>').join('');
  msg.textContent = st.lines.length ? '' : (st.status || '');
}
function tick(){
  if(st && st.lines.length && st.t0){
    const pos=Date.now()/1000+offset-st.t0; let i=-1;
    for(let k=0;k<st.lines.length;k++){ if(st.lines[k][0]<=pos) i=k; else break; }
    if(i!==cur){
      document.querySelectorAll('.l.on').forEach(e=>e.classList.remove('on'));
      cur=i; const el=document.getElementById('l'+Math.max(i,0));
      if(i>=0) el.classList.add('on');
      const wrap=document.getElementById('wrap');
      list.style.transform='translateY('+(wrap.clientHeight*0.4-el.offsetTop)+'px)';
    }
  }
  requestAnimationFrame(tick);
}
async function peers(){
  try{
    const ps=(await (await fetch('/peers',{cache:'no-store'})).json()).filter(p=>/^http:\/\/[\d.]+:\d+$/.test(p.url)),
          f=document.getElementById('peers');
    f.style.display = ps.length ? 'block' : 'none';
    f.innerHTML = 'Other PCs: ' + ps.map(p=>'<a href="'+esc(p.url)+'">'+esc(p.name)+
      (p.title ? ' — ♪ '+esc(p.title) : ' — idle')+'</a>').join('');
  }catch(e){}
  setTimeout(peers,5000);
}
poll(); tick(); peers();
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith("/state"):
            m = re.search(r"[?&]lrev=(-?\d+)", self.path)
            with _lock:
                reply = {**_state, "now": time.time()}
                if m and int(m.group(1)) == _state["lrev"]:
                    del reply["lines"]               # they already have these lyrics: send timing only
                body = json.dumps(reply, ensure_ascii=False).encode("utf-8")
            ctype = "application/json; charset=utf-8"
        elif self.path.startswith("/peers"):
            body = json.dumps([{k: p[k] for k in ("name", "url", "title", "artist")} for p in peers(True)],
                              ensure_ascii=False).encode("utf-8")
            ctype = "application/json; charset=utf-8"
        elif self.path in ("/", "/index.html"):
            body, ctype = PAGE.encode("utf-8"), "text/html; charset=utf-8"
        else:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


def start():
    """Start the page server + network announcing. Returns the URL, or None if the port is busy."""
    global _my_url
    threading.Thread(target=_listen_loop, daemon=True).start()
    threading.Thread(target=_nas_loop, daemon=True).start()
    class Server(ThreadingHTTPServer):
        allow_reuse_address = False          # on Windows, reuse would let two apps grab the same port

        def server_bind(self):
            if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            super().server_bind()
    try:
        srv = Server(("0.0.0.0", PORT), Handler)
    except OSError:
        return None
    _my_url = url()
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    threading.Thread(target=_beacon_loop, daemon=True).start()
    return _my_url


def qr_png(text, path):
    import qrcode
    import qrcode.image.pure
    qrcode.make(text, image_factory=qrcode.image.pure.PyPNGImage, box_size=8, border=2).save(path)
    return path
