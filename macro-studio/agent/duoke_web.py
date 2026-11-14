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
import time
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

# What the page last complained about, for the caller that wants to say why.
_last_error = [""]


def last_error() -> str:
    return _last_error[0]

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
    // narrow is fine: "Ok" and "Thanks!" are whole messages, and a 60px
    // floor dropped them, so the newest line in a thread went unread
    if (r.width < 16 || r.height < 18) return;
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


def on_chat_page() -> bool:
    """Whether DuoKe is showing its chat page, by the page's own address --
    true even when the list is filtered down to nothing."""
    return any(p.get("type") == "page" and "/main/chat" in str(p.get("url") or "")
               for p in _pages())


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
            "params": {"expression": expression, "returnByValue": True,
                       "awaitPromise": True},
        }))
        while True:
            msg = json.loads(await ws.recv())
            if msg.get("id") != 2:
                continue
            payload = msg.get("result") or {}
            # An exception in the page comes back here rather than as a
            # failure, and silently returning None made a broken snippet look
            # like "nothing matched" for two rounds of debugging.
            if payload.get("exceptionDetails"):
                _last_error[0] = json.dumps(payload["exceptionDetails"])[:300]
                return None
            _last_error[0] = ""
            result = payload.get("result") or {}
            return result.get("value")


def _run(expression: str, timeout: float = TIMEOUT) -> Any:
    """The async client, called from the agent's ordinary threads.

    A new loop each time rather than one kept open: a pass happens every few
    seconds, a loop is cheap, and a socket held across passes is one more
    thing to notice when DuoKe restarts.
    """
    if websockets is None:
        return None
    try:
        return asyncio.run(asyncio.wait_for(_evaluate(expression), timeout))
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

# The conversation list, from the DOM.
#
# Same anchor trick as the messages: the list is the scroller to the LEFT of
# the reply box, and a row in it is an element that holds a name, a time and a
# preview. The unread badge is a small number in the row; a shop name is the
# other short line beside the name.
_READ_LIST = r"""
(() => {
  const boxes = [...document.querySelectorAll('textarea,[contenteditable="true"]')]
    .map(el => ({ el, r: el.getBoundingClientRect() }))
    .filter(b => b.r.width > 300)
    .sort((a, b) => b.r.width - a.r.width);
  // No conversation open yet (DuoKe just started) means no reply box:
  // the list still sits in the left third, so that is the edge used.
  const box = boxes.length ? boxes[0].r : { left: window.innerWidth / 3 };

  // THE ROWS ARE THE CHILDREN OF WHATEVER HOLDS MOST OF THEM.
  //
  // Looking for a container that scrolls found the wrong element: in DuoKe the
  // scroller is an ancestor (its class is virtual_list) and the rows are the
  // children of a plain div inside it, so "the scroller's children" was one
  // child holding thirty rows, and the list read as a single conversation.
  // Most-children-left-of-the-conversation is what actually describes it.
  let rows = null;
  document.querySelectorAll('div,ul').forEach(el => {
    const r = el.getBoundingClientRect();
    if (r.right > box.left - 4 || r.height < 150 || r.width < 120) return;
    const kids = [...el.children].filter(k => k.getBoundingClientRect().height > 28);
    // A row says when: a clock or a date. Counting only those lets a list
    // filtered down to one conversation (Pending, say) still be found -- a
    // bare "three children or more" read that as no list at all.
    const timed = kids.filter(k => (k.innerText || '').split('\n')
      .some(t => /^(\d{1,2}:\d{2}|\d{1,2}\/\d{1,2})$/.test(t.trim())));
    if (!timed.length) return;
    // most rows wins; on a tie the innermost, which is the one whose
    // children ARE the rows rather than a wrapper around them
    if (!rows || timed.length >= rows.kids.length) rows = { el, r, kids: timed };
  });
  if (!rows) return JSON.stringify({ ok: false, why: 'no conversation list' });

  const out = [];
  rows.kids.forEach(row => {
    const r = row.getBoundingClientRect();
    const parts = (row.innerText || '').split('\n').map(t => t.trim()).filter(Boolean);
    if (!parts.length) return;
    const isWhen = t => /^(\d{1,2}[:\/.]\d{2}|\d{1,2}\/\d{1,2}|yesterday|今天|昨天)$/i.test(t);
    const isBadge = t => /^\d{1,3}$/.test(t) && !isWhen(t);
    const badge = parts.find(isBadge) || '';
    // THE NAME IS THE FIRST LINE THAT IS NOT A NUMBER OR A CLOCK.
    //
    // parts[0] was the obvious choice and it was wrong in the one case that
    // matters: an unread row puts its badge first, so every conversation with
    // something waiting was read as a buyer called "1", "2" or "5" -- filed
    // under that, unfindable afterwards, and impossible to clear. The badge
    // and the time are the two things a row says that are not a name.
    const named = parts.filter(t => !isBadge(t) && !isWhen(t));
    const name = named[0] || '';
    const rest = named.slice(1);
    const shop = rest.find(t => t.length <= 28 && /^[A-Za-z]/.test(t)
      && t !== parts[parts.length - 1]) || '';
    if (!name) return;
    out.push({
      name: name.slice(0, 80),
      shop: shop.slice(0, 40),
      unread: !!badge,
      preview: (parts[parts.length - 1] || '').slice(0, 160),
      y: Math.round(r.top),
    });
  });
  return JSON.stringify({ ok: true, rows: out });
})()
"""


def list_chats() -> list[dict]:
    """The conversation list as [{name, shop, unread, preview}], top first.

    From the DOM, so it works with the window minimised -- which is the whole
    reason for this module: the accessibility tree this sync used to depend on
    disappears when nobody is looking at DuoKe.
    """
    raw = _run(_READ_LIST)
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return []
    if not data.get("ok"):
        return []
    return [r for r in (data.get("rows") or []) if str(r.get("name") or "").strip()]


