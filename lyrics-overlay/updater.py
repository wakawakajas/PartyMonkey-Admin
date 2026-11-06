"""Updates for PCs whose copy of the app has no git (copied folder, git not installed).

The PartyMonkey-Admin repo is public, so the app's files can be compared with GitHub's and the
changed ones downloaded. Git's own file fingerprint (blob SHA-1) is used, so nothing extra has to be
published. Only the lyrics-overlay folder is touched; settings.json, cache/ and .venv are not in the
repo and are never changed.
"""
import hashlib
import json
import os
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
API = "https://api.github.com/repos/wakawakajas/PartyMonkey-Admin/contents/lyrics-overlay?ref=main"
UA = {"User-Agent": "lyrics-overlay-updater", "Accept": "application/vnd.github+json"}


def _blob_sha(path):
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError:
        return None
    data = data.replace(b"\r\n", b"\n")          # git keeps LF; a Windows copy may have CRLF - same file
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def _get(url, timeout=20):
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout) as r:
        return r.read()


def changed_files():
    """[(name, download_url)] of files that differ from GitHub's main branch."""
    items = json.loads(_get(API).decode("utf-8"))
    out = []
    for it in items:
        if it.get("type") != "file" or not it.get("download_url"):
            continue
        name = it["name"]
        if "/" in name or "\\" in name or name.startswith("."):
            continue
        if _blob_sha(os.path.join(HERE, name)) != it.get("sha"):
            out.append((name, it["download_url"]))
    return out


def apply(files):
    """Download and put the files in place (each written whole, then swapped in)."""
    for name, url in files:
        data = _get(url, timeout=60)
        if name.lower().endswith((".bat", ".cmd")):    # cmd.exe needs CRLF line endings
            data = data.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")
        path = os.path.join(HERE, name)
        tmp = path + ".new"
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, path)


def local_version():
    """Short fingerprint of this copy's files (for the NAS diag)."""
    h = hashlib.sha1()
    for name in sorted(os.listdir(HERE)):
        if name.endswith((".py", ".bat", ".txt")):
            h.update((_blob_sha(os.path.join(HERE, name)) or "").encode())
    return h.hexdigest()[:7]
