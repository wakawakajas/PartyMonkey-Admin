"""Pigu remote control for Sit Pretty, through Supabase Realtime.

Pigu is a web page on the internet, so it cannot reach this PC directly. Instead
Sit Pretty keeps one quiet connection open to Supabase and listens on a private
channel (its name comes from the Sit Pretty PIN). Pigu sends a command there
(hello / start / stop / snap); only this PC answers it. Nothing is stored in the
database and no video goes through Supabase: only short messages.

Traffic when idle: one heartbeat every 25 s (well under 1 MB a day).
"""
import base64
import hashlib
import io
import json
import socket
import threading
import time

HOST = socket.gethostname()


def lan_ip():
    """This PC's address on the shop network (PC names do not work on every PC; numbers do)."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("192.168.10.1", 80))   # picks the shop-network card; nothing is sent
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return ""


def channel_name(pin):
    return "sitpretty-" + hashlib.sha256(("sitpretty:" + pin).encode()).hexdigest()[:16]


def trim_returns(rows):
    out = []
    for r in rows:
        out.append({k: r.get(k) for k in ("order", "request", "status", "reason", "date", "due", "todo",
                                          "return_tracking", "ship_tracking", "buyer_text", "buyer_videos",
                                          "buyer_photos", "shopee_ask", "shopee_ask_at", "found", "appeal")}
                   | {"items": [{k: i.get(k) for k in ("name", "model", "qty", "returned")} for i in r.get("items", [])]})
    return out


class PiguLink(threading.Thread):
    def __init__(self, url, key, pin, api, log):
        super().__init__(daemon=True, name="pigu-link")
        self.ws_url = url.replace("https://", "wss://").rstrip("/") + f"/realtime/v1/websocket?apikey={key}&vsn=1.0.0"
        self.key, self.api, self.log = key, api, log
        self.topic = "realtime:" + channel_name(pin)
        self.ws = None
        self.send_lock = threading.Lock()
        self.ref = 0
        self.connected = False
        self.last_unbox = None

    # ------------------------------------------------------------ wire
    def _send(self, event, payload, topic=None):
        with self.send_lock:
            self.ref += 1
            msg = {"topic": topic or self.topic, "event": event, "payload": payload, "ref": str(self.ref)}
            if topic is None:
                msg["join_ref"] = "1"
            self.ws.send(json.dumps(msg))

    def push(self, event, payload):
        if not self.connected:
            return
        try:
            self._send("broadcast", {"type": "broadcast", "event": event, "payload": payload})
        except Exception as e:
            self.log(f"Pigu link: send failed ({e})")

    # ------------------------------------------------------------ what Pigu sees
    def state(self, full=False):
        s = {"host": HOST, "ip": lan_ip(), "unbox": self.api["unbox_state"](), "at": int(time.time())}
        if full:
            cfg = self.api["cfg"]
            s["cameras"] = {c: v["label"] for c, v in cfg["cameras"].items()}
            s["returns_camera"] = cfg["returns_camera"]
            s["packing_cameras"] = cfg["packing_cameras"]
            s["found_opts"] = list(self.api["found_opts"].items())  # a list keeps the order
            s["returns"] = trim_returns(self.api["returns"]())
            s["returns_info"] = self.api["returns_info"]()
        return s

    def handle(self, p):
        cmd, rid = p.get("cmd"), p.get("id")
        if cmd == "hello":
            return self.push("state", self.state(full=True))
        if p.get("to") != HOST:
            return
        try:
            if cmd == "start":
                data = self.api["start"](p.get("order"), p.get("request", ""), "started from Pigu", p.get("cam"))
            elif cmd == "stop":
                data = self.api["stop"]()
                data["file"] = data["file"].rsplit("\\", 1)[-1].rsplit("/", 1)[-1]
            elif cmd == "snap":
                data = {"jpg": self.small_jpg(self.api["snapshot"](p.get("cam")))}
            elif cmd == "readwb":
                data = self.api["read_waybill"](p.get("cam"))
            elif cmd == "packvideo":
                data = self.api["pack_video"](p)
            elif cmd == "unboxvideo":
                data = self.api["unbox_video"](p)
            elif cmd == "finding":
                data = self.api["save_finding"](p.get("order"), p.get("issues"), p.get("note"), "Pigu")
            elif cmd == "refresh":
                data = self.api["ask_refresh"]()
            else:
                raise RuntimeError("Unknown command")
            self.push("reply", {"id": rid, "host": HOST, "ok": True, "data": data})
        except Exception as e:
            self.push("reply", {"id": rid, "host": HOST, "ok": False, "error": self.api["clean"](str(e))})

    @staticmethod
    def small_jpg(raw):
        from PIL import Image
        im = Image.open(io.BytesIO(raw)).convert("RGB")
        im.thumbnail((480, 480))
        b = io.BytesIO()
        im.save(b, "JPEG", quality=60)
        return base64.b64encode(b.getvalue()).decode()

    # ------------------------------------------------------------ loop
    def run(self):
        from websockets.sync.client import connect
        wait = 2
        while True:
            try:
                with connect(self.ws_url, open_timeout=15, max_size=2 ** 22) as ws:
                    self.ws = ws
                    self._send("phx_join", {"config": {"broadcast": {"self": False, "ack": False},
                                                       "presence": {"key": ""}, "postgres_changes": [],
                                                       "private": False},
                                            "access_token": self.key})
                    beat = time.time()
                    while True:
                        if time.time() - beat > 25:
                            self._send("heartbeat", {}, topic="phoenix")
                            beat = time.time()
                        un = self.api["unbox_state"]()
                        key = (un.get("recording"), un.get("order"), un.get("error"))
                        if self.connected and key != self.last_unbox:
                            self.last_unbox = key
                            self.push("state", self.state())
                        try:
                            raw = ws.recv(timeout=2)
                        except TimeoutError:
                            continue
                        m = json.loads(raw)
                        ev, pl = m.get("event"), m.get("payload") or {}
                        if ev == "phx_reply" and m.get("ref") == "1" and m.get("topic") == self.topic:
                            if pl.get("status") != "ok":
                                raise RuntimeError(f"Supabase refused the channel: {pl.get('response')}")
                            if not self.connected:
                                self.log("Pigu link connected: Pigu can now start and stop unboxings")
                            self.connected, wait = True, 2
                            self.push("state", self.state(full=True))
                        elif ev == "broadcast" and pl.get("event") == "cmd":
                            threading.Thread(target=self.handle, args=(pl.get("payload") or {},), daemon=True).start()
                        elif ev in ("phx_error", "phx_close"):
                            raise RuntimeError(ev)
            except Exception as e:
                if self.connected:
                    self.log(f"Pigu link dropped ({e}); reconnecting")
                self.connected = False
                self.ws = None
                time.sleep(wait)
                wait = min(wait * 2, 60)


def start(cfg, pin, api, log):
    sb = cfg.get("supabase") or {}
    if not sb.get("url") or not sb.get("anon_key"):
        log("Pigu link off: no Supabase settings in config.json")
        return None
    if not pin:
        log("Pigu link off: set a pin= in sitpretty-secrets.txt")
        return None
    try:
        import websockets  # noqa: F401
    except ImportError:
        log("Pigu link off: run start-sit-pretty.bat once to install its add-on")
        return None
    t = PiguLink(sb["url"], sb["anon_key"], pin, api, log)
    t.start()
    return t
