"""Read the lyrics drawn into a YouTube video's picture (动态歌词 / AI-song videos), with their timing.

Runs as its own process (so a crash or heavy CPU never touches the overlay):
    python ytvideo.py VIDEO_ID START_SECONDS [zh|en] [ahead]
Prints JSON lines: {"lines": [[seconds, text], ...]} as the scan goes (starting near START_SECONDS so
the part being played comes first), the last one with "final": true, then {"done": true} - or
{"none": true} if the video has no lyrics in its picture. "ahead" = reading ahead for the team: skip
the video if another PC is already reading it ({"skip": true}).

Finished reads are kept in cache/ AND on the NAS (video-lyrics/), so once any PC has read a video,
every PC gets its lyrics instantly.

How: a small 360p copy of the video is fetched to a temp file, one frame per second is read with
RapidOCR (Chinese + English), text in the lyric row (the height where the most different texts
appear) is kept, and frames showing the same line are joined. Each line's start is then checked half
a second earlier. Videos that write Chinese lyrics in vertical columns are read column by column
(the picture is turned on its side to find the columns, then each column is cut into characters).
"""
import difflib
import json
import os
import re
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(HERE, "cache")
CACHE_VERSION = "video3"
FORMAT = "134/bv*[height<=360][vcodec^=avc1]/18/bv*[height<=480]/worst"
CROP = 0.3             # lyrics live in the lower part of the picture (some mid-screen): read from 30% down
MIN_CONF = 0.75
BAND = 18              # px (in the 2x crop) a lyric may sit above/below the lyric row
GIVE_UP_AFTER = 120    # samples without any lyric row -> this video has no lyrics in its picture
TRY_VERTICAL_AFTER = 20  # samples with no row in the song's language -> check for vertical columns
VERTICAL = False         # vertical reading: works, but only gives 2-3 character pieces so far - off
LOCK_MINUTES = 15
PUNCT = " \"'“”‘’「」『』?？.。,，!！、:：;；…-—~～《》()（）[]【】"


def out(**d):
    # plain ASCII (\u escapes): the pipe to the app may use an old Windows code page that has no Chinese
    print(json.dumps(d, ensure_ascii=True), flush=True)


def similar(a, b):
    if not a or not b:
        return 0.0
    if a in b or b in a:
        return 1.0
    return difflib.SequenceMatcher(None, a, b).ratio()


def is_cjk(text):
    return sum(1 for c in text if "一" <= c <= "鿿") >= 2


def looks_like_lyric(text):
    words = re.findall(r"[A-Za-z']{2,}", text)
    return is_cjk(text) or len(words) >= 2


# ---------- where finished reads are kept ----------
def nas_dir():
    """The shared video-lyrics folder on the NAS, or None if the NAS can't be reached."""
    try:
        import share
        bases = [os.path.dirname(d) for d in share.NAS_DIRS]
    except Exception:
        return None
    for base in bases:
        if os.path.isdir(base):
            d = os.path.join(base, "video-lyrics")
            try:
                os.makedirs(d, exist_ok=True)
                return d
            except OSError:
                continue
    return None


def cache_name(video_id, lang):
    return f"{CACHE_VERSION}-{video_id}-{lang}.json"


def load_cached(video_id, lang):
    """A finished read of this video (this PC first, then the NAS), or None."""
    name = cache_name(video_id, lang)
    for d in (CACHE, nas_dir()):
        if not d:
            continue
        try:
            with open(os.path.join(d, name), encoding="utf-8") as f:
                data = json.load(f)
            if data.get("crop", 0.55) != CROP and len(data.get("lines") or []) < 15:
                continue                               # a poor read of a smaller part of the picture: read again
            if d != CACHE:                             # keep a copy here too
                save_local(name, data)
            return data
        except (OSError, ValueError):
            continue
    return None


def save_local(name, data):
    os.makedirs(CACHE, exist_ok=True)
    with open(os.path.join(CACHE, name), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)


