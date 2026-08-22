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

  const visible = (el) => {
    const r = el.getBoundingClientRect();
    if (r.width < 1 || r.height < 1) return false;
    const st = getComputedStyle(el);
    return st.visibility !== "hidden" && st.display !== "none" && st.opacity !== "0";
  };

  // A selector is only worth recording if it is short enough to read and
  // stable enough to survive a reload. An id wins outright; failing that we
  // walk up building a class path, and give up past four levels -- beyond
  // that, matching on the visible label is the more reliable half anyway.
  // Classes a framework toggles as you interact are dropped: recording the
  // hover state of a menu item would bake "only while the mouse is on it"
  // into the selector.
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
        .filter((c) => c && !/(^ng-|^is-|active|hover|focus|open$|checked|selected|highlight)/.test(c))
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

  const POPUP_SELECTOR = '[role="menu"], [role="listbox"], .dropdown-menu, .submenu,'
    + ' [class*="dropdown"], [class*="popup"], [class*="menu_content"], li ul, [aria-expanded="true"]';

  // Ant-design and friends render dropdowns at the end of <body>, not inside
  // whatever opens them, so no amount of walking up from the clicked item
  // finds its trigger. What does find it is remembering where the pointer
  // had just been: the thing hovered right before a click landed in a popup
  // is, in practice, the thing that opened it.
  const HOVER_TRAIL = [];
  document.addEventListener("mouseover", (event) => {
    const el = event.target instanceof Element
      ? event.target.closest("a, button, [role='button'], li, span, div")
      : null;
    if (!el) return;
    HOVER_TRAIL.push({ el: el, at: Date.now() });
    if (HOVER_TRAIL.length > 40) HOVER_TRAIL.shift();
  }, true);

  const triggerFromTrail = (el, panel) => {
    const now = Date.now();
    for (let i = HOVER_TRAIL.length - 1; i >= 0; i--) {
      const seen = HOVER_TRAIL[i];
      if (now - seen.at > 8000) break;
      if (!seen.el.isConnected || panel.contains(seen.el) || seen.el.contains(el)) continue;
      if (!visible(seen.el)) continue;
      const label = String(seen.el.innerText || "").trim().split("\n")[0];
      if (!label || label.length > 40) continue;
      return { selector: selectorFor(seen.el), text: label.slice(0, 60) };
    }
    return null;
  };

  // Menus are the case that breaks naive recording: the item clicked here
  // will not exist on replay unless something opens the menu first. If the
  // click happened inside one, hand back whatever opens it so a hover step
  // can be recorded ahead of the click.
  const menuOpenerFor = (el) => {
    const panel = el.closest(POPUP_SELECTOR);
    if (!panel) return null;
    const host = panel.closest("li, .dropdown, .menu-item, .nav-item");
    const nested = host ? host.querySelector("a, button") : null;
    if (nested && nested !== el && !nested.contains(el)) {
      const label = String(nested.innerText || "").trim().split("\n")[0];
      if (label) return { selector: selectorFor(nested), text: label.slice(0, 60) };
    }
    return triggerFromTrail(el, panel);
  };

  const capture = (event, button) => {
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
      button: button,
      url: location.href,
      at: Date.now(),
    });
  };

  document.addEventListener("click", (event) => capture(event, "left"), true);
  // Right-click arrives as contextmenu, not click. Chrome's own grey menu
  // opens too and nothing can drive that -- but a page with its own
  // right-click menu is just a page, and replays fine.
  document.addEventListener("contextmenu", (event) => capture(event, "right"), true);
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
        self._known_tabs: set = set()
        self._error: Optional[str] = None

    # -- lifecycle ---------------------------------------------------------
    def start(self, port: int = cdp.DEFAULT_PORT, url: str = "", tab_match: str = "",
              seed: bool = True) -> dict:
        """`seed` writes the two steps that get a fresh macro to the page --
        launch the browser, open the URL. Recording extra steps into a macro
        that already does both wants them left out, so the caller decides."""
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
            # Everything open when we start is "not new". A tab that shows
            # up later did so because of something the user clicked.
            self._known_tabs = {p.get("id") for p in cdp.list_pages(port)}
            self._known_tabs.add(self._target_id)
            self.steps = []
            self._error = None
            self._last_click_at = None
            self._stop_event = threading.Event()

            if seed:
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
                self._follow_new_tab()
                self._error = None
            except RuntimeError as exc:
                # A navigation in progress is the common case here, and it
                # resolves itself on the next pass -- only surface an error
                # if it's still failing when the user stops.
                self._error = str(exc)
            self._stop_event.wait(POLL_INTERVAL)

    def _follow_new_tab(self) -> None:
        """A click that opens a tab -- a PDF, a print preview, a report --
        moves the person to it, and everything they do next happens there.
        Without noticing, the recorder would keep watching the old tab and
        silently capture none of it. Writing a "follow new tab" step and
        moving with them keeps the recording and the replay in step."""
        fresh = [p for p in cdp.list_pages(self.port) if p.get("id") not in self._known_tabs]
        if not fresh:
            return
        page = fresh[-1]
        self._known_tabs.add(page.get("id"))
        self._target_id = page.get("id", "")
        self._add(self._step("web_switch_tab", mode="new", tab_match="", timeout_ms=15000))

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
            button=entry.get("button", "left"),
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
