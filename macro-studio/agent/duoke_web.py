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
  const ul = document.querySelector('ul.el-autocomplete-suggestion__list');
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

_PICK_SHORTCUT = r"""
((code) => {
  const ul = document.querySelector('ul.el-autocomplete-suggestion__list');
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


def use_shortcut(code: str) -> str:
    """Put one quick reply, photos and all, in an empty reply box. Not sent.

    'picked', 'busy', or why not."""
    opened = _json(_run(_OPEN_SHORTCUTS))
    if not opened.get("ok"):
        return opened.get("why") or last_error() or "the page did not answer"
    time.sleep(1.0)
    got = _run(f"({_PICK_SHORTCUT})({json.dumps(code)})")
    if got != "picked":
        _run(_CLEAR_SLASH)
        return str(got or last_error() or "the page did not answer")
    time.sleep(1.0)
    return "picked"


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
_SESSIONS = r"""
(() => {
  const vm = document.querySelector('#app') && document.querySelector('#app').__vue__;
  const chat = vm && vm.$store && vm.$store.state.Chat;
  if (!chat || !Array.isArray(chat.sessions)) return JSON.stringify({ ok: false });
  return JSON.stringify({ ok: true, open: String(chat.conversationId || ''),
    rows: chat.sessions.map(s => ({
      conversation_id: String(s.conversationId), shop_id: String(s.shopId),
      name: s.buyerNick || '', shop: s.shopName || '',
      unread: (s.unReadCount || s.unreadCount || 0) > 0,
      last_id: String(s.lastMessageId || s.latestMessageId || '') })) });
})()
"""

_MESSAGES = r"""
(async (shopId, conversationId) => {
  const vm = document.querySelector('#app').__vue__;
  const r = await vm.$http.getImMessageList({ shopId, conversationId, pageSize: 40 });
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
    if (!text && m.messageType === 'item' && c.title) text = '[product] ' + c.title;
    if (!text && m.messageType === 'order') text = '[order]';
    if (!text && !imgs.length) continue;
    out.push({ inbound: m.fromAccountType === 1, text: String(text).slice(0, 1200), imgs,
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
             "imgs": [str(u) for u in (l.get("imgs") or []) if str(u).startswith("http")]}
            for l in data.get("lines") or []]


_ALL = r"""
(async (pages) => {
  const vm = document.querySelector('#app').__vue__;
  const chat = vm.$store.state.Chat;
  const rows = [];
  let offset = 0;
  for (let i = 0; i < pages; i++) {
    const r = await vm.$http.queryConversationList({ shopIdList: chat.allowShopIds,
      size: 100, offset, filterGroups: [] });
    for (const s of (r && r.list) || []) rows.push({
      conversation_id: String(s.conversationId), shop_id: String(s.shopId),
      name: s.buyerNick || '', shop: s.shopName || '',
      unread: (s.unReadCount || s.unreadCount || 0) > 0,
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