def save_cached(video_id, lang, data):
    name = cache_name(video_id, lang)
    save_local(name, data)
    d = nas_dir()
    if d:
        tmp = os.path.join(d, f"~{name}.{os.getpid()}.tmp")
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
            os.replace(tmp, os.path.join(d, name))    # new name per version; only a poor old read is replaced
        except OSError:
            try:
                os.remove(tmp)
            except OSError:
                pass


def take_lock(video_id, lang):
    """Mark on the NAS that this PC is reading the video. False if another PC is already on it."""
    d = nas_dir()
    if not d:
        return True
    path = os.path.join(d, f"~reading-{video_id}-{lang}")
    try:
        if time.time() - os.path.getmtime(path) < LOCK_MINUTES * 60:
            return False
        os.remove(path)
    except OSError:
        pass
    try:
        with open(path, "x", encoding="utf-8") as f:
            f.write(os.environ.get("COMPUTERNAME", "?"))
        return True
    except OSError:
        return False


def drop_lock(video_id, lang):
    d = nas_dir()
    if d:
        try:
            os.remove(os.path.join(d, f"~reading-{video_id}-{lang}"))
        except OSError:
            pass
    for path in _my_parts:
        try:
            os.remove(path)
        except OSError:
            pass


# ---------- one PC reads, the others pass its lines on ----------
# The PC reading a video for someone else (a faster Joined PC) keeps its lock file "alive" and writes
# the lines read so far as small new files (~part-<id>-<lang>.<millis>.json). The playing PC shows those
# instead of reading the video itself. Times are compared on this PC's own clock only (PC clocks differ).
LOCK_TOUCH, PART_EVERY = 10, 8          # seconds between "still reading" marks / shared partial lines
READER_GONE = 45                         # no sign of the other reader for this long: read it ourselves
_my_parts = []


def touch_lock(video_id, lang):
    d = nas_dir()
    if d:
        try:
            os.utime(os.path.join(d, f"~reading-{video_id}-{lang}"))
        except OSError:
            pass


def _lock_mark(d, video_id, lang):
    try:
        return os.path.getmtime(os.path.join(d, f"~reading-{video_id}-{lang}"))
    except OSError:
        return None


