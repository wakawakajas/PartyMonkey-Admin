"""Thin ctypes wrappers around a handful of Win32 APIs.

Kept free of any Macro Studio-specific logic (self-exclusion, recording,
replay) so later phases can reuse these primitives -- e.g. Phase 4's
PostMessage replay path and Phase 3's UIA ancestor walking both need
window/PID resolution just like self-exclusion does here.
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wintypes

user32 = ctypes.windll.user32


def _make_process_dpi_aware() -> None:
    """Without this, a process defaults to DPI-unaware, and Windows
    silently rescales every coordinate we pass to/from USER32 (WindowFromPoint,
    GetWindowRect, ScreenToClient, SetCursorPos...) to a virtualized value on
    any scaled display. pynput's low-level input hook reports true physical
    pixels regardless, so the mismatch makes clicks resolve to the wrong
    window (or nothing) on any machine running above 100% scaling -- which is
    most laptops. Must run before any coordinate-based USER32 call, so this
    fires at import time, as early as this module (the first Windows-facing
    one in the import chain) is loaded.
    """
    try:
        # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2, Windows 10 1703+.
        if user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
            return
    except (AttributeError, OSError):
        pass
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PROCESS_PER_MONITOR_DPI_AWARE
        return
    except (AttributeError, OSError):
        pass
    try:
        user32.SetProcessDPIAware()  # legacy, system-DPI-aware fallback
    except (AttributeError, OSError):
        pass


_make_process_dpi_aware()

GA_ROOT = 2
SM_CXDOUBLECLK = 36
SM_CYDOUBLECLK = 37


def window_from_point(x: int, y: int) -> int:
    pt = wintypes.POINT(x, y)
    return user32.WindowFromPoint(pt)


def root_window(hwnd: int) -> int:
    """Walk up from a (possibly child) window to its top-level ancestor."""
    if not hwnd:
        return 0
    return user32.GetAncestor(hwnd, GA_ROOT) or hwnd


def window_pid(hwnd: int) -> int:
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value


def window_title(hwnd: int) -> str:
    length = user32.GetWindowTextLengthW(hwnd)
    if length <= 0:
        return ""
    buf = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buf, length + 1)
    return buf.value


def window_class_name(hwnd: int) -> str:
    buf = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, buf, 256)
    return buf.value


def window_rect(hwnd: int) -> tuple[int, int, int, int]:
    rect = wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(rect))
    return rect.left, rect.top, rect.right, rect.bottom


def double_click_time_ms() -> int:
    """The user's configured Windows double-click speed threshold."""
    return user32.GetDoubleClickTime()


def double_click_box() -> tuple[int, int]:
    """Width/height (px) of the box the 2nd click must land in to count
    as a double-click, per the user's Windows mouse settings."""
    return (
        user32.GetSystemMetrics(SM_CXDOUBLECLK),
        user32.GetSystemMetrics(SM_CYDOUBLECLK),
    )


# --- window enumeration (replay: re-finding a recorded window) -----------
WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)


def enum_top_level_windows() -> list[int]:
    hwnds: list[int] = []

    def _cb(hwnd, _lparam):
        if user32.IsWindowVisible(hwnd):
            hwnds.append(hwnd)
        return True

    user32.EnumWindows(WNDENUMPROC(_cb), 0)
    return hwnds


def get_foreground_window() -> int:
    return user32.GetForegroundWindow()


def get_cursor_pos() -> tuple[int, int]:
    pt = wintypes.POINT()
    user32.GetCursorPos(ctypes.byref(pt))
    return pt.x, pt.y


# --- capture exclusion (Phase 10: keep Macro Studio's own windows out of ---
# a screen/region video recording). Windows 10 2004+ only -- on older
# Windows this just silently fails (returns False), which is fine: the
# recording still happens, it just isn't guaranteed to exclude our UI.
WDA_NONE = 0x00000000
WDA_EXCLUDEFROMCAPTURE = 0x00000011


def set_display_affinity(hwnd: int, exclude: bool) -> bool:
    try:
        return bool(user32.SetWindowDisplayAffinity(hwnd, WDA_EXCLUDEFROMCAPTURE if exclude else WDA_NONE))
    except Exception:
        return False


