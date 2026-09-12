"""Seed labels: queued on the tablet, printed here on the P-touch.

The bundle is packed on a tablet, and the P-touch is plugged into the PC in
the packing area. A tablet cannot reach that printer -- not over the network
(the app is served over https and a browser will not let an https page talk
to a plain-http box on the LAN), and not through a print dialog either,
because a dialog is exactly the extra step this is meant to remove.

So the tablet writes the label down and this prints it. Pressing print in
Pigu inserts a row in `label_jobs`; the agent on the packing PC is signed in
to the same project, sees it within a few seconds, draws the words and hands
the bitmap straight to the Windows driver. No dialog, no P-touch Editor
window, no template file to keep in step with anything -- the label is out of
the machine before anybody could have pressed OK.

The tape itself is not our business. Whatever width is loaded and whatever
length rule is set in the printer's own Printing Preferences is what the
driver reports as its printable area, and the label is drawn to fill exactly
that. Change the tape, change nothing here.

Settings live in labels.json beside duoke.json, and are edited the same way:
open the file, fill it in, save. The Supabase four are left blank by default
and borrowed from duoke.json when they are -- it is the same project and the
same account, and typing a password into two files is how one of them ends
up wrong.
"""
from __future__ import annotations

import json
import platform
import threading
from datetime import datetime, timezone
from typing import Any, Optional

from PIL import Image, ImageDraw, ImageFont

from agent import config, duoke
from agent.duoke import Cloud, CloudError

CONFIG_PATH = config.ROOT_DIR / "labels.json"

DEFAULTS: dict[str, Any] = {
    # Off until somebody fills this in. On means the agent watches for labels
    # to print for as long as it is running.
    "enabled": False,
    # Supabase -- leave all four blank to use duoke.json's. Fill them in only
    # if labels are meant to go through a different project or account.
    "supabase_url": "",
    "supabase_anon_key": "",
    "email": "",
    "password": "",
    # Which Windows printer to print on. Blank means the first one that looks
    # like a P-touch, which on a PC with one label printer is the right
    # answer. GET /api/labels/status lists the names to copy from.
    "printer": "",
    # The label itself, in millimetres. 62 is the width of the roll; 15 is how
    # far down it each label runs, which is fixed rather than grown to fit the
    # words -- a drawer of labels that are all the same height is one that can
    # be cut in a stack. Both are pushed at the driver as a custom page size
    # every time a job starts, so a Preferences dialog somebody changed last
    # week cannot quietly make them something else.
    "label_width_mm": 62,
    "label_height_mm": 15,
    # Two names across the 62mm, cut down the middle afterwards: one press of
    # the roll for two labels. Set to 1 to put one name on the full width.
    "columns": 2,
    # The dashes down the middle to cut along. Off leaves it blank.
    "cut_line": True,
    # How big the words are, in millimetres, and the same on every label. Type
    # fitted to each name means Tomato comes out twice the size of Pak Choi
    # Green Stem, and a drawer of labels that disagree with each other looks
    # like a mistake even when every one of them is right. So the size is set
    # here and held: a long name wraps onto a second line rather than
    # shrinking. It only gives way when a name will not fit the half-label at
    # all even wrapped, which is the alternative to printing it over the cut
    # line. 0 means the old behaviour -- as big as each name can be.
    "font_mm": 5,
    # How often to look. This is somebody standing at a machine waiting for a
    # label, so it is seconds, not the twenty a chat sync can afford.
    "poll_seconds": 3,
    # Only print labels queued for this shop, e.g. "planttalks". Blank prints
    # whatever is queued, which is right when one PC serves one floor.
    "store": "",
    # A label queued and never printed -- the PC was off, the tape ran out --
    # is stale by the time anybody notices. Older than this and it is failed
    # rather than printed, so a machine switched on at four o'clock does not
    # spit out the morning's labels at somebody's back.
    "stale_minutes": 30,
}