# Opening a conversation by clicking its row IN THE PAGE.
#
# THIS IS WHY IT MATTERS. A real mouse click has to happen where the window
# actually is, which means raising it and moving somebody's pointer -- DuoKe
# jumping in front of whoever was working was exactly that, and this is what
# replaces it. element.click() needs no window, no focus and no pointer, and
# DuoKe cannot tell the difference.
_OPEN_CHAT = r"""
(name => {
  const want = String(name).trim().toLowerCase();
  const boxes = [...document.querySelectorAll('textarea,[contenteditable="true"]')]
    .map(el => ({ el, r: el.getBoundingClientRect() }))
    .filter(b => b.r.width > 300)
    .sort((a, b) => b.r.width - a.r.width);
  // No conversation open yet (DuoKe just started) means no reply box:
  // the list still sits in the left third, so that is the edge used.
  const box = boxes.length ? boxes[0].r : { left: window.innerWidth / 3 };

  let best = null;
  document.querySelectorAll('div,li').forEach(el => {
    const r = el.getBoundingClientRect();
    if (r.right > box.left - 4 || r.height < 28 || r.height > 220) return;
    const first = (el.innerText || '').trim().split('\n')[0].trim().toLowerCase();
    if (!first || first !== want) return;
    // THE BIGGEST one that is still row-sized. Taking the smallest was the
    // mistake: the name is its own 21px span inside the row, clicking it does
    // nothing, and the conversation never opened. The row carries the handler.
    if (!best || r.height > best.r.height) best = { el, r };
  });
  if (!best) return 'not in the list';
  // a full sequence rather than click() alone: a list built on mousedown --
  // and plenty are -- ignores a bare click
  const at = { bubbles: true, cancelable: true, view: window,
               clientX: Math.round(best.r.left + best.r.width / 2),
               clientY: Math.round(best.r.top + best.r.height / 2) };
  best.el.dispatchEvent(new MouseEvent('mousedown', at));
  best.el.dispatchEvent(new MouseEvent('mouseup', at));
  best.el.dispatchEvent(new MouseEvent('click', at));
  return 'clicked';
})
"""


def open_chat(name: str) -> bool:
    """Click a conversation open. False when it is not in the list."""
    raw = _run(f"({_OPEN_CHAT})({json.dumps(str(name or ''))})")
    return raw == "clicked"


# A draft left in the reply box, NOT sent.
#
# execCommand('insertText') is what a person typing produces as far as the
# page can tell -- input events and all, so the app's own state holds the
# words -- and it types no key at all, so there is no Enter that could send
# it. A box that already has something in it is left alone: that is somebody
# half-way through their own reply.
_PUT_DRAFT = r"""
((text) => {
  const boxes = [...document.querySelectorAll('textarea,[contenteditable="true"]')]
    .map(el => ({ el, r: el.getBoundingClientRect() }))
    .filter(b => b.r.width > 300)
    .sort((a, b) => b.r.width - a.r.width);
  if (!boxes.length) return 'no reply box';
  const el = boxes[0].el;
  const now = el.tagName === 'TEXTAREA' ? el.value : el.innerText;
  if ((now || '').trim()) return 'busy';
  el.focus();
  document.execCommand('insertText', false, text);
  const after = el.tagName === 'TEXTAREA' ? el.value : el.innerText;
  return (after || '').includes(text.slice(0, 30)) ? 'typed' : 'did not land';
})
"""


_SCROLL_LATEST = r"""
(() => {
  const boxes = [...document.querySelectorAll('textarea,[contenteditable="true"]')]
    .map(el => el.getBoundingClientRect()).filter(r => r.width > 300)
    .sort((a, b) => b.width - a.width);
  if (!boxes.length) return 'no reply box';
  const box = boxes[0];
  let moved = 0;
  document.querySelectorAll('div,ul,section').forEach(el => {
    const r = el.getBoundingClientRect();
    if (r.bottom > box.top + 16 || r.height < 120) return;
    if (Math.abs(r.width - box.width) > 240) return;
    if (el.scrollHeight <= el.clientHeight + 40) return;
    el.scrollTop = el.scrollHeight;
    moved++;
  });
  return 'scrolled ' + moved;
})()
"""


def scroll_to_latest() -> None:
    """Bring the open conversation down to its newest message. A thread that
    was left scrolled up otherwise reads as whatever was on screen then, and
    the message actually waiting is never seen."""
    _run(_SCROLL_LATEST)


# THE SHOP'S OWN QUICK REPLIES ("/" in the reply box).
#
# A quick reply picked from that list goes into the composer with its photos
# attached -- the pickup map, the printing guide -- and is not sent: DuoKe
# sends one only on Alt+Enter, which nothing here presses. So when a draft is
# really one of these, the quick reply goes in instead of the written words,
# photos and all.
_BOX = r"""
  const boxes = [...document.querySelectorAll('textarea,[contenteditable="true"]')]
    .map(el => ({ el, r: el.getBoundingClientRect() }))
    .filter(b => b.r.width > 300)
    .sort((a, b) => b.r.width - a.r.width);
  if (!boxes.length) return JSON.stringify({ ok: false, why: 'no reply box' });
  const el = boxes[0].el;
  const value = () => (el.tagName === 'TEXTAREA' ? el.value : el.innerText) || '';
"""

_OPEN_SHORTCUTS = r"""
(() => {""" + _BOX + r"""
  if (value().trim()) return JSON.stringify({ ok: false, why: 'busy' });
  el.focus();
  document.execCommand('insertText', false, '/');
  return JSON.stringify({ ok: true });
})()
"""

_READ_SHORTCUTS = r"""
(() => {
  // el- in the old DuoKe, dk- in DuokeAI (same list, renamed)
  const ul = document.querySelector('ul.el-autocomplete-suggestion__list, ul.dk-autocomplete-suggestion__list');
  if (!ul) return JSON.stringify({ ok: false, why: 'no quick reply list' });
  const rows = [...ul.children].map(li => ({
    code: ((li.querySelector('.container_text_instruction') || {}).textContent || '').trim(),
    text: (li.innerText || '').replace(/\s+/g, ' ').trim(),
    // the picture is shown as an icon, not an <img>
    photo: li.querySelectorAll('img,svg,[class*=pic],[class*=img],[class*=image]').length > 0,
  })).filter(r => r.code);
  // the code and its list number lead the row's text; the reply is the rest
  rows.forEach(r => { r.text = r.text.slice(r.text.indexOf(r.code) + r.code.length).trim(); });
  return JSON.stringify({ ok: true, rows });
})()
"""