def get_console_window() -> int:
    """Our own console window (the "Macro Studio Agent" cmd window),
    if we have one."""
    try:
        return kernel32.GetConsoleWindow()
    except Exception:
        return 0


# --- elevation (Phase 4: detect a target app running as Administrator) ---
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
TOKEN_QUERY = 0x0008
TOKEN_ELEVATION_CLASS = 20  # TokenElevation, from the TOKEN_INFORMATION_CLASS enum

advapi32 = ctypes.windll.advapi32
kernel32 = ctypes.windll.kernel32


def is_process_elevated(pid: int) -> bool | None:
    """True/False if determinable, None if we couldn't tell (treated as
    "don't block on it" by callers -- an unknown shouldn't halt a run)."""
    h_process = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h_process:
        return None
    try:
        h_token = wintypes.HANDLE()
        if not advapi32.OpenProcessToken(h_process, TOKEN_QUERY, ctypes.byref(h_token)):
            return None
        try:
            value = wintypes.DWORD()
            size = wintypes.DWORD()
            ok = advapi32.GetTokenInformation(
                h_token, TOKEN_ELEVATION_CLASS, ctypes.byref(value), ctypes.sizeof(value), ctypes.byref(size)
            )
            return bool(value.value) if ok else None
        finally:
            kernel32.CloseHandle(h_token)
    finally:
        kernel32.CloseHandle(h_process)


def is_self_elevated() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


# --- client-area coordinate helpers ---------------------------------------
def screen_to_client(hwnd: int, x: int, y: int) -> tuple[int, int]:
    pt = wintypes.POINT(x, y)
    user32.ScreenToClient(hwnd, ctypes.byref(pt))
    return pt.x, pt.y


def child_window_from_point(hwnd: int, x_client: int, y_client: int) -> int:
    """The specific child control at a client-area point, if any -- many
    classic Win32 controls only respond to messages sent to their own
    HWND, not their parent's."""
    CWP_SKIPINVISIBLE = 0x0001
    CWP_SKIPDISABLED = 0x0002
    pt = wintypes.POINT(x_client, y_client)
    child = user32.ChildWindowFromPointEx(hwnd, pt, CWP_SKIPINVISIBLE | CWP_SKIPDISABLED)
    return child or hwnd


# --- posted mouse/keyboard messages (replay tier 2) -----------------------
# Doesn't move the physical cursor or steal keyboard focus -- but plenty of
# apps ignore posted input entirely, which is exactly why tier 1 (UIA) is
# tried first and tier 3 (physical) exists as a fallback.
WM_LBUTTONDOWN, WM_LBUTTONUP = 0x0201, 0x0202
WM_RBUTTONDOWN, WM_RBUTTONUP = 0x0204, 0x0205
WM_MBUTTONDOWN, WM_MBUTTONUP = 0x0207, 0x0208
WM_KEYDOWN, WM_KEYUP = 0x0100, 0x0101
WM_CHAR = 0x0102
MK_LBUTTON, MK_RBUTTON, MK_MBUTTON = 0x0001, 0x0002, 0x0010

_BUTTON_MSGS = {
    "left": (WM_LBUTTONDOWN, WM_LBUTTONUP, MK_LBUTTON),
    "right": (WM_RBUTTONDOWN, WM_RBUTTONUP, MK_RBUTTON),
    "middle": (WM_MBUTTONDOWN, WM_MBUTTONUP, MK_MBUTTON),
}


def post_click(hwnd: int, x_client: int, y_client: int, button: str = "left", double: bool = False) -> None:
    down, up, mk = _BUTTON_MSGS.get(button, _BUTTON_MSGS["left"])
    lparam = (y_client << 16) | (x_client & 0xFFFF)
    for _ in range(2 if double else 1):
        user32.PostMessageW(hwnd, down, mk, lparam)
        user32.PostMessageW(hwnd, up, 0, lparam)


