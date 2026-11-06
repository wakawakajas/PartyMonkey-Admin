"""Read the lyrics drawn into a YouTube video's picture (动态歌词 / AI-song videos), with their timing.

Runs as its own process (so a crash or heavy CPU never touches the overlay):
    python ytvideo.py VIDEO_ID START_SECONDS [zh|en]
Prints JSON lines: {"lines": [[seconds, text], ...]} as the scan goes (starting near START_SECONDS so
the part being played comes first), then {"done": true} - or {"none": true} if the video has no
lyrics in its picture. Finished scans are kept in cache/ so a replay is instant.

How: a small 360p copy of the video is fetched to a temp file, one frame per second is read with
RapidOCR (Chinese + English), text in the lyric row (the height where text keeps appearing) is kept,
and frames showing the same line are joined. Each line's start is then checked half a second earlier.
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
FORMAT = "134/bv*[height<=360][vcodec^=avc1]/18/bv*[height<=480]/worst"
CROP = 0.55            # lyrics live in the lower part of the picture: read from 55% down
MIN_CONF = 0.75
BAND = 18              # px (in the 2x crop) a lyric may sit above/below the lyric row
GIVE_UP_AFTER = 120    # samples without any lyric row -> this video has no lyrics in its picture


def out(**d):
    print(json.dumps(d, ensure_ascii=False), flush=True)


def similar(a, b):
    if not a or not b:
        return 0.0
    if a in b or b in a:
        return 1.0
    return difflib.SequenceMatcher(None, a, b).ratio()


def looks_like_lyric(text):
    cjk = sum(1 for c in text if "一" <= c <= "鿿")
    words = re.findall(r"[A-Za-z']{2,}", text)
    return cjk >= 2 or len(words) >= 2


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
        from rapidocr_onnxruntime import RapidOCR
        self.cv2 = cv2
        self.ocr = RapidOCR()
        self.cap = cv2.VideoCapture(path)
        fps = self.cap.get(cv2.CAP_PROP_FPS) or 30
        self.length = self.cap.get(cv2.CAP_PROP_FRAME_COUNT) / fps

    def read(self, sec):
        """[(text, y, conf)] of the text found in the lower part of the frame at `sec`."""
        cv2 = self.cv2
        self.cap.set(cv2.CAP_PROP_POS_MSEC, sec * 1000)
        ok, f = self.cap.read()
        if not ok:
            return []
        h = f.shape[0]
        crop = cv2.resize(f[int(h * CROP):], None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)
        res, _ = self.ocr(crop)
        found = []
        for box, text, conf in res or []:
            text = text.strip()
            if conf >= MIN_CONF and looks_like_lyric(text):
                found.append((text, (box[0][1] + box[2][1]) / 2, conf))
        return found


def is_cjk(text):
    return sum(1 for c in text if "一" <= c <= "鿿") >= 2


def lyric_row(samples, lang):
    """The height where lyric-looking text appears most often, or None if not clear yet.
    Videos often show two rows (Chinese + English translation): the row in the song's language
    (`lang`, zh or en) wins; another language's row is used only if the song's never shows up."""
    def row(ys):
        if len(ys) < 6:
            return None
        best = max(set(ys), key=ys.count)
        return best * BAND if ys.count(best) >= 4 else None
    want = [round(y / BAND) for _, found in samples for text, y, _ in found if is_cjk(text) == (lang == "zh")]
    best = row(want)
    if best is None and len(samples) >= 60:
        best = row([round(y / BAND) for _, found in samples for _, y, _ in found])
    return best


def group(samples, row):
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
        # the version read most often; ties -> longest (lines that build up word by word) then surest
        text = max(seen, key=lambda s: (texts.count(s[0]), len(s[0]), s[1]))[0]
        out_lines.append((start, text))
    return out_lines


def fix_with(lines, known):
    """Correct misread characters using the lyric-site lyrics of the same song (`known`, may be None):
    each line read from the video takes the closest known line's text. When most lines match (so it
    really is the same song), lines that match nothing (titles, credits) are dropped. Repeats of the
    same line a moment later (a line building up word by word) are joined."""
    texts = [w for _, w in known or [] if w]
    fixed, matched = [], 0
    for t, w in lines:
        best = max(texts, key=lambda x: difflib.SequenceMatcher(None, w, x).ratio()) if texts else None
        if best and difflib.SequenceMatcher(None, w, best).ratio() >= 0.5:
            fixed.append((t, best, True))
            matched += 1
        else:
            fixed.append((t, w, False))
    keep_all = matched < 0.6 * max(1, len(lines))
    out_lines = []
    for t, w, ok in fixed:
        if not (ok or keep_all):
            continue
        if out_lines and out_lines[-1][1] == w and t - out_lines[-1][0] < 4:
            continue
        out_lines.append((t, w))
    return out_lines


def scan(video_id, start, lang="zh"):
    os.makedirs(CACHE, exist_ok=True)
    cached = os.path.join(CACHE, f"video2-{video_id}-{lang}.json")
    if os.path.exists(cached):
        with open(cached, encoding="utf-8") as f:
            d = json.load(f)
        out(**({"lines": d["lines"]} if d["lines"] else {"none": True}))
        out(done=True)
        return
    folder = tempfile.mkdtemp(prefix="lyrics-video-")
    path = None
    try:
        path = download(video_id, folder)
        r = Reader(path)
        end = int(r.length)
        first = max(0, int(start) - 10)
        order = list(range(first, end)) + list(range(0, first))   # what's playing now comes first
        samples, row, sent = [], None, None
        for k, sec in enumerate(order):
            samples.append((sec, r.read(sec)))
            row = row or lyric_row(samples, lang)
            if row is None and k >= GIVE_UP_AFTER:
                break
            if row is not None and (k % 8 == 7 or k == len(order) - 1):
                lines = group(samples, row)
                if lines and lines != sent:
                    out(lines=lines)
                    sent = lines
        if row is None:
            final = []
        else:
            final = group(samples, row)
            # sharpen each start: is the line already showing half a second earlier?
            for i, (t, text) in enumerate(final):
                if t >= 0.5 and any(abs(y - row) <= BAND and similar(x, text) >= 0.5
                                    for x, y, _ in r.read(t - 0.5)):
                    final[i] = (t - 0.5, text)
        out(**({"lines": final} if final else {"none": True}))
        with open(cached, "w", encoding="utf-8") as f:
            json.dump({"lines": final, "at": time.time()}, f, ensure_ascii=False)
        out(done=True)
    finally:
        try:
            if path and os.path.exists(path):
                os.remove(path)
            os.rmdir(folder)
        except OSError:
            pass


if __name__ == "__main__":
    scan(sys.argv[1], float(sys.argv[2]) if len(sys.argv) > 2 else 0, sys.argv[3] if len(sys.argv) > 3 else "zh")
