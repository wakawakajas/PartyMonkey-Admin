"""All-day camera footage on the NAS (Mon-Sat 8:00-18:00 by default), and clips cut from it.

One PC records (the one with "Record on this PC" switched on). Every camera is streamed as it is
(4K HEVC, no re-encoding) into 5-minute files:
    <dir>\\Flower table (183)\\2026-11-13\\2026-11-13 08-00-00.mp4   (the folder follows the camera name)
Files are fragmented MP4, so a file being written can already be read, and a crash loses seconds, not
the file. Footage older than keep_days is deleted; the oldest day goes first if the NAS gets full.
Other PCs only read the folder: packing clips come from here first, the memory card only for gaps.
"""
import json
import os
import re
import shutil
import socket
import subprocess
import threading
import time
from datetime import datetime, timedelta

DEFAULTS = {"dir": r"\\PARTYMONKEY-NAS\CCTV Footage\Sit Pretty", "days": [0, 1, 2, 3, 4, 5],
            "start": "08:00", "end": "18:00", "keep_days": 45, "segment_minutes": 5, "min_free_gb": 60}
PC = socket.gethostname()
H = {}          # filled by start(): cfg, rtsp, log, ffmpeg, local_cfg, nowin, clean
PROCS = {}      # folder key -> {"p": Popen, "since": datetime, "fails": int, "next": float, "err": str}
STATE = {"recording": False, "why": "", "other_pc": "", "free_gb": None, "checked": 0}
LOCK = threading.Lock()


def conf():
    c = dict(DEFAULTS)
    c.update(H["cfg"].get("footage") or {})
    return c


def key_of(ip):
    return ip.rsplit(".", 1)[1]


def cams_by_ip(all_cams=False):
    """One stream per physical camera; the other table boxes on it share the footage.
    Cameras switched to "live only" (e.g. Front Door) are not recorded."""
    out, rec = {}, set()
    for cam, v in H["cfg"]["cameras"].items():
        out.setdefault(key_of(v["ip"]), cam)
        if v.get("record", True) is not False:
            rec.add(key_of(v["ip"]))
    return out if all_cams else {k: c for k, c in out.items() if k in rec}


def live_list():
    """One tile per physical camera for the Live page."""
    rec = cams_by_ip()
    tiles = [{"cam": cam, "key": k, "name": folder_name(k)[:-len(f" ({k})")], "record": k in rec}
             for k, cam in cams_by_ip(True).items()]
    lo = H["cfg"].get("live_order") or []
    pos = {c: i for i, c in enumerate(lo)}
    return sorted(tiles, key=lambda t: (pos.get(t["cam"], len(lo)), list(H["cfg"]["cameras"]).index(t["cam"])))


def folder_name(k):
    """The camera's name as staff call it, e.g. "Flower table (183)". Table boxes on one camera share
    their common start ("Party table Latex" + "Party table Drawer" -> "Party table")."""
    labels = [v["label"] for v in H["cfg"]["cameras"].values() if key_of(v["ip"]) == k]
    name = os.path.commonprefix(labels).strip() if len(labels) > 1 else (labels[0] if labels else "")
    if len(name) < 3:
        name = labels[0] if labels else "Camera"
    name = re.sub(r'[<>:"/\\|?*%]+', " ", name)
    return " ".join(name.split())[:40] + f" ({k})"


def cam_dir(k):
    """Where this camera records now: its current name. (A renamed camera starts a new folder; the NAS
    refuses to rename folders it is indexing, and the old one empties itself after keep_days.)"""
    return os.path.join(conf()["dir"], folder_name(k))


def cam_dirs(k):
    """Every folder with this camera's footage, under any name it has had."""
    root = conf()["dir"]
    return [os.path.join(root, n) for n in os.listdir(root)
            if n.endswith(f"({k})") and os.path.isdir(os.path.join(root, n))]


def in_hours(now=None):
    c, now = conf(), now or datetime.now()
    hm = now.hour * 60 + now.minute
    s, e = (int(x.split(":")[0]) * 60 + int(x.split(":")[1]) for x in (c["start"], c["end"]))
    return now.weekday() in c["days"] and s <= hm < e


def wanted():
    return bool(H["local_cfg"]().get("record"))


def set_wanted(on):
    lc = H["local_cfg"]()
    lc["record"] = bool(on)
    json.dump(lc, open(H["local_path"], "w", encoding="utf8"), indent=2)
    H["log"](("Recording to the NAS switched on" if on else "Recording to the NAS switched off") + f" on {PC}")
    tick()
    return status()


