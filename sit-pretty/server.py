"""Sit Pretty: return-dispute evidence recorder for PartyMonkey.

Runs on a shop PC on the camera network. Serves a web page (web/index.html)
and records Tapo cameras over RTSP with ffmpeg:
  * Unboxing: Start/Stop on the returns-table camera, saved straight into
    <save_dir>/<OrderID>/<OrderID>_unboxing_<YYYYMMDD-HHMMSS>.mp4
  * Packing: a clip for an order is downloaded from the packing cameras'
    own memory cards (sdclip.py, via pytapo) for the minutes it was packed.
Nothing is recorded in between. Standard library plus ffmpeg; pytapo for clips.
"""
import json, os, re, shutil, subprocess, sys, threading, time, urllib.parse
from datetime import datetime, timedelta
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

HERE = os.path.dirname(os.path.abspath(__file__))
CFG = json.load(open(os.path.join(HERE, "config.json"), encoding="utf8"))
SAVE = CFG["save_dir"]


def find_ffmpeg():
    """config.json's path if set, else ffmpeg on PATH, else winget's or the repo's copy."""
    if CFG.get("ffmpeg") and os.path.isfile(CFG["ffmpeg"]):
        return CFG["ffmpeg"]
    w = shutil.which("ffmpeg")
    if w:
        return w
    import glob
    for pat in [os.path.join(os.environ.get("LOCALAPPDATA", ""), "Microsoft", "WinGet", "Packages", "Gyan.FFmpeg*", "*", "bin", "ffmpeg.exe"),
                os.path.join(HERE, "..", "ffmpeg-*", "bin", "ffmpeg.exe")]:
        hits = glob.glob(pat)
        if hits:
            return hits[0]
    return "ffmpeg"


FFMPEG = find_ffmpeg()
VENV_PY = os.path.join(HERE, ".venv", "Scripts", "python.exe")
NOWIN = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
LOG_LOCK = threading.Lock()
LOGS = []


def clean(msg):
    """Never let a camera login leave this process: strip it from ffmpeg errors."""
    return re.sub(r"rtsp://[^@\s/]+@", "rtsp://", str(msg))


def log(msg):
    line = datetime.now().strftime("%H:%M:%S ") + clean(msg)
    with LOG_LOCK:
        LOGS.append(line)
        del LOGS[:-200]
    print(line, flush=True)


def secrets():
    s = {}
    try:
        for ln in open(os.path.join(HERE, "sitpretty-secrets.txt"), encoding="utf8"):
            ln = ln.strip()
            if ln and not ln.startswith("#") and "=" in ln:
                k, v = ln.split("=", 1)
                s[k.strip()] = v.strip()
    except FileNotFoundError:
        pass
    return s


def cam_login(cam, s=None):
    """A camera's own login (e.g. cam114.username) wins over the shared one."""
    s = s or secrets()
    key = "cam" + CFG["cameras"][cam]["ip"].rsplit(".", 1)[1]
    return s.get(key + ".username") or s.get("username"), s.get(key + ".password") or s.get("password")


def rtsp(cam, stream="stream1"):
    user, pw = cam_login(cam)
    if not user or not pw:
        raise RuntimeError(f"Camera login missing for {CFG['cameras'][cam]['label']}: fill in sitpretty-secrets.txt")
    u = urllib.parse.quote(user, safe="")
    p = urllib.parse.quote(pw, safe="")
    return f"rtsp://{u}:{p}@{CFG['cameras'][cam]['ip']}:554/{stream}"


def safe_id(x):
    x = re.sub(r"[^A-Za-z0-9_-]", "", str(x or ""))[:40]
    if not x:
        raise ValueError("Order ID is empty")
    return x


def order_dir(order):
    d = os.path.join(SAVE, safe_id(order))
    os.makedirs(d, exist_ok=True)
    return d


def hhmm(s):
    h, m = s.split(":")
    return int(h) * 60 + int(m)


