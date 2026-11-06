"""Lyrics from a YouTube video's own subtitles (the CC button).

When the uploader added subtitles, they are timed to that exact video - right for remixes, sped-up
versions and AI songs that no lyric site has. Auto-generated captions are ignored (too poor on music).
The video is found by searching YouTube for the title Windows reports, checked by exact title + length.
"""
import json
import re

try:
    import yt_dlp
except Exception:            # not installed yet -> feature quietly off
    yt_dlp = None

OPTS = {"quiet": True, "no_warnings": True, "skip_download": True, "socket_timeout": 10,
        "noprogress": True, "logger": None}
NOISE = re.compile(r"^[\[\(（【♪♫\s]*(music|音乐|音樂|applause|instrumental|intro|outro|间奏|間奏)?[\]\)）】♪♫\s]*$",
                   re.I)


def _norm(s):
    return re.sub(r"[\W_]+", "", s or "").lower()


def find_video(title, duration=0):
    """(video info) for this exact title and length, or None.
    YouTube search can return a translated title, so a same-length result is opened to read its
    original title too."""
    with yt_dlp.YoutubeDL({**OPTS, "extract_flat": True}) as ydl:
        res = ydl.extract_info("ytsearch8:" + title, download=False)
    want = _norm(title)
    with yt_dlp.YoutubeDL(OPTS) as ydl:
        for e in res.get("entries") or []:
            if duration and (not e.get("duration") or abs(e["duration"] - duration) > 2):
                continue
            flat_ok = _norm(e.get("title")) == want
            if not duration and not flat_ok:
                continue
            info = ydl.extract_info("https://www.youtube.com/watch?v=" + e["id"], download=False)
            if flat_ok or _norm(info.get("title")) == want:
                return info
    return None


def _pick(subs, cjk):
    """Best subtitle track: the song's own language first (Chinese for Chinese titles)."""
    langs = list(subs)
    zh = [l for l in langs if l.lower().startswith(("zh", "yue", "cmn"))]
    en = [l for l in langs if l.lower().startswith("en")]
    others = [l for l in langs if l not in zh + en and l != "live_chat"]
    for l in (zh + en + others) if cjk else (en + zh + others):
        fmts = {f.get("ext"): f.get("url") for f in subs[l]}
        if fmts.get("json3"):
            return fmts["json3"]
    return None


def _parse(raw):
    lines = []
    for ev in json.loads(raw).get("events") or []:
        text = "".join(s.get("utf8", "") for s in ev.get("segs") or []).replace("\n", " ").strip()
        text = re.sub(r"\s+", " ", text.strip("♪♫ "))
        if "tStartMs" in ev and text and not NOISE.match(text):
            lines.append((ev["tStartMs"] / 1000.0, text))
    lines.sort()
    out = []
    for t, w in lines:                  # same line repeated back-to-back (roll-up captions) -> keep first
        if out and out[-1][1] == w and t - out[-1][0] < 1:
            continue
        out.append((t, w))
    return out


def _cjk(s):
    return any("一" <= c <= "鿿" for c in s)


def captions_for(info):
    """(lines [(seconds, text)], is_translation) from the uploader's subtitles, or (None, False).
    is_translation: the subtitles are in another language than the song (e.g. English under a
    Chinese song) - only worth showing when no real lyrics exist."""
    subs = info.get("subtitles") or {}                # uploader's own only (not automatic_captions)
    cjk = _cjk(info.get("title", ""))
    url = _pick(subs, cjk)
    if not url:
        return None, False
    with yt_dlp.YoutubeDL(OPTS) as ydl:
        raw = ydl.urlopen(url).read().decode("utf-8", "ignore")
    lines = _parse(raw)
    if len(lines) <= 5:
        return None, False
    return lines, _cjk(" ".join(w for _, w in lines)) != cjk


def _size(s):
    """Rough amount of singing in a line: Chinese characters + other-language words."""
    cjk = sum(1 for c in s if "一" <= c <= "鿿")
    return cjk + len(re.findall(r"[^\W\d_一-鿿]+", s)) or 1


def retime(lines, sub):
    """Lyrics with badly timed lines + subtitles in another language that are timed to the video
    (translation, line by line) -> the lyrics re-timed to the subtitles, or None if they don't line up.
    Lines are paired in order (dynamic programming), allowing a few lines to be missing on either side;
    missing lines at the very start / end (titles, a last ad-lib) are cheap."""
    n, m = len(lines), len(sub)
    if not n or not m or n > 400 or m > 400 or not 0.75 <= n / m <= 1.33:
        return None
    import math
    ratios = sorted(math.log(_size(lines[i][1]) / _size(sub[i * m // n][1])) for i in range(n))
    mid = ratios[n // 2]                           # typical length ratio between the two languages
    GAP = 2.0
    INF = float("inf")
    cost = [[INF] * (m + 1) for _ in range(n + 1)]
    back = [[None] * (m + 1) for _ in range(n + 1)]
    cost[0][0] = 0.0
    for i in range(n + 1):
        for j in range(m + 1):
            c = cost[i][j]
            if c == INF:
                continue
            if i < n and j < m:
                d = c + abs(math.log(_size(lines[i][1]) / _size(sub[j][1])) - mid)
                if d < cost[i + 1][j + 1]:
                    cost[i + 1][j + 1], back[i + 1][j + 1] = d, (i, j, True)
            g = GAP * (0.2 if j in (0, m) else 1)
            if i < n and c + g < cost[i + 1][j]:
                cost[i + 1][j], back[i + 1][j] = c + g, (i, j, False)
            g = GAP * (0.2 if i in (0, n) else 1)
            if j < m and c + g < cost[i][j + 1]:
                cost[i][j + 1], back[i][j + 1] = c + g, (i, j, False)
    pairs, i, j = {}, n, m
    while back[i][j]:
        pi, pj, matched = back[i][j]
        if matched:
            pairs[pi] = pj
        i, j = pi, pj
    if len(pairs) < 0.8 * min(n, m):
        return None
    out, delta = [], None
    for i, (t, w) in enumerate(lines):
        if i in pairs:
            delta = sub[pairs[i]][0] - t
        elif delta is None:                        # unpaired lines before the first pair: use its shift
            k = min(pairs)
            delta = sub[pairs[k]][0] - lines[k][0]
        out.append((round(t + delta, 2), w))
    for k in range(1, len(out)):                   # never out of order
        if out[k][0] <= out[k - 1][0]:
            out[k] = (out[k - 1][0] + 0.5, out[k][1])
    return out


_cache = {}


def captions(np):
    """(lines, is_translation) from the subtitles of the YouTube video Windows says is playing in a
    browser, or (None, False). Remembered per video title."""
    if yt_dlp is None or not np or not np.get("browser") or not np.get("title"):
        return None, False
    key = (np["title"], round(np.get("duration") or 0))
    if key not in _cache:
        try:
            info = find_video(np["title"], np.get("duration") or 0)
            _cache[key] = captions_for(info) if info else (None, False)
        except Exception:
            return None, False                       # network hiccup: try again next time
        if len(_cache) > 50:
            _cache.pop(next(iter(_cache)))
    return _cache[key]