_CLEAR_SLASH = r"""
(() => {""" + _BOX + r"""
  if (value().trim() !== '/') return JSON.stringify({ ok: false, why: 'not ours to clear' });
  el.focus();
  if (el.select) el.select(); else document.execCommand('selectAll');
  document.execCommand('delete');
  return JSON.stringify({ ok: true });
})()
"""

_BOX_TEXT = r"""
(() => {""" + _BOX + r"""
  return JSON.stringify({ ok: true, text: value() });
})()
"""

_PICK_SHORTCUT_UNUSED = r"""
((code) => {
  // el- in the old DuoKe, dk- in DuokeAI (same list, renamed)
  const ul = document.querySelector('ul.el-autocomplete-suggestion__list, ul.dk-autocomplete-suggestion__list');
  if (!ul) return 'no quick reply list';
  const li = [...ul.children].find(li =>
    ((li.querySelector('.container_text_instruction') || {}).textContent || '').trim() === code);
  if (!li) return 'no such quick reply';
  li.click();
  return 'picked';
})
"""


_CLEAR_BOX = r"""
(() => {""" + _BOX + r"""
  // A draft left here (words, and the photos a quick reply attaches) is
  // replaced by the reply somebody approved in Pigu, so it goes first. The
  // words can be deleted; an attached photo is only counted, and the caller
  // does not type over one.
  const box = boxes[0].r;
  const photos = [...document.querySelectorAll('img')].filter(i => {
    const q = i.getBoundingClientRect();
    return q.width > 8 && q.top >= box.top - 4 && q.top <= box.bottom + 160
      && q.left >= box.left - 4 && q.right <= box.right + 4;
  }).length;
  if (value().trim()) {
    el.focus();
    if (el.select) el.select(); else document.execCommand('selectAll');
    document.execCommand('delete');
  }
  return JSON.stringify({ ok: !value().trim(), photos });
})()
"""


def clear_box() -> dict:
    """Empty the reply box of a left draft. {ok, photos}: ok when no words are
    left, photos the number of attached pictures still sitting under it."""
    return _json(_run(_CLEAR_BOX))


def _json(raw: Any) -> dict:
    try:
        return json.loads(raw) if raw else {}
    except (json.JSONDecodeError, TypeError):
        return {}


def list_shortcuts() -> list[dict]:
    """The quick replies as [{code, text, photo}]. Opens the "/" list in an
    EMPTY reply box, reads it, and takes the "/" back out."""
    if not _json(_run(_OPEN_SHORTCUTS)).get("ok"):
        return []
    time.sleep(1.0)
    rows = _json(_run(_READ_SHORTCUTS)).get("rows") or []
    _run(_CLEAR_SLASH)
    return rows


# use_shortcut() was here. Picking a DuoKe quick reply can SEND it on the
# spot, and customers got messages nobody pressed Send on (2026-09-25). Only
# plain words go in the box now, and nothing in this module picks, clicks or
# presses anything that sends.