# ---------------------------------------------------------------- unboxing
class Unboxer:
    def __init__(self):
        self.proc = None
        self.info = None
        self.lock = threading.Lock()

    def start(self, order, request="", note=""):
        with self.lock:
            if self.proc and self.proc.poll() is None:
                raise RuntimeError(f"Already recording order {self.info['order']}")
            order = safe_id(order)
            d = order_dir(order)
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            final = os.path.join(d, f"{order}_unboxing_{stamp}.mp4")
            tmpdir = os.path.join(HERE, "tmp")
            os.makedirs(tmpdir, exist_ok=True)
            tmp = os.path.join(tmpdir, f"{order}_{stamp}.mp4")
            prev = os.path.join(tmpdir, "preview_rec.jpg")
            cmd = [FFMPEG, "-hide_banner", "-loglevel", "error", "-rtsp_transport", "tcp",
                   "-i", rtsp(CFG["returns_camera"]),
                   "-map", "0:v", "-map", "0:a?", "-c:v", "copy", "-c:a", "aac", "-b:a", "64k",
                   "-movflags", "+faststart", tmp,
                   "-map", "0:v", "-vf", "fps=1,scale=640:-2", "-update", "1", "-q:v", "6", "-y", prev]
            self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=NOWIN)
            self.info = {"order": order, "request": request, "note": note, "tmp": tmp, "final": final,
                         "started": time.time()}
            log(f"Unboxing started for {order}")
            return {"order": order, "file": os.path.basename(final)}

    def stop(self):
        with self.lock:
            if not self.proc:
                raise RuntimeError("Nothing is recording")
            p, info = self.proc, self.info
            try:
                p.stdin.write(b"q")
                p.stdin.flush()
            except Exception:
                pass
            try:
                p.wait(timeout=20)
            except subprocess.TimeoutExpired:
                p.kill()
            err = (p.stderr.read() or b"").decode("utf8", "replace")[-500:]
            self.proc = None
            self.info = None
            if not os.path.exists(info["tmp"]) or os.path.getsize(info["tmp"]) < 10000:
                log(f"Unboxing for {info['order']} failed: {err.strip()}")
                raise RuntimeError("The recording is empty. Check the camera login and that the returns camera is online. " + err.strip()[:200])
            shutil.move(info["tmp"], info["final"])
            secs = int(time.time() - info["started"])
            meta = os.path.join(os.path.dirname(info["final"]), "evidence-log.txt")
            with open(meta, "a", encoding="utf8") as f:
                f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S}  unboxing  {os.path.basename(info['final'])}  {secs}s"
                        f"  request={info['request']}  note={info['note']}\n")
            EV_CACHE[0] = 0
            log(f"Unboxing saved: {info['final']} ({secs}s)")
            return {"order": info["order"], "file": info["final"], "seconds": secs}

    def state(self):
        if self.proc and self.proc.poll() is None:
            return {"recording": True, "order": self.info["order"], "request": self.info["request"],
                    "seconds": int(time.time() - self.info["started"])}
        if self.proc and self.proc.poll() is not None:
            err = (self.proc.stderr.read() or b"").decode("utf8", "replace")[-300:]
            log("Unboxing recorder stopped by itself: " + err.strip())
            self.proc = None
            return {"recording": False, "error": "The camera connection dropped. " + err.strip()[:200]}
        return {"recording": False}


UNBOX = Unboxer()


