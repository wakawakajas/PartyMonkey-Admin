"""UI Automation element capture at a screen point -- the semantic-target
half of a recorded click (name, control_type, automation_id, class_name,
ancestor path). Coordinate fallback lives in self_exclusion.window_info_at;
this module is what decides whether an app offers anything better than
that at a given point.

Talks to UIAutomationCore directly via comtypes rather than pulling in
pywinauto, to keep the dependency footprint small -- pywinauto's UIA
backend is a thin wrapper over exactly these same COM calls.

Every public call here is defensive: a COM failure degrades to
"not accessible" rather than crashing the recorder thread, since a
crash here would silently stop capturing every event after it.
"""
from __future__ import annotations

import ctypes.wintypes as wintypes
import threading
from typing import Optional

import comtypes
import comtypes.client

comtypes.client.GetModule("UIAutomationCore.dll")
from comtypes.gen import UIAutomationClient as UIA  # noqa: E402

MAX_ANCESTOR_DEPTH = 25

# Common UIA control type IDs -> friendly names (UIA_ControlTypeIds).
# Not exhaustive -- anything missing just shows as "ControlType(<id>)"
# instead of crashing.
_CONTROL_TYPE_NAMES = {
    50000: "Button", 50001: "Calendar", 50002: "CheckBox", 50003: "ComboBox",
    50004: "Edit", 50005: "Hyperlink", 50006: "Image", 50007: "ListItem",
    50008: "List", 50009: "Menu", 50010: "MenuBar", 50011: "MenuItem",
    50012: "ProgressBar", 50013: "RadioButton", 50014: "ScrollBar",
    50015: "Slider", 50016: "Spinner", 50017: "StatusBar", 50018: "Tab",
    50019: "TabItem", 50020: "Text", 50021: "ToolBar", 50022: "ToolTip",
    50023: "Tree", 50024: "TreeItem", 50025: "Custom", 50026: "Group",
    50027: "Thumb", 50028: "DataGrid", 50029: "DataItem", 50030: "Document",
    50031: "SplitButton", 50032: "Window", 50033: "Pane", 50034: "Header",
    50035: "HeaderItem", 50036: "Table", 50037: "TitleBar", 50038: "Separator",
}

# One IUIAutomation instance per thread -- COM objects are thread-affine.
# Recording only ever calls this from pynput's dedicated listener thread,
# so this stays a single entry in practice, but it's correct if that changes.
_local = threading.local()


def _automation():
    inst = getattr(_local, "automation", None)
    if inst is None:
        comtypes.CoInitialize()
        inst = comtypes.client.CreateObject(UIA.CUIAutomation, interface=UIA.IUIAutomation)
        _local.automation = inst
    return inst


def _control_type_name(control_type_id) -> str:
    try:
        return _CONTROL_TYPE_NAMES.get(int(control_type_id), f"ControlType({control_type_id})")
    except (TypeError, ValueError):
        return "Unknown"


def _safe(getter, default=""):
    try:
        value = getter()
        return value if value is not None else default
    except Exception:
        return default


def _describe(element) -> dict:
    return {
        "name": _safe(lambda: element.CurrentName, ""),
        "control_type": _control_type_name(_safe(lambda: element.CurrentControlType, 0)),
        "automation_id": _safe(lambda: element.CurrentAutomationId, ""),
        "class_name": _safe(lambda: element.CurrentClassName, ""),
    }


def _native_handle(element) -> Optional[int]:
    handle = _safe(lambda: element.CurrentNativeWindowHandle, None)
    try:
        return int(handle) if handle else None
    except (TypeError, ValueError):
        return None


def capture_at_point(x: int, y: int, window_hwnd: Optional[int]) -> dict:
    """Best-effort UIA capture at a screen point.

    Returns:
      accessible    -- False if UIA gave us nothing more specific than the
                        top-level window itself (or the call failed outright)
      target        -- leaf element's {name, control_type, automation_id,
                        class_name}, or None
      ancestor_path -- list of the same dict shape, root-to-leaf, stopping
                        at (not including) the top-level window
      error         -- present with a message if UIA itself raised
    """
    try:
        automation = _automation()
        element = automation.ElementFromPoint(wintypes.POINT(x, y))
    except Exception as exc:
        return {"accessible": False, "target": None, "ancestor_path": [], "error": str(exc)}

    if element is None:
        return {"accessible": False, "target": None, "ancestor_path": []}

    leaf_handle = _native_handle(element)
    leaf = _describe(element)

    ancestor_path: list[dict] = []
    try:
        walker = automation.RawViewWalker
        current = element
        for _ in range(MAX_ANCESTOR_DEPTH):
            parent = walker.GetParentElement(current)
            if parent is None:
                break
            parent_handle = _native_handle(parent)
            if parent_handle and window_hwnd and parent_handle == window_hwnd:
                break
            ancestor_path.append(_describe(parent))
            current = parent
    except Exception:
        pass
    ancestor_path.reverse()

    # "Accessible" means UIA resolved something more specific than the
    # bare top-level window at this exact point -- that's the signal that
    # this app (or at least this spot in it) is custom-drawn / opaque to UIA.
    accessible = not (leaf_handle is not None and window_hwnd is not None and leaf_handle == window_hwnd)

    return {"accessible": accessible, "target": leaf, "ancestor_path": ancestor_path}


