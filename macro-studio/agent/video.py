"""Optional per-macro screen recording during replay, encoded to H.264
MP4 via ffmpeg (Windows' gdigrab input device does the actual screen
capture -- ffmpeg handles capture and encoding together, so there's no
Python-side frame grabbing to get wrong).

Never fails a run silently: if ffmpeg isn't found, start() raises with a
message that names the problem and links the download -- callers decide
whether that should stop the run or just mean "no video this time".
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Optional

from agent import self_exclusion, winapi

FFMPEG_DOWNLOAD_URL = "https://www.gyan.dev/ffmpeg/builds/"
DEFAULT_FPS = 10
MAX_FPS = 30


def find_ffmpeg() -> Optional[str]:
    return shutil.which("ffmpeg")


class VideoRecorder:
    """One capture per instance: start() once, stop() once. Not reusable."""

    def __init__(self, output_path: Path, mode: str = "fullscreen", fps: int = DEFAULT_FPS,
                 region: Optional[dict] = None, window_title: Optional[str] = None):
        self.output_path = output_path
        self.mode = mode  # fullscreen | window | region
        self.fps = max(1, min(fps or DEFAULT_FPS, MAX_FPS))
        self.region = region
        self.window_title = window_title
        self._proc: Optional[subprocess.Popen] = None
        self._excluded_hwnds: list[int] = []

    def start(self) -> None:
        ffmpeg = find_ffmpeg()
        if not ffmpeg:
            raise RuntimeError(
                "ffmpeg isn't installed (or isn't on PATH) -- video recording needs it. "
                f"Download a Windows build from {FFMPEG_DOWNLOAD_URL}, add its bin/ folder "
                "to PATH, then try again."
            )
        if self.mode == "window" and not self.window_title:
            raise RuntimeError('Video mode is "specific window" but no window title was set.')
        self.output_path.parent.mkdir(parents=True, exist_ok=True)
        self._exclude_own_windows()

        args = [ffmpeg, "-y", "-f", "gdigrab", "-framerate", str(self.fps)]
        if self.mode == "window" and self.window_title:
            args += ["-i", f"title={self.window_title}"]
        elif self.mode == "region" and self.region:
            args += [
                "-offset_x", str(self.region.get("left", 0)),
                "-offset_y", str(self.region.get("top", 0)),
                "-video_size", f"{self.region.get('width', 800)}x{self.region.get('height', 600)}",
                "-i", "desktop",
            ]
        else:
            args += ["-i", "desktop"]
        args += ["-c:v", "libx264", "-pix_fmt", "yuv420p", "-preset", "ultrafast", str(self.output_path)]

        try:
            self._proc = subprocess.Popen(
                args, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        except OSError as exc:
            self._restore_own_windows()
            raise RuntimeError(f"Couldn't start ffmpeg: {exc}") from exc

    def stop(self) -> Optional[str]:
        """Stops capture and restores display affinity on any windows we
        excluded. Returns the finished file's path, or None if nothing
        usable was recorded (start() never called, or it produced an
        empty file)."""
        self._restore_own_windows()
        if self._proc is None:
            return None
        try:
            if self._proc.stdin:
                self._proc.stdin.write(b"q")  # ffmpeg's documented graceful-stop signal
                self._proc.stdin.flush()
            self._proc.wait(timeout=10)
        except Exception:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=5)
            except Exception:
                self._proc.kill()
        self._proc = None
        if not self.output_path.exists() or self.output_path.stat().st_size == 0:
            return None
        return str(self.output_path)

    def _exclude_own_windows(self) -> None:
        if self.mode == "window":
            return  # capturing a specific other window -- ours is never in frame anyway
        for hwnd in winapi.enum_top_level_windows():
            if self_exclusion.is_own_window(winapi.window_pid(hwnd), winapi.window_title(hwnd)):
                if winapi.set_display_affinity(hwnd, exclude=True):
                    self._excluded_hwnds.append(hwnd)
        console_hwnd = winapi.get_console_window()
        if console_hwnd and console_hwnd not in self._excluded_hwnds:
            if winapi.set_display_affinity(console_hwnd, exclude=True):
                self._excluded_hwnds.append(console_hwnd)

    def _restore_own_windows(self) -> None:
        for hwnd in self._excluded_hwnds:
            winapi.set_display_affinity(hwnd, exclude=False)
        self._excluded_hwnds.clear()
