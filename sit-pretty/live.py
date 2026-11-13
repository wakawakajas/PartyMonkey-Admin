"""Live camera pictures for the browser.

One ffmpeg per camera reads its small stream (stream2) and keeps the newest JPEG frame in memory.
Every viewer on every PC just asks for that frame (/api/live.jpg) a few times a second, so a camera
has one live connection however many people watch, and the browser never holds a connection open.
The stream stops 20 seconds after the last viewer.
"""
import subprocess
import threading
import time

H = {}         # ffmpeg, rtsp, nowin, log, clean, in_job
LIVES = {}     # ip -> Live
LOCK = threading.Lock()
IDLE = 20


class Live:
    def __init__(self, ip, cam):
        self.ip, self.cam = ip, cam
        self.frame, self.at, self.seq = None, 0.0, 0
        self.seen = time.time()
        self.err = ""
        self.cond = threading.Condition()
        self.proc = None
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        fails = 0
        while time.time() - self.seen < IDLE:
            try:
                url = H["rtsp"](self.cam, "stream2")
            except Exception as e:
                self.err = H["clean"](e)
                break
            p = subprocess.Popen([H["ffmpeg"], "-hide_banner", "-loglevel", "error", "-nostdin",
                                  "-rtsp_transport", "tcp", "-timeout", "10000000", "-i", url, "-an",
                                  "-vf", "fps=8", "-c:v", "mjpeg", "-q:v", "6", "-f", "image2pipe", "-"],
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=H["nowin"])
            self.proc = p
            if H.get("in_job"):
                H["in_job"](p)
            threading.Thread(target=self._errs, args=(p,), daemon=True).start()
            buf = b""
            got = False
            while time.time() - self.seen < IDLE:
                chunk = p.stdout.read1(65536) if hasattr(p.stdout, "read1") else p.stdout.read(65536)
                if not chunk:
                    break
                buf += chunk
                while True:
                    a = buf.find(b"\xff\xd8")
                    b = buf.find(b"\xff\xd9", a + 2) if a >= 0 else -1
                    if a < 0 or b < 0:
                        if a > 0:
                            buf = buf[a:]
                        break
                    with self.cond:
                        self.frame, self.at, self.seq = buf[a:b + 2], time.time(), self.seq + 1
                        self.cond.notify_all()
                    got, self.err, fails = True, "", 0
                    buf = buf[b + 2:]
            try:
                p.kill()
            except Exception:
                pass
            if time.time() - self.seen >= IDLE:
                break
            fails += 0 if got else 1
            time.sleep(min(10, 2 * fails))   # camera dropped the stream: reconnect
        with LOCK:
            if LIVES.get(self.ip) is self:
                LIVES.pop(self.ip)
        with self.cond:
            self.cond.notify_all()

    def _errs(self, p):
        for ln in p.stderr:
            m = H["clean"](ln.decode("utf8", "replace").strip())
            if m:
                self.err = m[-160:]

    def get(self, after=0, wait=8.0):
        """The newest frame, waiting up to `wait` seconds for one newer than seq `after`."""
        self.seen = time.time()
        end = time.time() + wait
        with self.cond:
            while (self.frame is None or self.seq <= after) and time.time() < end:
                self.cond.wait(end - time.time())
            return self.frame, self.seq


def live(cam):
    ip = H["cfg"]["cameras"][cam]["ip"]
    with LOCK:
        lv = LIVES.get(ip)
        if not lv or (lv.proc and lv.proc.poll() is not None and time.time() - lv.seen > IDLE):
            lv = LIVES[ip] = Live(ip, cam)
        lv.seen = time.time()
        return lv


def frame(cam, after=0):
    lv = live(cam)
    data, seq = lv.get(after)
    if not data:
        raise RuntimeError(lv.err or "The camera did not send a picture")
    return data, seq


def recent(cam, max_age=3.0):
    """The live frame if this camera is streaming already (so a snapshot needs no extra connection)."""
    lv = LIVES.get(H["cfg"]["cameras"][cam]["ip"])
    if lv and lv.frame and time.time() - lv.at < max_age:
        return lv.frame
    return None


def start(**helpers):
    H.update(helpers)
