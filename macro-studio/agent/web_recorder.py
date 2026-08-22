"""Record a macro by clicking through a web page.

The global input recorder captures OS-level clicks, which is the right
model for desktop apps and useless for the CDP steps: a click on a
browser window tells us a screen coordinate, and what a web step needs is
the element -- a selector, its visible label, whether it lives in a menu
that only exists while something is hovered.

So this recorder listens inside the page instead. It injects a
capture-phase click listener into the tab, then polls for what that
listener collected and turns each entry into a `web_click` step, streaming
them to the UI as they arrive. Nothing here touches the mouse or the
keyboard: the person clicks normally in the debugging Chrome, and the
steps appear in Macro Studio next to it.

Re-injection is on every poll rather than once at start, because a click
that navigates throws away the whole JS context -- including our listener.
Checking for it costs one cheap evaluate per poll and means a recording
survives moving between pages, which is most of what a real macro does.
"""
from __future__ import annotations

import threading
import time
import uuid
from typing import Callable, Optional

from agent import cdp

POLL_INTERVAL = 0.4

# What the injected listener records for each click, and how it decides
# what to record. Kept in one string so the whole page-side contract is
# readable in one place.
_LISTENER_JS = r"""
(() => {
  if (window.__msRecorder) return "already";
  const store = [];
  window.__msRecorder = store;

  const cssEscape = (s) => (window.CSS && CSS.escape ? CSS.escape(s) : String(s).replace(/[^\w-]/g, "\\$&"));

  // A selector is only worth recording if it is short enough to read and
  // stable enough to survive a page reload. An id wins outright; failing
  // that we walk up building a class path, and give up on anything that
  // needs more than four levels -- past that, matching on the visible
  // label is the more reliable half of the pair anyway.
  const selectorFor = (el) => {
    if (el.id && document.querySelectorAll("#" + cssEscape(el.id)).length === 1) {
      return "#" + cssEscape(el.id);
    }
    const parts = [];
    let node = el;
    for (let depth = 0; node && node.nodeType === 1 && depth < 4; depth++) {
      let part = node.tagName.toLowerCase();
      const classes = String(node.className || "")
        .split(/\s+/)
        .filter((c) => c && !/^(ng-|is-|active$|hover$|selected$)/.test(c))
        .slice(0, 2);
      if (classes.length) part += "." + classes.map(cssEscape).join(".");
      parts.unshift(part);
      const candidate = parts.join(" ");
      try {
        if (document.querySelectorAll(candidate).length === 1) return candidate;
      } catch (err) { /* generated selector wasn't valid; keep walking */ }
      node = node.parentElement;
    }
    return parts.join(" ");
  };

  // Menus are the case that breaks naive recording: the item clicked here
  // will not exist on replay unless something opens the menu first. If the
  // click happened inside a submenu, we hand back the label of whatever
  // opens it so a hover step can be recorded ahead of the click.
  const menuOpenerFor = (el) => {
    const panel = el.closest('[role="menu"], .dropdown-menu, .submenu, li ul, [aria-expanded="true"]');
    if (!panel) return null;
    const host = panel.closest("li, .dropdown, .menu-item, .nav-item") || panel.parentElement;
    if (!host) return null;
    const opener = host.querySelector("a, button");
    if (!opener || opener === el || opener.contains(el)) return null;
    return {
      selector: selectorFor(opener),
      text: String(opener.innerText || "").trim().split("\n")[0].slice(0, 60),
    };
  };

  document.addEventListener("click", (event) => {
    const el = event.target instanceof Element ? event.target : null;
    if (!el) return;
    // The clickable thing is usually an ancestor of whatever the pointer
    // landed on -- a span inside the link, an icon inside the button.
    const target = el.closest("a, button, [role='button'], li, th, td, label, input, select") || el;
    store.push({
      selector: selectorFor(target),
      text: String(target.innerText || target.value || "").trim().split("\n")[0].slice(0, 60),
      tag: target.tagName.toLowerCase(),
      href: target.getAttribute ? target.getAttribute("href") : null,
      opener: menuOpenerFor(target),
      url: location.href,
      at: Date.now(),
    });
  }, true);
  return "installed";
})()
"""