_lock = threading.RLock()
_cache: Optional[dict] = None
_cache_stamp: float = -1.0       # the file's mtime when the cache was filled

# One label at a time. Two print jobs racing into the same DC is a jam, and
# nobody is pressing two buttons at once anyway.
_print_lock = threading.Lock()

# GDI's GetDeviceCaps indices, and the one DEVMODE constant, named rather
# than magic.
_HORZRES = 8
_VERTRES = 10
_LOGPIXELSX = 88
_LOGPIXELSY = 90
_DMPAPER_USER = 256

# Bold, because a seed label is read at arm's length in a warehouse. Tried in
# order; the last resort is PIL's own bitmap font, which is ugly but prints.
_FONT_CANDIDATES = (
    r"C:\Windows\Fonts\arialbd.ttf",
    r"C:\Windows\Fonts\segoeuib.ttf",
    r"C:\Windows\Fonts\calibrib.ttf",
    r"C:\Windows\Fonts\arial.ttf",
)

# A name that will not fit on one line is better on two or three than shrunk
# until it is unreadable.
_MAX_LINES = 3

MAX_PER_PASS = 40


class LabelError(RuntimeError):
    """Something the person at the machine can act on."""


# ---------------------------------------------------------------- settings


def load() -> dict:
    """The settings as the file has them now, not as it had them at boot.

    This file is meant to be opened and edited by hand -- that is what the
    README tells whoever sets the printer up. A cache that is filled once and
    never looked at again turns that instruction into a lie: the file says one
    printer, the agent goes on using the one it read at breakfast, and the
    error that comes back names a printer nobody can find in the settings. So
    the file's own timestamp decides whether the cache still stands.
    """
    global _cache, _cache_stamp
    with _lock:
        try:
            stamp = CONFIG_PATH.stat().st_mtime
        except OSError:
            stamp = 0.0
        if _cache is not None and stamp == _cache_stamp:
            return _cache
        saved: dict = {}
        if CONFIG_PATH.exists():
            try:
                saved = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                saved = {}
        merged = dict(DEFAULTS)
        merged.update({k: v for k, v in saved.items() if k in DEFAULTS})
        _cache, _cache_stamp = merged, stamp
        return _cache


def save(patch: dict) -> dict:
    global _cache, _cache_stamp
    with _lock:
        data = dict(load())
        data.update({k: v for k, v in (patch or {}).items()
                     if k in DEFAULTS and v is not None})
        CONFIG_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")
        _cache = data
        try:
            _cache_stamp = CONFIG_PATH.stat().st_mtime
        except OSError:
            _cache_stamp = 0.0
        return data


def redacted() -> dict:
    data = dict(load())
    data["password"] = "*" * len(data.get("password") or "")
    data["supabase_anon_key"] = (data.get("supabase_anon_key") or "")[:12] + "…" \
        if data.get("supabase_anon_key") else ""
    return data


def _credentials() -> tuple[str, str, str, str]:
    """Ours if they are filled in, DuoKe's if they are not."""
    cfg = load()
    mine = (cfg.get("supabase_url", ""), cfg.get("supabase_anon_key", ""),
            cfg.get("email", ""), cfg.get("password", ""))
    if all(mine):
        return mine
    d = duoke.load()
    return (cfg.get("supabase_url") or d.get("supabase_url", ""),
            cfg.get("supabase_anon_key") or d.get("supabase_anon_key", ""),
            cfg.get("email") or d.get("email", ""),
            cfg.get("password") or d.get("password", ""))


# ---------------------------------------------------------------- printers


def _win32():
    """win32print/win32ui, or a LabelError saying what to install.

    Imported here rather than at the top so the agent still starts on a
    machine that has never printed a label, and so the message arrives at
    whoever pressed the button instead of in the console at boot.
    """
    if platform.system() != "Windows":
        raise LabelError("Label printing only works on Windows.")
    try:
        import win32print
        import win32ui
        from PIL import ImageWin
    except ImportError as exc:
        raise LabelError(
            "pywin32 is not installed in this agent's environment -- close the "
            "agent window and run start.bat again to install it."
        ) from exc
    return win32print, win32ui, ImageWin


