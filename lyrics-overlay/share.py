"""Live lyrics web page for colleagues on the same network (phone or PC browser).

The overlay calls publish(); colleagues open http://<this-pc>:8765 and see the same song + lyrics in sync.
"""
import json
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
_state = {"title": "", "artist": "", "lines": [], "t0": None, "status": "Waiting for a song…", "via": None,
          "rev": 0, "lrev": 0}
_lock = threading.Lock()
_peers = {}                   # url -> {"name", "url", "title", "artist", "via", "seen"}
_my_url = None
URL_OK = re.compile(r"^http://\d{1,3}(\.\d{1,3}){3}:\d{1,5}$")   # beacons come from the open network: check them


def publish(song, lines, t0, status, via=None):
    """via = the PC we're Joined to (we only pass its song on), None if this PC finds songs itself."""
    with _lock:
        new = {"title": song["title"] if song else "", "artist": song["artist"] if song else "",
               "lines": [list(x) for x in lines or []], "t0": t0 if song else None, "status": status, "via": via}
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


def peers(hosts_only=False):
    """Other PCs running the app, seen in the last 8 seconds. Ones playing a song come first.
    hosts_only: leave out PCs that are themselves Joined to someone (they only pass a song on)."""
    now = time.time()
    with _lock:
        live = [p for p in _peers.values() if now - p["seen"] < 8 and not (hosts_only and p.get("via"))]
    return sorted(live, key=lambda p: (not p.get("title"), p["name"]))


def fetch_state(host_url, lrev=None):
    """Read another PC's current song. Returns (state, clock_offset) where offset = their clock - ours.
    With the lrev we already have, the reply leaves the lyrics out if they haven't changed."""
    import urllib.request
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
