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
import footage
import live

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
            try:
                make_view(info["order"], os.path.basename(info["final"]))
            except Exception:
                pass
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
    if footage.has(cam, start, end):
        footage.clip(cam, start, end, out)
        with open(os.path.join(order_dir(order), "evidence-log.txt"), "a", encoding="utf8") as f:
            f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S}  packing  {name}  {start:%Y-%m-%d %H:%M} to {end:%H:%M} (NAS footage)\n")
        EV_CACHE[0] = 0
        log(f"Packing clip saved from the NAS footage: {out}")
        return name
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
SNAP_CAM_LOCKS = {}


def snapshot(cam):
    lf = live.recent(cam)   # already streaming live: no extra connection to the camera
    if lf:
        return lf
    st = UNBOX.state()
    if st.get("recording") and cam == st.get("cam"):
        p = os.path.join(HERE, "tmp", "preview_rec.jpg")
        if os.path.exists(p):
            return open(p, "rb").read()
    with SNAP_LOCK:   # one grab per camera at a time; different cameras in parallel
        lk = SNAP_CAM_LOCKS.setdefault(cam, threading.Lock())
    with lk:
        t, data = SNAPS.get(cam, (0, None))
        if data and time.time() - t < 4:
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
    a = {c: {"x": 0.1, "y": 0.1, "w": 0.8, "h": 0.8} for c in CFG["cameras"]}
    a.update(DEFAULT_AREAS)
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
           "before_min": pack_minutes(b)[0], "after_min": pack_minutes(b)[1],
           "requested_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "status": "queued"}
    json.dump(job, open(p, "w", encoding="utf8"), indent=1)
    JOB_CACHE[0] = 0
    log(f"Evidence video requested for {order} ({', '.join(cams)})")
    job["now"] = kick_claude()
    return job


def pack_clips(order, start, end, cams=None, raise_all=True):
    """Download the packing minutes from each table camera's memory card into the order folder."""
    if end <= start or end - start > timedelta(hours=2):
        raise ValueError("Pick an end time after the start, at most 2 hours later")
    made, errs, by_ip = [], [], {}
    for cam in cams or CFG["packing_cameras"]:
        try:
            ip = CFG["cameras"][cam]["ip"]
            if ip in by_ip:  # two table views of one camera: download once, copy for the second view
                first_cam, first = by_ip[ip]
                name = first.replace(f"_packing_{first_cam}_", f"_packing_{cam}_")
                shutil.copy(os.path.join(order_dir(order), first), os.path.join(order_dir(order), name))
                made.append(name)
                continue
            name = sd_clip(order, cam, start, end)
            by_ip[ip] = (cam, name)
            made.append(name)
        except Exception as e:
            errs.append(f"{CFG['cameras'][cam]['label']}: {clean(e)}")
    if not made and raise_all:
        raise RuntimeError("; ".join(errs))
    return {"files": made, "errors": errs}


CLIP_REQ = "packclip-request.json"


def pack_minutes(b):
    """Staff choose how many minutes before and after the 'Scan to ship' time the footage covers."""
    def num(k, d, lo, hi):
        try:
            return max(lo, min(hi, int(float(b.get(k)))))
        except (TypeError, ValueError):
            return d
    return (num("before", CFG.get("default_pack_minutes_before_scan", 5), 1, 60),
            num("after", CFG.get("default_pack_minutes_after_scan", 2), 0, 30))


def request_packclip(b):
    """Order ID only: Claude reads the 'Scan to ship' time in BigSeller, then Sit Pretty downloads the clip."""
    order = safe_id(b.get("order"))
    cams = [c for c in (b.get("cams") or CFG["packing_cameras"]) if c in CFG["cameras"]]
    if not cams:
        raise ValueError("Tick at least one table")
    before, after = pack_minutes(b)
    job = {"kind": "clip", "order": order, "cams": cams, "requested_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
           "status": "queued", "before": before, "after": after}
    scan = (b.get("scan_time") or "").strip()
    if scan:   # the BigSeller pop-up read the scan time already: download straight away, no Claude
        datetime.strptime(scan, "%Y-%m-%dT%H:%M")
        job.update(scan_time=scan, status="time_found", progress="Downloading from the camera's memory card")
        json.dump(job, open(os.path.join(order_dir(order), CLIP_REQ), "w", encoding="utf8"), indent=1)
        JOB_CACHE[0] = 0
        log(f"Packing clip asked for {order} (scan to ship {scan})")
        threading.Thread(target=fetch_found_clips, daemon=True).start()
        job["now"] = True
        return job
    json.dump(job, open(os.path.join(order_dir(order), CLIP_REQ), "w", encoding="utf8"), indent=1)
    JOB_CACHE[0] = 0
    log(f"Packing clip asked for {order}: Claude looks up the scan time first")
    job["now"] = kick_claude()
    return job


# ---------------------------------------------------------------- packing clips that play while they download
# The memory card only hands footage over as a download, so it is fetched one minute at a time; each minute gets a
# small playable copy straight away (the pop-up plays them in order), and at the end the minutes are joined into the
# full 4K clip plus one playable copy.
LIVE_LOCK = threading.Lock()


def encode_view(src, dst, cam):
    a = areas().get(cam) if cam else None
    vf = "fps=15," + (f"crop=iw*{a['w']}:ih*{a['h']}:iw*{a['x']}:ih*{a['y']}," if a else "") + "scale=960:-2"
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    tmp = dst + ".part.mp4"
    r = subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-hwaccel", "auto", "-i", src, "-vf", vf,
                        "-c:v", "libx264", "-preset", "veryfast", "-crf", "30", "-g", "30", "-an", "-movflags", "+faststart", tmp],
                       capture_output=True, timeout=1800, creationflags=NOWIN)
    if r.returncode:
        raise RuntimeError(r.stderr.decode("utf8", "replace")[-200:])
    os.replace(tmp, dst)