def save_part(video_id, lang, lines):
    d = nas_dir()
    if not d:
        return
    name = f"~part-{video_id}-{lang}.{int(time.time() * 1000)}.json"
    tmp = os.path.join(d, f"~{name}.{os.getpid()}.tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"lines": lines}, f, ensure_ascii=False)
        os.rename(tmp, os.path.join(d, name))
        _my_parts.append(os.path.join(d, name))
    except OSError:
        return
    while len(_my_parts) > 2:                         # keep the newest two
        try:
            os.remove(_my_parts.pop(0))
        except OSError:
            pass


def _newest_part(d, video_id, lang):
    pre = f"~part-{video_id}-{lang}."
    try:
        names = [n for n in os.listdir(d) if n.startswith(pre) and n.endswith(".json")]
    except OSError:
        return None, None
    for n in sorted(names, key=lambda n: int(n[len(pre):-5] or 0) if n[len(pre):-5].isdigit() else 0,
                    reverse=True):
        try:
            with open(os.path.join(d, n), encoding="utf-8") as f:
                return n, json.load(f).get("lines")
        except (OSError, ValueError):
            continue
    return None, None


def follow(video_id, lang, first_wait=0):
    """Another PC is reading this video (or is about to, within first_wait seconds): pass its lines on
    as they come. True when its finished read arrived; False = nobody is reading it, read it here."""
    d = nas_dir()
    if not d:
        return False
    start = last_change = time.time()
    last_sig, sent, seen_lock, gone_at = None, None, False, None
    while True:
        data = load_cached(video_id, lang)
        if data is not None:
            out(**({"lines": data["lines"], "final": True} if data["lines"] else {"none": True}))
            out(done=True)
            return True
        mark = _lock_mark(d, video_id, lang)
        part, lines = _newest_part(d, video_id, lang)
        if lines and lines != sent:
            out(lines=lines)
            sent = lines
        if (mark, part) != last_sig:
            last_sig, last_change = (mark, part), time.time()
        now = time.time()
        if mark is None:
            if seen_lock:                              # reader finished: its result may take a moment to show
                gone_at = gone_at or now
                if now - gone_at > 12:
                    return False
            elif now - start >= first_wait:
                return False
        else:
            seen_lock, gone_at = True, None
            if now - last_change > READER_GONE:        # the other PC stopped (closed, crashed)
                return False
        time.sleep(2)


# ---------- reading the picture ----------
def download(video_id, folder):
    import yt_dlp
    opts = {"quiet": True, "no_warnings": True, "noprogress": True, "format": FORMAT,
            "outtmpl": os.path.join(folder, "%(id)s.%(ext)s"), "socket_timeout": 15}
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info("https://www.youtube.com/watch?v=" + video_id, download=True)
        return ydl.prepare_filename(info)


class Reader:
    def __init__(self, path):
        import cv2
        import numpy
        from rapidocr_onnxruntime import RapidOCR
        self.cv2, self.np = cv2, numpy
        self.ocr = RapidOCR()
        self.cap = cv2.VideoCapture(path)
        fps = self.cap.get(cv2.CAP_PROP_FPS) or 30
        self.length = self.cap.get(cv2.CAP_PROP_FRAME_COUNT) / fps
        self.vertical = False

    def frame(self, sec):
        self.cap.set(self.cv2.CAP_PROP_POS_MSEC, sec * 1000)
        ok, f = self.cap.read()
        return f if ok else None

    def read(self, sec):
        return self.read_vertical(sec) if self.vertical else self.read_rows(sec)

    def read_rows(self, sec):
        """[(text, y, conf)] of the text found in the lower part of the frame at `sec`."""
        f = self.frame(sec)
        if f is None:
            return []
        cv2 = self.cv2
        h = f.shape[0]
        crop = cv2.resize(f[int(h * CROP):], None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)
        res, _ = self.ocr(crop)
        found = []
        for box, text, conf in res or []:
            text = text.strip()
            if conf >= MIN_CONF and looks_like_lyric(text):
                found.append((text, (box[0][1] + box[2][1]) / 2, conf))
        return found

    def read_vertical(self, sec):
        """Chinese written in vertical columns: [(all columns right to left as one text, 0, conf)]."""
        f = self.frame(sec)
        if f is None:
            return []
        cv2, np = self.cv2, self.np
        big = cv2.resize(f, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)
        rot = cv2.rotate(big, cv2.ROTATE_90_CLOCKWISE)      # columns become rows, easy to find
        boxes, _ = self.ocr(rot, use_rec=False)
        cols = []
        for b in boxes or []:
            b = np.array(b)
            x0, y0 = b.min(0)
            x1, y1 = b.max(0)
            if x1 - x0 < 1.5 * (y1 - y0) or y1 - y0 < 20:
                continue                                      # not a column of 2+ characters
            crop = rot[max(0, int(y0) - 4):int(y1) + 4, max(0, int(x0)):int(x1)]
            col = cv2.rotate(crop, cv2.ROTATE_90_COUNTERCLOCKWISE)   # upright column again
            cw = col.shape[1]
            n = max(1, round(col.shape[0] / cw))
            ch = col.shape[0] / n
            strip = np.hstack([cv2.resize(col[int(i * ch):int((i + 1) * ch)], (cw, cw)) for i in range(n)])
            res, _ = self.ocr(strip, use_det=False, use_cls=False)
            if res and res[0][1] >= MIN_CONF and is_cjk(res[0][0]):
                cols.append((y0, res[0][0], float(res[0][1])))  # y in the turned picture = x, right first
        if not cols:
            return []
        cols.sort(key=lambda c: c[0])
        return [("".join(t for _, t, _ in cols), 0.0, min(c for _, _, c in cols))]


def lyric_row(samples, lang, vertical=False):
    """The height of the lyric row, or None if not clear yet: the height where the most DIFFERENT
    texts appear (lyrics change all the time; name captions and logos repeat the same few words).
    Videos often show two rows (Chinese + English translation): the row in the song's language
    (`lang`, zh or en) wins; another language's row is used only if the song's never shows up."""
    def row(items):
        texts = {}
        for y, text in items:
            texts.setdefault(round(y / BAND), set()).add(text)
        if not texts:
            return None
        best = max(texts, key=lambda b: len(texts[b]))
        return best * BAND if len(texts[best]) >= 3 else None
    every = [(y, text) for _, found in samples for text, y, _ in found]
    if vertical:
        return row(every)
    want = [(y, text) for y, text in every if is_cjk(text) == (lang == "zh")]
    best = row(want)
    if best is None and (len(samples) >= 60 or (not want and len(samples) >= TRY_VERTICAL_AFTER + 10)):
        best = row(every)
    return best


def group(samples, row, vertical=False):
    """Samples (time, found) -> [(start, text)]: neighbouring seconds showing the same line are joined."""
    hits = []
    for t, found in sorted(samples):
        on_row = [(text, conf) for text, y, conf in found if abs(y - row) <= BAND]
        if on_row:
            hits.append((t, " ".join(text for text, _ in on_row), max(c for _, c in on_row)))
    lines = []                                  # [start, last_seen, [(text, conf), ...]]
    for t, text, conf in hits:
        if lines and t - lines[-1][1] <= 2.5 and similar(text, lines[-1][2][-1][0]) > 0.55:
            lines[-1][1] = t
            lines[-1][2].append((text, conf))
        else:
            lines.append([t, t, [(text, conf)]])
    out_lines = []
    for start, _, seen in lines:
        texts = [x for x, _ in seen]
        if vertical:        # columns appear a character at a time: the fullest reading wins
            text = max(seen, key=lambda s: (len(s[0]), texts.count(s[0]), s[1]))[0]
        else:               # the version read most often; ties -> longest (built up word by word), surest
            text = max(seen, key=lambda s: (texts.count(s[0]), len(s[0]), s[1]))[0]
        text = text.strip(PUNCT)                # decorative quote marks around the line
        if text:
            out_lines.append((start, text))
    return out_lines


# ---------- matching what was read to the lyric-site lyrics ----------
_t2s = None


def plain(s):
    """For comparing: simplified Chinese, no spaces or punctuation, lower case."""
    global _t2s
    if _t2s is None:
        try:
            import opencc
            _t2s = opencc.OpenCC("t2s").convert
        except Exception:
            _t2s = lambda x: x
    return re.sub(r"[\W_]+", "", _t2s(s or "")).lower()


def match_score(read, line):
    """How well a line read from the video fits a lyric-site line: whole-line likeness, or - for a
    piece of a line (vertical columns show a few characters at a time) - how much of it is in there."""
    a, b = plain(read), plain(line)
    if not a or not b:
        return 0.0
    sm = difflib.SequenceMatcher(None, a, b)
    score = sm.ratio()
    if len(a) >= 3 and len(a) < len(b):
        covered = sum(m.size for m in sm.get_matching_blocks()) / len(a)
        if covered >= 0.8:
            score = max(score, 0.5 + 0.4 * covered * len(a) / len(b))
    return score


def fix_with(lines, known):
    """Correct misread characters using the lyric-site lyrics of the same song (`known`, may be None):
    each line read from the video takes the closest known line's text. When most lines match (so it
    really is the same song), lines that match nothing (titles, credits) are dropped. Repeats of the
    same line a moment later (a line building up word by word) are joined."""
    texts = list(dict.fromkeys(w for _, w in known or [] if w))
    fixed, matched = [], 0
    for t, w in lines:
        best, score = None, 0.0
        for x in texts:
            s = match_score(w, x)
            if s > score:
                best, score = x, s
        if best and score >= 0.5:
            fixed.append((t, best, True))
            matched += 1
        else:
            fixed.append((t, w, False))
    keep_all = matched < 0.6 * max(1, len(lines))
    out_lines = []
    for t, w, ok in fixed:
        if not (ok or keep_all):
            continue
        if out_lines and out_lines[-1][1] == w and t - out_lines[-1][0] < 6:
            continue
        out_lines.append((t, w))
    return out_lines


def _piece_score(read, line):
    """How much of a piece read from the video is in this lyric-site line (0..1)."""
    a, b = plain(read), plain(line)
    if len(a) < 2 or not b:
        return 0.0
    covered = sum(m.size for m in difflib.SequenceMatcher(None, a, b, autojunk=False).get_matching_blocks())
    return covered / min(len(a), len(b))


def align_known(read, known, min_score=0.6):
    """Time every lyric-site line (`known`) from what was read off the video (`read`), keeping the
    lyric site's complete lines. Works when the video shows lines in pieces (a few words at a time)
    or two lines at once. Pieces are given to lines in order (dynamic programming, most text matched
    wins); a line starts at its first piece. Lines with no piece of their own are placed by the line
    before them, or left out when they don't fit (verses / choruses this version doesn't sing).
    None if too few lines could be timed."""
    n, m = len(known), len(read)
    if not n or not m or n > 400 or m > 600:
        return None
    s = [[0.0] * m for _ in range(n)]
    for i in range(n):
        for j in range(m):
            v = _piece_score(read[j][1], known[i][1])
            if v >= min_score:
                s[i][j] = v * min(len(plain(read[j][1])), len(plain(known[i][1])))   # more text counts more
    NEG = float("-inf")
    # f[j][i]: best with pieces[:j] given out, all to lines <= i
    # g[j][i]: best with piece j given to line i (and maybe also to the line before: two lines at once)
    f = [[0.0] * n for _ in range(m + 1)]
    g = [[NEG] * n for _ in range(m)]
    fhow = [[None] * n for _ in range(m + 1)]
    ghow = [[None] * n for _ in range(m)]
    for j in range(m):
        for i in range(n):
            if s[i][j] > 0:
                best, why = f[j][i], "new"
                if i and g[j][i - 1] > best:
                    best, why = g[j][i - 1], "shared"
                g[j][i], ghow[j][i] = best + s[i][j], why
            best, why = f[j][i], "skip"
            if g[j][i] > best:
                best, why = g[j][i], "take"
            if i and f[j + 1][i - 1] > best:
                best, why = f[j + 1][i - 1], "up"
            f[j + 1][i], fhow[j + 1][i] = best, why
    first, together = {}, {}     # line -> its first piece of its own / first piece shown with the line before
    j, i, in_g = m, n - 1, False
    while i >= 0 and (j > 0 if not in_g else j >= 0):
        if in_g:                                       # piece j belongs to line i
            if ghow[j][i] == "shared":                 # (walking backwards: ends at the line's first piece)
                together[i] = read[j][0]
                i -= 1
                continue
            first[i] = read[j][0]
            in_g = False
            continue
        why = fhow[j][i]
        if why == "up":
            i -= 1
        elif why == "take":
            j -= 1
            in_g = True
        else:
            j -= 1
    if len(first) < 0.75 * n:                       # not sure enough (mixed languages, another version)
        return None
    last_seen = max(t for t, _ in read)
    out = []
    anchors = sorted(first)
    for i, (t, w) in enumerate(known):
        prev = max((k for k in anchors if k < i), default=None)
        nxt = min((k for k in anchors if k > i), default=None)
        if i in first:
            out.append((round(first[i], 2), w, i))
            continue
        if prev is None or not out:
            continue                                   # before the first line seen in the video: not sung
        # a line sharing a piece with the line before it, or with no piece: keep the lyric site's
        # spacing from the line before
        bt, _, bi = out[-1]
        at = bt + (t - known[bi][0])
        hi = first[nxt] - 0.5 if nxt is not None else last_seen + 1
        if bt + 0.5 <= at <= hi:
            out.append((round(at, 2), w, i))
        elif i in together and bt + 0.5 <= hi:           # sung together with the line before: just after it
            out.append((round(min(bt + 1.5, hi), 2), w, i))
    return [(t, w) for t, w, _ in out]


def offset_to(read, known, tol=1.5):
    """How many seconds the lyric-site lyrics (`known`) are early/late against the few lines already
    read from the video (`read`, texts already corrected by fix_with). None if not sure yet.
    A repeated line (chorus) could pair with any of its repeats: the shift most lines agree on wins."""
    diffs = []
    for t, w in read:
        ds = [t - kt for kt, kw in known if kw == w]
        if ds:
            diffs.append(ds)
    if len(diffs) < 2:
        return None

    def support(c):
        return [min(ds, key=lambda d: abs(d - c)) for ds in diffs if min(abs(d - c) for d in ds) <= tol]
    best = max((d for ds in diffs for d in ds), key=lambda c: len(support(c)))
    agree = sorted(support(best))
    if len(agree) < 2 or len(agree) < 0.6 * len(diffs):
        return None
    return agree[len(agree) // 2]


# ---------- the whole read ----------
def scan(video_id, start, lang="zh", mode=""):
    """mode: "" = the playing PC; "wait" = the playing PC, a faster Joined PC will read it for us;
    "help" = reading the host's video for it; "ahead" = reading a likely next video in the background."""
    ahead = mode == "ahead"
    data = load_cached(video_id, lang)
    if data is not None:
        out(**({"lines": data["lines"], "final": True} if data["lines"] else {"none": True}))
        out(done=True)
        return
    if mode in ("", "wait") and follow(video_id, lang, 20 if mode == "wait" else 0):
        return                                    # another PC read it for us
    if not take_lock(video_id, lang) and mode in ("ahead", "help"):
        out(skip=True)                            # another PC is reading it right now
        return
    folder = tempfile.mkdtemp(prefix="lyrics-video-")
    path = None
    try:
        path = download(video_id, folder)
        r = Reader(path)
        end = int(r.length)
        first = max(0, int(start) - 10)
        order = list(range(first, end)) + list(range(0, first))   # what's playing now comes first
        samples, row, sent, tried_vertical = [], None, None, False
        touched = parted = time.time()
        k = 0
        while k < len(order):
            if time.time() - touched > LOCK_TOUCH:  # tell waiting PCs this read is still going
                touch_lock(video_id, lang)
                touched = time.time()
            sec = order[k]
            samples.append((sec, r.read(sec)))
            row = lyric_row(samples, lang, r.vertical)   # re-checked as more of the video is read
            if (VERTICAL and lang == "zh" and not r.vertical and not tried_vertical and k + 1 >= TRY_VERTICAL_AFTER
                    and len({t for _, f in samples for t, _, _ in f if is_cjk(t)}) < 3):
                # no Chinese row so far: is the Chinese written in vertical columns?
                tried_vertical = True
                probe = [order[i] for i in range(0, min(len(order), 60), 5)]
                if sum(1 for s in probe if r.read_vertical(s)) >= 4:
                    r.vertical = True
                    samples, row, sent, k = [], None, None, 0
                    continue
            if row is None and k >= GIVE_UP_AFTER:
                break
            if row is not None and not ahead and (k % 4 == 3 or k == len(order) - 1):
                lines = group(samples, row, r.vertical)
                if lines and lines != sent:
                    out(lines=lines)
                    sent = lines
                    if time.time() - parted > PART_EVERY:   # share the lines so far with waiting PCs
                        save_part(video_id, lang, lines)
                        parted = time.time()
            k += 1
        if row is None:
            final = []
        else:
            final = group(samples, row, r.vertical)
            if not r.vertical:
                # sharpen each start: is the line already showing half a second earlier?
                for i, (t, text) in enumerate(final):
                    if t >= 0.5 and any(abs(y - row) <= BAND and similar(x, text) >= 0.5
                                        for x, y, _ in r.read(t - 0.5)):
                        final[i] = (t - 0.5, text)
        out(**({"lines": final, "final": True} if final else {"none": True}))
        save_cached(video_id, lang, {"lines": final, "vertical": r.vertical, "at": time.time(), "crop": CROP})
        out(done=True)
    finally:
        drop_lock(video_id, lang)
        try:
            if path and os.path.exists(path):
                os.remove(path)
            os.rmdir(folder)
        except OSError:
            pass


if __name__ == "__main__":
    a = sys.argv
    scan(a[1], float(a[2]) if len(a) > 2 else 0, a[3] if len(a) > 3 else "zh", a[4] if len(a) > 4 else "")