# THE REPLY PICKER: a small panel over DuoKe's reply box with a few answers
# for the open chat -- Pigu's draft and the quick replies that fit. Clicking one
# REPLACES what is in the box with it and puts the cursor there. It never
# sends: there is no Enter, no click on Send, and quick replies go in as plain
# words (picking DuoKe's own can send them, 2026-09-25). The person reads it,
# edits if they like, and presses Enter themselves.
_PICKER = r"""
((conv, opts) => {
  const ID = 'pigu-picker';
  let p = document.getElementById(ID);
  const boxes = [...document.querySelectorAll('textarea,[contenteditable="true"]')]
    .filter(el => !el.closest('#' + ID))
    .map(el => ({ el, r: el.getBoundingClientRect() }))
    .filter(b => b.r.width > 300)
    .sort((a, b) => b.r.width - a.r.width);
  // Double-click the reply box: show the panel if it was closed, close it if
  // it is open. Installed once; works for whichever chat is open.
  if (!window.__piguDbl) {
    window.__piguDbl = true;
    document.addEventListener('dblclick', e => {
      const t = e.target;
      if (!t || !t.matches || !t.matches('textarea,[contenteditable="true"]')) return;
      if (t.getBoundingClientRect().width < 300 || t.closest('#' + ID)) return;
      const q = document.getElementById(ID);
      if (!q) {
        let n = document.getElementById('pigu-note');
        if (!n) { n = document.createElement('div'); n.id = 'pigu-note'; document.body.appendChild(n); }
        const r = t.getBoundingClientRect();
        n.style.cssText = 'position:fixed;z-index:99999;background:#333;color:#fff;font:14px system-ui;padding:6px 10px;' +
          'border-radius:8px;left:' + Math.round(r.left) + 'px;top:' + Math.round(r.top - 40) + 'px';
        n.textContent = 'Pigu: no reply waiting in this chat';
        clearTimeout(window.__piguNoteT); window.__piguNoteT = setTimeout(() => n.remove(), 1800);
        return;
      }
      const shut = q.style.display === 'none';
      q.style.display = shut ? '' : 'none';
      if (shut) delete q.dataset.closed; else q.dataset.closed = q.dataset.sig.split('|')[0];
    });
  }
  // Alt+1, Alt+2 ... puts that answer in the box, as a click would
  if (!window.__piguKeys) {
    window.__piguKeys = true;
    document.addEventListener('keydown', e => {
      if (!e.altKey || e.ctrlKey || e.shiftKey || !/^Digit[1-9]$/.test(e.code)) return;
      const q = document.getElementById(ID);
      if (!q || q.style.display === 'none') return;
      const b = q.querySelectorAll('button[type=button]')[Number(e.code.slice(5)) - 1];
      if (!b) return;
      e.preventDefault(); e.stopPropagation();
      b.dispatchEvent(new MouseEvent('mousedown', { bubbles: true, cancelable: true }));
    }, true);
  }
  if (!conv || !opts.length || !boxes.length) { if (p) p.remove(); return 'hidden'; }
  const box = boxes[0];
  const sig = conv + '|' + opts.map(o => o.text).join('|');
  const place = () => {
    let pos = null;
    try { pos = JSON.parse(localStorage.getItem('piguPos') || 'null'); } catch (e) {}
    if (pos && typeof pos.x === 'number') {
      p.style.bottom = 'auto';
      p.style.left = Math.max(0, Math.min(window.innerWidth - 120, pos.x)) + 'px';
      p.style.top = Math.max(0, Math.min(window.innerHeight - 40, pos.y)) + 'px';
      if (pos.w) p.style.width = pos.w + 'px';
      return;
    }
    p.style.top = 'auto';
    // above the whole composer (its emoji / photo buttons stay clickable)
    const r = box.el.getBoundingClientRect();
    // the composer: the nearest wrapper that starts above the box (where its
    // buttons are) and is still composer-sized, not the whole chat column
    let top = r.top;
    for (let n = box.el.parentElement; n && n !== document.body; n = n.parentElement) {
      const q = n.getBoundingClientRect();
      if (q.height > 420) break;
      if (q.top < top) top = q.top;
    }
    p.style.left = Math.round(r.left) + 'px';
    p.style.width = Math.round(Math.min(r.width, 820)) + 'px';
    p.style.bottom = Math.round(window.innerHeight - Math.min(r.top, top) + 6) + 'px';
  };
  if (p && p.dataset.sig === sig) { if (p.dataset.closed !== conv) place(); return 'same'; }
  if (p) p.remove();
  if (!window.__piguDrop) {
    const previewOpen = () => [...document.querySelectorAll('span,div,header')].some(e =>
      !e.childElementCount && e.textContent.trim() === 'Send Image' && e.getBoundingClientRect().width > 0);
    window.__piguDrop = (urls, target) => {
      clearInterval(window.__piguDropT);
      const next = () => {
        if (previewOpen()) return;
        const u = urls.shift();
        if (!u) { clearInterval(window.__piguDropT); return; }
        const b64 = (window.__piguImg || {})[u];
        if (!b64) return;              // not fetched yet: try again shortly
        const bytes = Uint8Array.from(atob(b64), c => c.charCodeAt(0));
        const dt = new DataTransfer();
        dt.items.add(new File([bytes], 'photo.jpg', { type: 'image/jpeg' }));
        for (const t of ['dragenter', 'dragover', 'drop'])
          target.dispatchEvent(new DragEvent(t, { dataTransfer: dt, bubbles: true, cancelable: true }));
      };
      next();
      window.__piguDropT = setInterval(next, 600);
    };
  }
  p = document.createElement('div');
  p.id = ID; p.dataset.sig = sig;
  // COMPACT: it sits over the bottom of the conversation, so one line per
  // answer (the whole text on hover) and a button that folds it to one bar --
  // the buyer's newest message must stay readable.
  p.style.cssText = 'position:fixed;z-index:99999;background:rgba(255,255,255,.97);border:1px solid #d9d9e3;' +
    'border-radius:10px;box-shadow:0 4px 16px rgba(0,0,0,.15);padding:4px 8px 6px;' +
    'font:15px/1.35 system-ui,sans-serif;color:#222';
  let small = false;
  try { small = localStorage.getItem('piguSmall') === '1'; } catch (e) {}
  const head = document.createElement('div');
  head.style.cssText = 'display:flex;align-items:center;gap:8px;color:#888;font-size:13px';
  head.innerHTML = '<b style="color:#e4572e;font-size:14px">Pigu</b><span style="flex:1">' + opts.length +
    (opts.length > 1 ? ' replies' : ' reply') + ' &middot; click or Alt+number, then you press Enter &middot; hover to read all</span>';
  const fold = document.createElement('button');
  const x = document.createElement('button');
  [fold, x].forEach(k => { k.style.cssText = 'border:0;background:none;font-size:18px;cursor:pointer;color:#888;padding:0 4px;line-height:1'; });
  x.textContent = '\u00d7'; x.title = 'Hide (double-click the reply box to bring it back)';
  x.onclick = () => { p.dataset.closed = conv; p.style.display = 'none'; };
  const list = document.createElement('div');
  const setSmall = v => { small = v; list.style.display = v ? 'none' : 'block';
    fold.textContent = v ? '\u25b4' : '\u25be'; fold.title = v ? 'Show the replies' : 'Fold to one line';
    try { localStorage.setItem('piguSmall', v ? '1' : '0'); } catch (e) {} };
  fold.onclick = () => setSmall(!small);
  head.appendChild(fold); head.appendChild(x); p.appendChild(head); p.appendChild(list);
  // drag by the top bar; where it is left is remembered on this PC
  head.style.cursor = 'move'; head.title = 'Drag to move \u00b7 double-click to put it back';
  head.addEventListener('mousedown', e => {
    if (e.target.closest('button')) return;
    e.preventDefault();
    const q = p.getBoundingClientRect(), dx = e.clientX - q.left, dy = e.clientY - q.top;
    const move = ev => { p.style.bottom = 'auto';
      p.style.left = Math.max(0, Math.min(window.innerWidth - 120, ev.clientX - dx)) + 'px';
      p.style.top = Math.max(0, Math.min(window.innerHeight - 40, ev.clientY - dy)) + 'px'; };
    const up = () => { document.removeEventListener('mousemove', move); document.removeEventListener('mouseup', up);
      const z = p.getBoundingClientRect();
      try { localStorage.setItem('piguPos', JSON.stringify({ x: Math.round(z.left), y: Math.round(z.top), w: Math.round(z.width) })); } catch (err) {} };
    document.addEventListener('mousemove', move); document.addEventListener('mouseup', up);
  });
  head.addEventListener('dblclick', e => {
    if (e.target.closest('button')) return;
    try { localStorage.removeItem('piguPos'); } catch (err) {}
    place();
  });
  setSmall(small);
  opts.forEach((o, i) => {
    const b = document.createElement('button');
    b.type = 'button';
    b.title = o.text + (i < 9 ? '\n\n(Alt+' + (i + 1) + ')' : '');
    b.style.cssText = 'display:flex;gap:8px;align-items:baseline;width:100%;text-align:left;border:1px solid #eee;' +
      'background:#fafafa;border-radius:8px;padding:4px 8px;margin:3px 0;cursor:pointer;font:inherit;color:inherit';
    b.onmouseenter = () => { b.style.background = '#fff4ef'; b.style.borderColor = '#f3b49c'; };
    b.onmouseleave = () => { b.style.background = '#fafafa'; b.style.borderColor = '#eee'; };
    const tag = document.createElement('span');
    tag.style.cssText = 'flex:none;font-size:13px;color:#e4572e;font-weight:600;white-space:nowrap';
    tag.textContent = (i < 9 ? (i + 1) + ' \u00b7 ' : '') + o.label + ((o.photos || []).length ? ' \ud83d\udcf7' : '');
    const t = document.createElement('span');
    t.style.cssText = 'flex:1;min-width:0;white-space:nowrap;overflow:hidden;text-overflow:ellipsis';
    t.textContent = o.text.replace(/\s+/g, ' ');
    b.appendChild(tag); b.appendChild(t);
    // mousedown, not click: keeps the reply box's focus and selection
    b.addEventListener('mousedown', e => {
      e.preventDefault();
      const el = box.el;
      el.focus();
      if (el.tagName === 'TEXTAREA') el.select();
      else { const r = document.createRange(); r.selectNodeContents(el);
             const s = getSelection(); s.removeAllRanges(); s.addRange(r); }
      document.execCommand('insertText', false, o.text);
      // DuoKe offers its own quick reply when the words match one, and Enter
      // would pick that instead of sending these words: close its list
      const esc = () => el.dispatchEvent(new KeyboardEvent('keydown',
        { key: 'Escape', code: 'Escape', keyCode: 27, bubbles: true }));
      setTimeout(esc, 250); setTimeout(esc, 700);
      b.style.background = '#e9f7ee'; b.style.borderColor = '#8fd1a6';
      if ((o.photos || []).length) window.__piguDrop(o.photos.slice(), el);
    });
    list.appendChild(b);
  });
  document.body.appendChild(p);
  place();
  return 'shown';
})
"""