def _concat(parts, out, extra=()):
    lst = out + ".txt"
    with open(lst, "w", encoding="utf8") as f:
        for p_ in parts:
            f.write("file '" + p_.replace("\\", "/").replace("'", "'\\''") + "'\n")
    r = subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-f", "concat", "-safe", "0", "-i", lst,
                        "-map", "0:v", "-c", "copy", "-an", *extra, "-movflags", "+faststart", out],
                       capture_output=True, timeout=900, creationflags=NOWIN)
    os.remove(lst)
    if r.returncode:
        raise RuntimeError(r.stderr.decode("utf8", "replace")[-200:])


def sd_clip_live(order, cam, start, end, job, save):
    """Like sd_clip, but minute by minute, with each minute playable as soon as it is in."""
    order = safe_id(order)
    name = f"{order}_packing_{cam}_{start:%Y%m%d-%H%M}.mp4"
    base = name[:-4]
    out = os.path.join(order_dir(order), name)
    if footage.has(cam, start, end):
        job["progress"] = "Cutting the clip from the NAS footage"
        save()
        try:
            footage.clip(cam, start, end, out)
            job["progress"] = "Making a playable copy"
            save()
            try:
                encode_view(out, view_path(order, name), cam)
            except Exception as e:
                log(f"Could not make {name} playable: {clean(e)}")
            with open(os.path.join(order_dir(order), "evidence-log.txt"), "a", encoding="utf8") as f:
                f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S}  packing  {name}  {start:%Y-%m-%d %H:%M} to {end:%H:%M} (NAS footage)\n")
            EV_CACHE[0] = 0
            log(f"Packing clip saved from the NAS footage: {out}")
            return name
        except Exception as e:
            log(f"NAS footage failed for {name}, using the memory card: {clean(e)}")
    n = max(1, int(-(-(end - start).total_seconds() // 60)))
    raws, views, errs = [], [], []
    job.update(live=[], live_total=n, live_cam=cam)
    save()
    enc = []
    for i in range(n):
        a = start + timedelta(minutes=i)
        b = min(end, a + timedelta(minutes=1))
        tmp = os.path.join(HERE, "tmp", f"sd_{base}_p{i:02d}.mp4")
        for tries in range(4):
            try:
                sd(cam, "get", str(int(a.timestamp())), str(int(b.timestamp())), tmp)
                break
            except Exception as e:
                msg = str(e).lower()
                if ("busy" in msg or "in progress" in msg) and tries < 3:
                    job["progress"] = f"Minute {i + 1} of {n}: the camera is busy, trying again in 1 minute"
                    save()
                    time.sleep(60)
                    continue
                errs.append(f"{a:%H:%M}: {clean(e)}")   # quiet minutes have no footage on the card
                tmp = None
                break
        if not tmp:
            continue
        raws.append(tmp)
        vname = f"{base}_p{i:02d}.mp4"
        dst = view_path(order, vname)

        def work(src=tmp, dst=dst, vname=vname):
            try:
                encode_view(src, dst, cam)
                with LIVE_LOCK:
                    job["live"] = sorted(job["live"] + [vname])
                    save()
            except Exception as e:
                log(f"Could not make minute {vname} playable: {clean(e)}")
        t = threading.Thread(target=work, daemon=True)
        t.start()
        enc.append(t)
        views.append(dst)
        job["progress"] = f"Downloading minute {i + 2 if i + 1 < n else n} of {n} (you can watch the first minutes already)"
        save()
    for t in enc:
        t.join()
    if not raws:
        raise RuntimeError("No footage on the memory card for that time" + (f" ({errs[0]})" if errs else ""))
    job["progress"] = "Joining the minutes into one clip"
    save()
    _concat(raws, out)
    have_views = [v for v in views if os.path.isfile(v)]
    if have_views:
        try:
            _concat(have_views, view_path(order, name))
        except Exception as e:
            log(f"Could not join the playable minutes for {name}: {clean(e)}")
    for f_ in raws + have_views:
        try:
            os.remove(f_)
        except OSError:
            pass
    with open(os.path.join(order_dir(order), "evidence-log.txt"), "a", encoding="utf8") as f:
        f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S}  packing  {name}  {start:%Y-%m-%d %H:%M} to {end:%H:%M} (memory card)\n")
    EV_CACHE[0] = 0
    log(f"Packing clip saved: {out}")
    return name


def fetch_found_clips():
    """Download the clips whose scan time Claude has found (status "time_found")."""
    if not os.path.isdir(SAVE):
        return
    for name in os.listdir(SAVE):
        p = os.path.join(SAVE, name, CLIP_REQ)
        if not os.path.isfile(p):
            continue
        j = _read_json(p) or {}
        if j.get("status") != "time_found":
            continue
        try:
            scan = datetime.strptime(j["scan_time"], "%Y-%m-%dT%H:%M")
            before, after = pack_minutes(j)
            start = scan - timedelta(minutes=before)
            end = scan + timedelta(minutes=after)
            ready = end + timedelta(seconds=75)   # the camera hands over footage about a minute after it is recorded
            fs = footage.status()
            if fs["recording"] or fs["other_pc"]:
                ready = end + timedelta(seconds=20)   # the NAS files are readable while they are being written
            j["status"] = "downloading"
            if datetime.now() < ready:
                j["progress"] = f"Waiting until {ready:%H:%M} for the camera to finish recording, then downloading"
                json.dump(j, open(p, "w", encoding="utf8"), indent=1)
                JOB_CACHE[0] = 0
                time.sleep(max(0, (ready - datetime.now()).total_seconds()))
            j["progress"] = "Downloading minute 1"

            def save(j=j, p=p):
                json.dump(j, open(p, "w", encoding="utf8"), indent=1)
                JOB_CACHE[0] = 0
            save()
            made, errs, by_ip = [], [], {}
            for cam in j.get("cams") or CFG["packing_cameras"]:
                try:
                    ip = CFG["cameras"][cam]["ip"]
                    if ip in by_ip:   # the other half of the same camera: copy, don't download twice
                        first_cam, first = by_ip[ip]
                        nm = first.replace(f"_packing_{first_cam}_", f"_packing_{cam}_")
                        shutil.copy(os.path.join(order_dir(j["order"]), first), os.path.join(order_dir(j["order"]), nm))
                        if os.path.isfile(view_path(j["order"], first)):
                            shutil.copy(view_path(j["order"], first), view_path(j["order"], nm))
                        made.append(nm)
                        continue
                    nm = sd_clip_live(j["order"], cam, start, end, j, save)
                    by_ip[ip] = (cam, nm)
                    made.append(nm)
                except Exception as e:
                    errs.append(f"{CFG['cameras'][cam]['label']}: {clean(e)}")
            r = {"files": made, "errors": errs}
            for k in ("live", "live_total", "live_cam"):
                j.pop(k, None)
            if not r["files"]:
                raise RuntimeError("; ".join(r["errors"]))
            j.pop("progress", None)
            j.update(status="done", files=r["files"], videos=r["files"],
                     summary=f"Scan to ship {scan:%d %b %H:%M}; clip {start:%H:%M}-{end:%H:%M}"
                     + (" · not saved: " + "; ".join(r["errors"]) if r["errors"] else ""))
            log(f"Packing clip saved for {j['order']}: {', '.join(r['files'])}")
            for f_ in r["files"]:
                make_view(j["order"], f_)   # no-op when the live download already made it
        except Exception as e:
            j.pop("progress", None)
            j.update(status="problem", problem=f"Could not download the clip: {clean(e)}")
            log(f"Packing clip for {j.get('order')} failed: {clean(e)}")
        json.dump(j, open(p, "w", encoding="utf8"), indent=1)
        JOB_CACHE[0] = 0


# ---------------------------------------------------------------- Claude Code makes the videos straight away
REPO = os.path.dirname(HERE)
CLAUDE_EXE = shutil.which("claude") or os.path.join(os.path.expanduser("~"), ".local", "bin", "claude.exe")
AUTO = {"proc": None, "again": False, "state": "idle"}   # idle / running / signin / missing
AUTO_LOCK = threading.Lock()
# Unboxing videos are simpler (no BigSeller lookup, no packing count), so they use the cheaper Sonnet model;
# packing videos keep the default (stronger) model because the item counting needs care.
CLIP_PROMPT = (
    "Staff want raw packing clips. For every N:\\Yong Chuan\\Dispute\\*\\packclip-request.json whose status is "
    "\"queued\" (leave every other kind of request alone): open the order in BigSeller in Claude in Chrome (All Orders, "
    "search the order number, Order Details, Order Log) and read the time of \"Scan to ship on APP\". Write it into "
    "that request file as \"scan_time\": \"YYYY-MM-DDTHH:MM\" (Singapore time) and, if the same table's previous ship "
    "scan shows when this order's packing started, \"start_time\" in the same format; then set \"status\": "
    "\"time_found\". If the order has no ship scan or cannot be found, set \"status\": \"problem\" and \"problem\" "
    "to one plain sentence for staff. Do NOT download footage or make videos: Sit Pretty downloads the clip itself. "
    "Never type passwords. In BigSeller only read: never click Ship, Print or anything that changes an order. "
    "Finish with one line per request.")
AUTO_KINDS = [("refresh", "sonnet"), (CLIP_REQ, "sonnet"), ("count-request.json", "sonnet"), ("unboxing-video-request.json", "sonnet"), ("evidence-request.json", None)]
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
            if fname == CLIP_REQ:
                threading.Thread(target=fetch_found_clips, daemon=True).start()
        if not AUTO["again"]:
            AUTO["state"] = "idle"
            return


def _run_claude_once(fname, model):
    if True:
        out_p = os.path.join(HERE, "tmp", "claude-run.log")
        with open(out_p, "w", encoding="utf8") as out:
            cmd = [CLAUDE_EXE, "-p", (REFRESH_PROMPT if fname == "refresh" else CLIP_PROMPT if fname == CLIP_REQ else AUTO_PROMPT.replace("{fname}", fname)), "--chrome", "--permission-mode", "acceptEdits",
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
            kind = "returns-list refresh" if fname == "refresh" else "packing-clip time" if fname == CLIP_REQ else "unboxing" if fname.startswith("unboxing") else "packing"
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
            for fname in ("evidence-request.json", "unboxing-video-request.json", CLIP_REQ, COUNT_REQ):
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

    # Owner's wording (13 Nov): full sentences, our facts, the videos and the ask; no retelling of the buyer's
    # claim (Shopee has it). Shopee's dispute box takes at most 256 characters, so every sentence has shorter
    # wordings, used (longest first) until the whole appeal fits.
    LIMIT = 256
    sealed = any(w in (row.get("reason") or "").lower() for w in ("sealed", "original"))
    m = ", ".join(miss)
    got = [n for n in names if n and n not in miss]
    both = "both items" if n_sent == 2 else f"all {n_sent} items"
    parts = []   # each: wordings from longest to shortest
    if "missing" in issues:
        if miss:
            parts.append([f"Only {n_sent - len(miss)} of {n_sent} items came back" + (f" ({', '.join(got)} returned)" if got else "") + f". Missing: {m}.",
                          f"Only {n_sent - len(miss)} of {n_sent} items came back. Missing: {m}.",
                          f"{m} missing."])
        else:
            parts.append(["Not every item came back in the return parcel.", "Items missing."])
    elif "opened" in issues:
        parts.append(["The return parcel arrived already opened" + (", although the buyer said it was sealed." if sealed else "."),
                      "The return parcel arrived opened."])
    for k, long_, short in (("used", "The item came back used, so it cannot be returned.", "Item returned used."),
                            ("damaged", "The item came back damaged; it left us in good condition.", "Item returned damaged."),
                            ("dirty", "The item came back dirty or stained.", "Item returned dirty."),
                            ("wrong_item", "The item sent back is not the item we sent.", "Wrong item returned."),
                            ("photo_mismatch", "The buyer's photos do not show our item or this order.", "Buyer's photos are not our item.")):
        if k in issues:
            parts.append([long_, short])
    if unbox:
        parts.append([f"Our unboxing video shows the return parcel opened in one take, from the waybill to the end" + (f", with {m} not inside." if miss else "."),
                      f"Our unboxing video shows {m} was not inside." if miss else "Our unboxing video shows the parcel opened in one take.",
                      f"Our unboxing video shows {m} missing." if miss else "Our unboxing video shows the opening."])
    if pack:
        parts.append([f"Our packing video shows all {n_sent} item{'s' if n_sent != 1 else ''}"
                      + (f" ({', '.join(n for n in names if n)})" if 1 < len(names) <= 4 else "") + " packed and sealed before shipping.",
                      f"Our packing video shows {both} packed and sealed.",
                      f"Our packing video shows {both} packed."])
    if "wrong_item" in issues or "photo_mismatch" in issues:
        parts.append(["We request that the refund be rejected.", "Please reject the refund."])
    elif "missing" in issues and not set(issues) & {"used", "damaged", "dirty"}:
        parts.append([f"We request that the refund be limited to the item actually returned, or that the buyer returns {m or 'the missing item'}.",
                      f"We request a refund for the returned item only, or the buyer returns {m or 'the missing item'}.",
                      "Please refund only the returned item."])
    else:
        parts.append(["We request that the refund be rejected or reduced, as the item was not returned in the condition it was sent.",
                      "We request that the refund be rejected or reduced.", "Please reject or reduce the refund."])
    # shorten one step at a time, videos first, then the request, our facts last; stop as soon as it fits
    n_parts = len(parts)
    kinds = ["fact"] * n_parts
    k_ = n_parts - 1
    kinds[k_] = "ask"
    if pack:
        k_ -= 1
        kinds[k_] = "pack"
    if unbox:
        k_ -= 1
        kinds[k_] = "unbox"
    order_ = [i_ for kind in ("pack", "unbox", "ask", "fact") for i_ in range(n_parts) if kinds[i_] == kind]
    pick = [0] * n_parts
    lines = [p_[0] for p_ in parts]
    for level in (1, 2):
        for i_ in order_:
            if len(" ".join(lines)) <= LIMIT:
                break
            if level < len(parts[i_]):
                pick[i_] = level
                lines[i_] = parts[i_][level]
    while len(" ".join(lines)) > LIMIT and len(lines) > 1:
        lines.pop(-2)   # still too long: drop sentences before the ask, never cut one in half
    text = " ".join(lines)
    APPEAL_CACHE[order] = (key, text)
    return text


# ---------------------------------------------------------------- one order's videos (the BigSeller pop-up)
COUNT_REQ = "count-request.json"
DIR_CACHE = [0, set()]


def order_folders():
    if time.time() - DIR_CACHE[0] > 30:
        try:
            DIR_CACHE[:] = [time.time(), set(os.listdir(SAVE))]
        except OSError:
            DIR_CACHE[:] = [time.time(), set()]
    return DIR_CACHE[1]


def video_kind(name):
    """simple (raw packing clip), count (simple count), detailed (labelled packing), unboxing, or other."""
    n = name.lower()
    if "_unboxing_" in n:
        return "unboxing"
    if "_count_" in n:
        return "count"
    if "_packing_" in n and re.search(r"_video\d+of\d+", n):
        return "detailed"
    if "_packing_" in n and "_shopee" not in n:
        return "simple"
    return "other"


def video_label(name):
    n, k = name, video_kind(name)
    cam = next((c for c in CFG["cameras"] if f"_{c}_" in n), None)
    table = CFG["cameras"][cam]["label"] if cam else ""
    part = re.search(r"video(\d+)of(\d+)", n)
    part = f" {part.group(1)}/{part.group(2)}" if part else ""
    if k == "unboxing":
        return ("Unboxing, labelled" if "_labelled_" in n else "Unboxing recording") + part
    if k == "count":
        return f"Simple count · {table}{part}"
    if k == "detailed":
        return f"Detailed packing · {table}{part}"
    if k == "simple":
        t = re.search(r"_(\d{8})-(\d{4})", n)
        when = f" · from {t.group(2)[:2]}:{t.group(2)[2:]}" if t else ""
        return f"Packing clip · {table}{when}"
    return n


def have(orders):
    """How many videos each order has (for the camera button colour on BigSeller)."""
    folders, out = order_folders(), {}
    for o in (orders or [])[:400]:
        o = safe_id(o)
        if o not in folders:
            continue
        try:
            out[o] = sum(1 for n in os.listdir(os.path.join(SAVE, o)) if n.endswith(".mp4"))
        except OSError:
            pass
    return out


def order_info(order):
    order = safe_id(order)
    d = os.path.join(SAVE, order)
    vids = []
    if os.path.isdir(d):
        for n in sorted(os.listdir(d)):
            if n.endswith(".mp4"):
                p = os.path.join(d, n)
                vids.append({"file": n, "kind": video_kind(n), "label": video_label(n), "view": view_state(order, n),
                             "mb": round(os.path.getsize(p) / 1e6, 1), "at": datetime.fromtimestamp(os.path.getmtime(p)).strftime("%d %b %H:%M")})
    photos = []
    if os.path.isdir(d):
        for n in sorted(os.listdir(d)):
            if n.endswith(".jpg") and "_photo_" in n:
                p = os.path.join(d, n)
                photos.append({"file": n, "label": photo_label(n), "mb": round(os.path.getsize(p) / 1e6, 1),
                               "at": datetime.fromtimestamp(os.path.getmtime(p)).strftime("%d %b %H:%M")})
    reqs = []
    for fn in ("evidence-request.json", "unboxing-video-request.json", CLIP_REQ, COUNT_REQ):
        j = _read_json(os.path.join(d, fn))
        if j:
            j.setdefault("kind", "packing")
            reqs.append({k: j.get(k) for k in ("kind", "status", "progress", "problem", "summary", "requested_at", "scan_time", "cams", "clip",
                                               "live", "live_total")})
    row = next((r for r in returns_list() if r.get("order") == order), None)
    return {"order": order, "videos": vids, "photos": photos, "jobs": reqs, "return": {k: row.get(k) for k in ("request", "status", "reason", "due", "todo", "appeal")} if row else None,
            "packing_cameras": [[c, CFG["cameras"][c]["label"]] for c in CFG["packing_cameras"]], "auto_videos": auto_state()}


def request_count(b):
    """Simple count: Claude adds only an ITEMS PACKED counter to a packing clip that is already downloaded."""
    order = safe_id(b.get("order"))
    clip = os.path.basename(b.get("clip") or "")
    if not (clip.endswith(".mp4") and os.path.isfile(os.path.join(SAVE, order, clip))):
        raise ValueError("Get the packing clip first")
    cam = next((c for c in CFG["cameras"] if f"_{c}_" in clip), CFG["packing_cameras"][0])
    job = {"kind": "count", "order": order, "clip": clip, "cam": cam, "area": areas().get(cam),
           "scan_time": b.get("scan_time") or "", "requested_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "status": "queued"}
    json.dump(job, open(os.path.join(order_dir(order), COUNT_REQ), "w", encoding="utf8"), indent=1)
    JOB_CACHE[0] = 0
    log(f"Simple count asked for {order} ({clip})")
    job["now"] = kick_claude()
    return job


# ---------------------------------------------------------------- playable copies of the 4K H.265 recordings
# Chrome on the shop PCs cannot play the cameras' 4K H.265 files, so each raw clip / unboxing recording gets a
# small H.264 copy (cropped to the table, 960 px, 15 fps) kept in _sitpretty\view. Download still gives the original.
VIEW_DIR = os.path.join(SAVE, "_sitpretty", "view")
VIEW_Q, VIEW_BUSY = [], set()
VIEW_LOCK = threading.Lock()


def needs_view(name):
    k = video_kind(name)
    return k == "simple" or (k == "unboxing" and "_labelled_" not in name)


def view_path(order, name):
    return os.path.join(VIEW_DIR, safe_id(order), os.path.basename(name))


def view_state(order, name):
    if not needs_view(name):
        return "ok"
    if os.path.isfile(view_path(order, name)):
        return "ready"
    return "making" if (safe_id(order), name) in VIEW_BUSY else "none"


def _view_worker():
    while True:
        with VIEW_LOCK:
            if not VIEW_Q:
                return
            order, name = VIEW_Q.pop(0)
        src, dst = os.path.join(SAVE, order, name), view_path(order, name)
        try:
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            cam = next((c for c in CFG["cameras"] if f"_{c}_" in name), None)
            a = areas().get(cam) if cam else None
            vf = "fps=15," + (f"crop=iw*{a['w']}:ih*{a['h']}:iw*{a['x']}:ih*{a['y']}," if a else "") + "scale=960:-2"
            tmp = dst + ".part.mp4"
            r = subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-hwaccel", "auto", "-i", src, "-vf", vf,
                                "-c:v", "libx264", "-preset", "veryfast", "-crf", "30", "-g", "30", "-an", "-movflags", "+faststart", tmp],
                               capture_output=True, timeout=1800, creationflags=NOWIN)
            if r.returncode:
                raise RuntimeError(r.stderr.decode("utf8", "replace")[-200:])
            os.replace(tmp, dst)
        except Exception as e:
            log(f"Could not make a playable copy of {name}: {clean(e)}")
        finally:
            VIEW_BUSY.discard((order, name))


def tidy_views():
    """Remove playable copies whose original was deleted (staff delete order folders on the NAS)."""
    try:
        for order in os.listdir(VIEW_DIR):
            d = os.path.join(VIEW_DIR, order)
            for n in os.listdir(d):
                if not os.path.isfile(os.path.join(SAVE, order, n)) and (order, n) not in VIEW_BUSY:
                    os.remove(os.path.join(d, n))
            if not os.listdir(d):
                os.rmdir(d)
    except OSError:
        pass


def make_view(order, name):
    """Queue a playable copy (about 2 minutes for 15 minutes of footage)."""
    order, name = safe_id(order), os.path.basename(name)
    if not needs_view(name) or os.path.isfile(view_path(order, name)) or (order, name) in VIEW_BUSY:
        return view_state(order, name)
    if not os.path.isfile(os.path.join(SAVE, order, name)):
        raise ValueError("Video not found")
    with VIEW_LOCK:
        VIEW_BUSY.add((order, name))
        VIEW_Q.append((order, name))
        start = len(VIEW_Q) == 1 and len(VIEW_BUSY) == 1
    if start:
        threading.Thread(target=_view_worker, daemon=True).start()
    return "making"


# ---------------------------------------------------------------- the camera list, shared by every shop PC
# config.json has the starting cameras; names, order, which ones pack, and added cameras live on the NAS
# (_sitpretty\cameras.json), so a rename on one PC shows on all of them.
CAMS_SHARED = os.path.join(SAVE, "_sitpretty", "cameras.json")
BASE_CAMS = {"cameras": {k: dict(v) for k, v in CFG["cameras"].items()}, "packing": list(CFG["packing_cameras"])}
CAM_MTIME = [False]
CAM_LOCK = threading.Lock()


def apply_cameras(force=False):
    try:
        m = os.path.getmtime(CAMS_SHARED)
    except OSError:
        m = None
    if m == CAM_MTIME[0] and not force:
        return
    CAM_MTIME[0] = m
    s = _read_json(CAMS_SHARED) or {}
    cams = {k: dict(v) for k, v in BASE_CAMS["cameras"].items()}
    for k, v in (s.get("cameras") or {}).items():
        cams[k] = dict(cams.get(k, {}), **v)
    for k in s.get("removed") or []:
        if k != CFG["returns_camera"]:
            cams.pop(k, None)
    cams = {k: v for k, v in cams.items() if v.get("ip")}
    order = [k for k in (s.get("order") or []) if k in cams]
    order += [k for k in cams if k not in order]
    packing = s.get("packing") if s.get("packing") is not None else BASE_CAMS["packing"]
    CFG["cameras"] = {k: cams[k] for k in order}
    CFG["packing_cameras"] = [k for k in order if k in packing]
    CFG["live_order"] = [k for k in (s.get("live_order") or []) if k in cams]


def edit_cameras(b):
    apply_cameras()
    act, cam = b.get("action"), b.get("cam")
    dup_area = None
    with CAM_LOCK:
        s = _read_json(CAMS_SHARED) or {}
        order = list(CFG["cameras"])
        if act == "live_order":   # the Live page's own order (long press and drag), the same on every PC
            s["live_order"] = [k for k in (b.get("order") or []) if k in CFG["cameras"]]
            cam = None
        elif act != "add" and cam not in CFG["cameras"]:
            raise ValueError("Unknown camera")
        elif act == "rename":
            label = " ".join(str(b.get("label") or "").split())[:40]
            if not label:
                raise ValueError("Type a name")
            s.setdefault("cameras", {}).setdefault(cam, {})["label"] = label
        elif act == "move":
            i = order.index(cam)
            j = i - 1 if b.get("dir") == "up" else i + 1
            if 0 <= j < len(order):
                order[i], order[j] = order[j], order[i]
            s["order"] = order
        elif act == "record":   # all-day NAS recording for this camera (off = live view only)
            s.setdefault("cameras", {}).setdefault(cam, {})["record"] = bool(b.get("on"))
        elif act == "packing":
            pk = set(CFG["packing_cameras"])
            pk.add(cam) if b.get("on") else pk.discard(cam)
            s["packing"] = [k for k in order if k in pk]
        elif act == "add":
            ip = str(b.get("ip") or "").strip()
            if not re.fullmatch(r"192\.168\.\d{1,3}\.\d{1,3}", ip):
                raise ValueError("Pick a camera from the list")
            label = " ".join(str(b.get("label") or "").split())[:40] or f"Camera {ip.rsplit('.', 1)[1]}"
            base = "cam-" + ip.rsplit(".", 1)[1]
            new, n = base, 2
            while new in CFG["cameras"] or new in BASE_CAMS["cameras"]:
                new, n = f"{base}-{n}", n + 1
            s.setdefault("cameras", {})[new] = {"ip": ip, "label": label}
            if b.get("record") is False:
                s["cameras"][new]["record"] = False
            s["removed"] = [k for k in s.get("removed") or [] if k != new]
            s["order"] = order + [new]
            if b.get("packing", True):
                s["packing"] = list(CFG["packing_cameras"]) + [new]
            cam = new
        elif act == "duplicate":   # another view (own table box) of the same camera, placed right after it
            src = CFG["cameras"][cam]
            base = cam + "-copy"
            new, n = base, 2
            while new in CFG["cameras"] or new in BASE_CAMS["cameras"]:
                new, n = f"{base}{n}", n + 1
            s.setdefault("cameras", {})[new] = {"ip": src["ip"], "label": (src["label"] + " 2")[:40]}
            s["removed"] = [k for k in s.get("removed") or [] if k != new]
            s["order"] = order[:order.index(cam) + 1] + [new] + order[order.index(cam) + 1:]
            if cam in CFG["packing_cameras"]:
                s["packing"] = list(CFG["packing_cameras"]) + [new]
            dup_area = areas().get(cam)
            cam = new
        elif act == "remove":
            if cam == CFG["returns_camera"]:
                raise ValueError("This is the unboxing camera; it cannot be removed")
            s.setdefault("removed", []).append(cam)
            s["order"] = [k for k in order if k != cam]
            s["packing"] = [k for k in CFG["packing_cameras"] if k != cam]
        else:
            raise ValueError("Unknown change")
        os.makedirs(os.path.dirname(CAMS_SHARED), exist_ok=True)
        json.dump(s, open(CAMS_SHARED, "w", encoding="utf8"), indent=1)
        apply_cameras(force=True)
        if act == "duplicate" and dup_area:
            save_area(cam, dup_area)
    log(f"Cameras: {act} {CFG['cameras'].get(cam, {}).get('label', cam)}")
    return {"cameras": CFG["cameras"], "packing_cameras": CFG["packing_cameras"], "cam": cam}


DISCOVER = {"running": False, "at": 0, "found": []}


def _discover():
    import socket
    from concurrent.futures import ThreadPoolExecutor
    try:
        mine = {v["ip"] for v in CFG["cameras"].values()}
        prefix = next(iter(mine)).rsplit(".", 1)[0] if mine else "192.168.10"

        def rtsp_open(ip):
            try:
                with socket.create_connection((ip, 554), timeout=1.2):
                    return ip
            except OSError:
                return None
        with ThreadPoolExecutor(64) as ex:
            ips = [ip for ip in ex.map(rtsp_open, [f"{prefix}.{i}" for i in range(1, 255)]) if ip]
        found = []
        for ip in ips:
            info = {"ip": ip, "name": "", "model": "", "added": ip in mine}
            if not info["added"]:
                try:
                    r = subprocess.run([VENV_PY, os.path.join(HERE, "sdclip.py"), "info", ip], capture_output=True, text=True,
                                       timeout=40, creationflags=NOWIN, cwd=HERE)
                    j = json.loads([l for l in r.stdout.splitlines() if l.startswith("{")][-1])
                    info.update(name=j.get("name") or "", model=j.get("model") or "")
                except Exception:
                    pass
            found.append(info)
        DISCOVER.update(found=found, at=time.time())
    finally:
        DISCOVER["running"] = False


def discover(again=False):
    if not DISCOVER["running"] and (again or time.time() - DISCOVER["at"] > 600):
        DISCOVER["running"] = True
        threading.Thread(target=_discover, daemon=True).start()
    return DISCOVER


# ---------------------------------------------------------------- 4K photos for Shopee (waybill, outer packaging)
PHOTO_KINDS = {"waybill": "Parcel with its waybill", "packaging": "Outer packaging", "item": "Item"}


def save_photo(b):
    """A full-size JPG for Shopee: a moment of a saved recording (file + t seconds), or the live camera now."""
    order = safe_id(b.get("order"))
    kind = b.get("kind") if b.get("kind") in PHOTO_KINDS else "item"
    d = order_dir(order)
    n = 1
    while os.path.exists(os.path.join(d, f"{order}_photo_{kind}_{n}.jpg")):
        n += 1
    out = os.path.join(d, f"{order}_photo_{kind}_{n}.jpg")
    if b.get("file"):
        src = os.path.join(SAVE, order, os.path.basename(b["file"]))
        if not (src.endswith(".mp4") and os.path.isfile(src)):
            raise ValueError("Video not found")
        t = max(0.0, float(b.get("t") or 0))
        cmd = [FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{t:.3f}", "-i", src, "-frames:v", "1", "-q:v", "1", out]
    else:
        cam = b.get("cam") if b.get("cam") in CFG["cameras"] else CFG["returns_camera"]
        cmd = [FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-rtsp_transport", "tcp", "-i", rtsp(cam, "stream1"),
               "-frames:v", "1", "-q:v", "1", out]
    r = subprocess.run(cmd, capture_output=True, timeout=40, creationflags=NOWIN)
    if r.returncode or not os.path.isfile(out):
        raise RuntimeError("Could not save the photo. " + clean(r.stderr.decode("utf8", "replace")[-150:]))
    with open(os.path.join(d, "evidence-log.txt"), "a", encoding="utf8") as f:
        f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S}  photo  {os.path.basename(out)}  "
                + (f"from {os.path.basename(b['file'])} at {float(b.get('t') or 0):.1f}s" if b.get("file") else "live camera") + "\n")
    log(f"Photo saved: {os.path.basename(out)}")
    return {"file": os.path.basename(out), "label": photo_label(os.path.basename(out))}


def photo_label(name):
    m = re.search(r"_photo_([a-z]+)_(\d+)\.jpg$", name)
    return f"{PHOTO_KINDS.get(m.group(1), 'Photo')} {m.group(2)}" if m else name


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
        apply_cameras()
        try:
            if u.path in ("/", "/index.html"):
                return self.send(200, open(os.path.join(HERE, "web", "index.html"), "rb").read(), "text/html; charset=utf-8")
            if u.path == "/order":   # the pop-up the BigSeller extension opens
                return self.send(200, open(os.path.join(HERE, "web", "order.html"), "rb").read(), "text/html; charset=utf-8")
            if not self.authed():
                return self.send(401, {"error": "PIN needed"})
            if u.path == "/api/state":
                s = secrets()
                return self.send(200, {
                    "unbox": UNBOX.state(), "returns": returns_list(), "evidence": evidence(),
                    "cameras": CFG["cameras"], "returns_camera": CFG["returns_camera"],
                    "packing_cameras": CFG["packing_cameras"], "save_dir": SAVE,
                    "login_ok": all(cam_login(CFG["returns_camera"], s)),
                    "cam_login": {c: all(cam_login(c, s)) for c in CFG["cameras"]},
                    "sd_ready": bool(s.get("tapo_app_password")) and os.path.isfile(VENV_PY),
                    "pack_before": CFG["default_pack_minutes_before_scan"], "pack_after": CFG.get("default_pack_minutes_after_scan", 2), "areas": areas(), "overview": overview(), "jobs": jobs(), "returns_info": returns_info(), "pigu_link": bool(LINK and LINK.connected), "found_opts": FOUND_OPTS, "auto_videos": auto_state(), "live": footage.live_list(), "log": LOGS[-30:]})
            if u.path == "/api/footage":
                return self.send(200, footage.status())
            if u.path == "/api/discover":
                return self.send(200, discover(q.get("again") == "1"))
            if u.path == "/api/order":
                return self.send(200, order_info(q.get("order")))
            if u.path == "/api/live.jpg":
                cam = q.get("cam")
                if cam not in CFG["cameras"]:
                    return self.send(404, {"error": "Unknown camera"})
                try:
                    data, seq = live.frame(cam, int(q.get("after") or 0))
                except Exception as e:
                    return self.send(503, {"error": clean(e)})
                self.send_response(200)
                self.send_header("Content-Type", "image/jpeg")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Seq", str(seq))
                self.end_headers()
                self.wfile.write(data)
                return
            if u.path == "/api/snapshot.jpg":
                try:
                    return self.send(200, snapshot(q.get("cam", CFG["returns_camera"])), "image/jpeg")
                except Exception as e:
                    return self.send(503, {"error": str(e)})
            if u.path == "/photo":
                order, name = safe_id(q.get("order")), os.path.basename(q.get("file", ""))
                p = os.path.join(SAVE, order, name)
                if not (name.endswith(".jpg") and "_photo_" in name and os.path.isfile(p)):
                    return self.send(404, {"error": "not found"})
                data = open(p, "rb").read()
                self.send_response(200)
                self.send_header("Content-Type", "image/jpeg")
                self.send_header("Content-Length", str(len(data)))
                if q.get("dl") == "1":
                    self.send_header("Content-Disposition", f'attachment; filename="{name}"')
                self.end_headers()
                self.wfile.write(data)
                return
            if u.path == "/media":
                order, name = safe_id(q.get("order")), os.path.basename(q.get("file", ""))
                p = view_path(order, name) if q.get("view") == "1" else os.path.join(SAVE, order, name)
                if not (name.endswith(".mp4") and os.path.isfile(p)):
                    return self.send(404, {"error": "not found"})
                return self.stream(p, q.get("dl") == "1")
            return self.send(404, {"error": "not found"})
        except Exception as e:
            return self.send(500, {"error": str(e)})

    def stream(self, p, download=False):
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
        if download:
            self.send_header("Content-Disposition", f'attachment; filename="{os.path.basename(p)}"')
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
        apply_cameras()
        try:
            if not self.authed():
                return self.send(401, {"error": "PIN needed"})
            b = self.body()
            if u.path == "/api/unbox/start":
                return self.send(200, UNBOX.start(b.get("order"), b.get("request", ""), b.get("note", ""), b.get("cam")))
            if u.path == "/api/unbox/stop":
                return self.send(200, UNBOX.stop())
            if u.path == "/api/packclip":
                if not b.get("start") and not b.get("end"):   # order ID only: Claude finds the scan time first
                    return self.send(200, request_packclip(b))
                start = datetime.strptime(b["start"], "%Y-%m-%dT%H:%M")
                end = datetime.strptime(b["end"], "%Y-%m-%dT%H:%M")
                return self.send(200, pack_clips(b["order"], start, end, b.get("cams")))
            if u.path == "/api/footage":
                return self.send(200, footage.set_wanted(b.get("on")))
            if u.path == "/api/cameras":
                return self.send(200, edit_cameras(b))
            if u.path == "/api/have":
                return self.send(200, have(b.get("orders")))
            if u.path == "/api/photo":
                return self.send(200, save_photo(b))
            if u.path == "/api/makeview":
                return self.send(200, {"view": make_view(b.get("order"), b.get("file", ""))})
            if u.path == "/api/count":
                return self.send(200, request_count(b))
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
    apply_cameras()
    threading.Thread(target=tidy_views, daemon=True).start()
    footage.start(cfg=CFG, rtsp=rtsp, log=log, ffmpeg=FFMPEG, local_cfg=local_cfg, local_path=LOCAL, nowin=NOWIN,
                  clean=clean)
    live.start(cfg=CFG, rtsp=rtsp, log=log, ffmpeg=FFMPEG, nowin=NOWIN, clean=clean, in_job=footage._in_job)
    try:   # clip downloads cut off by a restart start again
        for nm in os.listdir(SAVE):
            rp = os.path.join(SAVE, nm, CLIP_REQ)
            jj = _read_json(rp) if os.path.isfile(rp) else None
            if jj and jj.get("status") == "downloading":
                jj["status"] = "time_found"
                json.dump(jj, open(rp, "w", encoding="utf8"), indent=1)
        threading.Thread(target=fetch_found_clips, daemon=True).start()
    except OSError:
        pass
    LINK = pigulink.start(CFG, secrets().get("pin", ""), {
        "cfg": CFG, "unbox_state": UNBOX.state, "start": UNBOX.start, "stop": UNBOX.stop,
        "snapshot": snapshot, "returns": returns_list, "returns_info": returns_info,
        "ask_refresh": ask_refresh, "clean": clean, "save_finding": save_finding, "read_waybill": read_waybill, "unbox_video": request_unbox_video, "pack_video": request_video, "found_opts": FOUND_OPTS}, log)
    srv = ThreadingHTTPServer(("0.0.0.0", CFG["port"]), H)
    log(f"Sit Pretty on http://localhost:{CFG['port']}  (saving to {SAVE})")
    srv.serve_forever()


if __name__ == "__main__":
    main()
