"""Supabase Realtime, just enough to be told "a row you care about changed".

The label and Fiery watchers used to ask the database every few seconds
whether anything was queued -- thousands of requests an hour to hear "no".
This holds one websocket open instead and wakes the watcher only when a
queued row is inserted or updated.

Uses the websockets package the agent already ships for CDP, and the same
signed-in Cloud the REST calls use, so nothing new to install or configure.
"""
from __future__ import annotations

import json
import threading
import time
from typing import Callable, Optional

from websockets.sync.client import connect as ws_connect

HEARTBEAT_SECONDS = 25
# The sign-in token lasts an hour; reconnecting well inside that hands the
# socket a fresh one, and the reconnect itself triggers a catch-up pass.
RECONNECT_SECONDS = 50 * 60


class Listener:
    def __init__(self, cloud_factory: Callable[[], object], tables: list[str],
                 on_change: Callable[[], None], name: str = "realtime"):
        self._cloud_factory = cloud_factory
        self._tables = tables
        self._on_change = on_change
        self._name = name
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.connected = False
        self.last_error = ""

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name=self._name, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    # ------------------------------------------------------------------
    def _run(self) -> None:
        backoff = 3.0
        while not self._stop.is_set():
            try:
                self._session()
                backoff = 3.0
            except Exception as exc:        # never let the listener thread die
                self.last_error = f"{type(exc).__name__}: {exc}"
            self.connected = False
            self._stop.wait(backoff)
            backoff = min(backoff * 2, 60.0)

    def _session(self) -> None:
        cloud = self._cloud_factory()
        cloud._auth()                                   # signs in if needed
        base = cloud.url.replace("https://", "wss://").replace("http://", "ws://")
        url = f"{base}/realtime/v1/websocket?apikey={cloud.anon}&vsn=1.0.0"
        topic = "realtime:" + self._name
        ref = 0
        with ws_connect(url, open_timeout=15, max_size=2 ** 22) as ws:
            ref += 1
            ws.send(json.dumps({
                "topic": topic, "event": "phx_join", "ref": str(ref), "join_ref": "1",
                "payload": {
                    "config": {
                        "broadcast": {"ack": False, "self": False},
                        "presence": {"key": ""},
                        "postgres_changes": [
                            {"event": "*", "schema": "public", "table": t}
                            for t in self._tables],
                        "private": False,
                    },
                    "access_token": cloud.token,
                },
            }))
            started = time.time()
            beat_at = started
            joined = False
            while not self._stop.is_set():
                if time.time() - started > RECONNECT_SECONDS:
                    cloud.token = None                  # fresh token next time
                    return
                try:
                    raw = ws.recv(timeout=5)
                except TimeoutError:
                    raw = None
                if time.time() - beat_at >= HEARTBEAT_SECONDS:
                    beat_at = time.time()
                    ref += 1
                    ws.send(json.dumps({"topic": "phoenix", "event": "heartbeat",
                                        "payload": {}, "ref": str(ref)}))
                if raw is None:
                    continue
                try:
                    msg = json.loads(raw)
                except (TypeError, ValueError):
                    continue
                event = msg.get("event")
                if event == "phx_reply" and msg.get("topic") == topic:
                    status = (msg.get("payload") or {}).get("status")
                    if status == "ok" and not joined:
                        joined = True
                        self.connected = True
                        self.last_error = ""
                        self._on_change()               # catch up on what was missed
                    elif status == "error":
                        raise RuntimeError("realtime join refused: "
                                           + json.dumps(msg.get("payload"))[:200])
                elif event == "phx_error" or event == "phx_close":
                    return
                elif event == "postgres_changes":
                    data = (msg.get("payload") or {}).get("data") or {}
                    if data.get("type") == "DELETE":
                        continue
                    record = data.get("record") or {}
                    if record.get("status") in (None, "queued"):
                        self._on_change()
