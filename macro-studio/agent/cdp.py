"""Chrome DevTools Protocol driver -- the web half of replay.

UI Automation is the right tool for desktop apps, but it hits a hard wall
on modern web pages: Chrome only exposes the *active* tab's accessibility
tree, and a site built out of custom widgets (BigSeller's nav tabs, for
one) hands us elements with no Invoke, Select, Toggle, or DoDefaultAction
pattern -- nothing UIA can click. The only fallback left on that side is a
physical mouse click, which defeats the whole point of background replay.

CDP sidesteps all of it. We talk to Chrome's own debugging endpoint over
HTTP + WebSocket and click the DOM node directly: custom widgets, icon
glyphs, wrapper elements, background tabs, no window focus needed. It's
local and free -- the same protocol DevTools itself uses, no API key, no
per-run cost, nothing leaving the machine.

The catch, stated plainly: since Chrome 136 the debugging port is refused
on the default user-data-dir, so CDP steps run against a Chrome started
with its own `--user-data-dir`. That's a separate profile, which means
signing in to a site once inside it. The session persists after that, so
it is a one-time cost.

Everything here is synchronous on purpose -- replay runs on a worker
thread, not an event loop, so `websockets.sync` fits it better than the
async client. Every function raises RuntimeError with a message written to
be shown to the user as-is; replay.py puts it straight into the step's
failure reason.
"""
from __future__ import annotations

import json
import os
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request

from websockets.sync.client import connect as ws_connect

from agent import actions

DEFAULT_PORT = 9222
DEFAULT_PROFILE_DIR = os.path.join(
    os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "MacroStudio", "ChromeProfile"
)
# One CDP round trip is near-instant on loopback; a call that needs longer
# than this is a page problem, not a transport problem.
_WS_TIMEOUT = 20.0


# -- endpoint plumbing -------------------------------------------------------
def _http_json(port: int, path: str, timeout: float = 3.0):
    with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=timeout) as resp:
        body = resp.read().decode("utf-8", "replace")
    return json.loads(body) if body.strip() else None


def is_running(port: int = DEFAULT_PORT) -> bool:
    """True if something is answering CDP on this port -- lets a launch
    step skip a redundant launch, and tells "Chrome isn't up" apart from
    "Chrome is up but that tab isn't there"."""
    try:
        _http_json(port, "/json/version", timeout=1.5)
        return True
    except Exception:
        return False


def launch(port: int = DEFAULT_PORT, user_data_dir: str = "", url: str = "",
           wait_seconds: float = 20.0) -> str:
    """Starts a debuggable Chrome, or reuses one already listening on the
    port. Returns a sentence saying which of the two happened."""
    if is_running(port):
        if url:
            open_tab(port, url)
            return f"Reused the Chrome already debugging on port {port}; opened {url}."
        return f"Reused the Chrome already debugging on port {port}."

    chrome = actions.find_chrome()
    if not chrome:
        raise RuntimeError(
            "Chrome wasn't found in its usual install locations. "
            "Install it from https://www.google.com/chrome/, or use a different step for this."
        )
    profile = user_data_dir or DEFAULT_PROFILE_DIR
    os.makedirs(profile, exist_ok=True)
    args = [
        chrome,
        f"--remote-debugging-port={port}",
        f"--user-data-dir={profile}",
        "--no-first-run",
        "--no-default-browser-check",
    ]
    if url:
        args.append(url)
    subprocess.Popen(args)

    deadline = time.time() + wait_seconds
    while time.time() < deadline:
        if is_running(port):
            return f'Started Chrome on debugging port {port} with profile "{profile}".'
        time.sleep(0.3)
    raise RuntimeError(
        f"Chrome was launched but never answered on debugging port {port} within "
        f"{round(wait_seconds)}s. If a Chrome is already open on the same profile folder "
        f'("{profile}"), close it and try again -- one profile can only be used by one '
        "Chrome at a time."
    )


def list_pages(port: int = DEFAULT_PORT) -> list:
    try:
        targets = _http_json(port, "/json/list") or []
    except Exception:
        raise RuntimeError(
            f"Nothing is listening for CDP on port {port} -- put a "
            '"Launch Chrome (CDP)" step before this one, or fix the port.'
        )
    return [t for t in targets if t.get("type") == "page" and t.get("webSocketDebuggerUrl")]


