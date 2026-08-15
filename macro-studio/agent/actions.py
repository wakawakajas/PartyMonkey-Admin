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

import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Optional

VAR_PATTERN = re.compile(r"\{\{(\w+)\}\}")


def substitute(value, variables: dict):
    """Replaces {{name}} in a string with str(variables[name]) (empty
    string if the variable was never set). Lists join with ", ".
    Non-string values pass through untouched."""
    if not isinstance(value, str) or "{{" not in value:
        return value

    def _sub(m: re.Match) -> str:
        v = variables.get(m.group(1), "")
        if isinstance(v, list):
            return ", ".join(str(x) for x in v)
        return str(v)

    return VAR_PATTERN.sub(_sub, value)


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


def open_url_in_chrome(url: str) -> str:
    """Launches Chrome at url. Chrome's own single-instance behavior
    means this opens a new tab in the existing window if one's already
    running -- "launch or reuse a tab" comes for free, no extra
    detection needed. Returns the chrome.exe path used."""
    chrome = find_chrome()
    if not chrome:
        raise RuntimeError(
            "Chrome wasn't found in its usual install locations. "
            "Install it from https://www.google.com/chrome/, or use a different step for this."
        )
    subprocess.Popen([chrome, url])
    return chrome


# -- file search / file ops --------------------------------------------------
def search_files(folder: str, pattern: str, recursive: bool = False, limit: int = 500) -> list[str]:
    base = Path(folder)
    if not base.is_dir():
        raise RuntimeError(f'Folder "{folder}" doesn\'t exist.')
    matches = base.rglob(pattern) if recursive else base.glob(pattern)
    results: list[str] = []
    for p in matches:
        results.append(str(p))
        if len(results) >= limit:
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
def clipboard_write(text: str) -> None:
    proc = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", "$input | Set-Clipboard"],
        input=text, capture_output=True, text=True, timeout=5,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"Clipboard write failed: {proc.stderr.strip() or 'unknown error'}")


def clipboard_read() -> str:
    proc = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", "Get-Clipboard -Raw"],
        capture_output=True, text=True, timeout=5,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"Clipboard read failed: {proc.stderr.strip() or 'unknown error'}")
    return proc.stdout.rstrip("\r\n")