# --- replay: re-finding an element and invoking it (Phase 4) --------------
# Numeric UIA pattern IDs from the UI Automation spec -- hardcoded rather
# than relying on comtypes-generated constant names, which aren't stable
# across how the typelib happens to get imported.
_PATTERN_INVOKE = 10000
_PATTERN_VALUE = 10002
_PATTERN_SELECTION_ITEM = 10010
_PATTERN_TOGGLE = 10015

MAX_SEARCH_DEPTH = 40
MAX_SEARCH_NODES = 3000


def element_from_handle(hwnd: Optional[int]):
    if not hwnd:
        return None
    try:
        return _automation().ElementFromHandle(hwnd)
    except Exception:
        return None


def find_descendant(root, target: dict):
    """Breadth-first search under `root` for the best match to a recorded
    semantic target. An automation_id match wins outright and returns
    immediately; otherwise the first name+control_type match found is
    used. Capped in depth and node count so a pathological UI tree can't
    hang a replay run."""
    if root is None:
        return None
    wanted_aid = (target.get("automation_id") or "").strip()
    wanted_name = (target.get("name") or "").strip()
    wanted_type = (target.get("control_type") or "").strip()
    if not wanted_aid and not wanted_name:
        return None

    try:
        walker = _automation().RawViewWalker
    except Exception:
        return None

    best = None
    visited = 0
    queue = [(root, 0)]
    while queue and visited < MAX_SEARCH_NODES:
        node, depth = queue.pop(0)
        visited += 1
        desc = _describe(node)
        if wanted_aid and desc["automation_id"] == wanted_aid:
            return node
        if wanted_name and desc["name"] == wanted_name and desc["control_type"] == wanted_type and best is None:
            best = node
        if depth < MAX_SEARCH_DEPTH:
            try:
                child = walker.GetFirstChildElement(node)
                while child is not None:
                    queue.append((child, depth + 1))
                    child = walker.GetNextSiblingElement(child)
            except Exception:
                pass
    return best


def find_all_by_text(root, text: str, exact: bool = False, limit: int = 12):
    """Breadth-first search under `root` for every element whose Name
    matches `text`, nearest-to-root first. "Find and click by text"
    walks the list rather than taking the first hit: one visible label
    usually appears several times in the same tree -- a nav item's list
    wrapper, the link inside it, and the text node inside that all carry
    the same name, and only one of them actually responds to a click.
    Same depth/node caps as find_descendant, for the same reason."""
    if root is None or not text:
        return []
    try:
        walker = _automation().RawViewWalker
    except Exception:
        return []

    found = []
    visited = 0
    queue = [(root, 0)]
    while queue and visited < MAX_SEARCH_NODES:
        node, depth = queue.pop(0)
        visited += 1
        name = _safe(lambda: node.CurrentName, "")
        if (exact and name == text) or (not exact and text.lower() in name.lower()):
            found.append(node)
            if len(found) >= limit:
                return found
        if depth < MAX_SEARCH_DEPTH:
            try:
                child = walker.GetFirstChildElement(node)
                while child is not None:
                    queue.append((child, depth + 1))
                    child = walker.GetNextSiblingElement(child)
            except Exception:
                pass
    return found


def _get_pattern(element, pattern_id: int, iface):
    try:
        raw = element.GetCurrentPattern(pattern_id)
        if not raw:
            return None
        return raw.QueryInterface(iface)
    except Exception:
        return None


def try_invoke(element) -> bool:
    pattern = _get_pattern(element, _PATTERN_INVOKE, UIA.IUIAutomationInvokePattern)
    if pattern is None:
        return False
    try:
        pattern.Invoke()
        return True
    except Exception:
        return False


def try_toggle(element) -> bool:
    pattern = _get_pattern(element, _PATTERN_TOGGLE, UIA.IUIAutomationTogglePattern)
    if pattern is None:
        return False
    try:
        pattern.Toggle()
        return True
    except Exception:
        return False


def try_select(element) -> bool:
    pattern = _get_pattern(element, _PATTERN_SELECTION_ITEM, UIA.IUIAutomationSelectionItemPattern)
    if pattern is None:
        return False
    try:
        pattern.Select()
        return True
    except Exception:
        return False


def try_set_value(element, value: str) -> bool:
    pattern = _get_pattern(element, _PATTERN_VALUE, UIA.IUIAutomationValuePattern)
    if pattern is None:
        return False
    try:
        pattern.SetValue(value)
        return True
    except Exception:
        return False


def get_current_value(element) -> Optional[str]:
    """None means "no ValuePattern here" (not a text field we can type
    into this way) -- distinct from "" which is a legitimately empty
    field. Callers use this to decide whether SetValue is even an option."""
    pattern = _get_pattern(element, _PATTERN_VALUE, UIA.IUIAutomationValuePattern)
    if pattern is None:
        return None
    try:
        return pattern.CurrentValue
    except Exception:
        return None