# ---------------------------------------------------------------- packing clips (memory card)
def sd(cam, *args, timeout=900):
    if not os.path.isfile(VENV_PY):
        raise RuntimeError("Sit Pretty's add-ons are missing: run start-sit-pretty.bat once")
    env = dict(os.environ, PATH=os.path.dirname(FFMPEG) + os.pathsep + os.environ.get("PATH", ""))
    r = subprocess.run([VENV_PY, os.path.join(HERE, "sdclip.py"), args[0], CFG["cameras"][cam]["ip"], *args[1:]],
                       capture_output=True, text=True, timeout=timeout, creationflags=NOWIN, env=env, cwd=HERE)
    lines = [l for l in r.stdout.splitlines() if l.startswith("{")]
    if not lines:
        raise RuntimeError("The memory-card download gave no answer. " + clean(r.stderr[-200:]))
    out = json.loads(lines[-1])
    if not out.get("ok"):
        raise RuntimeError(out.get("error") or out.get("status") or "download failed")
    return out


def sd_clip(order, cam, start, end):
    name = f"{safe_id(order)}_packing_{cam}_{start:%Y%m%d-%H%M}.mp4"
    out = os.path.join(order_dir(order), name)
    sd(cam, "get", str(int(start.timestamp())), str(int(end.timestamp())), out)
    with open(os.path.join(order_dir(order), "evidence-log.txt"), "a", encoding="utf8") as f:
        f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S}  packing  {name}  {start:%Y-%m-%d %H:%M} to {end:%H:%M} (memory card)\n")
    EV_CACHE[0] = 0
    log(f"Packing clip saved: {out}")
    return name


# ---------------------------------------------------------------- snapshots
SNAP_LOCK = threading.Lock()
SNAPS = {}


def snapshot(cam):
    if cam == CFG["returns_camera"] and UNBOX.state().get("recording"):
        p = os.path.join(HERE, "tmp", "preview_rec.jpg")
        if os.path.exists(p):
            return open(p, "rb").read()
    with SNAP_LOCK:
        t, data = SNAPS.get(cam, (0, None))
        if data and time.time() - t < 2:
            return data
        r = subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-rtsp_transport", "tcp",
                            "-i", rtsp(cam, "stream2"), "-frames:v", "1", "-q:v", "5", "-f", "image2", "-"],
                           capture_output=True, timeout=15, creationflags=NOWIN)
        if r.returncode or not r.stdout:
            raise RuntimeError(r.stderr.decode("utf8", "replace")[-200:] or "no picture")
        SNAPS[cam] = (time.time(), r.stdout)
        return r.stdout


# ---------------------------------------------------------------- data for the page
def returns_list():
    p = os.path.join(SAVE, "_sitpretty", "returns.json")
    try:
        return json.load(open(p, encoding="utf8"))
    except Exception:
        return []


EV_CACHE = [0, []]


def evidence():
    if time.time() - EV_CACHE[0] < 5:
        return EV_CACHE[1]
    EV_CACHE[1] = _evidence()
    EV_CACHE[0] = time.time()
    return EV_CACHE[1]


def _evidence():
    out = []
    if not os.path.isdir(SAVE):
        return out
    for name in sorted(os.listdir(SAVE), reverse=True):
        d = os.path.join(SAVE, name)
        if name.startswith("_") or not os.path.isdir(d):
            continue
        files = [f for f in sorted(os.listdir(d)) if f.lower().endswith(".mp4")]
        if files:
            out.append({"order": name, "files": files, "mtime": os.path.getmtime(d)})
    out.sort(key=lambda x: -x["mtime"])
    return out[:200]




