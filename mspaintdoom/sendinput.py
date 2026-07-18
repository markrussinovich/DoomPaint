"""SendInput-based key injection with real scan codes.

Win11 Paint (XAML) ignores keybd_event-style injection that lacks scan codes,
so every event here carries MapVirtualKey scan data.
"""
import ctypes
from ctypes import wintypes

_user32 = ctypes.WinDLL("user32", use_last_error=True)

_INPUT_KEYBOARD = 1
_KEYEVENTF_KEYUP = 0x0002
_KEYEVENTF_SCANCODE = 0x0008
_KEYEVENTF_EXTENDEDKEY = 0x0001
_MAPVK_VK_TO_VSC = 0

_EXTENDED_VKS = {0x25, 0x26, 0x27, 0x28, 0x2D, 0x2E, 0x21, 0x22, 0x23, 0x24}

# Stamped into dwExtraInfo so the keyboard hook in keys.py can tell our own
# injected keystrokes (pastes, focus nudges) apart from the player's input.
INJECT_MARKER = 0x4D504431  # "MPD1"

_ULONG_PTR = ctypes.c_size_t


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = (("wVk", wintypes.WORD), ("wScan", wintypes.WORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
                ("dwExtraInfo", _ULONG_PTR))


class _INPUTUNION(ctypes.Union):
    _fields_ = (("ki", _KEYBDINPUT), ("_pad", ctypes.c_byte * 32))


class _INPUT(ctypes.Structure):
    _fields_ = (("type", wintypes.DWORD), ("u", _INPUTUNION))


def _make(vk: int, up: bool) -> _INPUT:
    scan = _user32.MapVirtualKeyW(vk, _MAPVK_VK_TO_VSC)
    flags = _KEYEVENTF_KEYUP if up else 0
    if vk in _EXTENDED_VKS:
        flags |= _KEYEVENTF_EXTENDEDKEY
    inp = _INPUT()
    inp.type = _INPUT_KEYBOARD
    inp.u.ki = _KEYBDINPUT(vk, scan, flags, 0, INJECT_MARKER)
    return inp


def send_keys(*events: tuple[int, bool]) -> int:
    """Send (vk, is_keyup) events as one atomic SendInput batch."""
    arr = (_INPUT * len(events))(*(_make(vk, up) for vk, up in events))
    sent = _user32.SendInput(len(events), arr, ctypes.sizeof(_INPUT))
    if sent != len(events):
        raise ctypes.WinError(ctypes.get_last_error())
    return sent


def tap(vk: int) -> None:
    send_keys((vk, False), (vk, True))