# ---------------------------------------------------------------- the recorder
def _heartbeat_path():
    return os.path.join(conf()["dir"], "recorder.json")


def _other_recorder():
    """Another PC that recorded in the last 2 minutes (only one PC may record)."""
    try:
        j = json.load(open(_heartbeat_path(), encoding="utf8"))
        if j.get("pc") != PC and time.time() - j.get("at", 0) < 120:
            return j.get("pc")
    except (OSError, ValueError):
        pass
    return ""


def _beat():
    try:
        tmp = _heartbeat_path() + f".{PC}.tmp"
        json.dump({"pc": PC, "at": time.time(), "cams": sorted(PROCS)}, open(tmp, "w", encoding="utf8"))
        os.replace(tmp, _heartbeat_path())
    except OSError:
        pass


def _job():
    """A Windows job object: the recordings end when Sit Pretty ends (closed window, crash or restart)."""
    if "job" in H or os.name != "nt":
        return H.get("job")
    import ctypes
    from ctypes import wintypes
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateJobObjectW.restype = wintypes.HANDLE

    class BASIC(ctypes.Structure):
        _fields_ = [("a", ctypes.c_int64), ("b", ctypes.c_int64), ("LimitFlags", wintypes.DWORD),
                    ("c", ctypes.c_size_t), ("d", ctypes.c_size_t), ("e", wintypes.DWORD),
                    ("f", ctypes.c_size_t), ("g", wintypes.DWORD), ("h", wintypes.DWORD)]

    class IO(ctypes.Structure):
        _fields_ = [(n, ctypes.c_uint64) for n in "abcdef"]

    class EXT(ctypes.Structure):
        _fields_ = [("basic", BASIC), ("io", IO), ("p", ctypes.c_size_t), ("j", ctypes.c_size_t),
                    ("pp", ctypes.c_size_t), ("pj", ctypes.c_size_t)]
    h = k32.CreateJobObjectW(None, None)
    info = EXT()
    info.basic.LimitFlags = 0x2000   # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    k32.SetInformationJobObject(wintypes.HANDLE(h), 9, ctypes.byref(info), ctypes.sizeof(info))
    H["job"] = (k32, h)
    return H["job"]


def _in_job(p):
    try:
        j = _job()
        if j:
            from ctypes import wintypes
            j[0].AssignProcessToJobObject(wintypes.HANDLE(j[1]), wintypes.HANDLE(int(p._handle)))
    except Exception as e:
        H["log"](f"Recorder: could not tie the recording to Sit Pretty ({e})")


def _kill_leftovers():
    """Recordings left running by an earlier Sit Pretty on this PC (they would hold the camera and files)."""
    if os.name != "nt":
        return
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-Command",
                            "Get-CimInstance Win32_Process -Filter \"Name='ffmpeg.exe'\" | "
                            "? CommandLine -match 'segment_atclocktime' | % { Stop-Process -Id $_.ProcessId -Force; $_.ProcessId }"],
                           capture_output=True, text=True, timeout=60, creationflags=H["nowin"])
        n = len(r.stdout.split())
        if n:
            H["log"](f"Stopped {n} recording(s) left over from before")
    except Exception:
        pass


def _start(k, cam):
    c = conf()
    base = cam_dir(k)
    day = os.path.join(base, datetime.now().strftime("%Y-%m-%d"))
    os.makedirs(day, exist_ok=True)
    pattern = os.path.join(base, "%Y-%m-%d", "%Y-%m-%d %H-%M-%S.mp4")
    cmd = [H["ffmpeg"], "-hide_banner", "-loglevel", "error", "-nostdin", "-rtsp_transport", "tcp",
           "-timeout", "15000000", "-i", H["rtsp"](cam, "stream1"), "-map", "0:v", "-c", "copy", "-an",
           "-f", "segment", "-segment_time", str(int(c["segment_minutes"]) * 60), "-segment_atclocktime", "1",
           "-strftime", "1", "-reset_timestamps", "1", "-segment_format", "mp4",
           "-segment_format_options", "movflags=+frag_keyframe+empty_moov+default_base_moof", pattern]
    p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                         creationflags=H["nowin"])
    _in_job(p)
    PROCS[k] = dict(PROCS.get(k, {}), p=p, since=datetime.now(), cam=cam, day=day, err="")
    threading.Thread(target=_read_err, args=(k, p), daemon=True).start()
    H["log"](f"Recording {H['cfg']['cameras'][cam]['label']} to the NAS")


