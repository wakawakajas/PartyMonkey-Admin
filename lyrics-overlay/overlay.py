"""Lyrics Overlay - listens to PC sound or a mic, finds the song, shows synced lyrics on top of everything.

Right-click the bar for: sound source, Listen now, Auto, text size, quit. Drag to move.
"""
import ctypes
import json
import re
import os

try:  # real pixel coordinates on scaled (125%/150%) displays, so the bar lands where expected
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    pass
import sys
import tempfile
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
import warnings

import soundcard as sc

import lyrics
import media
import share
import ytcc
import ytvideo

warnings.filterwarnings("ignore", category=sc.SoundcardRuntimeWarning)

RATE = 44100
CLIP_SECONDS = 8
RECHECK_SECONDS = 40          # auto mode: re-listen this often to catch song changes / resync
HERE = os.path.dirname(os.path.abspath(__file__))
SETTINGS = os.path.join(HERE, "settings.json")
KEY = "#010101"               # transparent colour


JOIN_OFF = "Off - use my own listening"


def app_version():
    """Short git commit of this copy, or why there isn't one (shown in the NAS diag, to spot PCs that
    can't update themselves)."""
    import shutil
    import subprocess
    if not os.path.isdir(os.path.join(os.path.dirname(HERE), ".git")):
        return "no git folder: " + HERE[:120]
    if not shutil.which("git"):
        return "git not installed"
    try:
        r = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=HERE, capture_output=True, text=True,
                           timeout=10, creationflags=0x08000000)
        return r.stdout.strip() or ("git error: " + r.stderr.strip()[:120])
    except Exception as ex:
        return f"git error: {ex}"[:120]


