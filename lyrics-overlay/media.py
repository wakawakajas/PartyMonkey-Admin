"""What this PC is playing, from Windows' media controls (Spotify, YouTube in Chrome/Edge, etc.).

Gives the title, artist/channel and exact playback position - used when Shazam can't recognise a
song (covers, AI remixes) and to notice instantly when the song changes.
"""
import asyncio
import re
import time

try:
    from winrt.windows.media.control import GlobalSystemMediaTransportControlsSessionManager as _Mgr
except Exception:          # winrt not installed -> feature quietly off
    _Mgr = None

PLAYING = 4
BROWSERS = ("chrome", "msedge", "firefox", "opera", "brave", "vivaldi", "claude")
JUNK = re.compile(r"(官方|M/?V|official|music video|lyrics?|歌词|歌詞|動態|动态|4k|hd|高音质|高音質|完整版|"
                  r"audio|visualizer|拼音|pinyin|live|现场|現場|翻唱|cover|伴奏|karaoke|ktv)", re.I)


FRIENDLY = (("chrome", "Chrome"), ("msedge", "Edge"), ("firefox", "Firefox"), ("spotify", "Spotify"),
            ("opera", "Opera"), ("brave", "Brave"), ("vivaldi", "Vivaldi"), ("claude", "Claude"))


def friendly(app):
    low = app.lower()
    for key, name in FRIENDLY:
        if key in low:
            return name
    return re.split(r"[!\\.]", app)[0] or app


async def _read_all():
    mgr = await _Mgr.request_async()
    out = []
    for s in mgr.get_sessions():
        playing = s.get_playback_info().playback_status == PLAYING
        p = await s.try_get_media_properties_async()
        t = s.get_timeline_properties()
        end = t.end_time.total_seconds()
        pos = None
        if end > 0:
            pos = t.position.total_seconds()
            if playing:      # Windows gives the position at the last update; add the time since then
                pos += max(0.0, time.time() - t.last_updated_time.timestamp())
        app = s.source_app_user_model_id or ""
        out.append({"app": app, "name": friendly(app), "title": p.title or "", "artist": p.artist or "",
                    "pos": pos, "duration": end, "playing": playing,
                    "browser": any(b in app.lower() for b in BROWSERS)})
    return out


def sessions():
    """Every media player Windows knows about (playing or paused)."""
    if _Mgr is None:
        return []
    try:
        return asyncio.run(_read_all())
    except Exception:
        return []


def now_playing(app=None):
    """The playing session (or the given app's session, even if paused). None if nothing."""
    ss = sessions()
    if app:
        return next((s for s in ss if s["app"] == app), None)
    return next((s for s in ss if s["playing"]), None)


def _variants(title):
    """'甲乙丙丁Strangers' -> also '甲乙丙丁' (Chinese part only)."""
    out = [title]
    m = re.match(r"^\s*([一-鿿　\s]{2,})", title)
    if m and m.group(1).strip() != title.strip():
        out.append(m.group(1).strip())
    return out


def queries(np):
    """Possible (title, artist) pairs to search lyrics for, best first."""
    title, artist = np["title"].strip(), np["artist"].strip()
    cands = []
    if not np["browser"]:                         # Spotify etc: real song + singer
        cands += [(t, artist) for t in _variants(title)]
    else:                                         # YouTube: song name is hidden in the video title
        for a, b in (("《", "》"), ("「", "」"), ("『", "』"), ("【", "】"), ("“", "”"), ('"', '"')):
            for inner in re.findall(re.escape(a) + r"(.+?)" + re.escape(b), title):
                inner = inner.strip()
                if inner and not JUNK.search(inner):
                    before = title.split(a)[0].strip(" -|｜:：")
                    singer = before if 0 < len(before) <= 20 else ""
                    cands += [(t, singer) for t in _variants(inner)]
        base = re.split(r"[|｜/]", title)[0]
        base = re.sub(r"[\(\[（【].*?[\)\]）】]", "", base).strip()
        if " - " in base:
            a, t = base.split(" - ", 1)
            cands += [(t.strip(), a.strip()), ]
        if base and not JUNK.fullmatch(base):
            cands.append((base, ""))
    seen, out = set(), []
    for t, a in cands:
        if t and (t, a) not in seen:
            seen.add((t, a))
            out.append((t, a))
    return out[:4]