class WebRecorder:
    """One browser recording at a time, same rule as the input recorder --
    two of them would fight over the same step list."""

    def __init__(self, broadcast: Callable[[dict], None]):
        self._broadcast = broadcast
        self._lock = threading.RLock()
        self.state = "idle"  # idle | recording | stopped
        self.steps: list[dict] = []
        self.port = cdp.DEFAULT_PORT
        self._target_id = ""
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._last_click_at: Optional[float] = None
        self._error: Optional[str] = None

    # -- lifecycle ---------------------------------------------------------
    def start(self, port: int = cdp.DEFAULT_PORT, url: str = "", tab_match: str = "") -> dict:
        with self._lock:
            if self.state == "recording":
                raise RuntimeError("Already recording the browser -- stop that first.")
            if not cdp.is_running(port):
                cdp.launch(port=port, url=url or "")
            page = cdp.open_tab(port, url) if url else cdp.find_page(port, tab_match)
            if url:
                page = cdp.wait_ready(port, page.get("id", ""), url, timeout_ms=20000)

            self.state = "recording"
            self.port = port
            self._target_id = page.get("id", "")
            self.steps = []
            self._error = None
            self._last_click_at = None
            self._stop_event = threading.Event()

            # Step 0 is the page we started on, so replaying the macro
            # doesn't depend on the browser happening to sit there.
            self.steps.append(self._step("cdp_launch", url="", user_data_dir="", timeout_ms=25000, tab_match=""))
            self.steps.append(self._step("web_goto", url=page.get("url", url), new_tab=True,
                                         tab_match="", timeout_ms=20000))
            for step in self.steps:
                self._broadcast({"type": "step_added", "step": step})

            self._thread = threading.Thread(target=self._poll_loop, name="web-recorder", daemon=True)
            self._thread.start()
        self._broadcast({"type": "web_recording_state", "state": "recording", "url": page.get("url", "")})
        return {"state": self.state, "url": page.get("url", ""), "steps": list(self.steps)}

    def stop(self) -> dict:
        with self._lock:
            if self.state != "recording":
                raise RuntimeError("Not currently recording the browser.")
            self._stop_event.set()
            self.state = "stopped"
            steps = list(self.steps)
        self._uninstall()
        self._broadcast({"type": "web_recording_state", "state": "stopped", "step_count": len(steps)})
        return {"state": "stopped", "steps": steps}

    def cancel(self) -> dict:
        with self._lock:
            if self.state == "recording":
                self._stop_event.set()
            self.state = "idle"
            self.steps = []
        self._uninstall()
        self._broadcast({"type": "web_recording_state", "state": "idle"})
        return {"state": "idle"}

    def snapshot(self) -> dict:
        with self._lock:
            return {"state": self.state, "steps": list(self.steps), "error": self._error,
                    "port": self.port}

    # -- the poll loop -----------------------------------------------------
    def _poll_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                page = self._page()
                cdp.evaluate(page, _LISTENER_JS, timeout=8)
                drained = cdp.evaluate(page, "window.__msRecorder ? window.__msRecorder.splice(0) : []", timeout=8)
                for entry in drained or []:
                    self._record(entry)
                self._error = None
            except RuntimeError as exc:
                # A navigation in progress is the common case here, and it
                # resolves itself on the next pass -- only surface an error
                # if it's still failing when the user stops.
                self._error = str(exc)
            self._stop_event.wait(POLL_INTERVAL)

    def _page(self) -> dict:
        return cdp.find_page(self.port, self._target_id, timeout_ms=3000)

    def _uninstall(self) -> None:
        try:
            cdp.evaluate(self._page(), "delete window.__msRecorder; true", timeout=5)
        except RuntimeError:
            pass  # tab closed or navigated away; the listener died with it

    # -- turning a click into steps ---------------------------------------
    def _record(self, entry: dict) -> None:
        clicked_at = entry.get("at", 0) / 1000.0
        gap_ms = 0
        if self._last_click_at:
            gap_ms = max(0, min(int((clicked_at - self._last_click_at) * 1000), 5000))
        self._last_click_at = clicked_at

        opener = entry.get("opener")
        if opener and opener.get("text"):
            # The menu has to be open before the item exists to click.
            self._add(self._step("web_hover", selector=opener.get("selector", ""),
                                 text=opener.get("text", ""), exact=True,
                                 timeout_ms=10000, delay_ms=gap_ms))
            gap_ms = 0

        text = str(entry.get("text") or "")
        # Selector and text narrow each other on replay, so record both --
        # unless the label is long or multi-line, where it's more likely to
        # be a whole row of content than the thing that was clicked.
        keep_text = 0 < len(text) <= 40
        self._add(self._step(
            "web_click",
            selector=entry.get("selector", ""),
            text=text if keep_text else "",
            exact=keep_text,
            timeout_ms=10000,
            delay_ms=gap_ms,
        ))

    def _add(self, step: dict) -> None:
        with self._lock:
            self.steps.append(step)
        self._broadcast({"type": "step_added", "step": step})

    def _step(self, step_type: str, **fields) -> dict:
        step = {"id": uuid.uuid4().hex, "type": step_type, "delay_ms": 0, "port": self.port}
        step.update(fields)
        step["seq"] = len(self.steps)
        return step