_QUICK = r"""
(() => {
  const v3 = document.querySelector('#app-vue3') && document.querySelector('#app-vue3').__vue_app__;
  const p = v3 && v3.config.globalProperties.$pinia;
  const rows = p && (p.state.value.system || {}).instMsgs;
  if (!Array.isArray(rows)) return JSON.stringify({ ok: false });
  return JSON.stringify({ ok: true, rows: rows.map(m => ({
    code: String(m.quickInstructions || '').trim(), text: String(m.content || '').trim(),
    photo: !!(m.mediaUrl || (m.mediaInfos || []).length),
    photos: (m.mediaInfos || []).filter(x => x.mediaType === 'image' && x.mediaUrl).map(x => String(x.mediaUrl))
      .concat(!(m.mediaInfos || []).length && m.mediaType === 'image' && m.mediaUrl ? [String(m.mediaUrl)] : [])
  })).filter(r => r.code && r.text) });
})()
"""


def quick_replies() -> list[dict]:
    """The shop's quick replies as [{code, text, photo}] -- from DuokeAI's own
    store, so the reply box is never touched; the old DuoKe falls back to
    opening the "/" list in an empty box."""
    data = _json(_run(_QUICK))
    if data.get("ok"):
        return data.get("rows") or []
    return list_shortcuts()


# DuokeAI's knowledge base: question-and-answer pairs it wrote from the shop's
# past chats (Knowledge base > shop Q&A). Read only.
_KB = r"""
(async (pages) => {
  const v3 = document.querySelector('#app-vue3') && document.querySelector('#app-vue3').__vue_app__;
  const p = v3 && v3.config.globalProperties.$pinia;
  if (!p) return JSON.stringify({ ok: false });
  const h = { 'x-access-token': p.state.value.runtime.token, 'X-Duoke-Version': 'v2',
              'Content-Type': 'application/json' };
  const rows = [];
  for (let n = 1; n <= pages; n++) {
    const r = await (await fetch('/api/v1/kb/qa/v1/shop/qa/list',
      { method: 'POST', headers: h, body: JSON.stringify({ pageNo: n, pageSize: 50 }) })).json();
    const d = r && r.code === 0 && r.data;
    if (!d) break;
    for (const q of d.list || []) rows.push({ q: String(q.question || ''), a: String(q.answer || ''),
      shops: (q.shops || []).map(s => String(s.shopId)) });
    if (n >= (d.totalPage || 1)) break;
  }
  return JSON.stringify({ ok: true, rows });
})
"""


def knowledge(pages: int = 10) -> list[dict]:
    """DuokeAI's shop Q&As as [{q, a, shops}]; empty on the old DuoKe."""
    data = _json(_run(f"({_KB})({int(pages)})", timeout=30))
    return data.get("rows") or [] if data.get("ok") else []


_photo_cache: dict[str, str] = {}


def load_photos(urls: list[str]) -> None:
    """Hand the page the quick replies' photos (it cannot fetch them itself:
    the photo host does not allow it). Downloaded once on this PC, then only
    the ones the page does not have yet are passed in."""
    urls = list(dict.fromkeys(urls))
    if not urls:
        return
    have = _json(_run("JSON.stringify({ ok: true, have: Object.keys(window.__piguImg || {}) })"))
    missing = [u for u in urls if u not in set(have.get("have") or [])]
    import base64
    for u in missing[:4]:
        if u not in _photo_cache:
            try:
                data = urllib.request.urlopen(u, timeout=20).read()
            except (urllib.error.URLError, OSError):
                continue
            if len(data) > 8_000_000:
                continue
            _photo_cache[u] = base64.b64encode(data).decode("ascii")
        _run(f"(window.__piguImg = window.__piguImg || {{}})[{json.dumps(u)}] = {json.dumps(_photo_cache[u])}")