def open_tab(port: int, url: str) -> dict:
    """New tab via the HTTP endpoint rather than Target.createTarget -- no
    WebSocket needed for the one call, and it behaves identically."""
    quoted = urllib.parse.quote(url, safe="")
    request = urllib.request.Request(f"http://127.0.0.1:{port}/json/new?{quoted}", method="PUT")
    try:
        with urllib.request.urlopen(request, timeout=8) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"Chrome refused to open a new tab ({exc.code}).")


def find_page(port: int, match: str = "", timeout_ms: int = 0) -> dict:
    """Picks the tab to act on. An empty match takes the first page, which
    covers the common single-tab case; otherwise it is a case-insensitive
    substring test against the tab's URL and title -- the two things a
    person can see and type -- or a target id handed down from an earlier
    step in the same run. A timeout lets a step wait for a tab that a
    previous step's navigation is still opening."""
    deadline = time.time() + max(0, timeout_ms) / 1000.0
    exact_id = (match or "").strip()
    while True:
        pages = list_pages(port)
        needle = exact_id.lower()
        if not needle and pages:
            return pages[0]
        # A step that inherits the tab from earlier in the run passes a
        # target id, which survives navigation; a hand-typed match is a
        # URL or title fragment. Ids are checked first so they can't be
        # mistaken for a fragment that happens to match nothing.
        for page in pages:
            if page.get("id") == exact_id:
                return page
        for page in pages:
            haystack = f"{page.get('url', '')} {page.get('title', '')}".lower()
            if needle and needle in haystack:
                return page
        if time.time() >= deadline:
            if not pages:
                raise RuntimeError(f"Chrome is debugging on port {port} but has no open page tabs.")
            seen = ", ".join('"' + str(p.get("title", ""))[:40] + '"' for p in pages[:5])
            raise RuntimeError(f'No open tab matching "{match}". Open tabs: {seen}.')
        time.sleep(0.3)


# -- one round trip ----------------------------------------------------------
def _send(page: dict, method: str, params: dict, timeout: float = _WS_TIMEOUT) -> dict:
    """Opens a socket, sends one command, waits for that command's reply.
    A fresh connection per call costs a few milliseconds on loopback and
    saves owning a long-lived socket across a replay that can pause, stop,
    or fail at any step."""
    ws_url = page.get("webSocketDebuggerUrl")
    if not ws_url:
        raise RuntimeError("That tab has no debugger socket -- it may have just closed.")
    try:
        with ws_connect(ws_url, open_timeout=5, close_timeout=2, max_size=None) as sock:
            sock.send(json.dumps({"id": 1, "method": method, "params": params}))
            deadline = time.time() + timeout
            while time.time() < deadline:
                message = json.loads(sock.recv(timeout=max(0.5, deadline - time.time())))
                if message.get("id") != 1:
                    continue
                if "error" in message:
                    raise RuntimeError(f"Chrome rejected {method}: {message['error'].get('message')}")
                return message.get("result", {})
    except RuntimeError:
        raise
    except Exception as exc:
        raise RuntimeError(f"CDP call {method} failed: {exc}")
    raise RuntimeError(f"CDP call {method} timed out after {round(timeout)}s.")


def evaluate(page: dict, expression: str, timeout: float = _WS_TIMEOUT):
    """Runs JS in the page and returns its value. Promises are awaited, so
    the helpers below can poll with setTimeout and resolve when they're
    ready -- that's how waiting and retrying stay inside one round trip."""
    result = _send(page, "Runtime.evaluate", {
        "expression": expression,
        "awaitPromise": True,
        "returnByValue": True,
        "userGesture": True,
    }, timeout=timeout)
    if result.get("exceptionDetails"):
        detail = result["exceptionDetails"]
        text = (detail.get("exception") or {}).get("description") or detail.get("text") or "unknown error"
        raise RuntimeError(f"The page threw an error: {str(text).splitlines()[0]}")
    return (result.get("result") or {}).get("value")


