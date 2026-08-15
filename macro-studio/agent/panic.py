"""Global panic hotkey: hold Esc for 1 second to immediately halt any
active recording or replay. Runs continuously from agent startup on its
own global keyboard hook -- independent of the Recorder's own
(recording-session-scoped, configurable) stop hotkey, and always active
regardless of whether a recording or replay is currently running.
"""
from __future__ import annotations

import threading
import time
from typing import Callable

from pynput import keyboard

HOLD_SECONDS = 1.0


class PanicWatcher:
    def __init__(self, on_panic: Callable[[], None]):
        self._on_panic = on_panic
        self._down_at: float | None = None
        self._triggered = False
        self._listener: keyboard.Listener | None = None
        self._lock = threading.Lock()

    def start(self) -> None:
        self._listener = keyboard.Listener(on_press=self._on_press, on_release=self._on_release)
        self._listener.start()

    def _on_press(self, key) -> None:
        if key != keyboard.Key.esc:
            return
        with self._lock:
            if self._down_at is not None:
                return  # already tracking this hold (key-repeat sends many presses)
            self._down_at = time.time()
            self._triggered = False
            marker = self._down_at
        threading.Thread(target=self._watch, args=(marker,), daemon=True).start()

    def _watch(self, marker: float) -> None:
        time.sleep(HOLD_SECONDS)
        with self._lock:
            still_held = self._down_at == marker and not self._triggered
            if still_held:
                self._triggered = True
        if still_held:
            self._on_panic()

    def _on_release(self, key) -> None:
        if key == keyboard.Key.esc:
            with self._lock:
                self._down_at = None
