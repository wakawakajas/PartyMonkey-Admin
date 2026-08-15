"""Windows toast notifications via the classic Shell_NotifyIcon balloon
API, which Windows 10/11 render as a native Action Center toast.

Deliberately not using a WinRT-based toast library: this needs zero new
pip dependencies, so there's nothing extra that can fail to install --
matches the "6 things that always work" priority over adding another
moving part for a nice-to-have notification.

Best-effort throughout: a failure here never raises into the caller. A
toast not appearing (e.g. Windows Focus Assist suppressing it) isn't
something we can detect or control, and isn't treated as an error.
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wintypes
import threading
import time

user32 = ctypes.windll.user32
shell32 = ctypes.windll.shell32
kernel32 = ctypes.windll.kernel32

NIM_ADD = 0x00000000
NIM_DELETE = 0x00000002
NIF_ICON = 0x00000002
NIF_TIP = 0x00000004
NIF_INFO = 0x00000010
NIIF_INFO = 0x00000001
NIIF_ERROR = 0x00000003
WS_OVERLAPPED = 0x00000000
WM_DESTROY = 0x0002

WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_long, wintypes.HWND, ctypes.c_uint, wintypes.WPARAM, wintypes.LPARAM)


class _WNDCLASSW(ctypes.Structure):
    _fields_ = [
        ("style", ctypes.c_uint), ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
        ("hInstance", wintypes.HINSTANCE), ("hIcon", wintypes.HICON),
        ("hCursor", wintypes.HANDLE), ("hbrBackground", wintypes.HBRUSH),
        ("lpszMenuName", wintypes.LPCWSTR), ("lpszClassName", wintypes.LPCWSTR),
    ]


class _NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD), ("hWnd", wintypes.HWND), ("uID", wintypes.UINT),
        ("uFlags", wintypes.UINT), ("uCallbackMessage", wintypes.UINT), ("hIcon", wintypes.HICON),
        ("szTip", wintypes.WCHAR * 128), ("dwState", wintypes.DWORD), ("dwStateMask", wintypes.DWORD),
        ("szInfo", wintypes.WCHAR * 256), ("uTimeoutOrVersion", wintypes.UINT),
        ("szInfoTitle", wintypes.WCHAR * 64), ("dwInfoFlags", wintypes.DWORD),
        ("guidItem", ctypes.c_byte * 16), ("hBalloonIcon", wintypes.HICON),
    ]


def _default_wndproc(hwnd, msg, wparam, lparam):
    if msg == WM_DESTROY:
        user32.PostQuitMessage(0)
        return 0
    return user32.DefWindowProcW(hwnd, msg, wparam, lparam)


_wndproc_ref = WNDPROC(_default_wndproc)  # must outlive the window -- keep a module-level reference
_class_name = "MacroStudioToastWnd"
_class_registered = False
_class_lock = threading.Lock()


def _ensure_window():
    global _class_registered
    hinstance = kernel32.GetModuleHandleW(None)
    with _class_lock:
        if not _class_registered:
            wc = _WNDCLASSW()
            wc.lpfnWndProc = _wndproc_ref
            wc.hInstance = hinstance
            wc.lpszClassName = _class_name
            user32.RegisterClassW(ctypes.byref(wc))  # ignore "already exists" -- fine either way
            _class_registered = True
    return user32.CreateWindowExW(0, _class_name, "Macro Studio", WS_OVERLAPPED, 0, 0, 0, 0, None, None, hinstance, None)


def notify(title: str, message: str, is_error: bool = False, visible_seconds: float = 8.0) -> bool:
    """Fires a toast; returns whether the underlying API calls succeeded
    (not whether the user actually saw it)."""
    try:
        hwnd = _ensure_window()
        if not hwnd:
            return False
        nid = _NOTIFYICONDATAW()
        nid.cbSize = ctypes.sizeof(_NOTIFYICONDATAW)
        nid.hWnd = hwnd
        nid.uID = 1
        nid.uFlags = NIF_INFO | NIF_ICON | NIF_TIP
        nid.szTip = "Macro Studio"
        nid.szInfo = message[:255]
        nid.szInfoTitle = title[:63]
        nid.dwInfoFlags = NIIF_ERROR if is_error else NIIF_INFO
        shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(nid))

        def _cleanup():
            time.sleep(visible_seconds)
            try:
                del_nid = _NOTIFYICONDATAW()
                del_nid.cbSize = ctypes.sizeof(_NOTIFYICONDATAW)
                del_nid.hWnd = hwnd
                del_nid.uID = 1
                shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(del_nid))
            except Exception:
                pass
            try:
                user32.DestroyWindow(hwnd)
            except Exception:
                pass

        threading.Thread(target=_cleanup, daemon=True).start()
        return True
    except Exception:
        return False
