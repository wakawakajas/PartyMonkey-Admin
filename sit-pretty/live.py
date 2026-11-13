"""Live camera pictures for the browser.

One ffmpeg per camera reads its small stream (stream2) and keeps the newest JPEG frame in memory.
Every viewer on every PC just asks for that frame (/api/live.jpg) a few times a second, so a camera
has one live connection however many people watch, and the browser never holds a connection open.
The stream stops 20 seconds after the last viewer.
"""
import collections
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
        self.q = collections.deque(maxlen=12)   # recent frames, so a viewer gets every one, not just the newest
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
                                  "-vf", "fps=20,scale='min(960,iw)':-2", "-c:v", "mjpeg", "-q:v", "6", "-flush_packets", "1", "-f", "image2pipe", "-"],
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
                        self.q.append((self.seq, self.frame))
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


def ws_send(sock, data, opcode=2):
    n = len(data)
    head = bytes([0x80 | opcode])
    if n < 126:
        head += bytes([n])
    elif n < 65536:
        head += bytes([126]) + n.to_bytes(2, "big")
    else:
        head += bytes([127]) + n.to_bytes(8, "big")
    sock.sendall(head + data)


def serve_ws(handler, cams):
    """One WebSocket per viewer carries every camera: each new frame is pushed the moment it exists,
    as [1 byte: length of the camera id][camera id][JPEG]. No request per frame, so it plays smoothly."""
    import base64
    import hashlib
    import json
    key = handler.headers.get("Sec-WebSocket-Key", "")
    acc = base64.b64encode(hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()).decode()
    handler.send_response(101)
    handler.send_header("Upgrade", "websocket")
    handler.send_header("Connection", "Upgrade")
    handler.send_header("Sec-WebSocket-Accept", acc)
    handler.end_headers()
    handler.wfile.flush()
    sock = handler.connection
    sock.settimeout(10)
    cams = [c for c in cams if c in H["cfg"]["cameras"]]
    last = {c: 0 for c in cams}
    told = {c: 0.0 for c in cams}
    closed = threading.Event()

    def reader():   # the browser closing the page ends this viewer
        while not closed.is_set():
            try:
                b = sock.recv(64)
            except TimeoutError:
                continue
            except OSError:
                break
            if not b or (b[0] & 0x0F) == 8:
                break
        closed.set()
    threading.Thread(target=reader, daemon=True).start()
    try:
        while not closed.is_set():
            sent = False
            for c in cams:
                lv = live(c)
                if lv.frame is not None and lv.seq > last[c]:
                    cid = c.encode()
                    for sq, fr in list(lv.q):
                        if sq > last[c]:
                            ws_send(sock, bytes([len(cid)]) + cid + fr)
                            last[c] = sq
                    last[c] = max(last[c], lv.seq)
                    sent = True
                elif lv.err and time.time() - lv.at > 5 and time.time() - told[c] > 5:
                    told[c] = time.time()
                    ws_send(sock, json.dumps({"cam": c, "error": lv.err}).encode(), 1)
            if not sent:
                time.sleep(0.01)
    except OSError:
        pass
    finally:
        closed.set()


def start(**helpers):
    H.update(helpers)
