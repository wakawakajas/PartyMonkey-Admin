"""Song recognition (Shazam) + synced lyrics lookup (Kugou, NetEase, LRCLIB)."""
import asyncio
import base64
import io
import logging
import re
import wave

import numpy as np
import requests
from shazamio import Shazam

logging.getLogger("asyncio").setLevel(logging.CRITICAL)
logging.getLogger("aiohttp_retry").setLevel(logging.CRITICAL)
UA = {"User-Agent": "Mozilla/5.0 LyricsOverlay/1.0"}
NETEASE = {**UA, "Referer": "https://music.163.com"}


def to_wav_bytes(samples: np.ndarray, rate: int) -> bytes:
    mono = samples.mean(axis=1) if samples.ndim > 1 else samples
    peak = float(np.abs(mono).max()) or 1.0
    pcm = (mono / max(peak, 1e-4) * 0.9 * 32767).astype(np.int16)  # normalise quiet audio
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm.tobytes())
    return buf.getvalue()


def recognize(wav: bytes):
    """Returns dict(title, artist, offset, static_lyrics) or None."""
    # zh-CN gives Chinese song names in Chinese (not "Sunny Day"/pinyin); English songs stay English
    # Shazam often answers "429 too many requests" and its built-in retry then keeps hitting the same
    # wall for minutes. A fresh request usually goes straight through, so: up to 3 fresh tries, 4s each.
    async def once():
        async with Shazam(language="zh-CN") as shazam:   # closes its connection even on timeout
            return await shazam.recognize(wav)

    async def ask():
        for _ in range(3):
            try:
                return await asyncio.wait_for(once(), timeout=4)
            except Exception:
                await asyncio.sleep(0.5)
        return {}
    try:
        out = asyncio.run(ask())
    except Exception:
        return None
    track = out.get("track")
    if not track:
        return None
    offset = 0.0
    if out.get("matches"):
        offset = float(out["matches"][0].get("offset", 0.0))
    static = None
    for sec in track.get("sections", []):
        if sec.get("type") == "LYRICS" and sec.get("text"):
            static = sec["text"]
    return {"title": track.get("title", ""), "artist": track.get("subtitle", ""),
            "offset": offset, "static_lyrics": static}


def parse_lrc(text: str):
    lines = []
    for raw in text.splitlines():
        stamps = re.findall(r"\[(\d+):(\d+(?:\.\d+)?)\]", raw)
        words = re.sub(r"\[[^\]]*\]", "", raw).strip()
        for m, s in stamps:
            lines.append((int(m) * 60 + float(s), words))
    lines.sort(key=lambda x: x[0])
    # drop credit lines like "作词 : xxx", "合声编写：xxx", "Composer: xxx"
    credit = re.compile(r"^([\w ./&]{0,12}(工程|制作|编|词|曲|监制|混|录|和声|合声|吉他|贝斯|鼓|弦|键盘|母带|出品|"
                        r"发行|企划|统筹|OP|SP)[\w ]{0,12}|(lyrics|lyricist|composer|producer|arranger|written by|"
                        r"mixed by|mastered by|vocals?)[\w ]{0,10})\s*[:：]", re.I)
    early = re.compile(r"^[一-鿿/ ]{1,8}\s*[:：]")      # other "xx：name" credits, only before 25s
    notice = re.compile(r"^【.*】$|著作权|版权所有|未经.*许可|翻唱.*翻录")   # copyright notices

    def is_credit(w):
        if credit.match(w):
            return True
        m = re.match(r"^(.{1,50}?)\s*[:：]", w)      # "大提琴独奏 Cello Solo：张平" -> look at the part before ：
        return bool(m and CREDIT_WORDS.search(m.group(1)))
    kept = [(t, w) for t, w in lines
            if w and not is_credit(w) and not (t < 25 and early.match(w)) and not notice.search(w)]
    # two-language files put "你好" and "Hello" at the same moment: show them together, not just the last
    merged = []
    for t, w in kept:
        if merged and abs(merged[-1][0] - t) < 0.01:
            if w != merged[-1][1]:
                merged[-1] = (merged[-1][0], merged[-1][1] + "  /  " + w)
        else:
            merged.append((t, w))
    return merged