def post_char(hwnd: int, char: str) -> None:
    user32.PostMessageW(hwnd, WM_CHAR, ord(char), 0)


def post_key_down(hwnd: int, vk_code: int) -> None:
    user32.PostMessageW(hwnd, WM_KEYDOWN, vk_code, 0)


def post_key_up(hwnd: int, vk_code: int) -> None:
    user32.PostMessageW(hwnd, WM_KEYUP, vk_code, 0xC0000001)


_VK_MAP = {
    "enter": 0x0D, "tab": 0x09, "backspace": 0x08, "esc": 0x1B, "escape": 0x1B,
    "space": 0x20, "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28,
    "delete": 0x2E, "home": 0x24, "end": 0x23, "pageup": 0x21, "pagedown": 0x22,
    "insert": 0x2D, "ctrl": 0x11, "alt": 0x12, "shift": 0x10, "win": 0x5B,
    "f1": 0x70, "f2": 0x71, "f3": 0x72, "f4": 0x73, "f5": 0x74, "f6": 0x75,
    "f7": 0x76, "f8": 0x77, "f9": 0x78, "f10": 0x79, "f11": 0x7A, "f12": 0x7B,
}


def vk_for(key: str) -> int | None:
    key = key.lower()
    if key in _VK_MAP:
        return _VK_MAP[key]
    if len(key) == 1:
        res = user32.VkKeyScanW(ord(key))
        vk = res & 0xFF
        return vk if vk != 0xFF else None
    return None


# --- physical input (replay tier 3 -- last resort, foreground only) -------
PUL = ctypes.POINTER(ctypes.c_ulong)


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", ctypes.c_ushort), ("wScan", ctypes.c_ushort),
                ("dwFlags", ctypes.c_ulong), ("time", ctypes.c_ulong),
                ("dwExtraInfo", PUL)]


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", ctypes.c_long), ("dy", ctypes.c_long),
                ("mouseData", ctypes.c_ulong), ("dwFlags", ctypes.c_ulong),
                ("time", ctypes.c_ulong), ("dwExtraInfo", PUL)]


class _INPUT_UNION(ctypes.Union):
    _fields_ = [("mi", _MOUSEINPUT), ("ki", _KEYBDINPUT)]


class _INPUT(ctypes.Structure):
    _fields_ = [("type", ctypes.c_ulong), ("union", _INPUT_UNION)]


INPUT_MOUSE = 0
INPUT_KEYBOARD = 1
MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP = 0x0002, 0x0004
MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP = 0x0008, 0x0010
MOUSEEVENTF_MIDDLEDOWN, MOUSEEVENTF_MIDDLEUP = 0x0020, 0x0040
KEYEVENTF_KEYUP = 0x0002

_MOUSE_BUTTON_FLAGS = {
    "left": (MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP),
    "right": (MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP),
    "middle": (MOUSEEVENTF_MIDDLEDOWN, MOUSEEVENTF_MIDDLEUP),
}


def _send_input(*inputs: _INPUT) -> None:
    arr = (_INPUT * len(inputs))(*inputs)
    user32.SendInput(len(inputs), arr, ctypes.sizeof(_INPUT))


def physical_move_and_click(x: int, y: int, button: str = "left", double: bool = False) -> None:
    user32.SetCursorPos(x, y)
    down_flag, up_flag = _MOUSE_BUTTON_FLAGS.get(button, _MOUSE_BUTTON_FLAGS["left"])
    for _ in range(2 if double else 1):
        _send_input(_INPUT(type=INPUT_MOUSE, union=_INPUT_UNION(mi=_MOUSEINPUT(0, 0, 0, down_flag, 0, None))))
        _send_input(_INPUT(type=INPUT_MOUSE, union=_INPUT_UNION(mi=_MOUSEINPUT(0, 0, 0, up_flag, 0, None))))


def physical_key(vk_code: int, key_up: bool = False) -> None:
    flags = KEYEVENTF_KEYUP if key_up else 0
    _send_input(_INPUT(type=INPUT_KEYBOARD, union=_INPUT_UNION(ki=_KEYBDINPUT(vk_code, 0, flags, 0, None))))
