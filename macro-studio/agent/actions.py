"""Extra action types beyond click/key replay: Chrome URLs, file search
and file ops, clipboard, and {{variable}} substitution. Kept separate
from replay.py's click/key/UIA engine since these don't touch a window
or need element resolution.

Every function here either returns a plain value or raises RuntimeError
with a message written to be shown directly to the user -- replay.py
catches that and puts it straight into the step's failure reason, so
messages here should already be the whole explanation.
"""
from __future__ import annotations

import base64
import os
import re
import shutil
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Optional

VAR_PATTERN = re.compile(r"\{\{([^}:]+)(?::([^}]+))?\}\}")

# Stripping the leading zero off an hour is a C library flag, and the two
# families spell it differently. Windows is the only one that matters here,
# but a wrong flag silently prints "#I" into a filename, so both are named.
_NO_PAD = "#" if os.name == "nt" else "-"

# Friendly date tokens, because nobody should need strftime to name a file.
# Longest first, so YYYY matches before YY -- and "hh" before "h", or a
# two-digit hour would come out as one digit followed by a stray "h".
_DATE_TOKENS = [
    ("YYYY", "%Y"), ("YY", "%y"), ("MMMM", "%B"), ("MMM", "%b"), ("MM", "%m"),
    ("DDDD", "%A"), ("DDD", "%a"), ("DD", "%d"),
    ("HH", "%H"), ("hh", "%I"), ("h", f"%{_NO_PAD}I"),
    ("mm", "%M"), ("ss", "%S"), ("AP", "%p"),
]
DEFAULT_DATE_FORMAT = "YYYY-MM-DD"
DEFAULT_TIME_FORMAT = "HH-mm-ss"


def _strftime_pattern(fmt: str) -> str:
    """Turns "DD-MM-YYYY" into "%d-%m-%Y", leaving any %-codes alone so
    anyone who does know strftime can write it directly."""
    out, i = [], 0
    while i < len(fmt):
        if fmt[i] == "%" and i + 1 < len(fmt):
            out.append(fmt[i:i + 2])
            i += 2
            continue
        for token, code in _DATE_TOKENS:
            if fmt.startswith(token, i):
                out.append(code)
                i += len(token)
                break
        else:
            out.append(fmt[i])
            i += 1
    return "".join(out)


_DATE_SEPARATORS = set(" ._-/:,")


def _looks_like_date_format(name: str) -> bool:
    """True for {{DD.MM}} and friends -- a format written on its own, with
    no "date:" in front of it. People reach for that spelling constantly,
    and treating it as a missing variable turns it into an empty string or
    a filename with braces in it, neither of which reads as a mistake
    until you go looking for the file."""
    if not any(token in name for token in ("YYYY", "YY", "MM", "DD", "HH", "ss")):
        return False
    letters = {c for c in name if c not in _DATE_SEPARATORS}
    return letters.issubset(set("YMDHhmsAP"))


def _builtin_value(name: str, fmt: Optional[str]) -> Optional[str]:
    """{{date}}, {{time}} and {{now}} are always available -- no step has to
    produce them first. Naming a file after today is the commonest thing a
    macro needs that it cannot click its way to."""
    now = datetime.now()
    if name == "date":
        return now.strftime(_strftime_pattern(fmt or DEFAULT_DATE_FORMAT))
    if name == "time":
        return now.strftime(_strftime_pattern(fmt or DEFAULT_TIME_FORMAT))
    if name == "now":
        return now.strftime(_strftime_pattern(fmt or (DEFAULT_DATE_FORMAT + " " + DEFAULT_TIME_FORMAT)))
    return None


def substitute(value, variables: dict):
    """Replaces {{name}} in a string with str(variables[name]) (empty string
    if the variable was never set). Lists join with ", ". Non-string values
    pass through untouched.

    {{date}}, {{time}} and {{now}} are built in and take an optional format
    after a colon -- {{date:DD-MM-YYYY}} -- so a step can name a file after
    today without anything having to fetch the date first. A variable of the
    same name set by an earlier step wins, since that was deliberate."""
    if not isinstance(value, str) or "{{" not in value:
        return value

    def _sub(m: re.Match) -> str:
        name, fmt = m.group(1), m.group(2)
        if name in variables:
            v = variables[name]
            if isinstance(v, list):
                return ", ".join(str(x) for x in v)
            return str(v)
        builtin = _builtin_value(name, fmt)
        if builtin is not None:
            return builtin
        if _looks_like_date_format(name):
            return datetime.now().strftime(_strftime_pattern(name))
        return ""

    return VAR_PATTERN.sub(_sub, value)


def expand_path(value):
    """Expands %USERPROFILE% and friends in a path.

    A macro's folders are the one part that can't survive being handed
    to a colleague: a path under one person's user folder is a fact about
    one machine. Writing %USERPROFILE% instead makes the same macro file
    work on any of them, which matters because macros are shared by
    copying the JSON."""
    if not isinstance(value, str) or "%" not in value:
        return value
    return os.path.expandvars(value)


# -- Chrome ------------------------------------------------------------------
_CHROME_CANDIDATES = [
    r"%ProgramFiles%\Google\Chrome\Application\chrome.exe",
    r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe",
    r"%LocalAppData%\Google\Chrome\Application\chrome.exe",
]


def find_chrome() -> Optional[str]:
    for template in _CHROME_CANDIDATES:
        path = os.path.expandvars(template)
        if os.path.isfile(path):
            return path
    return shutil.which("chrome")


