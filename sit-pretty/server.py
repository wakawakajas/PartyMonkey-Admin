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
def retime(src, dst):
    """The cameras send frames in bursts over the network (10 at once, then nothing for ~0.5 s), so a live
    recording plays jerky. Every frame is there, so space them evenly over the same length of time.
    Falls back to the recording as it is if anything goes wrong."""
    try:
        fp = os.path.join(os.path.dirname(FFMPEG), "ffprobe.exe" if os.name == "nt" else "ffprobe")
        r = subprocess.run([fp, "-v", "error", "-select_streams", "v", "-count_packets", "-show_entries",
                            "stream=codec_name,nb_read_packets:format=duration", "-of", "json", src],
                           capture_output=True, text=True, timeout=120, creationflags=NOWIN)
        info = json.loads(r.stdout)
        codec = info["streams"][0]["codec_name"]
        frames = int(info["streams"][0]["nb_read_packets"])
        secs = float(info["format"]["duration"])
        if codec not in ("hevc", "h264") or frames < 10 or secs <= 0:
            raise ValueError(f"cannot retime {codec}")
        es = src + "." + codec
        subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-i", src, "-map", "0:v", "-c", "copy",
                        "-bsf:v", f"{codec}_mp4toannexb", "-f", codec, es], check=True, timeout=600, creationflags=NOWIN)
        subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-r", f"{frames}/{secs:.4f}", "-i", es,
                        "-c", "copy", "-movflags", "+faststart", dst], check=True, timeout=600, creationflags=NOWIN)
        os.remove(es)
        if os.path.getsize(dst) < 10000:
            raise ValueError("retimed file is empty")
        os.remove(src)
        return True
    except Exception as e:
        log(f"Could not smooth the recording ({clean(e)}); keeping it as recorded")
        if os.path.exists(dst):
            os.remove(dst)
        shutil.move(src, dst)
        return False


class Unboxer:
    def __init__(self):
        self.proc = None
        self.info = None
        self.lock = threading.Lock()

    def start(self, order, request="", note="", cam=None):
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
            cam = cam if cam in CFG["cameras"] else CFG["returns_camera"]
            cmd = [FFMPEG, "-hide_banner", "-loglevel", "error", "-rtsp_transport", "tcp",
                   "-i", rtsp(cam),
                   "-map", "0:v", "-c:v", "copy", "-an",
                   "-movflags", "+faststart", tmp,
                   "-map", "0:v", "-vf", "fps=1,scale=640:-2", "-update", "1", "-q:v", "6", "-y", prev]
            self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=NOWIN)
            self.info = {"order": order, "request": request, "note": note, "tmp": tmp, "final": final, "cam": cam,
                         "started": time.time()}
            log(f"Unboxing started for {order} on {CFG['cameras'][cam]['label']}")
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
            retime(info["tmp"], info["final"])
            secs = int(time.time() - info["started"])
            meta = os.path.join(os.path.dirname(info["final"]), "evidence-log.txt")
            with open(meta, "a", encoding="utf8") as f:
                f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S}  unboxing  {os.path.basename(info['final'])}  {secs}s"
                        f"  request={info['request']}  note={info['note']}\n")
            EV_CACHE[0] = 0
            log(f"Unboxing saved: {info['final']} ({secs}s)")
            return {"order": info["order"], "file": info["final"], "seconds": secs, "request": info["request"],
                    "cam": info["cam"]}

    def state(self):
        if self.proc and self.proc.poll() is None:
            return {"recording": True, "order": self.info["order"], "request": self.info["request"], "cam": self.info["cam"],
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
    tmp = os.path.join(HERE, "tmp", "sd_" + name)
    os.makedirs(os.path.dirname(tmp), exist_ok=True)
    sd(cam, "get", str(int(start.timestamp())), str(int(end.timestamp())), tmp)
    r = subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-i", tmp, "-map", "0:v", "-c", "copy", "-an",
                        "-movflags", "+faststart", out], capture_output=True, creationflags=NOWIN, timeout=600)
    os.remove(tmp)
    if r.returncode:
        raise RuntimeError("Could not save the clip: " + r.stderr.decode("utf8", "replace")[-200:])
    with open(os.path.join(order_dir(order), "evidence-log.txt"), "a", encoding="utf8") as f:
        f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S}  packing  {name}  {start:%Y-%m-%d %H:%M} to {end:%H:%M} (memory card)\n")
    EV_CACHE[0] = 0
    log(f"Packing clip saved: {out}")
    return name