# WHAT THE DRAFT SHOULD KNOW ABOUT THIS CHAT, read from DuokeAI on this PC:
# the buyer's latest orders (status, items, shipping, tracking -- never the
# address or phone) and the last product they sent (variations with price and
# stock, plus DuokeAI's own Q&As for that product). Read only.
_CONTEXT = r"""
(async (shopId, conversationId, guessId, platform) => {
  const v3 = document.querySelector('#app-vue3') && document.querySelector('#app-vue3').__vue_app__;
  const pin = v3 && v3.config.globalProperties.$pinia;
  if (!pin) return JSON.stringify({ ok: false });
  platform = platform || 'shopee';
  const st = pin.state.value;
  const h = { 'x-access-token': st.runtime.token, 'X-Duoke-Version': 'v2', 'Content-Type': 'application/json' };
  const get = async u => { try { const r = await (await fetch('/api/v1' + u, { headers: h })).json(); return r && r.code === 0 ? r.data : null; } catch (e) { return null; } };
  const post = async (u, b) => { try { const r = await (await fetch('/api/v1' + u, { method: 'POST', headers: h, body: JSON.stringify(b) })).json(); return r && r.code === 0 ? r.data : null; } catch (e) { return null; } };
  const day = t => t ? new Date(Number(t)).toISOString().slice(0, 10) : '';
  const out = { ok: true, orders: [], product: null };
  const ids = (await get(`/util/workOrderSystem/getAssociatedOrderList?platform=${platform}&shopId=${shopId}&conversationId=${conversationId}`)) || [];
  for (const id of ids.slice(0, 2)) {
    const o = await post('/dk/unity/order/detail', { shopId, uid: st.runtime.uid, puid: st.runtime.puid, platform, orderNumber: id });
    if (!o) continue;
    const lg = o.logistics || {};
    out.orders.push({ order: o.orderNumber, status: o.platformOrderStatus || o.dkOrderStatus || '',
      placed: day(o.platformCreateTime), paid: o.amount, shipping: lg.logisticsServiceName || '',
      tracking: (lg.trackingNumber || [])[0] || '', ship_by: day(lg.shipByDate), completed: day(o.completeTime),
      buyer_note: String(o.buyerNotes || '').slice(0, 200),
      items: (o.productList || []).slice(0, 8).map(p => `${p.quantity} x ${String(p.productName || '').slice(0, 70)}` +
        (p.variation ? ` (${p.variation})` : '')) });
  }
  // the last product card the buyer sent
  const m = await get(`/im/message/list?pageNo=1&pageSize=20&shopId=${shopId}&conversationId=${conversationId}&platform=${platform}`);
  let itemId = '';
  for (const x of (m && m.list) || []) {
    if (x.fromAccountType !== 1 || !/item|product|variation/.test(String(x.messageType))) continue;
    try { itemId = String(JSON.parse(x.messageContent).itemId || ''); } catch (e) {}
    if (itemId) break;
  }
  // a product their words clearly name (matched on this PC) beats an older
  // card further up the chat: "u hv clear tape?" after a balloon link is
  // about tape
  const fromWords = !!guessId && String(guessId) !== itemId;
  if (guessId) itemId = String(guessId);
  if (itemId) {
    const pl = await post('/dk/unity/product/list', { shopId, platform, pageNo: 1, pageSize: 1, messageItemIds: itemId });
    const p = pl && (pl.list || [])[0];
    if (p && String(p.productId) === itemId) {
      const qa = await post('/kb/qa/v1/product/qa/list', { shopId, productId: itemId, pageNo: 1, pageSize: 8 });
      out.product = { name: String(p.productName || '').slice(0, 160), status: p.platformProductStatus || '',
        from_words: fromWords,
        price: p.productMinPrice === p.productMaxPrice ? String(p.productMinPrice) : `${p.productMinPrice} - ${p.productMaxPrice}`,
        stock: p.productStock,
        variations: (p.items || []).filter(i => i.itemId).slice(0, 30)
          .map(i => `${i.attribute || i.itemSku}: $${i.price}, ${i.stock > 0 ? i.stock + ' in stock' : 'OUT OF STOCK'}`),
        qa: ((qa && qa.list) || []).slice(0, 6).map(q => ({ q: String(q.question || '').slice(0, 200), a: String(q.answer || '').slice(0, 300) })) };
    }
  }
  return JSON.stringify(out);
})
"""


_CATALOG = r"""
(async (shopId, platform) => {
  const v3 = document.querySelector('#app-vue3') && document.querySelector('#app-vue3').__vue_app__;
  const pin = v3 && v3.config.globalProperties.$pinia;
  if (!pin) return JSON.stringify({ ok: false });
  const h = { 'x-access-token': pin.state.value.runtime.token, 'X-Duoke-Version': 'v2', 'Content-Type': 'application/json' };
  const rows = [];
  for (let pg = 1; pg <= 15; pg++) {
    const r = await (await fetch('/api/v1/dk/unity/product/list', { method: 'POST', headers: h,
      body: JSON.stringify({ shopId, platform, pageNo: pg, pageSize: 100 }) })).json();
    if (!r || r.code !== 0) break;
    for (const p of r.data.list || []) rows.push({ id: String(p.productId), name: String(p.productName || ''),
      ok: p.platformProductStatus === 'NORMAL', stock: p.productStock,
      price: p.productMinPrice === p.productMaxPrice ? String(Number(p.productMinPrice))
        : `${Number(p.productMinPrice)} - ${Number(p.productMaxPrice)}`,
      vars: (p.items || []).filter(i => i.itemId).map(i => String(i.attribute || i.itemSku || '')).slice(0, 80) });
    if (rows.length >= Number(r.data.total) || !(r.data.list || []).length) break;
  }
  return JSON.stringify({ ok: true, rows });
})
"""


def catalog(shop_id: str, platform: str = "shopee") -> list[dict]:
    """Every listing in one shop as [{id, name, ok, vars}] -- read from
    DuokeAI on this PC, for finding the product a buyer names in words."""
    data = _json(_run(f"({_CATALOG})({json.dumps(str(shop_id))}, {json.dumps(platform or 'shopee')})", timeout=40))
    return data.get("rows") or [] if data.get("ok") else []


def chat_context(shop_id: str, conversation_id: str, guess_id: str = "",
                 platform: str = "shopee") -> dict:
    """{orders: [...], product: {...} or None} for the draft; {} when DuokeAI
    is not there or the chat has neither."""
    data = _json(_run(f"({_CONTEXT})({json.dumps(str(shop_id))}, {json.dumps(str(conversation_id))}, "
                      f"{json.dumps(str(guess_id or ''))}, {json.dumps(platform or 'shopee')})", timeout=20))
    if not data.get("ok"):
        return {}
    return {k: data[k] for k in ("orders", "product") if data.get(k)}


def show_picker(conversation_id: str, options: list[dict]) -> str:
    """Show (or keep, or hide with no options) the reply picker for the open
    chat. options: [{label, text}]. Never sends anything."""
    opts = [{"label": str(o.get("label") or "")[:80], "text": str(o.get("text") or "").strip(),
             "photos": [str(u) for u in (o.get("photos") or []) if str(u).startswith("http")][:4]}
            for o in options if str(o.get("text") or "").strip()]
    load_photos([u for o in opts for u in o["photos"]])
    return str(_run(f"({_PICKER})({json.dumps(str(conversation_id or ''))}, {json.dumps(opts)})") or "")


