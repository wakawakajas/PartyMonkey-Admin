"""Global input recorder: pynput hooks -> step list.

Applies self-exclusion to every event before capturing it, preserves
idle gaps between events as delay_ms on each step, coalesces two close
clicks into a double_click, and broadcasts each step live to connected
web UI clients as it's captured.
"""
from __future__ import annotations

import threading
import time
import uuid
from typing import Callable, Optional

from pynput import keyboard, mouse

from agent import self_exclusion, settings, uia, winapi

_dc_time_ms = winapi.double_click_time_ms()
_dc_w, _dc_h = winapi.double_click_box()
DOUBLE_CLICK_MAX_GAP = max(_dc_time_ms, 1) / 1000.0
DOUBLE_CLICK_MAX_DX = max(_dc_w // 2, 2)
DOUBLE_CLICK_MAX_DY = max(_dc_h // 2, 2)


def _modifier_label(key) -> Optional[str]:
    name = str(key)
    if "ctrl" in name:
        return "ctrl"
    if "alt" in name:
        return "alt"
    if "shift" in name:
        return "shift"
    if "cmd" in name:
        return "win"
    return None


def _key_label(key) -> str:
    if isinstance(key, keyboard.KeyCode):
        return (key.char or f"vk{key.vk}").lower()
    return str(key).replace("Key.", "")


class Recorder:
    """One global recording session at a time -- start() rejects a
    second concurrent recording rather than silently doing nothing."""

    def __init__(self, broadcast: Callable[[dict], None]):
        self._broadcast = broadcast
        self._lock = threading.RLock()
        self.state = "idle"  # idle | recording | paused | stopped
        self.steps: list[dict] = []
        self._last_event_time: Optional[float] = None
        self._held_modifiers: set[str] = set()
        self._pending_click: Optional[dict] = None
        self._mouse_listener: Optional[mouse.Listener] = None
        self._keyboard_listener: Optional[keyboard.Listener] = None
        self._warned_pids: set[int] = set()  # windows we've already sent an accessibility warning for

    # -- lifecycle ----------------------------------------------------
    def start(self) -> dict:
        with self._lock:
            if self.state == "recording":
                raise RuntimeError("Already recording -- stop the current recording first.")
            self.state = "recording"
            self.steps = []
            self._last_event_time = time.time()
            self._held_modifiers = set()
            self._pending_click = None
            self._warned_pids = set()
            self._mouse_listener = mouse.Listener(on_click=self._on_click, on_scroll=self._on_scroll)
            self._keyboard_listener = keyboard.Listener(on_press=self._on_press, on_release=self._on_release)
            self._mouse_listener.start()
            self._keyboard_listener.start()
        self._broadcast({"type": "recording_state", "state": "recording"})
        return {"state": self.state}

    def pause(self) -> dict:
        with self._lock:
            if self.state != "recording":
                raise RuntimeError("Not currently recording.")
            self._flush_pending_click()
            self.state = "paused"
        self._broadcast({"type": "recording_state", "state": "paused"})
        return {"state": self.state}

    def resume(self) -> dict:
        with self._lock:
            if self.state != "paused":
                raise RuntimeError("Not currently paused.")
            self.state = "recording"
            self._last_event_time = time.time()  # pause time isn't an idle gap
        self._broadcast({"type": "recording_state", "state": "recording"})
        return {"state": self.state}

    def stop(self) -> dict:
        with self._lock:
            if self.state not in ("recording", "paused"):
                raise RuntimeError("Not currently recording.")
            self._flush_pending_click()
            self.state = "stopped"
            mouse_listener, keyboard_listener = self._mouse_listener, self._keyboard_listener
            self._mouse_listener = None
            self._keyboard_listener = None
            steps = list(self.steps)
        # Stop the OS hooks outside the lock -- .stop() can briefly block,
        # and doing it inside the lock would delay anyone reading state.
        if mouse_listener:
            mouse_listener.stop()
        if keyboard_listener:
            keyboard_listener.stop()
        self._broadcast({"type": "recording_state", "state": "stopped", "step_count": len(steps)})
        return {"state": "stopped", "steps": steps}

    def adopt_steps(self, steps: list[dict]) -> dict:
        """Takes a step list captured somewhere else -- the browser
        recorder -- and parks it in the "stopped" state, which is what
        Save expects. One buffer, one Save button, whichever recorder
        filled it."""
        with self._lock:
            if self.state in ("recording", "paused"):
                raise RuntimeError("Stop the input recording before adopting browser steps.")
            self.steps = list(steps)
            self.state = "stopped" if steps else "idle"
        self._broadcast({"type": "recording_state", "state": self.state, "step_count": len(steps)})
        return {"state": self.state, "steps": list(steps)}

    def cancel(self) -> dict:
        """Discards the in-progress or just-stopped recording without
        saving -- distinct from stop(), which keeps the captured steps
        around for review/save."""
        with self._lock:
            if self.state not in ("recording", "paused", "stopped"):
                raise RuntimeError("Nothing to cancel.")
            mouse_listener, keyboard_listener = self._mouse_listener, self._keyboard_listener
            self._mouse_listener = None
            self._keyboard_listener = None
            self._pending_click = None
            self.steps = []
            self.state = "idle"
        if mouse_listener:
            mouse_listener.stop()
        if keyboard_listener:
            keyboard_listener.stop()
        self._broadcast({"type": "recording_state", "state": "idle"})
        return {"state": "idle"}

    # -- internals ------------------------------------------------------
    def _gap_ms(self) -> int:
        now = time.time()
        gap = 0.0 if self._last_event_time is None else max(0.0, now - self._last_event_time)
        self._last_event_time = now
        return round(gap * 1000)

    def _add_step(self, step: dict) -> None:
        step["id"] = uuid.uuid4().hex
        step["seq"] = len(self.steps)
        self.steps.append(step)
        self._broadcast({"type": "step_added", "step": step})

    def _flush_pending_click(self) -> None:
        if self._pending_click is not None:
            step, self._pending_click = self._pending_click, None
            step.pop("_t", None)  # internal-only timing field, not part of the public step shape
            self._add_step(step)

    # -- mouse ----------------------------------------------------------
    def _on_click(self, x, y, button, pressed) -> None:
        if not pressed:
            return
        if self_exclusion.is_point_in_own_window(x, y):
            return  # self-exclusion: silently drop, never capture our own UI

        # UIA calls happen outside the lock -- they're a COM round-trip
        # into another process and shouldn't block state reads/other events.
        win = self_exclusion.window_info_at(x, y)
        semantic = uia.capture_at_point(x, y, win.get("hwnd"))
        fragile = not semantic.get("accessible", False)
        rect = win.get("rect")
        relative_x = x - rect["left"] if rect else None
        relative_y = y - rect["top"] if rect else None

        with self._lock:
            if self.state != "recording":
                return

            if fragile:
                self._warn_no_accessible_elements(win)

            btn = {"left": "left", "right": "right", "middle": "middle"}.get(button.name, "left")
            now = time.time()

            pending = self._pending_click
            if (
                pending is not None
                and pending["type"] == "click"
                and pending["button"] == btn
                and (now - pending["_t"]) <= DOUBLE_CLICK_MAX_GAP
                and abs(pending["x"] - x) <= DOUBLE_CLICK_MAX_DX
                and abs(pending["y"] - y) <= DOUBLE_CLICK_MAX_DY
            ):
                self._pending_click = None
                self._add_step({
                    "type": "double_click",
                    "button": btn,
                    "x": x, "y": y,
                    "relative_x": relative_x, "relative_y": relative_y,
                    "delay_ms": pending["delay_ms"],
                    "window": win,
                    "semantic": semantic,
                    "fragile": fragile,
                })
                return

            self._flush_pending_click()
            self._pending_click = {
                "type": "click",
                "button": btn,
                "x": x, "y": y,
                "relative_x": relative_x, "relative_y": relative_y,
                "delay_ms": self._gap_ms(),
                "window": win,
                "semantic": semantic,
                "fragile": fragile,
                "_t": now,
            }

    def _warn_no_accessible_elements(self, win: dict) -> None:
        """Core requirement 2: tell the user plainly, once per window,
        when an app exposes nothing to UI Automation at all."""
        pid = win.get("pid")
        if pid is None or pid in self._warned_pids:
            return
        self._warned_pids.add(pid)
        title = win.get("title") or "This window"
        self._broadcast({
            "type": "accessibility_warning",
            "pid": pid,
            "window_title": win.get("title", ""),
            "message": (
                f'"{title}" exposes no accessible elements -- steps here will use '
                "coordinates and may break if windows move."
            ),
        })

    def _on_scroll(self, x, y, dx, dy) -> None:
        if self_exclusion.is_point_in_own_window(x, y):
            return
        with self._lock:
            if self.state != "recording":
                return
            self._flush_pending_click()
            self._add_step({
                "type": "scroll",
                "x": x, "y": y,
                "dx": dx, "dy": dy,
                "delay_ms": self._gap_ms(),
                "window": self_exclusion.window_info_at(x, y),
            })

    # -- keyboard ---------------------------------------------------------
    def _on_press(self, key) -> None:
        mod = _modifier_label(key)
        if mod:
            with self._lock:
                self._held_modifiers.add(mod)
            return

        label = _key_label(key)
        with self._lock:
            combo = sorted(self._held_modifiers) + [label]
            is_stop_hotkey = settings.matches_stop_hotkey(combo)
            state = self.state

        # The stop hotkey is checked regardless of recording state or focus
        # -- pynput's hook is global, so this works even if the browser
        # (or nothing) is focused.
        if is_stop_hotkey:
            self._trigger_stop_hotkey()
            return

        if state != "recording":
            return

        with self._lock:
            if self.state != "recording":
                return
            self._flush_pending_click()
            self._add_step({
                "type": "hotkey" if len(combo) > 1 else "key",
                "keys": combo,
                "delay_ms": self._gap_ms(),
            })

    def _on_release(self, key) -> None:
        mod = _modifier_label(key)
        if mod:
            with self._lock:
                self._held_modifiers.discard(mod)

    def _trigger_stop_hotkey(self) -> None:
        with self._lock:
            if self.state not in ("recording", "paused"):
                return
        # Run on a separate thread: calling Listener.stop() on the keyboard
        # listener from within its own callback thread can deadlock.
        threading.Thread(target=self._stop_from_hotkey, daemon=True).start()

    def _stop_from_hotkey(self) -> None:
        try:
            self.stop()
            self._broadcast({"type": "hotkey_stop_triggered"})
        except RuntimeError:
            pass