def _looks_like_label_printer(name: str) -> bool:
    low = name.lower()
    return ("brother" in low or "p-touch" in low or "ptouch" in low
            or low.startswith("pt-") or " pt-" in low or "ql-" in low)


def list_printers() -> list[dict]:
    """Every printer Windows knows about, with the label ones marked.

    Marked, not filtered: the guess below is a good one and it is still a
    guess, so it only decides what is offered first, never what is hidden.
    """
    win32print, _, _ = _win32()
    flags = win32print.PRINTER_ENUM_LOCAL | win32print.PRINTER_ENUM_CONNECTIONS
    names = [p[2] for p in win32print.EnumPrinters(flags, None, 1)]
    try:
        default = win32print.GetDefaultPrinter()
    except Exception:
        default = ""
    return [{"name": n, "likely": _looks_like_label_printer(n),
             "default": n == default} for n in sorted(set(names))]


def chosen_printer() -> str:
    """The printer labels go to: the one that was named, if it is still there.

    A name that has been unplugged or renamed since it was written down is
    worse than no choice at all -- it fails at the machine rather than here --
    so a saved name Windows no longer lists falls back to the best guess.
    """
    saved = (load().get("printer") or "").strip()
    try:
        printers = list_printers()
    except LabelError:
        return saved
    names = [p["name"] for p in printers]
    if saved and saved in names:
        return saved
    for p in printers:
        if p["likely"]:
            return p["name"]
    return ""


# ---------------------------------------------------------------- drawing