def put_draft(text: str) -> str:
    """Leave `text` in the open conversation's reply box without sending it.

    'typed', 'busy' (the box already holds something), or why not."""
    raw = _run(f"({_PUT_DRAFT})({json.dumps(str(text or ''))})")
    return str(raw) if raw else (last_error() or "the page did not answer")


# ---- reading WITHOUT opening anything
#
# DuoKe keeps its conversation list in its own store and fetches a
# conversation's messages from its own API. Both are read here straight from
# the page, so nothing on screen changes: no chat is opened, nothing is marked
# read, and whoever is working in DuoKe never sees a thing. This is what lets
# a draft be written in the background and only put in the box once the
# person opens that chat themselves.
# DuoKe was rebuilt as DuokeAI (2.0, Nov 2026): Vue 3 with Pinia stores
# (chatSession, chatConversation, chatMessage) instead of one Vuex "Chat"
# module, and its API is called with fetch and the login token rather than
# through vm.$http. _DK gives every snippet below the same few things on
# either app, so the old DuoKe still works on a PC not yet upgraded.
_DK = r"""
  const _v3 = document.querySelector('#app-vue3') && document.querySelector('#app-vue3').__vue_app__;
  const _pinia = _v3 && _v3.config.globalProperties.$pinia;
  const _vm = !_pinia && document.querySelector('#app') && document.querySelector('#app').__vue__;
  const DK = {
    ok: !!(_pinia || (_vm && _vm.$store)),
    v3: !!_pinia,
    sessions: () => _pinia ? (_pinia.state.value.chatSession || {}).sessions || []
                           : ((_vm.$store.state.Chat || {}).sessions || []),
    shopIds: () => _pinia ? (_pinia.state.value.chatSession || {}).allowShopIds || []
                          : _vm.$store.state.Chat.allowShopIds,
    openId: () => String((_pinia ? (_pinia.state.value.chatConversation || {}).conversationId
                                  : _vm.$store.state.Chat.conversationId) || ''),
    api: async (path, body, query) => {
      const h = { 'x-access-token': _pinia.state.value.runtime.token, 'X-Duoke-Version': 'v2' };
      let url = '/api/v1/im' + path;
      if (query) url += '?' + new URLSearchParams(query).toString();
      const opt = body ? { method: 'POST', body: JSON.stringify(body),
                           headers: { ...h, 'Content-Type': 'application/json' } } : { headers: h };
      const r = await (await fetch(url, opt)).json();
      return r && r.code === 0 ? r.data : null;
    },
    conversations: async (size, offset) => _pinia
      ? await DK.api('/conversation/queryConversationList', { shopIdList: DK.shopIds(), size, offset, filterGroups: [] })
      : await _vm.$http.queryConversationList({ shopIdList: DK.shopIds(), size, offset, filterGroups: [] }),
    messages: async (shopId, conversationId, platform) => _pinia
      ? await DK.api('/message/list', null, { pageNo: 1, pageSize: 40, shopId, conversationId, platform: platform || 'shopee' })
      : await _vm.$http.getImMessageList({ shopId, conversationId, pageSize: 40 }),
    send: async (msg) => _pinia ? await _pinia._s.get('chatMessage').sendMessage({ msg })
                                : await _vm.$store.dispatch('Chat/send-message', { msg }),
  };
"""

_SESSIONS = r"""
(() => {""" + _DK + r"""
  if (!DK.ok) return JSON.stringify({ ok: false });
  return JSON.stringify({ ok: true, open: DK.openId(),
    rows: DK.sessions().map(s => ({
      conversation_id: String(s.conversationId), shop_id: String(s.shopId),
      name: s.buyerNick || '', shop: s.shopName || '',
      unread: (s.unReadCount || s.unreadCount || 0) > 0,
      platform: s.platform || '',
      last_id: String(s.lastMessageId || s.latestMessageId || '') })) });
})()
"""

_MESSAGES = r"""
(async (shopId, conversationId) => {""" + _DK + r"""
  const s = DK.sessions().find(x => String(x.conversationId) === conversationId);
  const r = await DK.messages(shopId, conversationId, s && s.platform);
  const out = [];
  for (const m of (r && r.list) || []) {
    // 1 is the buyer, 2 the shop, 3 DuoKe's own notices
    if (m.fromAccountType !== 1 && m.fromAccountType !== 2) continue;
    // Shopee's away message and DuoKe's welcome/follow rules are not people
    if (m.fromAccountType === 2 && (m.platformReplyType === 1 || m.dkReplyType === 2)) continue;
    let c = m.messageContent;
    try { c = JSON.parse(c); } catch (e) { c = {}; }
    c = c || {};
    let text = c.text || '';
    const imgs = c.imageUrl ? [c.imageUrl] : [];
    const isProduct = ['item', 'product', 'variation_card', 'product_card'].includes(m.messageType);
    if (!text && isProduct) text = '[product] ' + (c.title || c.itemName || c.name || c.skuValue || c.itemId || '');
    if (!text && m.messageType === 'order') text = '[order]';
    if (!text && !imgs.length) continue;
    out.push({ inbound: m.fromAccountType === 1, text: String(text).slice(0, 1200), imgs, product: isProduct, product: isProduct,
               ts: m.createdTimestamp || 0 });
  }
  out.sort((a, b) => a.ts - b.ts);   // the API gives newest first
  return JSON.stringify({ ok: true, lines: out });
})
"""


def list_sessions() -> tuple[list[dict], str]:
    """DuoKe's loaded conversations as [{conversation_id, shop_id, name, shop,
    unread, last_id}], plus the id of the one open on screen. Opens nothing."""
    data = _json(_run(_SESSIONS))
    if not data.get("ok"):
        return [], ""
    return data.get("rows") or [], str(data.get("open") or "")


def open_conversation_id() -> str:
    """Which conversation the person has open, or ''."""
    return list_sessions()[1]