# ---------------------------------------------------------------- http
class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def authed(self):
        pin = secrets().get("pin", "")
        if not pin:
            return True
        q = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(self.path).query))
        return self.headers.get("X-Pin", "") == pin or q.get("pin") == pin

    def send(self, code, body, ctype="application/json"):
        if isinstance(body, (dict, list)):
            body = clean(json.dumps(body)).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = dict(urllib.parse.parse_qsl(u.query))
        try:
            if u.path in ("/", "/index.html"):
                return self.send(200, open(os.path.join(HERE, "web", "index.html"), "rb").read(), "text/html; charset=utf-8")
            if not self.authed():
                return self.send(401, {"error": "PIN needed"})
            if u.path == "/api/state":
                s = secrets()
                return self.send(200, {
                    "unbox": UNBOX.state(), "returns": returns_list(), "evidence": evidence(),
                    "cameras": CFG["cameras"], "returns_camera": CFG["returns_camera"],
                    "packing_cameras": CFG["packing_cameras"], "save_dir": SAVE,
                    "login_ok": all(all(cam_login(c, s)) for c in CFG["cameras"]),
                    "sd_ready": bool(s.get("tapo_app_password")) and os.path.isfile(VENV_PY),
                    "pack_before": CFG["default_pack_minutes_before_scan"], "log": LOGS[-30:]})
            if u.path == "/api/snapshot.jpg":
                try:
                    return self.send(200, snapshot(q.get("cam", CFG["returns_camera"])), "image/jpeg")
                except Exception as e:
                    return self.send(503, {"error": str(e)})
            if u.path == "/media":
                order, name = safe_id(q.get("order")), os.path.basename(q.get("file", ""))
                p = os.path.join(SAVE, order, name)
                if not (name.endswith(".mp4") and os.path.isfile(p)):
                    return self.send(404, {"error": "not found"})
                return self.stream(p)
            return self.send(404, {"error": "not found"})
        except Exception as e:
            return self.send(500, {"error": str(e)})

    def stream(self, p):
        size = os.path.getsize(p)
        rng = self.headers.get("Range")
        start, end = 0, size - 1
        if rng and rng.startswith("bytes="):
            a, b = rng[6:].split("-")
            start = int(a or 0)
            end = int(b) if b else size - 1
        self.send_response(206 if rng else 200)
        self.send_header("Content-Type", "video/mp4")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        if rng:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        with open(p, "rb") as f:
            f.seek(start)
            left = end - start + 1
            while left > 0:
                chunk = f.read(min(1 << 20, left))
                if not chunk:
                    break
                try:
                    self.wfile.write(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    break
                left -= len(chunk)

    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        try:
            if not self.authed():
                return self.send(401, {"error": "PIN needed"})
            b = self.body()
            if u.path == "/api/unbox/start":
                return self.send(200, UNBOX.start(b.get("order"), b.get("request", ""), b.get("note", "")))
            if u.path == "/api/unbox/stop":
                return self.send(200, UNBOX.stop())
            if u.path == "/api/packclip":
                start = datetime.strptime(b["start"], "%Y-%m-%dT%H:%M")
                end = datetime.strptime(b["end"], "%Y-%m-%dT%H:%M")
                if end <= start or end - start > timedelta(hours=2):
                    raise ValueError("Pick an end time after the start, at most 2 hours later")
                made, errs = [], []
                for cam in b.get("cams") or CFG["packing_cameras"]:
                    try:
                        made.append(sd_clip(b["order"], cam, start, end))
                    except Exception as e:
                        errs.append(f"{CFG['cameras'][cam]['label']}: {clean(e)}")
                if not made:
                    raise RuntimeError("; ".join(errs))
                return self.send(200, {"files": made, "errors": errs})
            if u.path == "/api/sdcheck":
                cam = b.get("cam")
                out = sd(cam, "list", datetime.now().strftime("%Y%m%d"), timeout=60)
                recs = out.get("recordings", [])
                first = datetime.fromtimestamp(recs[0][0]).strftime("%H:%M") if recs else None
                return self.send(200, {"cam": cam, "count": len(recs), "first": first})
            return self.send(404, {"error": "not found"})
        except Exception as e:
            return self.send(400, {"error": str(e)})


def main():
    os.makedirs(os.path.join(HERE, "tmp"), exist_ok=True)
    srv = ThreadingHTTPServer(("0.0.0.0", CFG["port"]), H)
    log(f"Sit Pretty on http://localhost:{CFG['port']}  (saving to {SAVE})")
    srv.serve_forever()


if __name__ == "__main__":
    main()