def _read_err(k, p):
    for ln in p.stderr:
        msg = H["clean"](ln.decode("utf8", "replace").strip())
        if msg and k in PROCS and PROCS[k].get("p") is p:
            PROCS[k]["err"] = msg[-200:]


def _stop(k, why=""):
    r = PROCS.get(k) or {}
    p = r.get("p")
    if p and p.poll() is None:
        try:
            p.stdin.write(b"q")   # a clean finish closes the last file properly
            p.stdin.flush()
            p.wait(timeout=10)
        except Exception:
            p.kill()
    r["p"] = None
    if why:
        H["log"](f"Stopped recording {k}: {why}")


def tick():
    with LOCK:
        c = conf()
        now = datetime.now()
        want = wanted() and in_hours(now)
        other = _other_recorder() if want else ""
        STATE.update(other_pc=other)
        if not want or other:
            STATE.update(recording=False, why=(f"{other} is already recording" if other else
                                               "switched off on this PC" if not wanted() else "outside recording hours"))
            for k in list(PROCS):
                _stop(k)
            return
        if not os.path.isdir(c["dir"]):
            try:
                os.makedirs(c["dir"], exist_ok=True)
            except OSError as e:
                STATE.update(recording=False, why=f"cannot reach {c['dir']}: {e}")
                return
        live = cams_by_ip()
        for k in list(PROCS):
            if k not in live:
                _stop(k, "camera removed")
                PROCS.pop(k, None)
        for k, cam in live.items():
            r = PROCS.get(k) or {}
            p = r.get("p")
            day = os.path.join(cam_dir(k), now.strftime("%Y-%m-%d"))
            if p and p.poll() is None and r.get("day") == day:
                r["fails"] = 0 if (now - r["since"]).total_seconds() > 120 else r.get("fails", 0)
                continue
            if p and p.poll() is None:   # a new day, or the camera was renamed: start a fresh folder
                _stop(k)
            elif p:
                r["fails"] = r.get("fails", 0) + 1
                r["next"] = time.time() + min(300, 15 * r["fails"])
                H["log"](f"Recording {k} stopped ({r.get('err') or 'camera did not answer'}); trying again")
                r["p"] = None
            if time.time() < r.get("next", 0):
                continue
            try:
                _start(k, cam)
            except Exception as e:
                PROCS[k] = dict(r, p=None, err=H["clean"](e), fails=r.get("fails", 0) + 1,
                                next=time.time() + 120)
        STATE.update(recording=True, why="")
        _beat()


def tidy():
    """Delete day folders older than keep_days, and the oldest days while the NAS is nearly full."""
    c = conf()
    root = c["dir"]
    if not os.path.isdir(root):
        return
    cutoff = (datetime.now() - timedelta(days=int(c["keep_days"]))).strftime("%Y-%m-%d")
    days = []
    for k in os.listdir(root):
        kd = os.path.join(root, k)
        if not (re.search(r"\(\d+\)$", k) and os.path.isdir(kd)):
            continue
        for d in os.listdir(kd):
            if len(d) == 10 and d[4] == "-":
                days.append((d, os.path.join(kd, d)))
    days.sort()
    for d, p in days:
        if d < cutoff:
            shutil.rmtree(p, ignore_errors=True)
            H["log"](f"Deleted footage from {d} (older than {c['keep_days']} days)")
    days = [x for x in days if x[0] >= cutoff and os.path.isdir(x[1])]
    today = datetime.now().strftime("%Y-%m-%d")
    while days and days[0][0] < today:
        free = shutil.disk_usage(root).free / 1e9
        STATE["free_gb"] = round(free)
        if free >= c["min_free_gb"]:
            break
        d0 = days[0][0]
        for d, p in [x for x in days if x[0] == d0]:
            shutil.rmtree(p, ignore_errors=True)
        H["log"](f"NAS nearly full: deleted the oldest footage ({d0})")
        days = [x for x in days if x[0] != d0]
    for k in os.listdir(root):   # a renamed camera's old folder, once its last day is gone
        kd = os.path.join(root, k)
        if re.search(r"\(\d+\)$", k) and os.path.isdir(kd) and not os.listdir(kd):
            try:
                os.rmdir(kd)
            except OSError:
                pass
    try:
        STATE["free_gb"] = round(shutil.disk_usage(root).free / 1e9)
    except OSError:
        pass


