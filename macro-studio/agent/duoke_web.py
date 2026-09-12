"""Reading DuoKe through its own DOM, over the debugging port.

WHY THIS EXISTS ALONGSIDE THE UI AUTOMATION READER.

Everything in duoke.py reads the window through UI Automation, which works but
has one permanent weakness: the accessibility tree is something Chromium
builds for whoever is watching, and it tears it down when the window is
minimised. Half the operational trouble with this sync has been that -- a
window sitting there with nothing readable in it.

The DOM has no such problem. DuoKe is Electron, and launched with
--remote-debugging-port it answers Chrome DevTools Protocol whether the window
is minimised, covered, or on a screen nobody is looking at. It also carries
things the accessibility tree simply does not have: the URL of every picture
in the conversation.

THAT URL IS THE POINT. A customer's photo lives on Shopee's own CDN
(cf.shopee.sg), which serves it publicly. Storing the URL is a hundred bytes;
storing the picture would be a megabyte of the shop's storage per photo, for
a copy of a file Shopee is already hosting. So the reply carries the link and
Pigu renders it straight from there.

This module is deliberately read-only and deliberately optional: if the port
is closed, every function here returns nothing and the UIA reader carries on
as before.
"""
from __future__ import annotations

import asyncio
import json
import urllib.error
import urllib.request
from typing import Any, Optional

try:
    import websockets
except ImportError:                        # pragma: no cover - only on a broken install
    websockets = None                      # type: ignore[assignment]

# Where DuoKe is asked to listen. Matches the --remote-debugging-port flag in
# duoke.json's duoke_flags; nothing else on this machine should be on it.
PORT = 9223
# A page that does not answer in this long is a page that is busy; the pass
# carries on with UIA rather than waiting.
TIMEOUT = 8.0

# What to pull out of the conversation on screen.
#
# The message column is found from the reply box rather than from a class name:
# the box is the widest text field on the page, the conversation is what sits
# above it, and the order panel on the right is everything beyond its right
# edge. Class names in a Vue app change with every release; a reply box does
# not move.
_READ_OPEN = r"""
(() => {
  // The reply box is the anchor: the widest text field on the page. Its own
  // width is the width of the conversation, and everything else is found
  // relative to it rather than by class name -- a Vue app renames its classes
  // every release, and a reply box stays where it is.
  const boxes = [...document.querySelectorAll('textarea,[contenteditable="true"]')]
    .map(el => ({ el, r: el.getBoundingClientRect() }))
    .filter(b => b.r.width > 300)
    .sort((a, b) => b.r.width - a.r.width);
  if (!boxes.length) return JSON.stringify({ ok: false, why: 'no reply box' });
  const box = boxes[0].r;

  // THE MESSAGE LIST IS THE SCROLLER ABOVE THE BOX, as wide as the box is.
  // Walking up N ancestors was the first attempt and it read the conversation
  // list and the order panel as well: nine levels up from anything is the
  // whole page. A scroller of the right width sitting directly above the
  // composer is unambiguous.
  let list = null;
  document.querySelectorAll('div,ul,section').forEach(el => {
    const r = el.getBoundingClientRect();
    if (r.bottom > box.top + 16 || r.height < 120) return;
    if (Math.abs(r.width - box.width) > 240) return;
    if (el.scrollHeight <= el.clientHeight + 40) return;   // it does not scroll
    if (!list || r.height > list.r.height) list = { el, r };
  });
  if (!list) {
    // no scroller found (a short conversation fits without one): fall back to
    // the widest block above the box
    document.querySelectorAll('div,ul,section').forEach(el => {
      const r = el.getBoundingClientRect();
      if (r.bottom > box.top + 16 || r.height < 80) return;
      if (Math.abs(r.width - box.width) > 240) return;
      if (!list || r.height > list.r.height) list = { el, r };
    });
  }
  if (!list) return JSON.stringify({ ok: false, why: 'no message list' });

  const middle = list.r.left + list.r.width / 2;
  const out = [];
  const said = new Set();
  list.el.querySelectorAll('div,p,span').forEach(el => {
    const r = el.getBoundingClientRect();
    if (r.width < 60 || r.height < 18) return;
    const text = (el.innerText || '').replace(/\s+/g, ' ').trim();
    // A clock on its own is the divider DuoKe draws between runs of messages.
    // Dropped even when it wraps a picture, because the picture is reported
    // again by the bubble that actually holds it.
    if (/^(\d{1,2}[\/.]\d{1,2}\s+)?\d{1,2}:\d{2}(:\d{2})?$/.test(text)) return;
    const imgs = [...el.querySelectorAll('img')]
      .filter(i => { const q = i.getBoundingClientRect(); return q.width > 60 && q.height > 60; })
      .map(i => i.currentSrc || i.src)
      .filter(u => u && !u.startsWith('data:'));
    if (!text && !imgs.length) return;
    // one bubble, not the nested divs that draw it: an element whose children
    // already say all of it is scaffolding
    const kids = [...el.children];
    const kidText = kids.map(k => (k.innerText || '').replace(/\s+/g, ' ').trim()).join('');
    if (!imgs.length && kids.length > 1 && kidText.length >= text.length * 0.9) return;
    // A picture is keyed by the picture: the same photo is otherwise reported
    // for the bubble and again for the <img> inside it, and only the bubble is
    // laid out against an edge, so only the bubble knows which side it is on.
    const key = imgs.length ? imgs.join(',') : text;
    if (said.has(key)) return;
    said.add(key);
    out.push({
      inbound: (r.left + r.width / 2) < middle,
      text: text.slice(0, 1200),
      imgs: imgs.slice(0, 4),
      y: Math.round(r.top),
    });
  });

  out.sort((a, b) => a.y - b.y);
  return JSON.stringify({ ok: true, lines: out.slice(-40) });
})()
"""