# -- the JS side -------------------------------------------------------------
# Kept as a string rather than a separate asset so there's nothing extra to
# ship or keep in sync -- the whole driver is these few functions.
_JS_HELPERS = r"""
const __ms = {
  visible(el) {
    const r = el.getBoundingClientRect();
    if (r.width < 1 || r.height < 1) return false;
    const s = getComputedStyle(el);
    return s.visibility !== "hidden" && s.display !== "none" && s.opacity !== "0";
  },
  // Deepest-wins: a page's clickable label usually sits inside several
  // containers that all report the same innerText, and clicking the
  // outermost one either misses or hits the wrong thing. The smallest
  // visible box still containing the text is the reliable pick.
  byText(text, exact) {
    const want = String(text).trim().toLowerCase();
    const out = [];
    for (const el of document.querySelectorAll("body *")) {
      if (!__ms.visible(el)) continue;
      const own = String(el.innerText || el.value || el.getAttribute("aria-label") || "").trim().toLowerCase();
      if (!own) continue;
      if (exact ? own === want : own.includes(want)) out.push(el);
    }
    out.sort((a, b) => {
      const ra = a.getBoundingClientRect(), rb = b.getBoundingClientRect();
      return (ra.width * ra.height) - (rb.width * rb.height);
    });
    return out;
  },
  // Selector and text together narrow each other: a site's nav opener is
  // one of a dozen elements sharing a class, and the text alone matches
  // badges and column headers elsewhere on the page. Either one alone
  // still works on its own terms.
  find(selector, text, exact) {
    if (!selector) return __ms.byText(text, exact);
    let els = [];
    try {
      els = [...document.querySelectorAll(selector)].filter(__ms.visible);
    } catch (err) { /* invalid selector: fall through to the label */ }
    if (text) {
      const want = String(text).trim().toLowerCase();
      const matches = els.filter((el) => {
        const own = String(el.innerText || el.value || el.getAttribute("aria-label") || "").trim().toLowerCase();
        return exact ? own === want : own.includes(want);
      });
      // A recorded selector describes the page as it looked that day:
      // class names change, a panel re-renders somewhere else in the tree,
      // a menu is open now that wasn't then. The label is the durable half
      // of the pair, so when the selector no longer finds anything, fall
      // back to it rather than failing a step the user can plainly see.
      if (matches.length) return matches;
      return __ms.byText(text, exact);
    }
    return els;
  },
  // A real pointer sequence, not just el.click() -- component libraries
  // listen for pointerdown/mousedown and ignore a bare click event, and
  // that is exactly the widget class UIA could not touch.
  click(el) {
    el.scrollIntoView({ block: "center", inline: "center" });
    const r = el.getBoundingClientRect();
    const x = r.left + r.width / 2, y = r.top + r.height / 2;
    const opts = { bubbles: true, cancelable: true, clientX: x, clientY: y, view: window };
    for (const type of ["pointerdown", "mousedown", "pointerup", "mouseup", "click"]) {
      const Ctor = type.startsWith("pointer") ? PointerEvent : MouseEvent;
      el.dispatchEvent(new Ctor(type, opts));
    }
    return { tag: el.tagName.toLowerCase(), label: String(el.innerText || "").trim().slice(0, 60) };
  },
  poll(fn, timeoutMs) {
    const started = Date.now();
    return new Promise((resolve) => {
      const tick = () => {
        const got = fn();
        if (got) return resolve(got);
        if (Date.now() - started >= timeoutMs) return resolve(null);
        setTimeout(tick, 150);
      };
      tick();
    });
  },
};
"""


def _js(body: str) -> str:
    return "(async () => {" + _JS_HELPERS + body + "})()"


def _locate(page: dict, selector: str, text: str, exact: bool, timeout_ms: int) -> dict:
    """Scrolls the best match into view and returns where it sits, so the
    caller can aim real browser input at it.

    `hit` says whether that point currently resolves back to the element.
    A menu that is still fading in, or an overlay on top, makes the answer
    no -- and a click aimed there would land on whatever is underneath,
    which is worse than not clicking at all. The caller uses it to pick
    the safer path."""
    args = json.dumps({"selector": selector, "text": text, "exact": exact, "timeout": timeout_ms})
    result = evaluate(page, _js(
        "const a = " + args + ";"
        "const el = await __ms.poll(() => __ms.find(a.selector, a.text, a.exact)[0] || null, a.timeout);"
        "if (!el) return { ok: false };"
        "el.scrollIntoView({ block: 'center', inline: 'center' });"
        "let r = el.getBoundingClientRect(), hit = false;"
        # Settle loop: animated menus report a moving rect for a frame or
        # two after they open, so re-measure until the point sticks.
        "for (let i = 0; i < 12 && !hit; i++) {"
        "  await new Promise((res) => setTimeout(res, 60));"
        "  r = el.getBoundingClientRect();"
        "  const at = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);"
        "  hit = !!at && (at === el || el.contains(at) || at.contains(el));"
        "}"
        "return { ok: true, hit, x: r.left + r.width / 2, y: r.top + r.height / 2,"
        "         tag: el.tagName.toLowerCase(), label: String(el.innerText || '').trim().slice(0, 60) };"
    ), timeout=max(_WS_TIMEOUT, timeout_ms / 1000.0 + 5))
    if not result or not result.get("ok"):
        what = f'selector "{selector}"' if selector else f'text "{text}"'
        if selector and text:
            what = f'selector "{selector}" with text "{text}"'
        raise RuntimeError(f"Nothing matching {what} appeared on the page within {timeout_ms}ms.")
    return result