CREDIT_WORDS = re.compile(
    r"编|词|曲|制作|监制|混音|混缩|录音|母带|和声|合声|和音|配唱|人声|吉他|贝斯|鼓|弦|钢琴|提琴|独奏|键盘|"
    r"统筹|出品|发行|企划|策划|版权|营销|推广|封面|设计|工程|乐队|演奏|"
    r"arrang|produc|compos|lyric|mix|master|record|vocal|guitar|bass|drum|piano|string|cello|violin|"
    r"solo|harmon|programm|engineer|\bOP\b|\bSP\b", re.I)


# ---------- matching ----------
def clean_title(title: str) -> str:
    return re.sub(r"\s*[\(\[（【].*?[\)\]）】]", "", title).strip() or title


def norm(s: str) -> str:
    return re.sub(r"[\W_]+", "", clean_title(s or "")).lower()


def title_ok(cand: str, title: str) -> bool:
    """Same song name. Extra English is OK ('甲乙丙丁Strangers' = '甲乙丙丁'), extra Chinese is not
    ('孟婆的碗' is a different song from '孟婆')."""
    a, b = norm(cand), norm(title)
    if not (a and b):
        return False
    if a == b:
        return True
    short, long_ = sorted((a, b), key=len)
    return short in long_ and not has_cjk(long_.replace(short, "", 1))


def artist_ok(cand: str, artist: str) -> bool:
    parts = [norm(p) for p in re.split(r"[/,&、，;]|feat\.?|ft\.?|\bx\b", artist or "", flags=re.I)]
    c = norm(cand)
    return any(p and (p in c or c in p) for p in parts if p) if c else False


# ---------- sources: each yields (cand_title, cand_artist, fetch() -> lrc text) ----------
def kugou(title, artist):
    r = requests.get("http://mobilecdn.kugou.com/api/v3/search/song",
                     params={"keyword": f"{clean_title(title)} {artist}", "page": 1, "pagesize": 8,
                             "format": "json"}, headers=UA, timeout=8).json()
    for it in (r.get("data") or {}).get("info", []):
        def fetch(h=it["hash"]):
            c = requests.get("http://krcs.kugou.com/search",
                             params={"ver": 1, "man": "yes", "client": "mobi", "keyword": "",
                                     "duration": "", "hash": h}, headers=UA, timeout=8).json()
            cands = c.get("candidates") or []
            if not cands:
                return None
            d = requests.get("http://lyrics.kugou.com/download",
                             params={"ver": 1, "client": "pc", "id": cands[0]["id"],
                                     "accesskey": cands[0]["accesskey"], "fmt": "lrc",
                                     "charset": "utf8"}, headers=UA, timeout=8).json()
            return base64.b64decode(d.get("content") or "").decode("utf-8", "ignore")
        yield it.get("songname", ""), it.get("singername", ""), fetch


def netease(title, artist):
    r = requests.post("https://music.163.com/api/cloudsearch/pc",
                      data={"s": f"{clean_title(title)} {artist}", "type": 1, "limit": 8, "offset": 0},
                      headers=NETEASE, timeout=8).json()
    for s in (r.get("result") or {}).get("songs", []):
        def fetch(i=s["id"]):
            lr = requests.get("https://music.163.com/api/song/lyric",
                              params={"id": i, "lv": 1, "tv": -1}, headers=NETEASE, timeout=8).json()
            return (lr.get("lrc") or {}).get("lyric")
        yield s.get("name", ""), "/".join(a.get("name", "") for a in s.get("ar") or []), fetch


def lrclib(title, artist):
    seen = set()
    for q in (f"{clean_title(title)} {artist}", clean_title(title)):
        r = requests.get("https://lrclib.net/api/search", params={"q": q}, headers=UA, timeout=8)
        for it in r.json() if r.status_code == 200 else []:
            if it.get("id") in seen or not it.get("syncedLyrics"):
                continue
            seen.add(it.get("id"))
            yield it.get("trackName", ""), it.get("artistName", ""), (lambda t=it["syncedLyrics"]: t)