def read_conversation(shop_id: str, conversation_id: str) -> list[dict]:
    """One conversation as [{inbound, text, imgs}], oldest first, fetched from
    DuoKe's API -- the chat is not opened and stays unread."""
    data = _json(_run(f"({_MESSAGES})({json.dumps(str(shop_id))}, {json.dumps(str(conversation_id))})"))
    if not data.get("ok"):
        return []
    return [{"inbound": bool(l.get("inbound")), "text": str(l.get("text") or "").strip(),
             "imgs": [str(u) for u in (l.get("imgs") or []) if str(u).startswith("http")],
             "product": bool(l.get("product"))}
            for l in data.get("lines") or []]


_ALL = r"""
(async (pages) => {""" + _DK + r"""
  if (!DK.ok) return JSON.stringify({ ok: false });
  const rows = [];
  let offset = 0;
  for (let i = 0; i < pages; i++) {
    const r = await DK.conversations(100, offset);
    for (const s of (r && r.list) || []) rows.push({
      conversation_id: String(s.conversationId), shop_id: String(s.shopId),
      name: s.buyerNick || '', shop: s.shopName || '',
      unread: (s.unReadCount || s.unreadCount || 0) > 0,
      platform: s.platform || '',
      last_id: String(s.lastMessageId || s.latestMessageId || '') });
    if (!r || !r.hasMore) break;
    offset = r.nextOffset;
  }
  return JSON.stringify({ ok: true, rows });
})
"""


def all_sessions(pages: int = 3) -> list[dict]:
    """Every conversation across all shops and tabs (newest first, up to
    100 x pages), from DuoKe's API -- not just the ones the list on screen has
    loaded. Same row shape as list_sessions. Opens nothing."""
    data = _json(_run(f"({_ALL})({int(pages)})"))
    return data.get("rows") or [] if data.get("ok") else []


def signature() -> str:
    """Changes whenever a recent conversation gets a new message or its unread
    count moves -- a cheap way to know a pass is worth running now."""
    rows = all_sessions(1)
    return "|".join(f"{r['conversation_id']}:{r['last_id']}:{int(r['unread'])}" for r in rows)


def reply_box_text() -> Optional[str]:
    """What is in the open chat's reply box now, or None when it cannot be
    read. The send guard's question: an Enter with words in this box sends
    them."""
    data = _json(_run(_BOX_TEXT))
    return str(data.get("text") or "") if data.get("ok") else None


# Sending WITHOUT opening the chat: DuoKe's own send, the one its reply box
# calls. Used for exactly one thing -- a reply a person pressed Send on in
# Pigu -- and only from inside duoke.approved_send() (the caller checks).
_SEND = r"""
(async (shopId, conversationId, text) => {""" + _DK + r"""
  const r = await DK.conversations(100, 0);
  const s = ((r && r.list) || []).find(x => String(x.conversationId) === conversationId
                                          && String(x.shopId) === shopId)
         || DK.sessions().find(x => String(x.conversationId) === conversationId);
  if (!s) return 'conversation not found';
  await DK.send({
    shopId: s.shopId, platform: s.platform, groupId: s.groupId,
    conversationId: s.conversationId, content: { text }, contentType: 'text' });
  return 'dispatched';
})
"""


def send_text(shop_id: str, conversation_id: str, text: str) -> str:
    """Send `text` into a conversation without opening it. 'sent' once DuoKe's
    API shows it as the shop's newest line, otherwise why not."""
    got = _run(f"({_SEND})({json.dumps(str(shop_id))}, {json.dumps(str(conversation_id))}, "
               f"{json.dumps(str(text))})")
    if got != "dispatched":
        return str(got or last_error() or "the page did not answer")
    want = " ".join(str(text).split())
    for _ in range(10):
        time.sleep(1.0)
        mine = [l for l in read_conversation(shop_id, conversation_id) if not l["inbound"]]
        if mine and " ".join(mine[-1]["text"].split()) == want:
            return "sent"
    return "sent, but DuoKe has not shown it yet"


# A photo the same way: uploaded through DuoKe's own uploader (the one its
# screenshot button uses), then sent as an image message. Same rule as
# _SEND -- only for something a person pressed Send on in Pigu, inside
# duoke.approved_send().
_SEND_IMAGE = r"""
(async (shopId, conversationId, b64, name) => {""" + _DK + r"""
  const r = await DK.conversations(100, 0);
  const s = ((r && r.list) || []).find(x => String(x.conversationId) === conversationId
                                          && String(x.shopId) === shopId)
         || DK.sessions().find(x => String(x.conversationId) === conversationId);
  if (!s) return 'conversation not found';
  const bytes = Uint8Array.from(atob(b64), c => c.charCodeAt(0));
  const file = new File([bytes], name, { type: 'image/jpeg' });
  const base = { shopId: s.shopId, platform: s.platform, groupId: s.groupId,
                 conversationId: s.conversationId, contentType: 'image' };
  if (DK.v3) {
    // DuokeAI uploads inside its own send: a message marked "uploading" with
    // the file attached, exactly what its photo button hands over
    await DK.send({ ...base, dkMessageType: 'image', feMessageStatus: 'uploading',
                    contentFile: { file }, content: { imageUrl: URL.createObjectURL(file) } });
    return 'dispatched';
  }
  const url = await document.querySelector('#app').__vue__.$commFn.uploadFile({ file });
  if (!url) return 'DuoKe would not take the photo';
  await DK.send({ ...base, content: { imageUrl: url } });
  return 'dispatched';
})
"""


def send_image(shop_id: str, conversation_id: str, image: bytes, name: str = "photo.jpg") -> str:
    """Send a JPEG into a conversation without opening it. 'sent' once DuoKe's
    API shows a picture as the shop's newest line."""
    import base64
    got = _run(f"({_SEND_IMAGE})({json.dumps(str(shop_id))}, {json.dumps(str(conversation_id))}, "
               f"{json.dumps(base64.b64encode(image).decode('ascii'))}, {json.dumps(name)})",
               timeout=60)
    if got != "dispatched":
        return str(got or last_error() or "the page did not answer")
    for _ in range(15):
        time.sleep(1.0)
        mine = [l for l in read_conversation(shop_id, conversation_id) if not l["inbound"]]
        if mine and mine[-1]["imgs"]:
            return "sent"
    return "sent, but DuoKe has not shown it yet"