def hover(page: dict, selector: str = "", text: str = "", exact: bool = False,
          timeout_ms: int = 8000) -> dict:
    """Moves the browser's own pointer over an element.

    This is the one thing dispatching DOM events cannot fake: a menu that
    opens on CSS `:hover` ignores a synthetic mouseover, because the
    browser -- not the page -- decides what is hovered. CDP's Input domain
    drives that same internal pipeline, so the menu opens for real. It is
    still not the OS cursor: nothing moves on screen and the tab need not
    be focused or even frontmost."""
    spot = _locate(page, selector, text, exact, timeout_ms)
    _send(page, "Input.dispatchMouseEvent", {
        "type": "mouseMoved", "x": spot["x"], "y": spot["y"], "buttons": 0,
    })
    return spot


def click(page: dict, selector: str = "", text: str = "", exact: bool = False,
          timeout_ms: int = 8000) -> dict:
    """Clicks the best match, retrying until timeout -- a page still
    rendering is the normal case right after a navigation, not something
    worth failing a run over."""
    spot = _locate(page, selector, text, exact, timeout_ms)
    if not spot.get("hit"):
        # The point doesn't resolve back to the element -- something is
        # over it, or it is still moving. Dispatch on the node itself.
        return _dispatch_click(page, selector, text, exact)
    try:
        # Browser-level input, not a dispatched DOM event: it carries
        # isTrusted, it moves the pointer first (so hover state settles
        # before the press), and sites that gate on either one behave the
        # way they do for a person. No OS cursor is involved.
        common = {"x": spot["x"], "y": spot["y"], "button": "left", "clickCount": 1}
        _send(page, "Input.dispatchMouseEvent", {"type": "mouseMoved", "buttons": 0, **common})
        _send(page, "Input.dispatchMouseEvent", {"type": "mousePressed", "buttons": 1, **common})
        _send(page, "Input.dispatchMouseEvent", {"type": "mouseReleased", "buttons": 0, **common})
        return spot
    except RuntimeError:
        # Some pages tear down and rebuild between locate and press; the
        # DOM-event path doesn't depend on coordinates staying valid.
        return _dispatch_click(page, selector, text, exact)


def _dispatch_click(page: dict, selector: str, text: str, exact: bool) -> dict:
    args = json.dumps({"selector": selector, "text": text, "exact": exact})
    result = evaluate(page, _js(
        "const a = " + args + ";"
        "const el = __ms.find(a.selector, a.text, a.exact)[0] || null;"
        "if (!el) return { ok: false };"
        # An anchor's own click() follows its href even when a dispatched
        # event chain doesn't, so prefer it when there is one.
        "if (el.tagName === 'A' && el.getAttribute('href')) {"
        "  const info = { tag: 'a', label: String(el.innerText || '').trim().slice(0, 60) };"
        "  el.click();"
        "  return Object.assign({ ok: true }, info);"
        "}"
        "return Object.assign({ ok: true }, __ms.click(el));"
    ))
    if not result or not result.get("ok"):
        what = f'selector "{selector}"' if selector else f'text "{text}"'
        raise RuntimeError(f"Nothing matching {what} could be clicked on the page.")
    return result


def type_text(page: dict, selector: str, value: str, submit: bool = False) -> dict:
    args = json.dumps({"selector": selector, "value": value, "submit": submit})
    result = evaluate(page, _js(
        "const a = " + args + ";"
        "const el = document.querySelector(a.selector);"
        "if (!el) return { ok: false };"
        "el.focus();"
        # React and friends track the last value they set themselves, so
        # going through the native setter is what makes them notice.
        "const d = Object.getOwnPropertyDescriptor(el.constructor.prototype, 'value');"
        "if (d && d.set) d.set.call(el, a.value); else el.value = a.value;"
        "el.dispatchEvent(new Event('input', { bubbles: true }));"
        "el.dispatchEvent(new Event('change', { bubbles: true }));"
        "if (a.submit) {"
        "  el.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', keyCode: 13, bubbles: true }));"
        "  if (el.form) { el.form.requestSubmit ? el.form.requestSubmit() : el.form.submit(); }"
        "}"
        "return { ok: true };"
    ))
    if not result or not result.get("ok"):
        raise RuntimeError(f'No element matched selector "{selector}" to type into.')
    return result


