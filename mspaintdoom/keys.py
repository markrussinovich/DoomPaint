"""Keyboard input: a low-level hook that captures game keys before Paint
sees them, with global GetAsyncKeyState polling as the fallback.

Why a hook and not just polling: Paint is the *focused* window while you play,
so every gameplay keystroke also lands in Paint. That is mostly harmless —
until a frame paste is in flight. The UIA menu paster briefly opens the Edit
menu each frame, and a Left/Right arrow during that window dismisses or
navigates the menu (observed: 15 s/frame stalls, frames only advancing on keys
that menus ignore, like Ctrl). Arrows also nudge the floating pasted selection,
and Space activates whatever control has focus. The WH_KEYBOARD_LL hook
swallows game keys while Paint is foreground and records them in its own state
table, so the game gets the input and Paint gets silence.

Details the hook/polling combination handles:

1. Low frame rate (~5 fps) means input is sampled every ~200 ms. A key tapped
   *and released* between samples is caught by the hook's tap latch (or, in
   fallback mode, GetAsyncKeyState's "pressed since last call" 0x0001 bit).
2. Our own injected keystrokes (Ctrl+V pastes) carry sendinput.INJECT_MARKER
   in dwExtraInfo; the hook passes them through untouched so pastes still work,
   and never mistakes them for the player firing.
3. Ctrl (fire) IS swallowed: utilities with their own low-level hooks
   (PowerToys and friends) can suppress bare Ctrl presses before
   GetAsyncKeyState sees them, silently breaking the fire key. Our hook runs
   first, so capturing Ctrl beats them to it. The Ctrl+Z rewind trick
   survives via re-injection: Z pressed while Ctrl is captured is turned into
   a clean marked Ctrl+Z chord for Paint. Shift stays pass-through (inert
   alone in Paint, and the Ctrl-eaters leave it alone).
4. If the hook can't install (or Windows silently removes it for being slow),
   polling still works — keys then leak into Paint as before, degraded but
   playable with keys menus ignore.
"""
import ctypes
import threading
from ctypes import wintypes

from .sendinput import INJECT_MARKER, send_keys

_user32 = ctypes.WinDLL("user32", use_last_error=True)
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

VK_LEFT, VK_UP, VK_RIGHT, VK_DOWN = 0x25, 0x26, 0x27, 0x28
VK_SHIFT, VK_CONTROL = 0x10, 0x11
VK_LSHIFT, VK_RSHIFT = 0xA0, 0xA1
VK_SPACE = 0x20
VK_OEM_COMMA, VK_OEM_PERIOD = 0xBC, 0xBE
VK_F12 = 0x7B


def _held(vk: int) -> bool:
    """Key is physically down right now (async fallback path)."""
    return bool(_user32.GetAsyncKeyState(vk) & 0x8000)


def _async_active(vk: int) -> bool:
    """Key is down now, or was tapped since the last poll (0x0001 bit)."""
    return bool(_user32.GetAsyncKeyState(vk) & 0x8001)


def consume_tap(vk: int) -> None:
    """Clear GetAsyncKeyState's tap latch for vk (it resets on read).

    Called right after injecting a synthetic tap of vk so the next poll
    doesn't mistake our own keystroke for the player's.
    """
    _user32.GetAsyncKeyState(vk)


# Each Doom button maps to one or more physical keys (any of them triggers it).
# Order must match the buttons registered in engine_vzd.BUTTONS.
_ACTION_BINDINGS = (
    (VK_UP, ord("W")),          # MOVE_FORWARD
    (VK_DOWN, ord("S")),        # MOVE_BACKWARD
    (VK_LEFT, ord("A")),        # TURN_LEFT
    (VK_RIGHT, ord("D")),       # TURN_RIGHT
    (VK_CONTROL, ord("F")),     # ATTACK  (Ctrl or F to fire)
    (VK_SPACE,),                # USE / open doors
    (VK_SHIFT,),                # SPEED (run)
    (ord("Q"), VK_OEM_COMMA),   # MOVE_LEFT (strafe)
    (ord("E"), VK_OEM_PERIOD),  # MOVE_RIGHT (strafe)
)

CONTROLS_HELP = (
    "WASD / arrows move+turn | Q E (or , .) strafe | Ctrl or F fire | "
    "Space use/open | Shift run | F12 quit"
)

# Keys the hook swallows while Paint is foreground. Ctrl is swallowed too:
# utilities with their own low-level hooks (PowerToys et al.) can suppress
# bare Ctrl presses before GetAsyncKeyState ever sees them, silently killing
# the fire key — our hook runs ahead of theirs (last installed, first called),
# so capturing Ctrl here makes fire reliable. Shift stays pass-through (inert
# alone in Paint, and unmolested by the Ctrl-eaters).
_SWALLOW_VKS = frozenset(
    {VK_LEFT, VK_UP, VK_RIGHT, VK_DOWN, VK_SPACE, VK_CONTROL,
     VK_OEM_COMMA, VK_OEM_PERIOD, VK_F12,
     ord("W"), ord("A"), ord("S"), ord("D"), ord("Q"), ord("E"), ord("F")})

# ---------------------------------------------------------------------------
# WH_KEYBOARD_LL machinery

_WH_KEYBOARD_LL = 13
_WM_KEYDOWN, _WM_KEYUP = 0x0100, 0x0101
_WM_SYSKEYDOWN, _WM_SYSKEYUP = 0x0104, 0x0105
_LLKHF_INJECTED = 0x10


class _KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = (("vkCode", wintypes.DWORD), ("scanCode", wintypes.DWORD),
                ("flags", wintypes.DWORD), ("time", wintypes.DWORD),
                ("dwExtraInfo", ctypes.c_size_t))


_HOOKPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, ctypes.c_int,
                               wintypes.WPARAM, wintypes.LPARAM)
_user32.SetWindowsHookExW.argtypes = (ctypes.c_int, _HOOKPROC,
                                      wintypes.HMODULE, wintypes.DWORD)
_user32.SetWindowsHookExW.restype = ctypes.c_void_p
_user32.CallNextHookEx.argtypes = (ctypes.c_void_p, ctypes.c_int,
                                   wintypes.WPARAM, wintypes.LPARAM)
_user32.CallNextHookEx.restype = ctypes.c_ssize_t
_kernel32.GetModuleHandleW.argtypes = (wintypes.LPCWSTR,)
_kernel32.GetModuleHandleW.restype = wintypes.HMODULE  # else truncated on x64

_down: set[int] = set()      # swallowed keys currently held
_tapped: set[int] = set()    # swallowed since last poll (tap latch)
_quit_tapped = False
_grab_when = None            # callable() -> bool: swallow game keys now?
_hook_installed = False      # True once the LL hook is active (else async-only)


def _hook_cb(ncode, wparam, lparam):
    if ncode == 0:  # HC_ACTION
        kb = ctypes.cast(lparam, ctypes.POINTER(_KBDLLHOOKSTRUCT)).contents
        vk = kb.vkCode
        if vk in (0xA2, 0xA3):  # LL hooks report L/RCONTROL, not the generic
            vk = VK_CONTROL
        own = bool(kb.flags & _LLKHF_INJECTED) and kb.dwExtraInfo == INJECT_MARKER
        is_down = wparam in (_WM_KEYDOWN, _WM_SYSKEYDOWN)
        if not own and vk == ord("Z") and is_down and VK_CONTROL in _down:
            # Keep the Ctrl+Z rewind trick alive: the player's Ctrl was
            # swallowed, so Paint would only see a bare Z. Swallow that too
            # and hand Paint a clean, marked Ctrl+Z chord instead.
            try:
                send_keys((VK_CONTROL, False), (ord("Z"), False),
                          (ord("Z"), True), (VK_CONTROL, True))
            except OSError:
                pass
            return 1
        if not own and vk in _SWALLOW_VKS:
            if is_down:
                try:
                    grab = _grab_when is not None and _grab_when()
                except Exception:
                    grab = False  # never break the user's keyboard
                if grab:
                    _down.add(vk)
                    _tapped.add(vk)
                    if vk == VK_F12:
                        global _quit_tapped
                        _quit_tapped = True
                    return 1
            elif vk in _down:
                # We swallowed this key's down; swallow the matching up so no
                # app receives a stray keyup, and stop "holding" it in-game.
                _down.discard(vk)
                return 1
    return _user32.CallNextHookEx(None, ncode, wparam, lparam)


_hook_cb_ptr = _HOOKPROC(_hook_cb)  # keep a reference; GC'd callback = crash


def install_hook(grab_when) -> bool:
    """Start the capture hook. grab_when() says when to swallow game keys
    (i.e. when Paint is the foreground window). Returns False if the hook
    could not be installed; polling then runs in fallback mode.
    """
    global _grab_when
    _grab_when = grab_when
    installed = threading.Event()
    result: list[bool] = []

    def pump():
        hmod = _kernel32.GetModuleHandleW(None)
        handle = _user32.SetWindowsHookExW(_WH_KEYBOARD_LL, _hook_cb_ptr,
                                           hmod, 0)
        if not handle:
            print(f"  (keyboard hook failed: WinError "
                  f"{ctypes.get_last_error()})")
        result.append(bool(handle))
        installed.set()
        if not handle:
            return
        msg = wintypes.MSG()
        while _user32.GetMessageW(ctypes.byref(msg), None, 0, 0):
            pass

    threading.Thread(target=pump, daemon=True, name="mspaintdoom-kbd").start()
    installed.wait(3.0)
    global _hook_installed
    _hook_installed = bool(result and result[0])
    return _hook_installed


# ---------------------------------------------------------------------------
# Polling API used by the game loop


def poll_action() -> list[int]:
    """Current action vector in engine button order (down OR tapped).

    Input is sampled on the render thread every tic, concurrently with the
    Ctrl+V paste the game loop injects. The hook's swallow table is authoritative
    for the keys it captures and — unlike global GetAsyncKeyState — excludes our
    own injected keystrokes, so a paste's Ctrl isn't misread as the player
    firing. Async key state is consulted only for keys the hook doesn't swallow
    (Shift), or when the hook isn't installed (degraded fallback).
    """
    taps = set(_tapped)
    _tapped.difference_update(taps)

    def active(vk: int) -> bool:
        if vk in _down or vk in taps:
            return True
        if _hook_installed and vk in _SWALLOW_VKS:
            return False
        return _async_active(vk)

    return [1 if any(active(vk) for vk in binding) else 0
            for binding in _ACTION_BINDINGS]


def quit_requested() -> bool:
    return _quit_tapped or _held(VK_F12)


def ctrl_physically_down() -> bool:
    return _held(VK_CONTROL)


def held_shift_vks() -> list[int]:
    """Which specific Shift keys are physically down right now.

    Paint reads modifiers globally (GetAsyncKeyState), so a held Shift (the run
    key) turns an injected Ctrl+V into Ctrl+Shift+V — not Paint's paste
    accelerator. The paster clears these for the paste chord, then re-presses
    them. LSHIFT/RSHIFT are checked specifically because injecting the generic
    VK_SHIFT would not clear the async state of the actual held key.
    """
    return [vk for vk in (VK_LSHIFT, VK_RSHIFT) if _held(vk)]