def load_settings():
    try:
        with open(SETTINGS, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def sources():
    """[(label, device)] - PC sound (loopback of each speaker) first, then microphones."""
    out = []
    for m in sc.all_microphones(include_loopback=True):
        label = ("PC sound: " if m.isloopback else "Mic: ") + m.name
        out.append((label, m))
    out.sort(key=lambda x: not x[0].startswith("PC"))
    return out


class Overlay:
    def __init__(self):
        self.cfg = load_settings()
        self.root = tk.Tk()
        self.root.title("Lyrics Overlay")
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.config(bg=KEY)
        self.root.attributes("-transparentcolor", KEY)
        self.k = max(1.0, self.root.winfo_fpixels("1i") / 96)   # display scaling (2.0 at 200%)
        w = int(1000 * self.k)
        h = int(self.cfg.get("size", 26) * self.HEIGHT_PER_PT * self.k)   # room for the sync buttons row
        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        x, y = self.cfg.get("pos", ((sw - w) // 2, sh - h - int(80 * self.k)))
        vx, vy = self.root.winfo_vrootx(), self.root.winfo_vrooty()
        vw, vh = max(sw, self.root.winfo_vrootwidth()), max(sh, self.root.winfo_vrootheight())
        if not (vx - w // 2 <= x <= vx + vw - w // 2 and vy <= y <= vy + vh - 40):
            x, y = (sw - w) // 2, sh - h - int(80 * self.k)   # saved spot is off-screen: reset to bottom-centre
        self.root.geometry(f"{w}x{h}+{x}+{y}")

        self.size = self.cfg.get("size", 26)
        self.font_main = tkfont.Font(family="Microsoft YaHei UI", size=self.size, weight="bold")
        self.font_small = tkfont.Font(family="Microsoft YaHei UI", size=max(10, self.size // 2))
        self.font_btn = tkfont.Font(family="Segoe UI", size=9)
        self.canvas = tk.Canvas(self.root, bg=KEY, highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)

        self.srcs = sources()
        names = [s[0] for s in self.srcs]
        saved = self.cfg.get("source") or ""
        # no speaker/mic at all (Join and Player modes still work): default to following the player
        default = names[0] if names else "Player:auto"
        self.source = tk.StringVar(value=saved if (saved in names or saved.startswith("Player:")) else default)
        self.no_pos = False            # Player mode: the player doesn't report its position
        self.follow_fails = 0          # Join: failed polls in a row (one hiccup shouldn't blank the lyrics)
        self.follow_lrev = None        # Join: which lyrics we already have from the host
        self.hotkeys = {}
        self.player_title = None       # Player mode: the title we last looked up lyrics for
        self.player_paused = False
        self.player_np = None
        self.follow_paused = None              # Joined and the host has paused: the position it stopped at
        self.player_app = None         # app id of the player we're following
        self.busy_player = False
        self.next_player = 0.0
        self.auto = tk.BooleanVar(value=self.cfg.get("auto", True))
        # read YouTube videos ahead of time for everyone (this PC's spare power), saved on the NAS
        self.read_ahead = tk.BooleanVar(value=self.cfg.get("read_ahead", False))
        self.ahead_seeds, self.ahead_done, self.ahead_proc = [], {}, None
        self.words, self.words_key = {}, None    # karaoke: Kugou word timing for the current song
        self.edit_seen = None                    # (song key, edit file) last applied

        self.song = None              # dict from lyrics.recognize
        self.lines = None             # [(t, text)]
        self.t0 = 0.0                 # wall-clock time when song position was 0
        self.rough = False            # lyric file timestamps look guessed
        self.status = "Right-click for menu"
        self.listening = False
        self.next_check = 0.0
        share.DIAG.update(ver=app_version(), scan="")
        self.share_url = share.start()   # live lyrics page for colleagues on the office network
        self.update_ready = False
        self.update_wait, self.restarting = None, False
        threading.Thread(target=self._update_loop, daemon=True).start()
        threading.Thread(target=self._ahead_loop, daemon=True).start()
        threading.Thread(target=self._edits_loop, daemon=True).start()
        follow = self.cfg.get("follow") or ""
        self.follow = tk.StringVar(value="" if follow == JOIN_OFF else follow)   # PC name we Join, "" = listen myself
        self.misses = 0                  # failed recognitions in a row
        self.last_media = None           # title Windows says is playing (to spot song changes)
        self.next_media_check = 0.0
        self.next_follow = 0.0
        self.busy_media = False
        self.busy_follow = False

        self.canvas.bind("<ButtonPress-1>", self._drag_start)
        self.canvas.bind("<B1-Motion>", self._drag)
        self.canvas.bind("<ButtonRelease-1>", lambda e: self._save())
        self.canvas.bind("<Button-3>", self._menu)
        self.canvas.bind("<Double-Button-1>", self._double_click)
        self.root.bind("<Escape>", lambda e: self.quit())
        # one-click sync buttons drawn on the bar (redrawn every frame; the bindings live on the tags)
        self._btn_clicked = 0.0
        for tag, fn in (("btn_early", lambda: self._nudge(0.5)), ("btn_sync", self._sync_next),
                        ("btn_late", lambda: self._nudge(-0.5))):
            self.canvas.tag_bind(tag, "<Button-1>", lambda e, fn=fn: self._button(fn))
            self.canvas.tag_bind(tag, "<Enter>", lambda e: self.canvas.config(cursor="hand2"))
            self.canvas.tag_bind(tag, "<Leave>", lambda e: self.canvas.config(cursor=""))
        self.scan_proc, self.scan_media = None, None   # reading lyrics from the video picture (ytvideo.py)
        self.scan_known = None                 # lyric-site lyrics of the video being read (to correct / align)
        self.flash = ("", 0.0)                 # short note after a sync ("lyrics 0.5s earlier")
        threading.Thread(target=self._hotkey_loop, daemon=True).start()

        self.tick()
        if self.auto.get():
            self.listen()

    # ---------- window ----------
    def _drag_start(self, e):
        self._dx, self._dy = e.x, e.y

    def _drag(self, e):
        self.root.geometry(f"+{e.x_root - self._dx}+{e.y_root - self._dy}")

    def _save(self):
        self.cfg.update(pos=(self.root.winfo_x(), self.root.winfo_y()), size=self.size,
                        source=self.source.get(), auto=self.auto.get(), read_ahead=self.read_ahead.get())
        with open(SETTINGS, "w", encoding="utf-8") as f:
            json.dump(self.cfg, f, ensure_ascii=False)

    def _menu(self, e):
        m = tk.Menu(self.root, tearoff=0)
        join = tk.Menu(m, tearoff=0)
        # Tk gives a radiobutton with value "" its label as the value, so "Off" is turned back into ""
        join.add_radiobutton(label=JOIN_OFF, value=JOIN_OFF, variable=self.follow,
                             command=self._follow_changed)
        share.refresh()                      # the folder is only read every 20s when not Joined
        ps = share.peers(hosts_only=True)    # PCs that are themselves Joined to someone aren't listed
        if ps:
            join.add_separator()
        for p in ps:
            label = f"{p['name']}  —  " + (f"♪ {p['title']}" if p.get("title") else "nothing playing")
            join.add_radiobutton(label=label, value=p["name"], variable=self.follow, command=self._follow_changed)
        if not ps:
            join.add_command(label="(no other PCs found yet - is their app open?)", state="disabled")
        if not share.nas_ok():
            join.add_command(label="(can't reach the NAS - only PCs on this same network show up)",
                             state="disabled")
        m.add_cascade(label="Join a colleague's lyrics", menu=join)
        m.add_separator()
        m.add_command(label="Listen now  (double-click)", command=self.listen)
        m.add_checkbutton(label="Auto (keep listening)", variable=self.auto, command=self._save)
        src = tk.Menu(m, tearoff=0)
        src.add_radiobutton(label="Follow this PC's player - auto (YouTube / Spotify, no listening)",
                            value="Player:auto", variable=self.source, command=self._source_changed)
        for s in media.sessions():
            label = f"Follow {s['name']}  —  {s['title'][:45]}" + ("" if s["playing"] else "  (paused)")
            src.add_radiobutton(label=label, value="Player:" + s["app"], variable=self.source,
                                command=self._source_changed)
        src.add_separator()
        for label, _ in self.srcs:
            src.add_radiobutton(label=label, value=label, variable=self.source, command=self._source_changed)
        m.add_cascade(label="Sound source", menu=src)
        m.add_command(label="Share with colleagues (QR code)…", command=self._share_window)
        m.add_separator()
        hk = lambda act: f"   [{self.hotkeys[act]}]" if act in self.hotkeys else ""
        m.add_command(label="▶ Next line starts NOW  (sync)" + hk("sync"), command=self._sync_next)
        m.add_command(label="Lyrics earlier  (-0.5s)" + hk("early"), command=lambda: self._nudge(0.5))
        m.add_command(label="Lyrics later  (+0.5s)" + hk("late"), command=lambda: self._nudge(-0.5))
        m.add_command(label="Reset sync for this song", command=self._reset_shift)
        m.add_command(label="Edit this song's lyrics…  (saved for everyone)", command=self._edit_window,
                      state="normal" if self.song and self.lines else "disabled")
        m.add_checkbutton(label="Read videos ahead for the team  (uses this PC's spare power)",
                          variable=self.read_ahead, command=self._save)
        m.add_command(label="Bigger text", command=lambda: self._resize(+4))
        m.add_command(label="Smaller text", command=lambda: self._resize(-4))
        m.add_separator()
        m.add_command(label="⬆ Restart to update  (new version ready)" if self.update_ready
                      else "Restart  (gets the newest version)", command=self.restart)
        m.add_command(label="Quit", command=self.quit)
        m.tk_popup(e.x_root, e.y_root)

    # ---------- sync (remembered per song, so badly timed lyric files only need fixing once) ----------
    def _song_key(self):
        if not self.song:
            return None
        key = f"{self.song['title']}|{self.song['artist']}"
        if self.follow.get():
            # Joined: the host's timing already has the host's sync in it - this PC's own fixes for
            # the song (made while listening itself) must not be added on top
            return "join|" + key
        return key + "|video" if self.song.get("cc") else key   # video-timed lyrics need their own sync

    def _shift(self):
        return self.cfg.get("shifts", {}).get(self._song_key(), 0.0)

    def _set_shift(self, value):
        key = self._song_key()
        if not key:
            return
        old = self._shift()
        self.cfg.setdefault("shifts", {})[key] = round(value, 2)
        self.t0 += value - old
        self._save()
        self._publish()

    def _nudge(self, s):
        if not self.lines:
            return
        self._set_shift(self._shift() - s)
        self.flash = (f"lyrics {abs(s):g}s {'earlier' if s > 0 else 'later'}", time.time() + 1.5)

    def _reset_shift(self):
        self._set_shift(0.0)

    def _sync_next(self):
        """The line shown underneath is being sung right now -> line up the lyrics to it."""
        if not self.lines:
            return
        pos = time.time() - self.t0
        nxt = next((t for t, _ in self.lines if t > pos), None)
        if nxt is not None:
            self._set_shift(self._shift() + (pos - nxt))
            self.flash = ("synced ✓", time.time() + 1.5)

    # ---------- shortcuts: buttons on the bar + keys that work from any app ----------
    HOTKEYS = (("Ctrl+Alt+←", 0x25, "early"), ("Ctrl+Alt+→", 0x27, "late"), ("Ctrl+Alt+↓", 0x28, "sync"))

    def _button(self, fn):
        self._btn_clicked = time.time()
        fn()

    def _double_click(self, e):
        if time.time() - self._btn_clicked > 0.6:   # fast clicks on a sync button aren't "Listen now"
            self.listen()

    def _hotkey_loop(self):
        """Windows-wide hotkeys, so you can sync without leaving YouTube / Spotify."""
        import ctypes.wintypes as wt
        user32 = ctypes.windll.user32
        actions = {"early": lambda: self._nudge(0.5), "late": lambda: self._nudge(-0.5), "sync": self._sync_next}
        ids = {}
        for i, (label, vk, act) in enumerate(self.HOTKEYS, start=1):
            if user32.RegisterHotKey(None, i, 0x0001 | 0x0002 | 0x4000, vk):   # Alt | Ctrl | no auto-repeat
                ids[i] = act
                self.hotkeys[act] = label
        msg = wt.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            if msg.message == 0x0312 and msg.wParam in ids:                     # WM_HOTKEY
                self.root.after(0, actions[ids[msg.wParam]])

    # ---------- join a colleague ----------
    def _follow_changed(self):
        if self.follow.get() == JOIN_OFF:
            self.follow.set("")
        self.cfg["follow"] = self.follow.get()
        self._save()
        if self.follow.get():                      # joining: this PC's own video reading isn't needed
            self._stop_scan()
            self.scan_media = None
        self.song, self.lines, self.misses = None, None, 0
        self.status = f"Joining {self.follow.get()}…" if self.follow.get() else "Right-click for menu"
        self.follow_fails, self.follow_lrev = 0, None
        self.follow_paused = None
        self.next_follow = 0.0
        self.next_check = 0.0
        self._publish()

    def _follow_worker(self, name, lrev):
        try:
            host = next((p for p in share.peers() if p["name"] == name), None)
            if not host:
                self.root.after(0, self._follow_apply, name, None, 0.0)
                return
            st, off = share.fetch_state(host, lrev)       # via the NAS file, or straight from that PC
            self.root.after(0, self._follow_apply, name, st, off)
        except Exception:
            self.root.after(0, self._follow_apply, name, None, 0.0)

    def _follow_apply(self, name, st, off):
        self.busy_follow = False
        if self.follow.get() != name:          # switched while we were fetching
            return
        if st is None:
            self.follow_fails += 1
            if self.follow_fails >= 3:         # ~6s without an answer, not just one dropped reply
                self.song, self.lines, self.follow_lrev = None, None, None
                self.status = f"Waiting for {name}… (is their app open?)"
                self._publish()
            return
        self.follow_fails = 0
        if not st.get("title"):
            self.song, self.lines = None, None
            self.status = f"{name}: {st.get('status') or 'nothing playing'}"
            self.follow_lrev = st.get("lrev")
        else:
            if "lines" in st:                  # lyrics left out = same as the ones we already have
                self.follow_lrev = st.get("lrev")
                lines = [tuple(x) for x in st["lines"]] or None
            else:
                lines = self.lines
            if lines != self.lines:
                self.rough = bool(lines) and lyrics.rough_timing(lines)
            self.song = {"title": st["title"], "artist": st["artist"], "offset": 0.0, "static_lyrics": None}
            self.lines = lines
            self.follow_paused = st.get("paused")
            if st.get("video"):
                self._ahead_seed(st["video"])
            if self.follow_paused is not None:     # host paused: stand still at that spot (see tick)
                self.t0 = time.time() - self.follow_paused + self._shift()
            elif st.get("t0"):
                self.t0 = st["t0"] - off + self._shift()
            self.status = "" if lines else (st.get("status") or "No synced lyrics found")
        self._publish()

    def _publish(self):
        paused = None
        if self._player_mode() and self.player_paused and self.song and not self.follow.get():
            paused = time.time() - self.t0             # where the song stands while paused
        share.publish(self.song, self.lines, self.t0, self.status, via=self.follow.get() or None, paused=paused)

    def _share_window(self):
        win = tk.Toplevel(self.root)
        win.title("Share lyrics")
        win.attributes("-topmost", True)
        win.configure(bg="white", padx=20, pady=16)
        if not self.share_url:
            tk.Label(win, text=f"Sharing is off: port {share.PORT} is already in use.\n"
                               "Close any other Lyrics Overlay and restart.", bg="white").pack()
            return
        tk.Label(win, text="Colleagues: scan with your phone camera\n(must be on the office Wi-Fi)",
                 bg="white", font=("Segoe UI", 11)).pack()
        path = os.path.join(os.environ.get("TEMP", "."), "lyrics_share_qr.png")
        win.qr = tk.PhotoImage(file=share.qr_png(self.share_url, path))
        if self.k > 1.4:
            win.qr = win.qr.zoom(2)
        tk.Label(win, image=win.qr, bg="white").pack(pady=8)
        e = tk.Entry(win, font=("Segoe UI", 13), justify="center", relief="flat", bg="#f1f3f6")
        e.insert(0, self.share_url)
        e.configure(state="readonly")
        e.pack(fill="x", ipady=4)

        def copy():
            win.clipboard_clear()
            win.clipboard_append(self.share_url)
            b.config(text="Copied ✓")
        b = tk.Button(win, text="Copy link", command=copy)
        b.pack(pady=(8, 0))

    def _resize(self, d):
        self.size = max(12, min(60, self.size + d))
        self.font_main.config(size=self.size)
        self.font_small.config(size=max(10, self.size // 2))
        self.root.geometry(f"{self.root.winfo_width()}x{int(self.size * self.HEIGHT_PER_PT * self.k)}")
        self._save()

    # ---------- updates (the app lives in a git repo; Start Lyrics Overlay.bat pulls before starting) ----------
    def _update_loop(self):
        """Every 10 min: is there a newer version on GitHub? If so the app restarts itself at the next
        song change / pause (see _auto_update), so colleagues never have to click Restart."""
        import subprocess
        repo = os.path.dirname(HERE)
        if not os.path.isdir(os.path.join(repo, ".git")):
            self._update_loop_files()
            return
        time.sleep(60)
        while True:
            try:
                kw = dict(cwd=repo, capture_output=True, text=True, timeout=60,
                          creationflags=0x08000000, env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})
                subprocess.run(["git", "fetch", "-q"], **kw)
                behind = subprocess.run(["git", "rev-list", "--count", "HEAD..@{u}", "--", "lyrics-overlay"], **kw)
                if behind.stdout.strip().isdigit() and int(behind.stdout.strip()) > 0:
                    self.update_ready = True
            except Exception:
                pass
            time.sleep(10 * 60)

    def _update_loop_files(self):
        """No git on this copy: compare the app's files with GitHub's and download changed ones."""
        import updater
        time.sleep(60)
        while True:
            try:
                files = updater.changed_files()
                if files:
                    updater.apply(files)
                    self.update_ready = True
            except Exception:
                pass
            time.sleep(10 * 60)

    def restart(self):
        import subprocess
        self._save()
        subprocess.Popen(["cmd", "/c", os.path.join(HERE, "Start Lyrics Overlay.bat")], cwd=HERE,
                         creationflags=0x08000000)       # it pulls the update, closes this copy, starts the new one

    def quit(self):
        self._stop_scan()
        p = self.ahead_proc
        if p and p.poll() is None:
            p.kill()
        share.stop()
        self._save()
        self.root.destroy()

    # ---------- listening ----------
    def _source_changed(self):
        self._save()
        self.song, self.lines, self.misses = None, None, 0
        self.player_title, self.next_player, self.next_check = None, 0.0, 0.0
        self.status = "Looking for the player…" if self._player_mode() else "Right-click for menu"
        self._publish()

    def _player_mode(self):
        return self.source.get().startswith("Player:")

    # ---------- follow a player on this PC (exact title + position from Windows, no listening) ----------
    def _player_worker(self, which, last_app):
        np = media.now_playing(None if which == "auto" else which)
        if np is None and which == "auto" and last_app:   # paused: stay on the same player, don't jump
            np = media.now_playing(last_app)
        self.root.after(0, self._player_apply, which, np, time.time())

    def _player_apply(self, which, np, read_at):
        self.busy_player = False
        if self.follow.get() or self.source.get() != "Player:" + which:
            return
        if not np:
            if self.song or self.status != "Nothing playing on this PC":
                self.song, self.lines, self.player_title = None, None, None
                self.status = "Nothing playing on this PC"
                self._publish()
            return
        self.player_paused = not np["playing"]
        self.player_app = np["app"]
        if np["title"] != self.player_title:      # new video / song -> look up its lyrics
            self.player_title, self.player_np = np["title"], np
            self.song = {"title": lyrics.clean_title(np["title"])[:40], "artist": np["artist"],
                         "offset": 0.0, "static_lyrics": None, "media": np["title"]}
            self.lines, self.rough, self.status = None, False, "Finding lyrics…"
            threading.Thread(target=self._player_lyrics, args=(np,), daemon=True).start()
            self.scan_known = None
            self._scan_video(np)                   # start reading the video picture straight away
        self.no_pos = np["pos"] is None and not self.song.get("from_shazam")
        if np["pos"] is not None and not self.song.get("from_shazam"):
            t0 = read_at - np["pos"] + self._shift()
            if abs(t0 - self.t0) > 0.3 or self.player_paused:   # small wobble ignored, skips/pauses followed
                self.t0 = t0
                self._publish()

    def _player_lyrics(self, np):
        cc, cc_translation = ytcc.captions(np)       # the YouTube video's own subtitles: exact timing
        if cc and not cc_translation:
            self.root.after(0, self._player_lyrics_done, np["title"], self._media_title(np), cc, None, "cc")
            return
        title, lines = self._lyrics_from_media(np)
        shazam = None
        if lines and cc and np["pos"] is not None:     # translated subtitles: borrow their exact timing
            timed = ytcc.retime(lines, cc)
            if timed:
                self.root.after(0, self._player_lyrics_done, np["title"], title, timed, None, "retimed")
                return
        if not lines:
            # the video title didn't lead to lyrics (odd title, cover...): listen to this PC's sound and ask
            # Shazam for the real song name, then look that up
            shazam = self._shazam_pc_sound()
            if shazam:
                lines = lyrics.find_lyrics(shazam["title"], shazam["artist"])
                if lines:
                    title = shazam["title"]
                    timed = cc and np["pos"] is not None and ytcc.retime(lines, cc)
                    if timed:
                        self.root.after(0, self._player_lyrics_done, np["title"], title, timed, None, "retimed")
                        return
        if not lines and cc:                         # nothing else: the video's translated subtitles
            shazam, lines, title = None, cc, self._media_title(np)
            self.root.after(0, self._player_lyrics_done, np["title"], title, lines, None, "translation")
            return
        self.root.after(0, self._player_lyrics_done, np["title"], title, lines, shazam)

    def _shazam_pc_sound(self):
        """Record 8s of this PC's sound and identify it. Returns Shazam's dict (+ 'started') or None."""
        try:
            ctypes.windll.ole32.CoInitializeEx(None, 0)
            spk = sc.default_speaker()
            dev = sc.get_microphone(spk.name, include_loopback=True)
            started = time.time()
            with dev.recorder(samplerate=RATE) as rec:
                audio = rec.record(numframes=RATE * CLIP_SECONDS)
            if abs(audio).max() < 0.0003:
                return None
            song = lyrics.recognize(lyrics.to_wav_bytes(audio, RATE))
            if song:
                song["started"] = started
            return song
        except Exception:
            return None

    def _player_lyrics_done(self, media_title, title, lines, shazam=None, cc=None):
        if media_title != self.player_title or not self.song:
            return                                 # the video changed while we were searching
        if cc not in ("cc", "retimed"):
            self.scan_known = lines if cc != "translation" else None
        if self.song.get("cc") == "video":         # the video reader got there first: keep its lines
            self.song["title"] = title
            self._publish()
            return
        if cc in ("cc", "retimed"):                # exact timing from subtitles: no need to read the picture
            self._stop_scan()
        old_shift = self._shift()
        self.song["title"] = title
        self.song["cc"] = cc
        if shazam and lines and self.no_pos:       # no position from the player: time it from Shazam
            self.song["artist"] = shazam["artist"]
            self.song["from_shazam"] = True
            self.no_pos = False
            self.t0 = shazam["started"] - shazam["offset"] + self._shift()
        else:
            self.t0 += self._shift() - old_shift   # saved sync for this song (key is the title)
        self.lines = lines
        self.rough = bool(lines) and lyrics.rough_timing(lines)
        self.status = "" if lines else "No synced lyrics found"
        self._publish()
        if cc not in ("cc", "retimed"):            # no exactly timed lyrics yet: read the video picture
            self._scan_video(self.player_np)       # (already running since the video started)

    def listen(self):
        if self.listening or self.follow.get() or self._player_mode():
            return
        dev = dict(self.srcs).get(self.source.get())
        if dev is None:
            self.status = "No speaker or mic found - use Sound source › Follow this PC's player"
            return
        self.listening = True
        self.status = "Listening…"
        threading.Thread(target=self._listen_worker, args=(dev,), daemon=True).start()

    def _listen_worker(self, dev):
        try:
            import ctypes
            ctypes.windll.ole32.CoInitializeEx(None, 0)   # COM per thread for WASAPI
            # what Windows says this PC is playing (Spotify / YouTube) - only meaningful for PC sound
            np = media.now_playing() if dev.isloopback else None
            np_at = time.time()
            cur = self.song
            if (np and np["pos"] is not None and cur and cur.get("media") == np["title"]
                    and cur.get("cc") in ("cc", "video", "retimed", "edited")):
                # same video, lyrics already timed to it: just follow the video's position
                self.root.after(0, self._done, dict(cur, offset=np["pos"]), self.lines, "", np_at, np)
                return
            if np and np["pos"] is not None:
                # the YouTube video's own subtitles in the song's language: exact timing, no Shazam needed
                cc, cc_translation = ytcc.captions(np)
                if cc and not cc_translation:
                    song = {"title": self._media_title(np), "artist": np["artist"], "offset": np["pos"],
                            "static_lyrics": None, "media": np["title"], "cc": "cc"}
                    self.root.after(0, self._done, song, cc, "", np_at, np)
                    return
            started = time.time()
            with dev.recorder(samplerate=RATE) as rec:
                audio = rec.record(numframes=RATE * CLIP_SECONDS)
            silent = abs(audio).max() < 0.0003
            if silent and not np:
                self.root.after(0, self._done, None, None, "No sound heard - check the source", started)
                return
            song = None if silent else lyrics.recognize(lyrics.to_wav_bytes(audio, RATE))
            cur = self.song
            if song:
                same = cur and (song["title"], song["artist"]) == (cur["title"], cur["artist"])
                lines = self.lines if same else lyrics.find_lyrics(song["title"], song["artist"])
                if not lines and np:              # Shazam named it but no lyrics: try the YouTube/Spotify title
                    lines = self._lyrics_from_media(np)[1]
            elif np:                              # Shazam can't place it (cover / AI remix): use the title
                same = cur and cur.get("media") == np["title"]
                title, lines = (cur["title"], self.lines) if same else self._lyrics_from_media(np)
                if np["pos"] is None:
                    self.root.after(0, self._done, None, None, "Song not recognised", started)
                    return
                song = {"title": title, "artist": np["artist"], "offset": np["pos"], "static_lyrics": None}
            else:
                self.root.after(0, self._done, None, None, "Song not recognised", started)
                return
            if np:
                song["media"] = np["title"]
                if np["pos"] is not None:
                    cc, cc_translation = ytcc.captions(np)
                    timed = cc and lines and ytcc.retime(lines, cc)
                    if timed:                     # translated subtitles: borrow their exact timing
                        song.update(offset=np["pos"], cc="retimed")
                        lines, started = timed, np_at
                    elif cc and not lines:        # nothing else: the video's translated subtitles
                        song.update(offset=np["pos"], cc="translation")
                        lines, started = cc, np_at
            self.root.after(0, self._done, song, lines, "", started, np)
        except Exception as ex:
            self.root.after(0, self._done, None, None, f"Error: {ex}", time.time())

    @staticmethod
    def _media_title(np):
        """Short song name from the video title."""
        q = media.queries(np)
        return (q[0][0] if q else lyrics.clean_title(np["title"]))[:40]

    @staticmethod
    def _lyrics_from_media(np):
        """(song title, lyrics) from the YouTube video / Spotify title."""
        for title, artist in media.queries(np):
            lines = lyrics.find_lyrics(title, artist)
            if lines:
                return title, lines
        return lyrics.clean_title(np["title"])[:40], None

    def _done(self, song, lines, msg, started, np=None):
        self.listening = False
        if self.follow.get():                     # user switched to Join while we were listening
            return
        if song and np and song.get("cc") not in ("cc", "retimed"):
            if song.get("cc") != "video":
                self.scan_known = lines if song.get("cc") != "translation" else None
            self.root.after(0, self._scan_video, np)  # no subtitles in the song's language: read the picture
        self.next_check = time.time() + RECHECK_SECONDS
        if song:
            self.misses = 0
            self.song, self.lines = song, lines
            self.t0 = started - song["offset"] + self._shift()
            self.rough = bool(lines) and lyrics.rough_timing(lines)
            self.status = "" if lines else "No synced lyrics found"
            self.last_media = song.get("media") or self.last_media
        else:
            self.misses += 1
            if self.song and self.misses < 2:
                self.next_check = time.time() + 5    # maybe a quiet bit - check again before giving up
            else:                                    # song has changed to something we can't place
                self.song, self.lines = None, None
                self.status = msg
                self.next_check = time.time() + 15
        self._publish()

    # ---------- lyrics drawn into the video picture (ytvideo.py, own process) ----------
    def _scan_video(self, np):
        if not np or not np.get("browser") or np["title"] == self.scan_media or (np.get("duration") or 999) < 60:
            return                                 # not a browser video, an ad, or already reading this one
        self._stop_scan()
        self.scan_media = np["title"]
        threading.Thread(target=self._scan_worker, args=(np,), daemon=True).start()

    def _stop_scan(self):
        p, self.scan_proc = self.scan_proc, None
        if p and p.poll() is None:
            try:
                p.kill()
            except Exception:
                pass

    def _scan_worker(self, np):
        import subprocess
        diag = share.DIAG
        if ytcc.yt_dlp is None:
            diag["scan"] = "yt-dlp not installed"
            return
        diag["scan"] = "finding the video: " + np["title"][:40]
        vid = ytcc.video_id(np)
        if self.scan_media != np["title"]:
            return
        if not vid:
            diag["scan"] = "video not found on YouTube: " + np["title"][:40]
            return
        err_path = os.path.join(tempfile.gettempdir(), "lyrics-overlay-video.log")
        try:
            err = open(err_path, "w", encoding="utf-8")
            # which row to read when the video shows two languages: the lyric sites' language, else the title's
            known = self.scan_known
            words = " ".join(w for _, w in known or [])
            lang = "en" if words and not lyrics.has_cjk(words) else "zh"   # mostly Chinese songs here
            p = subprocess.Popen([sys.executable, os.path.join(HERE, "ytvideo.py"), vid, str(np["pos"] or 0), lang],
                                 cwd=HERE, stdout=subprocess.PIPE, stderr=err,
                                 creationflags=0x08000000 | 0x00004000)   # no window, below-normal priority
        except Exception as ex:
            diag["scan"] = f"could not start: {ex}"
            return
        if self.scan_media != np["title"]:
            p.kill()
            return
        self.scan_proc = p
        diag["scan"] = f"reading {vid}"
        self.root.after(0, self._got_video_id, np["title"], vid)
        for raw in p.stdout:
            try:
                d = json.loads(raw)
            except ValueError:
                continue
            if d.get("lines"):
                if self.scan_media != np["title"]:
                    break
                known = self.scan_known                # may have arrived since the reading started
                read = [tuple(x) for x in d["lines"]]
                cjk_read = sum(lyrics.has_cjk(w) for _, w in read) > len(read) / 2
                if cjk_read != (lang == "zh") and known:
                    # only a translation row was readable (e.g. Chinese written in vertical columns):
                    # keep the lyric-site lyrics in the song's language rather than show the translation
                    diag["scan"] = f"{vid}: only a translation row readable"
                    continue
                lines = [tuple(x) for x in ytvideo.fix_with(read, known)]
                diag["scan"] = f"reading {vid}: {len(lines)} lines"
                if known and d.get("final") and len(lines) < 0.4 * len(known):
                    # the whole video gave only a few lines (titles, a bad read): don't trust it
                    diag["scan"] = f"{vid}: only {len(lines)} lines read - kept the lyric-site lyrics"
                    continue
                if known and not d.get("final") and len(lines) < 0.7 * len(known):
                    # only part of the video read so far: shift the lyric-site lyrics to match the lines
                    # already read, so they're right within seconds instead of after the whole video
                    off = ytvideo.offset_to(lines, known)
                    if off is None:
                        continue
                    lines = [(round(t + off, 2), w) for t, w in known]
                    diag["scan"] += f" (lyrics shifted {off:+.1f}s)"
                now = media.now_playing(np["app"])     # fresh position, to time lines to the video
                self.root.after(0, self._scan_lines, np["title"], lines, now, time.time())
            elif d.get("none"):
                diag["scan"] = f"{vid}: no lyrics in the picture"
            elif d.get("done") and "no lyrics" not in diag["scan"]:
                diag["scan"] = diag["scan"].replace("reading", "done")
        code = p.wait()
        err.close()
        if code not in (0, None) and self.scan_proc is p:
            try:
                tail = open(err_path, encoding="utf-8", errors="ignore").read()[-300:]
            except OSError:
                tail = ""
            diag["scan"] = f"{vid}: failed ({code}) {tail}"

    def _got_video_id(self, media_title, vid):
        if self.song and self.song.get("media") == media_title:
            self.song["vid"] = vid
            self._publish()
        self._ahead_seed(vid)

    def _scan_lines(self, media_title, lines, now, read_at):
        if not self.song or self.song.get("media") != media_title or len(lines) < 3:
            return
        if self.song.get("cc") == "edited":        # someone fixed this song's lyrics by hand
            return
        old_shift = self._shift()
        self.song["cc"] = "video"
        self.lines, self.rough, self.status = lines, False, ""
        if self._player_mode() or not now or now["pos"] is None:
            self.t0 += self._shift() - old_shift
        else:
            self.t0 = read_at - now["pos"] + self._shift()
        self._publish()

    # ---------- read videos ahead for the team ----------
    def _ahead_seed(self, vid):
        if vid and vid not in self.ahead_done and vid not in self.ahead_seeds:
            self.ahead_seeds.append(vid)

    def _ahead_list(self, vid):
        """Videos likely to be played next: the channel's newest uploads + YouTube's mix for this video."""
        import yt_dlp
        opts = {**ytcc.OPTS, "extract_flat": True, "playlistend": 30}
        found = []
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info("https://www.youtube.com/watch?v=" + vid, download=False, process=False)
            urls = []
            if info.get("channel_url"):
                urls.append(info["channel_url"].rstrip("/") + "/videos")
            urls.append(f"https://www.youtube.com/watch?v={vid}&list=RD{vid}")
            for url in urls:
                try:
                    res = ydl.extract_info(url, download=False)
                except Exception:
                    continue
                for e in res.get("entries") or []:
                    if e and e.get("id") and 60 <= (e.get("duration") or 0) <= 600 and e["id"] not in found:
                        found.append(e["id"])
        return found

    def _ahead_loop(self):
        import subprocess
        time.sleep(30)
        queue = []
        while True:
            time.sleep(5)
            if not self.read_ahead.get():
                continue
            if self.scan_proc and self.scan_proc.poll() is None:
                continue                           # this PC is reading the playing video: wait
            if self.ahead_seeds:
                seed = self.ahead_seeds.pop(0)
                self.ahead_done[seed] = time.time()
                try:
                    queue = [v for v in self._ahead_list(seed) if v not in queue] + queue
                except Exception:
                    pass
            while queue and ytvideo.load_cached(queue[0], "zh") is not None:
                self.ahead_done[queue.pop(0)] = time.time()
            if not queue:
                continue
            vid = queue.pop(0)
            self.ahead_done[vid] = time.time()
            share.DIAG["ahead"] = f"reading {vid}, {len(queue)} more queued"
            try:
                self.ahead_proc = subprocess.Popen(
                    [sys.executable, os.path.join(HERE, "ytvideo.py"), vid, "0", "zh", "ahead"], cwd=HERE,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    creationflags=0x08000000 | 0x00000040)   # no window, lowest priority
                while self.ahead_proc.poll() is None:
                    time.sleep(2)
                    if not self.read_ahead.get():
                        self.ahead_proc.kill()
            except Exception:
                pass
            share.DIAG["ahead"] = f"{len(queue)} queued"

    # ---------- lyrics fixed by hand (saved on the NAS, used by every PC) ----------
    def _edits_dir(self):
        d = ytvideo.nas_dir()
        d = os.path.join(os.path.dirname(d), "lyrics-edits") if d else os.path.join(HERE, "cache", "edits")
        os.makedirs(d, exist_ok=True)
        return d

    @staticmethod
    def _edit_id(title, artist):
        import hashlib
        return hashlib.md5(f"{title}|{artist}".encode("utf-8")).hexdigest()[:16]

    def _edit_latest(self, title, artist):
        """(file name, data) of the newest saved edit for this song, or (None, None).
        Each save is a new file (the NAS can serve an old copy of a rewritten file)."""
        d = self._edits_dir()
        eid = self._edit_id(title, artist)
        names = sorted(fn for fn in os.listdir(d) if fn.startswith(eid + ".") and fn.endswith(".json"))
        if not names:
            return None, None
        with open(os.path.join(d, names[-1]), encoding="utf-8") as f:
            return names[-1], json.load(f)

    def _edits_loop(self):
        while True:
            time.sleep(8)
            song = self.song
            if not song or self.follow.get():      # Joined: the host applies edits and passes them on
                continue
            try:
                name, data = self._edit_latest(song["title"], song["artist"])
            except (OSError, ValueError):
                continue
            key = (song["title"], song["artist"], name)
            if not name:
                continue
            new = key != self.edit_seen
            if new or (not data.get("removed") and song.get("cc") != "edited"):   # re-apply after a re-lookup
                self.root.after(0, self._edit_apply, key, data)

    def _edit_apply(self, key, data):
        if not self.song or (self.song["title"], self.song["artist"]) != key[:2]:
            return
        self.edit_seen = key
        if data.get("removed"):
            if self.song.get("cc") == "edited":    # edit taken back: find the lyrics again
                self.song["cc"] = None
                if self._player_mode():
                    self.player_title = None
                else:
                    self.next_check = 0.0
            return
        self.lines = [tuple(x) for x in data["lines"]]
        self.song["cc"], self.rough, self.status = "edited", False, ""
        self._publish()

    def _edit_window(self):
        if not self.song or not self.lines:
            return
        title, artist = self.song["title"], self.song["artist"]
        win = tk.Toplevel(self.root)
        win.title(f"Edit lyrics - {title}")
        win.attributes("-topmost", True)
        tk.Label(win, justify="left", text=(
            "One line per lyric:  [minutes:seconds] words.  Fix wrong words, add or delete lines.\n"
            "Timing: click a line, then press 'Line starts NOW' at the moment it's sung.\n"
            "Save puts it on the NAS: every PC shows your version of this song from now on.")).pack(
            anchor="w", padx=10, pady=(10, 4))
        txt = tk.Text(win, width=70, height=24, font=("Microsoft YaHei UI", 11), undo=True)
        txt.pack(fill="both", expand=True, padx=10)

        def fmt(t):
            return f"[{int(t // 60):02d}:{t % 60:05.2f}]"
        txt.insert("1.0", "\n".join(f"{fmt(t)} {w}" for t, w in self.lines))
        row = tk.Frame(win)
        row.pack(fill="x", padx=10, pady=10)
        note = tk.Label(row, text="")

        def now_here():
            line = txt.index("insert").split(".")[0]
            body = txt.get(f"{line}.0", f"{line}.end")
            m = re.match(r"^\s*\[\d+:\d+(?:\.\d+)?\]\s*(.*)$", body)
            words = m.group(1) if m else body.strip()
            txt.delete(f"{line}.0", f"{line}.end")
            txt.insert(f"{line}.0", f"{fmt(max(0.0, time.time() - self.t0))} {words}")
            txt.mark_set("insert", f"{int(line) + 1}.0")    # ready for the next line

        def parse():
            lines = []
            for body in txt.get("1.0", "end").splitlines():
                m = re.match(r"^\s*\[(\d+):(\d+(?:\.\d+)?)\]\s*(.*?)\s*$", body)
                if m and m.group(3):
                    lines.append((int(m.group(1)) * 60 + float(m.group(2)), m.group(3)))
            return sorted(lines)

        def save(data):
            d = self._edits_dir()
            name = f"{self._edit_id(title, artist)}.{int(time.time() * 1000)}.json"
            tmp = os.path.join(d, "~" + name)
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"title": title, "artist": artist, "by": share.NAME, "at": time.time(), **data}, f,
                          ensure_ascii=False)
            os.rename(tmp, os.path.join(d, name))

        def do_save():
            lines = parse()
            if len(lines) < 2:
                note.config(text="Need at least 2 lines like  [01:23.40] words")
                return
            try:
                save({"lines": lines})
            except OSError as ex:
                note.config(text=f"Couldn't save: {ex}")
                return
            if not self.follow.get():
                self._edit_apply((title, artist, None), {"lines": lines})
            win.destroy()

        def do_remove():
            try:
                save({"removed": True})
            except OSError:
                pass
            if not self.follow.get():
                self._edit_apply((title, artist, None), {"removed": True})
            win.destroy()
        tk.Button(row, text="Line starts NOW", command=now_here).pack(side="left")
        tk.Button(row, text="Save for everyone", command=do_save).pack(side="left", padx=8)
        tk.Button(row, text="Undo my fix (use found lyrics)", command=do_remove).pack(side="left")
        tk.Button(row, text="Cancel", command=win.destroy).pack(side="right")
        note.pack(side="left", padx=8)

    # ---------- spot song changes on this PC (Spotify / YouTube) ----------
    def _media_worker(self):
        np = media.now_playing()
        self.root.after(0, self._media_apply, np)

    def _media_apply(self, np):
        self.busy_media = False
        if self.follow.get() or self.listening or not np:
            return
        if np["title"] != self.last_media:          # new song / video started
            self.last_media = np["title"]
            self.next_check = 0.0                    # listen right away

    # ---------- drawing ----------
    def _auto_update(self):
        """Restart onto a new version at a quiet moment: nothing playing, paused, or a new song started."""
        cur = self.song["title"] if self.song else ""
        if self.update_wait is None:
            self.update_wait = cur                 # the song playing when the update arrived
        if not cur or self.player_paused or cur != self.update_wait:
            self.restarting = True
            if self.cfg.get("auto_update_from") == share.DIAG.get("ver"):
                return                             # tried before and the update didn't arrive: no restart loop
            self.cfg["auto_update_from"] = share.DIAG.get("ver")
            self.restart()

    def _words_check(self):
        key = (self.song["title"], self.song["artist"]) if self.song else None
        if key != self.words_key:
            self.words_key, self.words = key, {}
            if key:
                threading.Thread(target=self._words_worker, args=(key,), daemon=True).start()

    def _words_worker(self, key):
        try:
            words = lyrics.kugou_words(*key)
        except Exception:
            words = {}
        self.root.after(0, self._words_apply, key, words)

    def _words_apply(self, key, words):
        if key == self.words_key:
            self.words = words

    def tick(self):
        now = time.time()
        self._words_check()
        if self.update_ready and not self.restarting:
            self._auto_update()
        share.WATCH = bool(self.follow.get())
        if self.follow.get():
            if self.follow_paused is not None and self.song:
                self.t0 = now - self.follow_paused + self._shift()
            if not self.busy_follow and now >= self.next_follow:
                self.busy_follow, self.next_follow = True, now + 2
                threading.Thread(target=self._follow_worker, args=(self.follow.get(), self.follow_lrev),
                                 daemon=True).start()
        elif self._player_mode():
            if not self.busy_player and now >= self.next_player:
                self.busy_player, self.next_player = True, now + 1.5
                which = self.source.get()[len("Player:"):]
                threading.Thread(target=self._player_worker, args=(which, self.player_app), daemon=True).start()
        else:
            if (self.auto.get() and not self.busy_media and now >= self.next_media_check
                    and self.source.get().startswith("PC")):
                self.busy_media, self.next_media_check = True, now + 3
                threading.Thread(target=self._media_worker, daemon=True).start()
            if self.auto.get() and not self.listening and now >= self.next_check:
                self.listen()
        self.draw()
        self.root.after(100, self.tick)

    def _text_sung(self, x, y, s, sung):
        """Karaoke: the sung part of the line in gold, the rest white. False if the line is too long
        for one row (then it's drawn the normal way)."""
        font = self.font_main
        width = font.measure(s)
        if width > self.canvas.winfo_width() - int(40 * self.k):
            return False
        n, cut = 0, len(s)                       # `sung` counts letters/characters, not spaces/punctuation
        for k, ch in enumerate(s):
            if n >= sung:
                cut = k
                break
            if lyrics._plain(ch):
                n += 1
        left = x - width / 2
        o = max(2, round(2 * self.k))
        for part, px, fill in ((s[:cut], left, "#ffd34d"), (s[cut:], left + font.measure(s[:cut]), "#ffffff")):
            if not part:
                continue
            for dx, dy in ((-o, 0), (o, 0), (0, -o), (0, o), (-o, -o), (o, o), (-o, o), (o, -o)):
                self.canvas.create_text(px + dx, y + dy, text=part, font=font, fill="#111111", anchor="w")
            self.canvas.create_text(px, y, text=part, font=font, fill=fill, anchor="w")
        return True

    def _text(self, x, y, s, font, fill):
        wrap = max(200, self.canvas.winfo_width() - int(40 * self.k))
        o = max(2, round(2 * self.k))
        for dx, dy in ((-o, 0), (o, 0), (0, -o), (0, o), (-o, -o), (o, o), (-o, o), (o, -o)):
            self.canvas.create_text(x + dx, y + dy, text=s, font=font, fill="#111111", width=wrap,
                                    justify="center")
        self.canvas.create_text(x, y, text=s, font=font, fill=fill, width=wrap, justify="center")

    # layout, as parts of the bar's height: song name on top, sync buttons, lyric line, next line
    BTN_Y, LYRIC_Y, NEXT_Y = 0.17, 0.57, 0.87
    HEIGHT_PER_PT = 7.1                       # bar height = text size x this (x display scaling)

    def _draw_buttons(self):
        """Three small pills in a row right above the lyric line. They need a solid background: the
        see-through parts of the bar let clicks fall through to the window underneath."""
        c, k = self.canvas, self.k
        gap, pad_x, pad_y = int(6 * k), int(7 * k), int(3 * k)
        items = []
        for tag, label in (("btn_early", "◀ earlier"), ("btn_sync", "next line NOW"), ("btn_late", "later ▶")):
            t = c.create_text(0, 0, text=label, font=self.font_btn, fill="#ffffff", anchor="nw", tags=tag)
            x1, y1, x2, y2 = c.bbox(t)
            items.append((tag, t, x2 - x1 + 2 * pad_x, y2 - y1 + 2 * pad_y))
        total = sum(w for _, _, w, _ in items) + gap * (len(items) - 1)
        x = (c.winfo_width() - total) // 2                       # centred over the lyric
        y = int(self.canvas.winfo_height() * self.BTN_Y)
        for tag, t, w, h in items:
            r = c.create_rectangle(x, y, x + w, y + h, fill="#2b2f3a", outline="#5d6675", tags=tag)
            c.coords(t, x + pad_x, y + pad_y)
            c.tag_raise(t, r)
            x += w + gap

    def draw(self):
        c = self.canvas
        c.delete("all")
        w = c.winfo_width()
        h = c.winfo_height()
        cx = w // 2
        top = ""
        if self.song:
            top = f"♪ {self.song['title']} — {self.song['artist']}"
            if self.lines and self.song.get("cc"):
                top += {"cc": "   ▶ video subtitles", "translation": "   ▶ video subtitles (translation)",
                        "retimed": "   ▶ timed to the video",
                        "video": "   ▶ read from the video",
                        "edited": "   ✎ fixed by hand"}.get(self.song["cc"], "")
            elif self.lines and self.rough:
                top += "   ⚠ rough timing - right-click › sync"
            if self.scan_proc and self.scan_proc.poll() is None and self.song.get("cc") != "video":
                top += "   ⏳ reading lyrics from the video…"
        if self.follow.get():
            top = (top + "   " if top else "") + f"⇢ from {self.follow.get()}"
        if self.song and ((self._player_mode() and self.player_paused and not self.follow.get())
                          or (self.follow.get() and self.follow_paused is not None)):
            top += "   ⏸ paused"
        elif self.listening:
            top = (top + "   " if top else "") + "● listening"
        if self.update_ready:                    # its own line item, so it never hides paused / listening
            top = (top + "   " if top else "") + "⬆ update ready (right-click › Restart)"
        if self.flash[1] > time.time():
            top = self.flash[0]
        if top:
            self._text(cx, int(14 * self.k), top, self.font_small, "#9fe7ff")
        if self.lines:
            self._draw_buttons()

        cur, nxt, i, pos = self.status, "", -1, 0.0
        if self.lines and self.no_pos and self._player_mode():
            cur = "This player doesn't share its position - try Sound source › PC sound"
        elif self.lines:
            pos = time.time() - self.t0
            i = -1
            for k, (t, _) in enumerate(self.lines):
                if t <= pos:
                    i = k
                else:
                    break
            cur = self.lines[i][1] if i >= 0 else "♪ ♪ ♪"
            nxt = self.lines[i + 1][1] if i + 1 < len(self.lines) else ""
            if i >= 0:
                # instrumental break (or the song's end): don't leave the last sung line up the whole time
                gap = (self.lines[i + 1][0] if nxt else float("inf")) - self.lines[i][0]
                hold = min(8.0, max(5.0, 0.12 * len(cur) + 3))
                if gap > hold + 4 and pos - self.lines[i][0] > hold:
                    cur = "♪ ♪ ♪"
        elif self.song and self.song.get("static_lyrics") and not self.status:
            cur = "(lyrics not synced)"
        sung = None
        if self.lines and i >= 0 and cur == self.lines[i][1] and self.words:
            ws = self.words.get(lyrics._plain(cur))
            if ws:
                sung = lyrics.sung_chars(ws, pos - self.lines[i][0])
        if sung is None or not self._text_sung(cx, h * self.LYRIC_Y, cur, sung):
            self._text(cx, h * self.LYRIC_Y, cur, self.font_main, "#ffffff")
        if nxt:
            self._text(cx, h * self.NEXT_Y, nxt, self.font_small, "#cccccc")


if __name__ == "__main__":
    Overlay().root.mainloop()