def _pages() -> list[dict]:
    try:
        raw = urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json/list", timeout=3).read()
        return json.loads(raw)
    except (urllib.error.URLError, OSError, json.JSONDecodeError):
        return []


def available() -> bool:
    """Whether DuoKe is listening. Cheap: one HTTP call to loopback."""
    return websockets is not None and any(p.get("type") == "page" for p in _pages())


async def _evaluate(expression: str) -> Any:
    page = next((p for p in _pages() if p.get("type") == "page"), None)
    if not page or not page.get("webSocketDebuggerUrl"):
        return None
    async with websockets.connect(page["webSocketDebuggerUrl"], max_size=16_000_000,
                                  open_timeout=TIMEOUT, close_timeout=2) as ws:
        await ws.send(json.dumps({"id": 1, "method": "Runtime.enable"}))
        await ws.recv()
        await ws.send(json.dumps({
            "id": 2, "method": "Runtime.evaluate",
            "params": {"expression": expression, "returnByValue": True},
        }))
        while True:
            msg = json.loads(await ws.recv())
            if msg.get("id") != 2:
                continue
            result = (msg.get("result") or {}).get("result") or {}
            return result.get("value")


def _run(expression: str) -> Any:
    """The async client, called from the agent's ordinary threads.

    A new loop each time rather than one kept open: a pass happens every few
    seconds, a loop is cheap, and a socket held across passes is one more
    thing to notice when DuoKe restarts.
    """
    if websockets is None:
        return None
    try:
        return asyncio.run(asyncio.wait_for(_evaluate(expression), TIMEOUT))
    except Exception:
        # Deliberately broad: this is an optional source of a better answer,
        # and nothing here is worth failing a pass over.
        return None


def read_open_conversation() -> list[dict]:
    """The conversation on screen as [{inbound, text, imgs}], oldest first.

    Empty when the port is closed, the page is busy, or there is no
    conversation open -- all of which mean "use the other reader".
    """
    raw = _run(_READ_OPEN)
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return []
    if not data.get("ok"):
        return []
    lines = []
    for line in data.get("lines") or []:
        text = str(line.get("text") or "").strip()
        imgs = [str(u) for u in (line.get("imgs") or []) if str(u).startswith("http")]
        if not text and not imgs:
            continue
        lines.append({"inbound": bool(line.get("inbound")), "text": text, "imgs": imgs})
    return lines