def _loop():
    _kill_leftovers()
    last_tidy = 0
    while True:
        try:
            tick()
        except Exception as e:
            H["log"](f"Recorder: {H['clean'](e)}")
        if wanted() and time.time() - last_tidy > 3600:
            last_tidy = time.time()
            try:
                tidy()
            except Exception as e:
                H["log"](f"Footage clean-up: {H['clean'](e)}")
        time.sleep(15)


def start(**helpers):
    H.update(helpers)
    threading.Thread(target=_loop, daemon=True).start()


def status():
    c = conf()
    cams = {}
    for k, cam in cams_by_ip().items():
        r = PROCS.get(k) or {}
        p = r.get("p")
        cams[k] = {"label": H["cfg"]["cameras"][cam]["label"], "folder": folder_name(k), "on": bool(p and p.poll() is None),
                   "since": r["since"].strftime("%H:%M") if p and p.poll() is None else "", "err": r.get("err", "")}
    return {"pc": PC, "wanted": wanted(), "recording": STATE["recording"], "why": STATE["why"],
            "other_pc": STATE["other_pc"] or _other_recorder(), "free_gb": STATE["free_gb"], "cams": cams,
            "dir": c["dir"], "hours": f"{c['start']}-{c['end']}", "days": c["days"], "keep_days": c["keep_days"]}


# ---------------------------------------------------------------- clips from the NAS footage
def _files(k, start, end):
    """The footage files of one camera that overlap start..end, with their start times."""
    out = []
    d = start.date() - timedelta(days=1)
    while d <= end.date():
        for root in cam_dirs(k):
            dd = os.path.join(root, d.strftime("%Y-%m-%d"))
            for n in (os.listdir(dd) if os.path.isdir(dd) else []):
                if n.endswith(".mp4"):
                    try:
                        out.append((datetime.strptime(n[:-4], "%Y-%m-%d %H-%M-%S"), os.path.join(dd, n)))
                    except ValueError:
                        pass
        d += timedelta(days=1)
    out.sort()
    seg = timedelta(minutes=int(conf()["segment_minutes"]))
    keep = []
    for i, (t, p) in enumerate(out):
        t_end = out[i + 1][0] if i + 1 < len(out) else t + seg
        if t < end and min(t_end, t + seg) > start:
            keep.append((t, p))
    return keep


def has(cam, start, end):
    """True when the NAS footage covers most of start..end for this camera."""
    try:
        f = _files(key_of(H["cfg"]["cameras"][cam]["ip"]), start, end)
    except OSError:
        return False
    if not f:
        return False
    seg = timedelta(minutes=int(conf()["segment_minutes"]))
    covered = sum(((min(end, t + seg) - max(start, t)).total_seconds() for t, _ in f), 0.0)
    return covered >= 0.8 * (end - start).total_seconds()


def clip(cam, start, end, out, playable=False):
    """Cut start..end of one camera's NAS footage into out (stream copy: seconds, not minutes)."""
    f = _files(key_of(H["cfg"]["cameras"][cam]["ip"]), start, end)
    if not f:
        raise RuntimeError("No NAS footage for that time")
    lst = out + ".txt"
    with open(lst, "w", encoding="utf8") as fh:
        for _, p in f:
            fh.write("file '" + p.replace("\\", "/").replace("'", "'\\''") + "'\n")
    off = max(0.0, (start - f[0][0]).total_seconds())
    dur = (end - max(start, f[0][0])).total_seconds()
    try:
        r = subprocess.run([H["ffmpeg"], "-hide_banner", "-loglevel", "error", "-y", "-f", "concat", "-safe", "0",
                            "-ss", f"{off:.2f}", "-i", lst, "-t", f"{dur:.2f}", "-map", "0:v", "-c", "copy", "-an",
                            *(["-tag:v", "hvc1"] if playable else []), "-movflags", "+faststart", out], capture_output=True, timeout=600, creationflags=H["nowin"])
    finally:
        try:
            os.remove(lst)
        except OSError:
            pass
    if r.returncode or not os.path.isfile(out) or os.path.getsize(out) < 10000:
        raise RuntimeError("Could not cut the NAS footage: " + H["clean"](r.stderr.decode("utf8", "replace")[-200:]))
    return out