def rough_timing(lines) -> bool:
    """True when a lyric file's timestamps look guessed (most lines exactly the same distance apart)."""
    gaps = [round(b[0] - a[0], 1) for a, b in zip(lines, lines[1:])]
    if len(gaps) < 8:
        return False
    common = max(set(gaps), key=gaps.count)
    return gaps.count(common) / len(gaps) > 0.6


def has_cjk(s: str) -> bool:
    return any("一" <= c <= "鿿" for c in s)


def find_lyrics(title, artist):
    """Returns list of (seconds, line) or None.
    Pass 1: same title AND same singer. Pass 2: same title, any singer (covers often share lyrics)."""
    sources = [kugou, netease, lrclib] if has_cjk(title + artist) else [lrclib, kugou, netease]
    candidates = []
    for src in sources:
        try:
            for ct, ca, fetch in src(title, artist):
                if title_ok(ct, title):
                    candidates.append((artist_ok(ca, artist), fetch))
        except Exception:
            pass
    for want_artist in (True, False):
        for same_artist, fetch in candidates:
            if same_artist != want_artist:
                continue
            try:
                lines = parse_lrc(fetch() or "")
            except Exception:
                continue
            if len(lines) > 3:
                return lines
    return None


# ---------- word-by-word timing (karaoke highlight) ----------
KRC_KEY = bytes([64, 71, 97, 119, 94, 50, 116, 71, 81, 54, 49, 45, 206, 210, 110, 105])
KRC_LINE = re.compile(r"^\[(\d+),(\d+)\](.*)$")
KRC_WORD = re.compile(r"<(\d+),(\d+),\d+>([^<]*)")


def _plain(s):
    return re.sub(r"[\W_]+", "", s or "").lower()


def kugou_words(title, artist):
    """Kugou's word timing for this song: {line text without spaces/punctuation: [(start_s, dur_s, word)]}
    where start is from the line's own start. {} if Kugou doesn't have it."""
    import zlib
    r = requests.get("http://mobilecdn.kugou.com/api/v3/search/song",
                     params={"keyword": f"{clean_title(title)} {artist}", "page": 1, "pagesize": 8,
                             "format": "json"}, headers=UA, timeout=8).json()
    items = [it for it in (r.get("data") or {}).get("info", []) if title_ok(it.get("songname", ""), title)]
    items.sort(key=lambda it: not artist_ok(it.get("singername", ""), artist))
    for it in items[:3]:
        c = requests.get("http://krcs.kugou.com/search",
                         params={"ver": 1, "man": "yes", "client": "mobi", "keyword": "", "duration": "",
                                 "hash": it["hash"]}, headers=UA, timeout=8).json()
        cands = c.get("candidates") or []
        if not cands:
            continue
        d = requests.get("http://lyrics.kugou.com/download",
                         params={"ver": 1, "client": "pc", "id": cands[0]["id"],
                                 "accesskey": cands[0]["accesskey"], "fmt": "krc", "charset": "utf8"},
                         headers=UA, timeout=8).json()
        raw = base64.b64decode(d.get("content") or "")
        if raw[:4] != b"krc1":
            continue
        text = zlib.decompress(bytes(b ^ KRC_KEY[i % 16] for i, b in enumerate(raw[4:]))).decode("utf-8", "ignore")
        words = {}
        for line in text.splitlines():
            m = KRC_LINE.match(line.strip())
            if not m:
                continue
            ws = [(int(a) / 1000, int(b) / 1000, w) for a, b, w in KRC_WORD.findall(m.group(3))]
            key = _plain("".join(w for _, _, w in ws))
            if key and ws and ws[-1][0] > 0:          # credit lines have no real timing (all 0)
                words.setdefault(key, ws)
        if words:
            return words
    return {}


def sung_chars(words, elapsed):
    """How many letters/characters of the line (counted without spaces/punctuation) have been sung
    `elapsed` seconds after the line started."""
    n = 0
    for start, dur, w in words:
        k = len(_plain(w))
        if elapsed >= start + dur:
            n += k
        elif elapsed >= start:
            n += round(k * (elapsed - start) / dur) if dur > 0 else k
            break
        else:
            break
    return n