# ---------------------------------------------------------------- snapshots
SNAP_LOCK = threading.Lock()
SNAPS = {}


def snapshot(cam):
    st = UNBOX.state()
    if st.get("recording") and cam == st.get("cam"):
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


def read_waybill(cam):
    """Staff hold the return waybill up to a table camera; read every barcode in a full-size frame."""
    cam = cam if cam in CFG["cameras"] else CFG["returns_camera"]
    try:
        import zxingcpp
        from PIL import Image
    except ImportError:
        raise RuntimeError("The barcode reader is missing: run start-sit-pretty.bat once")
    r = subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-rtsp_transport", "tcp",
                        "-i", rtsp(cam, "stream1"), "-frames:v", "1", "-q:v", "2", "-f", "image2", "-"],
                       capture_output=True, timeout=25, creationflags=NOWIN)
    if r.returncode or not r.stdout:
        raise RuntimeError("Could not get a picture from the camera. " + clean(r.stderr.decode("utf8", "replace")[-150:]))
    import io
    im = Image.open(io.BytesIO(r.stdout)).convert("L")
    codes = []

    def take(img):
        for bc in zxingcpp.read_barcodes(img):
            t = (bc.text or "").strip()
            if t and t not in codes:
                codes.append(t)

    take(im)
    if not codes:  # small barcode: look at overlapping tiles, each enlarged 2x
        tw, th = im.width // 3, im.height // 2
        for x in range(0, im.width - tw + 1, tw // 2):
            for y in range(0, im.height - th + 1, th // 2):
                take(im.crop((x, y, x + tw, y + th)).resize((tw * 2, th * 2), Image.LANCZOS))
    log(f"Waybill scan on {CFG['cameras'][cam]['label']}: {', '.join(codes) or 'no barcode found'}")
    return {"codes": codes}


# ---------------------------------------------------------------- data for the page
# ---------------------------------------------------------------- packing area + Shopee copies
LOCAL = os.path.join(HERE, "sitpretty-local.json")   # this PC's own settings, git-ignored
DEFAULT_AREAS = {  # fractions of the picture: the table only, not people or the walkway
    "returns": {"x": 0.06, "y": 0.17, "w": 0.90, "h": 0.80},
    "pack-183": {"x": 0.22, "y": 0.24, "w": 0.64, "h": 0.68},
    "pack-121": {"x": 0.20, "y": 0.22, "w": 0.40, "h": 0.78},     # Party table left
    "pack-121-r": {"x": 0.55, "y": 0.22, "w": 0.40, "h": 0.78},   # Party table right (same camera)
}
SHOPEE_MAX_MB = 29.0   # Seller Centre: 1 minute and 30 MB per video, MP4/MOV
SHOPEE_SECS = 59


def local_cfg():
    try:
        return json.load(open(LOCAL, encoding="utf8"))
    except Exception:
        return {}


def areas():
    a = dict(DEFAULT_AREAS)
    a.update(local_cfg().get("areas", {}))
    return a


def save_area(cam, box):
    if cam not in CFG["cameras"]:
        raise ValueError("unknown camera")
    box = {k: round(min(1, max(0, float(box[k]))), 4) for k in ("x", "y", "w", "h")}
    if box["w"] < 0.1 or box["h"] < 0.1:
        raise ValueError("The box is too small")
    c = local_cfg()
    c.setdefault("areas", {})[cam] = box
    json.dump(c, open(LOCAL, "w", encoding="utf8"), indent=2)
    return box


def cam_of(fname):
    m = re.search(r"_packing_([a-z0-9-]+)_\d{8}", fname)
    if m and m.group(1) in CFG["cameras"]:
        return m.group(1)
    return CFG["returns_camera"]


def duration(p):
    probe = os.path.join(os.path.dirname(FFMPEG), "ffprobe.exe" if os.name == "nt" else "ffprobe")
    r = subprocess.run([probe if os.path.exists(probe) else "ffprobe", "-v", "error", "-show_entries", "format=duration",
                        "-of", "csv=p=0", p], capture_output=True, text=True, creationflags=NOWIN)
    return float(r.stdout.strip() or 0)


def shopee_copy(order, fname, mode, start=0.0):
    """A copy Seller Centre accepts: one minute or less, under 30 MB, H.264 MP4, cropped to the packing area.
    mode "from": the minute starting at `start` seconds; "fast": the whole video sped up into one minute."""
    order = safe_id(order)
    src = os.path.join(SAVE, order, os.path.basename(fname))
    if not os.path.isfile(src):
        raise RuntimeError("Video not found")
    a = areas()[cam_of(fname)]
    crop = f"crop=iw*{a['w']}:ih*{a['h']}:iw*{a['x']}:ih*{a['y']},scale=-2:'min(1080,ih)'"
    base = os.path.splitext(os.path.basename(fname))[0]
    total = duration(src)
    if mode == "fast":
        factor = max(1.0, total / SHOPEE_SECS)
        out = os.path.join(SAVE, order, f"{base}_shopee_fast{int(round(factor))}x.mp4")
        pre, vf, audio = [], f"{crop},setpts=PTS/{factor:.3f},fps=30", ["-an"]
    else:
        start = max(0.0, min(float(start), max(0.0, total - 5)))
        out = os.path.join(SAVE, order, f"{base}_shopee_{int(start)//60:02d}m{int(start)%60:02d}s.mp4")
        pre, vf, audio = ["-ss", f"{start:.1f}"], crop, ["-an"]
    for kbps in (3500, 2000, 1200):
        cmd = [FFMPEG, "-hide_banner", "-loglevel", "error", "-y", *pre, "-i", src, "-t", str(SHOPEE_SECS),
               "-vf", vf, "-c:v", "libx264", "-preset", "veryfast", "-b:v", f"{kbps}k", "-maxrate", f"{kbps}k",
               "-bufsize", f"{kbps * 2}k", "-pix_fmt", "yuv420p", *audio, "-movflags", "+faststart", out]
        r = subprocess.run(cmd, capture_output=True, creationflags=NOWIN, timeout=900)
        if r.returncode:
            raise RuntimeError("Could not make the Shopee copy: " + r.stderr.decode("utf8", "replace")[-200:])
        if os.path.getsize(out) <= SHOPEE_MAX_MB * 1e6:
            break
    mb = round(os.path.getsize(out) / 1e6, 1)
    with open(os.path.join(SAVE, order, "evidence-log.txt"), "a", encoding="utf8") as f:
        f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S}  shopee copy  {os.path.basename(out)}  {mb} MB  from {os.path.basename(fname)}\n")
    EV_CACHE[0] = 0
    log(f"Shopee copy saved: {out} ({mb} MB)")
    return {"file": os.path.basename(out), "mb": mb}


def overview():
    try:
        return json.load(open(os.path.join(SAVE, "_sitpretty", "overview.json"), encoding="utf8"))
    except Exception:
        return None


# ---------------------------------------------------------------- evidence video requests (Claude Code makes them)
JOB_CACHE = [0, []]


def request_video(b):
    order = safe_id(b.get("order"))
    cams = [c for c in (b.get("cams") or []) if c in CFG["cameras"]]
    if not cams:
        raise ValueError("Tick at least one camera that filmed the packing")
    scan = (b.get("scan_time") or "").strip()
    if scan:
        datetime.strptime(scan, "%Y-%m-%dT%H:%M")
    p = os.path.join(order_dir(order), "evidence-request.json")
    job = {"order": order, "request": b.get("request", ""), "cams": cams,
           "camera_ips": {c: CFG["cameras"][c]["ip"] for c in cams}, "scan_time": scan,
           "note": (b.get("note") or "")[:300], "areas": {c: areas()[c] for c in cams},
           "requested_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "status": "queued"}
    json.dump(job, open(p, "w", encoding="utf8"), indent=1)
    JOB_CACHE[0] = 0
    log(f"Evidence video requested for {order} ({', '.join(cams)})")
    job["now"] = kick_claude()
    return job


# ---------------------------------------------------------------- Claude Code makes the videos straight away
REPO = os.path.dirname(HERE)
CLAUDE_EXE = shutil.which("claude") or os.path.join(os.path.expanduser("~"), ".local", "bin", "claude.exe")
AUTO = {"proc": None, "again": False, "state": "idle"}   # idle / running / signin / missing
AUTO_LOCK = threading.Lock()
# Unboxing videos are simpler (no BigSeller lookup, no packing count), so they use the cheaper Sonnet model;
# packing videos keep the default (stronger) model because the item counting needs care.
AUTO_KINDS = [("refresh", "sonnet"), ("unboxing-video-request.json", "sonnet"), ("evidence-request.json", None)]
REFRESH_PROMPT = (
    "Staff pressed \"Refresh from Shopee\" in Sit Pretty. Read sit-pretty/evidence/README.md, section "
    "\"Returns list refresh\", and the daily task it points to "
    "(C:\\Users\\Admin\\.claude\\scheduled-tasks\\shopee-return-claims-refresh\\SKILL.md), and do ONLY the "
    "returns-list refresh now: rebuild N:\\Yong Chuan\\Dispute\\_sitpretty\\returns.json from Shopee Seller Centre "
    "in Claude in Chrome (returns that are approved, refunded, closed or cancelled drop off the list), then delete "
    "refresh-request.json. Do not make any videos and do not update the Claims Desk artifact. "
    "Never type passwords. In Seller Centre only read: never click Refund, Dispute, Accept or anything that changes "
    "an order. Use sit-pretty\\.venv\\Scripts\\python.exe for Python. Finish with one line: how many open returns.")
AUTO_PROMPT = (
    "Make the queued Sit Pretty video requests now. Read sit-pretty/evidence/README.md and follow it exactly for "
    "every N:\\Yong Chuan\\Dispute\\*\\{fname} whose status is "
    "\"queued\" (oldest first; skip any that say \"working\"; leave every other kind of request alone). Use sit-pretty\\.venv\\Scripts\\python.exe for Python. "
    "Never type passwords. In Shopee Seller Centre and BigSeller only read: never click Refund, Dispute, Accept, Ship "
    "or anything that changes an order. If a packing request needs the BigSeller scan time and Chrome is not "
    "available, leave that request queued for the hourly task. Delete temporary files when done. "
    "Finish with one line per request.")


def auto_state():
    if not os.path.isfile(CLAUDE_EXE):
        return "missing"
    return AUTO["state"]


def queued(fname):
    if fname == "refresh":
        return os.path.exists(REFRESH_REQ)
    if not os.path.isdir(SAVE):
        return False
    for name in os.listdir(SAVE):
        p = os.path.join(SAVE, name, fname)
        if os.path.isfile(p):
            try:
                if json.load(open(p, encoding="utf8")).get("status") == "queued":
                    return True
            except Exception:
                pass
    return False


def _run_claude():
    while True:
        AUTO["again"] = False
        for fname, model in AUTO_KINDS:
            if queued(fname) and not _run_claude_once(fname, model):
                return
        if not AUTO["again"]:
            AUTO["state"] = "idle"
            return


def _run_claude_once(fname, model):
    if True:
        out_p = os.path.join(HERE, "tmp", "claude-run.log")
        with open(out_p, "w", encoding="utf8") as out:
            cmd = [CLAUDE_EXE, "-p", (REFRESH_PROMPT if fname == "refresh" else AUTO_PROMPT.replace("{fname}", fname)), "--chrome", "--permission-mode", "acceptEdits",
                   "--add-dir", SAVE]
            if model:
                cmd += ["--model", model]
            cmd += ["--allowedTools", "Bash", "Read", "Write", "Edit", "Glob", "Grep", "mcp__claude-in-chrome"]
            try:
                p = subprocess.Popen(cmd, cwd=REPO, stdout=out, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                     creationflags=NOWIN)
            except Exception as e:
                AUTO["state"] = "missing"
                log(f"Could not start Claude Code: {e}")
                return False
            AUTO["proc"], AUTO["state"] = p, "running"
            kind = "returns-list refresh" if fname == "refresh" else "unboxing" if fname.startswith("unboxing") else "packing"
            log(f"Claude Code started making the {kind} videos ({model or 'default model'})")
            rc = p.wait()
        tail = open(out_p, encoding="utf8", errors="replace").read()[-3000:]
        JOB_CACHE[0] = 0
        if "Not logged in" in tail or "/login" in tail:
            AUTO["state"] = "signin"
            log("Claude Code is not signed in on this PC, so the hourly task will make the video. "
                "Sign in once: open a terminal, run claude, type /login")
            return False
        log(f"Claude Code finished the {kind} requests (exit {rc}). " + clean(tail.strip().splitlines()[-1][:200] if tail.strip() else ""))
        return True


def kick_claude():
    """Start Claude Code now for queued requests. Returns True if it should be making them right away."""
    if not os.path.isfile(CLAUDE_EXE):
        return False
    with AUTO_LOCK:
        if AUTO["proc"] and AUTO["proc"].poll() is None:
            AUTO["again"] = True
            return True
        was = AUTO["state"]
        threading.Thread(target=_run_claude, daemon=True).start()
        return was != "signin"


def request_unbox_video(b):
    """Staff want a labelled copy of an unboxing recording (made by Claude, like the packing videos)."""
    order = safe_id(b.get("order"))
    d = order_dir(order)
    video = os.path.basename(str(b.get("video") or ""))
    if not video or not os.path.isfile(os.path.join(d, video)):
        vids = sorted(n for n in os.listdir(d) if "_unboxing_" in n and n.endswith(".mp4"))
        if not vids:
            raise ValueError("There is no unboxing recording for this order yet")
        video = vids[-1]
    opts = {k: bool(b.get(k)) for k in ("count", "highlight_items", "highlight_weight", "missing")}
    if not any(opts.values()):
        raise ValueError("Tick at least one thing to show")
    cam = b.get("cam") if b.get("cam") in CFG["cameras"] else CFG["returns_camera"]
    job = {"kind": "unboxing", "order": order, "request": b.get("request", ""), "video": video, "cams": [cam],
           **opts, "missing_text": str(b.get("missing_text") or "")[:300], "note": str(b.get("note") or "")[:300],
           "areas": {cam: areas()[cam]}, "requested_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
           "status": "queued"}
    json.dump(job, open(os.path.join(d, "unboxing-video-request.json"), "w", encoding="utf8"), indent=1,
              ensure_ascii=False)
    JOB_CACHE[0] = 0
    log(f"Unboxing video requested for {order}")
    job["now"] = kick_claude()
    return job


def jobs():
    if time.time() - JOB_CACHE[0] < 5:
        return JOB_CACHE[1]
    out = []
    if os.path.isdir(SAVE):
        for name in os.listdir(SAVE):
            for fname in ("evidence-request.json", "unboxing-video-request.json"):
                p = os.path.join(SAVE, name, fname)
                if os.path.isfile(p):
                    try:
                        j = json.load(open(p, encoding="utf8"))
                        j["mtime"] = os.path.getmtime(p)
                        j.setdefault("kind", "packing")
                        out.append(j)
                    except Exception:
                        pass
    out.sort(key=lambda j: -j["mtime"])
    JOB_CACHE[:] = [time.time(), out[:100]]
    return JOB_CACHE[1]


REFRESH_REQ = os.path.join(SAVE, "_sitpretty", "refresh-request.json")


def ask_refresh():
    os.makedirs(os.path.dirname(REFRESH_REQ), exist_ok=True)
    json.dump({"requested_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}, open(REFRESH_REQ, "w", encoding="utf8"))
    log("Returns list refresh requested")
    return {"ok": True, "now": kick_claude()}


def returns_info():
    p = os.path.join(SAVE, "_sitpretty", "returns.json")
    upd = datetime.fromtimestamp(os.path.getmtime(p)).strftime("%d %b %H:%M") if os.path.exists(p) else None
    pending = None
    if os.path.exists(REFRESH_REQ):
        try:
            pending = json.load(open(REFRESH_REQ, encoding="utf8")).get("requested_at")
        except Exception:
            pending = "yes"
    return {"updated": upd, "refresh_pending": pending}


FOUND_OPTS = {  # what staff saw when the return was unboxed (used by Claude for the appeal)
    "used": "Item has been used",
    "opened": "Seal or packaging opened (buyer said sealed)",
    "missing": "Items missing from the return",
    "wrong_item": "Wrong item sent back (not ours)",
    "damaged": "Damaged after delivery (by the buyer)",
    "dirty": "Dirty, stained or smells",
    "photo_mismatch": "Buyer's photo or video is not our item / not this order",
    "fine": "Came back fine, nothing wrong",
}
FOUND_CACHE = {}


def finding(order):
    p = os.path.join(SAVE, safe_id(order), "findings.json")
    try:
        m = os.path.getmtime(p)
    except OSError:
        return None
    c = FOUND_CACHE.get(p)
    if not c or c[0] != m:
        try:
            c = (m, json.load(open(p, encoding="utf8")))
        except Exception:
            c = (m, None)
        FOUND_CACHE[p] = c
    return c[1]


def save_finding(order, issues, note="", by=""):
    order = safe_id(order)
    issues = [i for i in (issues or []) if i in FOUND_OPTS]
    note = str(note or "").strip()[:1000]
    if not issues and not note:
        raise RuntimeError("Tick what you found, or write a note")
    f = {"order": order, "issues": issues, "labels": [FOUND_OPTS[i] for i in issues], "note": note,
         "by": str(by or "")[:40], "at": datetime.now().strftime("%Y-%m-%d %H:%M")}
    d = order_dir(order)
    json.dump(f, open(os.path.join(d, "findings.json"), "w", encoding="utf8"), ensure_ascii=False, indent=1)
    with open(os.path.join(d, "evidence-log.txt"), "a", encoding="utf8") as fh:
        fh.write(f"{datetime.now():%Y-%m-%d %H:%M:%S}  found  {', '.join(f['labels'])}  note={note}\n")
    log(f"Findings saved for {order}: {', '.join(f['labels']) or note[:40]}")
    return f


# ---------------------------------------------------------------- the appeal message for Shopee
# Written from what staff ticked after unboxing (findings.json), the buyer's words, the items and the
# evidence videos in the order folder. Plain facts only; staff paste it into Shopee's Dispute box.
APPEAL_CACHE = {}


def _read_json(p):
    try:
        return json.load(open(p, encoding="utf8"))
    except Exception:
        return None


def _missing_items(row, d):
    """Which item(s) did not come back, from the unboxing request / summary (e.g. 'Matte Honey missing')."""
    texts = []
    for fn in ("unboxing-video-request.json",):
        j = _read_json(os.path.join(d, fn)) or {}
        texts += [j.get("missing_text") or "", j.get("summary") or ""]
    f = finding(row.get("order", "")) or {}
    texts.append(f.get("note") or "")
    segs = [s.strip().lower() for t in texts for s in re.split(r"[;,.]|\band\b", t) if s.strip()]
    out = []
    for it in row.get("items") or []:
        name = it.get("model") or it.get("name") or ""
        if name and any(name.lower() in s and ("missing" in s or "not returned" in s or "no " + name.lower() in s) for s in segs):
            out.append(name)
    return out


def _videos(d):
    try:
        names = sorted(os.listdir(d))
    except OSError:
        return [], []
    unbox = [n for n in names if n.endswith(".mp4") and "_unboxing_labelled_" in n]
    if not unbox:
        unbox = [n for n in names if n.endswith(".mp4") and "_unboxing_" in n]
    j = _read_json(os.path.join(d, "evidence-request.json")) or {}
    pack = [n for n in (j.get("videos") or []) if n in names] if j.get("status") == "done" else []
    if not pack:
        pack = [n for n in names if n.endswith(".mp4") and "_packing_" in n and re.search(r"video\d+of\d+", n)]
    return unbox, pack


def appeal_text(row):
    order = row.get("order", "")
    d = os.path.join(SAVE, safe_id(order))
    try:
        key = (os.path.getmtime(d), row.get("status"), row.get("buyer_text"), json.dumps(finding(order)))
    except OSError:
        return None
    c = APPEAL_CACHE.get(order)
    if c and c[0] == key:
        return c[1]
    f = finding(order) or {}
    issues = f.get("issues") or []
    if not issues or issues == ["fine"]:
        APPEAL_CACHE[order] = (key, None)
        return None
    items = row.get("items") or []
    n_sent = sum(int(i.get("qty") or 1) for i in items) or len(items)
    names = [(i.get("model") or i.get("name") or "").strip() for i in items]
    unbox, pack = _videos(d)
    miss = _missing_items(row, d)

    claim = f'as "{row["reason"]}"' if row.get("reason") else ""
    said = f', saying: "{row["buyer_text"].strip()}"' if (row.get("buyer_text") or "").strip() else ""
    lines = [f"The buyer returned this order {claim}{said}.".replace("order  ", "order ").replace(" ,", ",")]
    facts = []
    if "opened" in issues:
        sealed = "sealed" in (row.get("reason") or "").lower() or "original" in (row.get("reason") or "").lower()
        facts.append("The return parcel arrived already opened" + (", although the buyer said it was in original / sealed condition." if sealed else "."))
    if "missing" in issues:
        if miss:
            got = [n for n in names if n and n not in miss]
            facts.append(f"Only {n_sent - len(miss)} of {n_sent} items came back"
                         + (f" ({', '.join(got)} returned)" if got else "") + f". Missing: {', '.join(miss)}.")
        else:
            facts.append("Not every item came back: " + (f.get("note") or "items were missing from the return parcel") + ".")
    if "used" in issues:
        facts.append("The item came back used, so it is not in the original condition required for a return.")
    if "damaged" in issues:
        facts.append("The item came back damaged. It left us in good condition, as our packing video shows.")
    if "dirty" in issues:
        facts.append("The item came back dirty, stained or smelling, so it cannot be resold.")
    if "wrong_item" in issues:
        facts.append("The item sent back is not the item we sent.")
    if "photo_mismatch" in issues:
        facts.append("The buyer's photos / videos do not show our item or this order.")
    if f.get("note") and "missing" not in issues:
        facts.append(f"We also found: {f['note'].strip().rstrip('.')}.")
    lines += facts
    ev = []
    if unbox:
        ev.append("Our unboxing video shows the return parcel opened in one take, from the waybill to the end"
                  + (", with " + ", ".join(miss) + " not inside" if miss else "") + ".")
    if pack:
        ev.append(f"Our packing video shows all {n_sent} item{'s' if n_sent != 1 else ''}"
                  + (f" ({', '.join(n for n in names if n)})" if 1 < len(names) <= 4 else "")
                  + " packed and sealed before shipping.")
    lines += ev
    if "wrong_item" in issues or "photo_mismatch" in issues:
        ask = "We request that the refund be rejected."
    elif "missing" in issues and not set(issues) & {"used", "damaged", "dirty"}:
        ask = ("We request that the refund be limited to the item actually returned, or that the buyer returns "
               + (", ".join(miss) if miss else "the missing item") + ".")
    else:
        ask = "We request that the refund be rejected or reduced, as the item was not returned in the condition it was sent."
    lines.append(ask)
    text = " ".join(lines)
    APPEAL_CACHE[order] = (key, text)
    return text


def returns_list():
    p = os.path.join(SAVE, "_sitpretty", "returns.json")
    try:
        rows = json.load(open(p, encoding="utf8"))
    except Exception:
        return []
    for row in rows:
        row["found"] = finding(row.get("order", ""))
        try:
            row["appeal"] = appeal_text(row)
        except Exception as e:
            log(f"Could not write the appeal for {row.get('order')}: {clean(e)}")
    return rows


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
                    "pack_before": CFG["default_pack_minutes_before_scan"], "areas": areas(), "overview": overview(), "jobs": jobs(), "returns_info": returns_info(), "pigu_link": bool(LINK and LINK.connected), "found_opts": FOUND_OPTS, "auto_videos": auto_state(), "log": LOGS[-30:]})
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
                return self.send(200, UNBOX.start(b.get("order"), b.get("request", ""), b.get("note", ""), b.get("cam")))
            if u.path == "/api/unbox/stop":
                return self.send(200, UNBOX.stop())
            if u.path == "/api/packclip":
                start = datetime.strptime(b["start"], "%Y-%m-%dT%H:%M")
                end = datetime.strptime(b["end"], "%Y-%m-%dT%H:%M")
                if end <= start or end - start > timedelta(hours=2):
                    raise ValueError("Pick an end time after the start, at most 2 hours later")
                made, errs, by_ip = [], [], {}
                for cam in b.get("cams") or CFG["packing_cameras"]:
                    try:
                        ip = CFG["cameras"][cam]["ip"]
                        if ip in by_ip:  # two table views of one camera: download once, copy for the second view
                            first_cam, first = by_ip[ip]
                            name = first.replace(f"_packing_{first_cam}_", f"_packing_{cam}_")
                            shutil.copy(os.path.join(order_dir(b["order"]), first), os.path.join(order_dir(b["order"]), name))
                            made.append(name)
                            continue
                        name = sd_clip(b["order"], cam, start, end)
                        by_ip[ip] = (cam, name)
                        made.append(name)
                    except Exception as e:
                        errs.append(f"{CFG['cameras'][cam]['label']}: {clean(e)}")
                if not made:
                    raise RuntimeError("; ".join(errs))
                return self.send(200, {"files": made, "errors": errs})
            if u.path == "/api/readwb":
                return self.send(200, read_waybill(b.get("cam")))
            if u.path == "/api/unbox-video":
                return self.send(200, request_unbox_video(b))
            if u.path == "/api/finding":
                return self.send(200, save_finding(b.get("order"), b.get("issues"), b.get("note"), b.get("by")))
            if u.path == "/api/refresh-returns":
                return self.send(200, ask_refresh())
            if u.path == "/api/evidence":
                return self.send(200, request_video(b))
            if u.path == "/api/shopee":
                return self.send(200, shopee_copy(b.get("order"), b.get("file", ""), b.get("mode", "from"), b.get("start", 0)))
            if u.path == "/api/area":
                return self.send(200, save_area(b.get("cam"), b.get("box") or {}))
            if u.path == "/api/sdcheck":
                cam = b.get("cam")
                out = sd(cam, "list", datetime.now().strftime("%Y%m%d"), timeout=60)
                recs = out.get("recordings", [])
                first = datetime.fromtimestamp(recs[0][0]).strftime("%H:%M") if recs else None
                return self.send(200, {"cam": cam, "count": len(recs), "first": first})
            return self.send(404, {"error": "not found"})
        except Exception as e:
            return self.send(400, {"error": str(e)})


LINK = None


def main():
    global LINK
    os.makedirs(os.path.join(HERE, "tmp"), exist_ok=True)
    import pigulink
    LINK = pigulink.start(CFG, secrets().get("pin", ""), {
        "cfg": CFG, "unbox_state": UNBOX.state, "start": UNBOX.start, "stop": UNBOX.stop,
        "snapshot": snapshot, "returns": returns_list, "returns_info": returns_info,
        "ask_refresh": ask_refresh, "clean": clean, "save_finding": save_finding, "read_waybill": read_waybill, "unbox_video": request_unbox_video, "pack_video": request_video, "found_opts": FOUND_OPTS}, log)
    srv = ThreadingHTTPServer(("0.0.0.0", CFG["port"]), H)
    log(f"Sit Pretty on http://localhost:{CFG['port']}  (saving to {SAVE})")
    srv.serve_forever()


if __name__ == "__main__":
    main()