def open_url_in_chrome(url: str, new_window: bool = False) -> str:
    """Launches Chrome at url. Chrome's own single-instance behavior
    means this opens a new tab in the existing window if one's already
    running -- "launch or reuse a tab" comes for free, no extra
    detection needed. Returns the chrome.exe path used.

    That reused window is whichever one was active last, which during a
    replay is usually the one the user just clicked Run in -- the new tab
    takes over their view. `new_window` puts the page in its own window
    instead, leaving whatever they were looking at alone. It still isn't
    hidden: Chrome only exposes the active tab to UI Automation, so a
    later click-by-text step needs this tab frontmost in *its* window."""
    chrome = find_chrome()
    if not chrome:
        raise RuntimeError(
            "Chrome wasn't found in its usual install locations. "
            "Install it from https://www.google.com/chrome/, or use a different step for this."
        )
    args = [chrome, "--new-window", url] if new_window else [chrome, url]
    subprocess.Popen(args)
    return chrome


# -- file search / file ops --------------------------------------------------
def open_file(path: str) -> str:
    """Opens a file or folder with whatever Windows opens it with.

    The same double-click the user would do, minus the double-click: a
    folder full of files named after today's date can't be clicked at by
    coordinates, and the association lookup is Windows' job anyway --
    hard-coding a path to Excel would break on the machine that keeps it
    somewhere else, or opens .csv in something different on purpose."""
    target = Path(path)
    if not target.exists():
        raise RuntimeError(f'There is nothing at "{path}" to open.')
    try:
        os.startfile(str(target))
    except OSError as exc:
        raise RuntimeError(f'Windows would not open "{path}": {exc}')
    return str(target)


def search_files(folder: str, pattern: str, recursive: bool = False, limit: int = 500,
                 newest_first: bool = False) -> list[str]:
    """Newest-first exists for one job that comes up constantly: naming or
    moving the file a download just produced. Sorting needs the whole match
    list first, so the limit is applied after the sort rather than while
    walking -- otherwise "newest" would mean "newest of whichever 500 the
    filesystem happened to hand back first"."""
    base = Path(folder)
    if not base.is_dir():
        raise RuntimeError('Folder "' + folder + '" does not exist.')
    matches = base.rglob(pattern) if recursive else base.glob(pattern)

    # "~$whatever.xlsx" is the lock file Excel writes while a workbook is
    # open. It is newer than the download and never the thing anyone means.
    def wanted(path):
        return path.is_file() and not path.name.startswith("~$")

    if newest_first:
        found = [p for p in matches if wanted(p)]
        found.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        if limit:
            found = found[:limit]
        return [str(p) for p in found]

    results: list[str] = []
    for p in matches:
        if p.is_file() and not wanted(p):
            continue
        results.append(str(p))
        if limit and len(results) >= limit:
            break
    return results


def file_op(operation: str, source: str, destination: Optional[str] = None, overwrite: bool = False) -> str:
    if not source:
        raise RuntimeError("No source path given.")
    src = Path(source)

    if operation == "delete":
        if not src.exists():
            raise RuntimeError(f'"{source}" doesn\'t exist.')
        if src.is_dir():
            raise RuntimeError(f'"{source}" is a folder -- delete only supports single files, to avoid an accidental mass-delete.')
        src.unlink()
        return f'Deleted "{source}".'

    if operation not in ("copy", "move", "rename"):
        raise RuntimeError(f'Unknown file operation "{operation}".')
    if not destination:
        raise RuntimeError(f'"{operation}" needs a destination path.')
    if not src.exists():
        raise RuntimeError(f'"{source}" doesn\'t exist.')
    dest = Path(destination)
    if dest.exists() and not overwrite:
        raise RuntimeError(f'"{destination}" already exists -- enable overwrite to replace it.')

    if operation == "copy":
        shutil.copy2(src, dest)
        return f'Copied "{source}" to "{destination}".'
    if operation == "move":
        shutil.move(str(src), str(dest))
        return f'Moved "{source}" to "{destination}".'
    src.rename(dest)  # rename
    return f'Renamed "{source}" to "{destination}".'


# -- clipboard ----------------------------------------------------------
# Via PowerShell rather than raw Win32 clipboard APIs -- simpler and more
# robust than ctypes' global-memory-ownership dance, at the cost of
# ~200-500ms process-launch overhead per call, which is fine for a macro
# step (not a hot loop).
#
# The clipboard is also where Chinese goes wrong. PowerShell's console pipes
# use the machine's ANSI codepage, which cannot carry it at all: the text
# arrives as question marks, or the call dies encoding it, and the failure
# is silent enough to look like the copy simply didn't happen. Base64
# sidesteps the console entirely -- the command line and the output are
# both plain ASCII, whatever the text is, so no codepage is consulted at
# any point. Lithuanian and every other accented alphabet ride along on
# the same fix.
def _powershell(script: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True, text=True, encoding="ascii", errors="replace", timeout=5,
    )


def clipboard_write(text: str) -> None:
    payload = base64.b64encode(text.encode("utf-8")).decode("ascii")
    proc = _powershell(
        "Set-Clipboard -Value ([Text.Encoding]::UTF8.GetString("
        f"[Convert]::FromBase64String('{payload}')))"
    )
    if proc.returncode != 0:
        raise RuntimeError(f"Clipboard write failed: {proc.stderr.strip() or 'unknown error'}")


def clipboard_read() -> str:
    proc = _powershell(
        "[Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes([string](Get-Clipboard -Raw)))"
    )
    if proc.returncode != 0:
        raise RuntimeError(f"Clipboard read failed: {proc.stderr.strip() or 'unknown error'}")
    encoded = proc.stdout.strip()
    if not encoded:
        return ""
    try:
        text = base64.b64decode(encoded).decode("utf-8")
    except (ValueError, UnicodeDecodeError) as exc:
        raise RuntimeError(f"Could not read the clipboard back: {exc}")
    return text.rstrip("\r\n")