def wait_for(page: dict, selector: str = "", text: str = "", exact: bool = False,
             timeout_ms: int = 10000) -> dict:
    args = json.dumps({"selector": selector, "text": text, "exact": exact, "timeout": timeout_ms})
    result = evaluate(page, _js(
        "const a = " + args + ";"
        "const el = await __ms.poll(() => __ms.find(a.selector, a.text, a.exact)[0] || null, a.timeout);"
        "return el ? { ok: true, label: String(el.innerText || '').trim().slice(0, 60) } : { ok: false };"
    ), timeout=max(_WS_TIMEOUT, timeout_ms / 1000.0 + 5))
    if not result or not result.get("ok"):
        what = f'selector "{selector}"' if selector else f'text "{text}"'
        raise RuntimeError(f"{what} never appeared within {timeout_ms}ms.")
    return result


def read_text(page: dict, selector: str) -> str:
    args = json.dumps({"selector": selector})
    result = evaluate(page, _js(
        "const a = " + args + ";"
        "const el = document.querySelector(a.selector);"
        "return el ? { ok: true, value: String(el.innerText || el.value || '').trim() } : { ok: false };"
    ))
    if not result or not result.get("ok"):
        raise RuntimeError(f'No element matched selector "{selector}" to read.')
    return result.get("value", "")


# Errors that mean "the page moved under us", not "this failed". A login
# redirect or an SPA route change mid-call produces exactly these, and the
# right response is to re-resolve the tab and try again, not to fail a run.
_TRANSIENT = (
    "execution context was destroyed",
    "inspected target navigated",
    "cannot find context",
    "no execution context",
    "target closed",
)


def _is_transient(exc: Exception) -> bool:
    text = str(exc).lower()
    return any(marker in text for marker in _TRANSIENT)


def through_navigation(port: int, page: dict, timeout_ms: int, call):
    """Runs `call(page)`, re-resolving the tab and retrying whenever the
    page navigates out from under it, until the step's own timeout. The
    tab is re-found by target id because that survives navigation."""
    deadline = time.time() + max(1000, timeout_ms) / 1000.0
    target_id = page.get("id", "")
    last: Exception = RuntimeError("The page kept navigating; nothing ran to completion.")
    while time.time() < deadline:
        try:
            return call(page)
        except RuntimeError as exc:
            if not _is_transient(exc):
                raise
            last = exc
            time.sleep(0.4)
            refreshed = next((p for p in list_pages(port) if p.get("id") == target_id), None)
            if refreshed:
                page = refreshed
    raise last


def wait_ready(port: int, target_id: str, expect_url: str = "", timeout_ms: int = 20000) -> dict:
    """Blocks until the tab has actually committed and finished loading
    the page we asked for, and returns its refreshed entry.

    This matters more than it looks. A tab opened by /json/new starts on
    about:blank and navigates a moment later; JS evaluated in that window
    runs in the *old* document's execution context, which the navigation
    then throws away -- so a poll started there waits out its whole
    timeout against a document that will never gain the content. Re-reading
    the target list each pass (a target id survives navigation, its URL
    does not) is what tells us the new document is the live one."""
    deadline = time.time() + max(0, timeout_ms) / 1000.0
    expect = (expect_url or "").strip().lower()
    last = None
    while True:
        for entry in list_pages(port):
            if entry.get("id") != target_id:
                continue
            last = entry
            url = str(entry.get("url", "")).lower()
            committed = url not in ("", "about:blank") and (not expect or _same_page(url, expect))
            if committed:
                try:
                    if evaluate(entry, "document.readyState", timeout=5) in ("interactive", "complete"):
                        return entry
                except RuntimeError:
                    pass  # context swapped mid-check; next pass sees the new one
            break
        if time.time() >= deadline:
            return last or find_page(port, "")
        time.sleep(0.25)


def _same_page(url: str, expect: str) -> bool:
    """Substring either way: the browser normalizes what we asked for
    (adds a scheme, a trailing slash, drops a default port) and sites
    redirect, so an exact comparison would reject perfectly good loads."""
    url, expect = url.rstrip("/"), expect.rstrip("/")
    return url.startswith(expect) or expect.startswith(url) or expect in url


def navigate(page: dict, url: str, settle_ms: int = 1200) -> None:
    _send(page, "Page.navigate", {"url": url})
    time.sleep(max(0, settle_ms) / 1000.0)
