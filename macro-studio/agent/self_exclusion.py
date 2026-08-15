"""Never record or replay against Macro Studio's own windows.

Two things count as "ours":
  1. Any window owned by the agent process itself (matched by PID).
  2. The Macro Studio web UI's browser tab. A browser tab has no HWND of
     its own -- the tab lives inside the browser's window -- so we match
     it by window title instead, since the page's <title> ("Macro
     Studio") is reflected in the OS window title whenever that tab is
     the active/frontmost one, which is the only time it could receive a
     click anyway. This trades a theoretical false-positive (some other
     unrelated window happening to contain "Macro Studio" in its title)
     for not depending on a single HWND snapshot that churns on refresh.

Every recorded event is checked against this before it's captured, per
the build spec -- not just once at startup.
"""
from __future__ import annotations

import os

from agent import config, winapi

AGENT_PID = os.getpid()


def window_info_at(x: int, y: int) -> dict:
    """Resolve the top-level window under a screen point, including its
    position -- this is the coordinate-fallback half of a step's target:
    raw x/y plus the owning window's title and rect, so coordinates can
    be re-derived relative to the window rather than the screen."""
    hwnd = winapi.root_window(winapi.window_from_point(x, y))
    if not hwnd:
        return {"hwnd": None, "pid": None, "title": "", "class_name": "", "rect": None}
    left, top, right, bottom = winapi.window_rect(hwnd)
    return {
        "hwnd": hwnd,
        "pid": winapi.window_pid(hwnd),
        "title": winapi.window_title(hwnd),
        "class_name": winapi.window_class_name(hwnd),
        "rect": {"left": left, "top": top, "right": right, "bottom": bottom},
    }


def is_own_window(pid: int | None, title: str) -> bool:
    if pid == AGENT_PID:
        return True
    if title and config.WEB_WINDOW_TITLE_HINT in title:
        return True
    return False


def is_point_in_own_window(x: int, y: int) -> bool:
    info = window_info_at(x, y)
    return is_own_window(info["pid"], info["title"])