def _font(size: int):
    for path in _FONT_CANDIDATES:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _wrap(text: str, lines: int) -> list[str]:
    """Break text into `lines` roughly equal parts, on word breaks.

    Greedy by character count rather than by measured width: the break is
    chosen before a font size exists, and the size is then fitted to whatever
    this produced. A word longer than its share is left whole -- a seed name
    is not improved by being hyphenated.
    """
    words = text.split()
    if lines <= 1 or len(words) <= 1:
        return [text]
    target = max(1, len(text) // lines)
    out: list[str] = []
    cur = ""
    for w in words:
        if cur and len(cur) + 1 + len(w) > target and len(out) < lines - 1:
            out.append(cur)
            cur = w
        else:
            cur = f"{cur} {w}".strip()
    if cur:
        out.append(cur)
    return out


def _measure(draw, rows: list[str], font) -> tuple[int, int]:
    w = h = 0
    gap = max(2, getattr(font, "size", 10) // 6)
    for row in rows:
        box = draw.textbbox((0, 0), row, font=font)
        w = max(w, box[2] - box[0])
        h += (box[3] - box[1]) + gap
    return w, h


def _fit(draw, text: str, box_w: float, box_h: float, max_lines: int):
    """The biggest type that fits, and how it has to be broken to get there.

    Only used when font_mm is 0. Every line count up to max_lines is tried and
    whichever lets the type be biggest wins.
    """
    best_rows, best_font, best_size = [text], _font(6), 0
    for lines in range(1, max_lines + 1):
        rows = _wrap(text, lines)
        if lines > 1 and len(rows) != lines:
            break                       # no more words to break on
        lo, hi, fit = 5, max(8, int(box_h)), None
        while lo <= hi:                 # binary search the largest size that fits
            mid = (lo + hi) // 2
            font = _font(mid)
            w, h = _measure(draw, rows, font)
            if w <= box_w and h <= box_h:
                fit = (mid, font)
                lo = mid + 1
            else:
                hi = mid - 1
        if fit and fit[0] > best_size:
            best_size, best_font, best_rows = fit[0], fit[1], rows
    return best_rows, best_font


def _wrap_to_width(draw, text: str, font, max_w: float, max_lines: int) -> list[str]:
    """Break on words at a size already decided, filling each line as far as
    it will go. A single word wider than the line stays on its own line and
    overhangs -- the shrink below is what deals with that."""
    rows: list[str] = []
    cur = ""
    for word in text.split():
        trial = (cur + " " + word).strip()
        if cur and draw.textlength(trial, font=font) > max_w:
            rows.append(cur)
            cur = word
            if len(rows) >= max_lines:
                break
        else:
            cur = trial
    if cur and len(rows) < max_lines:
        rows.append(cur)
    return rows or [text]


def _fit_at(draw, text: str, box_w: float, box_h: float, size: int, max_lines: int):
    """The asked-for size, held unless the name genuinely will not go.

    Wrapping is tried first, because two lines of the same size is what keeps
    a drawer of labels looking like one drawer. Only a name that still runs
    over the half it has is stepped down, and then only as far as it takes --
    a word printed across the cut line is worse than a word a point smaller.
    """
    size = max(5, int(size))
    while size >= 5:
        font = _font(size)
        rows = _wrap_to_width(draw, text, font, box_w, max_lines)
        w, h = _measure(draw, rows, font)
        if w <= box_w and h <= box_h:
            return rows, font
        size = int(size * 0.92) if int(size * 0.92) < size else size - 1
    font = _font(5)
    return _wrap_to_width(draw, text, font, box_w, max_lines), font


def _draw_cell(draw, text: str, left: int, top: int, width: int, height: int,
               size: int = 0) -> None:
    pad_x, pad_y = width * 0.06, height * 0.10
    box_w, box_h = width - 2 * pad_x, height - 2 * pad_y
    rows, font = (_fit_at(draw, text, box_w, box_h, size, 2) if size
                  else _fit(draw, text, box_w, box_h, 2))
    _, total_h = _measure(draw, rows, font)
    gap = max(2, getattr(font, "size", 10) // 6)
    y = top + (height - total_h) / 2
    for row in rows:
        b = draw.textbbox((0, 0), row, font=font)
        draw.text((left + (width - (b[2] - b[0])) / 2 - b[0], y - b[1]),
                  row, font=font, fill="black")
        y += (b[3] - b[1]) + gap


def render(texts, width: int, height: int, cut_line: bool = True,
           font_px: int = 0) -> Image.Image:
    """One pass of the roll: the names side by side across the 62mm.

    A name per column, left to right, and a dashed line down each join to cut
    along afterwards. font_px is the one type size every label uses; 0 lets
    each name be as big as its half will take. A pass with only one name left leaves the other half
    blank rather than repeating it -- somebody who asked for one label is not
    helped by being given two.
    """
    if isinstance(texts, str):
        texts = [texts]
    texts = [" ".join(str(t or "").split()) for t in texts] or [""]
    width, height = max(1, int(width)), max(1, int(height))
    img = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(img)

    cols = max(1, len(texts))
    cell = width / cols
    for n, text in enumerate(texts):
        if not text:
            continue
        _draw_cell(draw, text, int(n * cell), 0, int(cell), height, font_px)

    # the cut guides, drawn last so nothing sits on top of them
    if cut_line and cols > 1:
        step = max(3, height // 12)
        for n in range(1, cols):
            x = int(n * cell)
            for y in range(0, height, step * 2):
                draw.line([(x, y), (x, min(height, y + step))], fill="black", width=1)
    return img


# ---------------------------------------------------------------- printing


def _printer_dc(name: str, width_mm: float, height_mm: float):
    """A device context whose page is the label, not whatever the dialog was
    last left on.

    The size is pushed at the driver as a custom page every time a job starts,
    rather than being set once in Printing Preferences and trusted: 62 by 15
    is what makes a drawer of labels cuttable in a stack, and it must not
    become something else because somebody printed an address last Tuesday.

    Not every driver accepts a custom page. One that refuses falls back to its
    own settings, and the caller is told so -- the labels still come out, at
    whatever length the roll is set to, which is a thing to go and fix rather
    than a reason to print nothing.
    """
    win32print, win32ui, _ = _win32()
    import win32con
    import win32gui

    devmode = None
    try:
        handle = win32print.OpenPrinter(name)
        try:
            devmode = win32print.GetPrinter(handle, 2).get("pDevMode")
        finally:
            win32print.ClosePrinter(handle)
    except Exception as exc:
        raise LabelError(f'Could not open "{name}": {exc}') from exc

    if devmode is not None:
        try:
            devmode.PaperSize = _DMPAPER_USER
            devmode.PaperWidth = int(round(width_mm * 10))    # tenths of a mm
            devmode.PaperLength = int(round(height_mm * 10))
            devmode.Orientation = 1                            # portrait
            devmode.Fields |= (win32con.DM_PAPERSIZE | win32con.DM_PAPERWIDTH
                               | win32con.DM_PAPERLENGTH | win32con.DM_ORIENTATION)
            return win32ui.CreateDCFromHandle(
                win32gui.CreateDC("WINSPOOL", name, devmode)), True
        except Exception:
            pass

    try:
        dc = win32ui.CreateDC()
        dc.CreatePrinterDC(name)
    except Exception as exc:
        raise LabelError(f'Could not open "{name}": {exc}') from exc
    return dc, False


def print_labels(labels: list[dict], printer: Optional[str] = None, shop: str = "") -> dict:
    """Print everything asked for, two names to a pass of the roll.

    The labels are flattened first -- six of one name is six labels, not one
    label that says six -- and then taken in pairs across the 62mm, so the
    roll advances once for every two. An odd one at the end prints alone with
    the right-hand half blank.

    The whole lot goes out as a single print job, so a bundle's worth comes
    off in one run rather than as a dozen jobs the spooler may interleave with
    somebody else's.
    """
    _, _, ImageWin = _win32()
    cfg = load()
    name = printer or chosen_printer()
    if not name:
        raise LabelError("No label printer found. Name one in labels.json.")
    width_mm = float(cfg.get("label_width_mm") or 62)
    height_mm = float(cfg.get("label_height_mm") or 15)
    cols = max(1, min(4, int(cfg.get("columns") or 2)))
    cut_line = bool(cfg.get("cut_line", True))
    font_mm = float(cfg.get("font_mm") or 0)

    # PartyMonkey uses different label dimensions: 20mm height, 1 column
    if (shop or "").lower() == "partymonkey":
        height_mm = 20
        cols = 1
        cut_line = False

    flat: list[str] = []
    for entry in labels or []:
        text = " ".join(str((entry or {}).get("text") or "").split())
        if not text:
            continue
        copies = max(1, min(99, int((entry or {}).get("copies") or 1)))
        flat.extend([text] * copies)
    if not flat:
        raise LabelError("Nothing to print -- every label was empty.")
    passes = [flat[i:i + cols] for i in range(0, len(flat), cols)]

    with _print_lock:
        dc, forced = _printer_dc(name, width_mm, height_mm)
        note = "" if forced else (
            f'"{name}" would not take a custom page size, so the label is '
            "whatever length its Printing Preferences are set to.")
        try:
            page_w = dc.GetDeviceCaps(_HORZRES)
            page_h = dc.GetDeviceCaps(_VERTRES)
            dpi_x = dc.GetDeviceCaps(_LOGPIXELSX) or 300
            dpi_y = dc.GetDeviceCaps(_LOGPIXELSY) or 300
            if page_w <= 0 or page_h <= 0:
                raise LabelError(
                    f'"{name}" reports no printable area -- check the roll is in '
                    "and a label size is set in its Printing Preferences.")
            # What we asked for, in this device's dots. A driver that gave us a
            # longer page than we asked for gets the label drawn in the top of
            # it rather than stretched down it: 15mm of words is 15mm of words.
            full_w = max(1, int(round(width_mm / 25.4 * dpi_x)))
            full_h = max(1, int(round(height_mm / 25.4 * dpi_y)))
            want_w = max(1, min(page_w, full_w))
            want_h = max(1, min(page_h, full_h))
            # The type size is set in millimetres so it is the same on every
            # label whatever the printer's resolution is.
            font_px = int(round(font_mm / 25.4 * dpi_y)) if font_mm else 0
            # A driver can take the custom page without honouring it -- an
            # inkjet says yes and hands back A4 -- so what it reports is what
            # decides, not whether the call raised.
            if abs(page_h - full_h) > max(8, full_h * 0.15):
                forced = False
                note = (f'"{name}" gave back a page {round(page_h / dpi_y * 25.4)}mm '
                        f"long instead of {round(height_mm)}mm. The labels are drawn "
                        "at the top of it; set the roll length in its Printing "
                        "Preferences to stop the rest being fed through.")
            dc.StartDoc("Seed labels")
            try:
                for row in passes:
                    names = row + [""] * (cols - len(row))
                    dib = ImageWin.Dib(render(names, want_w, want_h, cut_line, font_px))
                    dc.StartPage()
                    dib.draw(dc.GetHandleOutput(), (0, 0, want_w, want_h))
                    dc.EndPage()
            finally:
                dc.EndDoc()
        except LabelError:
            raise
        except Exception as exc:
            raise LabelError(f'"{name}" refused the job: {exc}') from exc
        finally:
            try:
                dc.DeleteDC()
            except Exception:
                pass
    return {"printed": len(flat), "passes": len(passes), "printer": name,
            "size_mm": [width_mm, height_mm], "columns": cols, "font_mm": font_mm,
            "page_forced": forced, "note": note}


# ---------------------------------------------------------------- the queue


_state: dict[str, Any] = {
    "running": False,
    "last_pass_at": None,
    "last_error": "",
    "printed_total": 0,
    "last_label": "",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _queued(cloud: Cloud, store: str) -> list[dict]:
    query = ("/label_jobs?select=id,text,copies,store,created_at"
             "&status=eq.queued&order=created_at.asc&limit=" + str(MAX_PER_PASS))
    if store:
        query += "&store=eq." + store
    out = cloud.rest("GET", query)
    return out if isinstance(out, list) else []


def _claim(cloud: Cloud, row_id: str, device: str) -> bool:
    """Take one row, or find it already taken.

    Two PCs both signed in is a real arrangement -- a second packing bench --
    and the guard against both printing the same label is the status column:
    the update only matches a row that is still queued, so the loser gets an
    empty list back and moves on.
    """
    out = cloud.rest("PATCH", f"/label_jobs?id=eq.{row_id}&status=eq.queued",
                     {"status": "printing", "device": device, "claimed_at": _now()},
                     prefer="return=representation")
    return bool(out)


def _finish(cloud: Cloud, row_id: str, ok: bool, note: str = "") -> None:
    cloud.rest("PATCH", f"/label_jobs?id=eq.{row_id}",
               {"status": "printed" if ok else "failed",
                "printed_at": _now(), "error": note[:300]},
               prefer="return=minimal")


def _too_old(row: dict, minutes: int) -> bool:
    stamp = str(row.get("created_at") or "")
    if not stamp or minutes <= 0:
        return False
    try:
        made = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return False
    return (datetime.now(timezone.utc) - made).total_seconds() > minutes * 60


def sync_once() -> dict:
    """One pass: everything queued, printed, marked off.

    The whole pass goes to the printer as one job -- a bundle's labels come
    off the tape in one run -- and only then are the rows marked, so a crash
    mid-pass leaves them claimed rather than falsely printed.
    """
    cfg = load()
    report: dict[str, Any] = {"printed": 0, "labels": 0, "stale": 0, "notes": []}
    url, anon, email, password = _credentials()
    cloud = Cloud(url, anon, email, password)
    device = (platform.node() or "packing PC")[:60]

    rows = _queued(cloud, (cfg.get("store") or "").strip())
    if not rows:
        _state["last_pass_at"] = _now()
        return report

    mine: list[dict] = []
    for row in rows:
        if _too_old(row, int(cfg.get("stale_minutes") or 0)):
            report["stale"] += 1
            _finish(cloud, row["id"], False, "too old to print -- queued while the PC was off")
            continue
        if _claim(cloud, row["id"], device):
            mine.append(row)
    if not mine:
        _state["last_pass_at"] = _now()
        return report

    try:
        # No printer named here on purpose. The name in the file is a
        # preference, not an instruction: chosen_printer() checks it against
        # what Windows actually has and falls back to the P-touch sitting
        # right there when it does not match. Handing the raw string down
        # skipped that check on the one path that matters -- somebody at a
        # tablet got "the printer name is invalid" about a printer they had
        # already corrected, while the test button, which does go through
        # chosen_printer(), printed perfectly.
        shop = mine[0].get("store", "").strip() if mine else ""
        out = print_labels([{"text": r.get("text"), "copies": r.get("copies")} for r in mine], shop=shop)
    except LabelError as exc:
        for row in mine:
            _finish(cloud, row["id"], False, str(exc))
        report["notes"].append(str(exc))
        _state["last_error"] = str(exc)
        _state["last_pass_at"] = _now()
        return report

    for row in mine:
        _finish(cloud, row["id"], True)
    report["printed"] = out["printed"]
    report["labels"] = len(mine)
    _state["printed_total"] += out["printed"]
    _state["last_label"] = str(mine[-1].get("text") or "")
    _state["last_error"] = ""
    _state["last_pass_at"] = _now()
    return report


def status() -> dict:
    cfg = load()
    url, anon, email, password = _credentials()
    try:
        printers = list_printers()
        printer_error = ""
    except LabelError as exc:
        printers, printer_error = [], str(exc)
    # A name in the file that Windows does not have is not fatal -- the P-touch
    # is found anyway -- but it is worth saying out loud, or the settings and
    # the machine go on disagreeing and nobody knows which one is printing.
    saved_name = (cfg.get("printer") or "").strip()
    printer = chosen_printer()
    printer_note = ""
    if saved_name and printer and saved_name != printer:
        printer_note = (f'labels.json asks for "{saved_name}", which Windows does '
                        f'not have. Printing on "{printer}" instead.')
    return {
        "configured": bool(url and anon and email and password),
        "enabled": bool(cfg.get("enabled")),
        "watching": bool(_state["running"]),
        "poll_seconds": int(cfg.get("poll_seconds") or 3),
        "store": cfg.get("store") or "",
        "printer": printer,
        "printer_saved": saved_name,
        "printer_note": printer_note,
        "printer_error": printer_error,
        "printers": printers,
        "printed_total": _state["printed_total"],
        "last_label": _state["last_label"],
        "last_pass_at": _state["last_pass_at"],
        "last_error": _state["last_error"],
        "config_file": str(CONFIG_PATH),
    }


class Watcher:
    """Calls sync_once on a timer for as long as the agent is up.

    One thread, holding no lock: a pass that hangs on the printer must not
    take the web UI down with it, so the only thing shared with the rest of
    the agent is the _state dict it writes at the end of each pass.
    """

    def __init__(self) -> None:
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> bool:
        if self._thread and self._thread.is_alive():
            return False
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="label-watcher", daemon=True)
        self._thread.start()
        _state["running"] = True
        return True

    def stop(self) -> None:
        self._stop.set()
        _state["running"] = False

    def _run(self) -> None:
        self._stop.wait(4)
        while not self._stop.is_set():
            cfg = load()
            if cfg.get("enabled"):
                try:
                    sync_once()
                except Exception as exc:        # a pass must never kill the loop
                    _state["last_error"] = f"{type(exc).__name__}: {exc}"
                    # a project that cannot be reached is not worth hammering
                    if isinstance(exc, CloudError):
                        self._stop.wait(10)
            self._stop.wait(max(2, int(cfg.get("poll_seconds") or 3)))
        _state["running"] = False


watcher = Watcher()
