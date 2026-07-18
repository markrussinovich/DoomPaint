"""Delayed-rendering clipboard owner: know exactly when Paint reads a frame.

Plain SetClipboardData at game speed races Paint's asynchronous paste:
rewrite the clipboard while Paint is mid-read and Paint pops a modal "Can't
complete operation" dialog that kills every later paste. Guessed settle
timers can't fix this — paste latency varies wildly with frame size and
Paint's mood (scale 2 blew through a 90 ms guard).

Delayed rendering flips the protocol. We advertise CF_DIB with a NULL handle;
when Paint's paste actually reads the clipboard, Windows sends our hidden
window WM_RENDERFORMAT and we hand over the bytes right then. That message is
a positive signal the frame was consumed, so publish() can wait exactly as
long as the previous paste needs before rewriting — no timing guesswork.

On shutdown, call finalize() to place real bytes on the clipboard, so quitting
the game doesn't leave a dead delayed-render promise behind.
"""
import atexit
import ctypes
import threading
import time
from ctypes import wintypes

_user32 = ctypes.WinDLL("user32", use_last_error=True)
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

CF_DIB = 8
_GMEM_MOVEABLE = 0x0002
_WM_RENDERFORMAT = 0x0305
_WM_RENDERALLFORMATS = 0x0306
_WM_DESTROYCLIPBOARD = 0x0307
_HWND_MESSAGE = wintypes.HWND(-3)

_WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wintypes.HWND, wintypes.UINT,
                              wintypes.WPARAM, wintypes.LPARAM)


class _WNDCLASSW(ctypes.Structure):
    _fields_ = (("style", wintypes.UINT), ("lpfnWndProc", _WNDPROC),
                ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
                ("hInstance", wintypes.HINSTANCE), ("hIcon", wintypes.HICON),
                ("hCursor", ctypes.c_void_p), ("hbrBackground", ctypes.c_void_p),
                ("lpszMenuName", wintypes.LPCWSTR),
                ("lpszClassName", wintypes.LPCWSTR))


for fn, res, args in (
        (_user32.DefWindowProcW, ctypes.c_ssize_t,
         (wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)),
        (_user32.CreateWindowExW, wintypes.HWND,
         (wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
          ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
          wintypes.HWND, ctypes.c_void_p, wintypes.HINSTANCE,
          ctypes.c_void_p)),
        (_user32.OpenClipboard, wintypes.BOOL, (wintypes.HWND,)),
        (_user32.GetClipboardOwner, wintypes.HWND, ()),
        (_user32.GetOpenClipboardWindow, wintypes.HWND, ()),
        (_user32.SetClipboardData, wintypes.HANDLE,
         (wintypes.UINT, wintypes.HANDLE)),
        (_kernel32.GlobalAlloc, wintypes.HGLOBAL,
         (wintypes.UINT, ctypes.c_size_t)),
        (_kernel32.GlobalLock, ctypes.c_void_p, (wintypes.HGLOBAL,)),
        (_kernel32.GlobalUnlock, wintypes.BOOL, (wintypes.HGLOBAL,)),
        (_kernel32.GlobalFree, wintypes.HGLOBAL, (wintypes.HGLOBAL,)),
        (_kernel32.GetModuleHandleW, wintypes.HMODULE, (wintypes.LPCWSTR,))):
    fn.restype, fn.argtypes = res, args


class ClipboardServer:
    """Owns the clipboard via a message-only window on its own thread."""

    def __init__(self):
        self._dib = b""
        self._consumed = threading.Event()
        self._consumed.set()  # nothing pending yet
        self._ready = threading.Event()
        self._hwnd = None
        self._proc = _WNDPROC(self._wndproc)  # keep alive: GC'd proc = crash
        threading.Thread(target=self._pump, daemon=True,
                         name="mspaintdoom-clip").start()
        if not self._ready.wait(5.0) or not self._hwnd:
            raise RuntimeError("clipboard server window failed to start")
        atexit.register(self.finalize)

    # -- worker thread ------------------------------------------------------

    def _pump(self):
        cls = _WNDCLASSW()
        cls.lpfnWndProc = self._proc
        cls.hInstance = _kernel32.GetModuleHandleW(None)
        cls.lpszClassName = "MsPaintDoomClipSrv"
        _user32.RegisterClassW(ctypes.byref(cls))
        self._hwnd = _user32.CreateWindowExW(
            0, cls.lpszClassName, None, 0, 0, 0, 0, 0, _HWND_MESSAGE,
            None, cls.hInstance, None)
        self._ready.set()
        if not self._hwnd:
            return
        msg = wintypes.MSG()
        while _user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            _user32.TranslateMessage(ctypes.byref(msg))
            _user32.DispatchMessageW(ctypes.byref(msg))

    def _wndproc(self, hwnd, msg, wparam, lparam):
        if msg == _WM_RENDERFORMAT:
            # Paint (or anyone) is reading our promise right now: deliver.
            self._render(wparam or CF_DIB)
            return 0
        if msg == _WM_RENDERALLFORMATS:
            if _user32.OpenClipboard(hwnd):
                try:
                    if _user32.GetClipboardOwner() == hwnd:
                        self._render(CF_DIB)
                finally:
                    _user32.CloseClipboard()
            return 0
        if msg == _WM_DESTROYCLIPBOARD:
            return 0  # our own EmptyClipboard on the next publish
        return _user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def _render(self, fmt):
        data = self._dib
        if data:
            hmem = _kernel32.GlobalAlloc(_GMEM_MOVEABLE, len(data))
            if hmem:
                ptr = _kernel32.GlobalLock(hmem)
                ctypes.memmove(ptr, data, len(data))
                _kernel32.GlobalUnlock(hmem)
                if not _user32.SetClipboardData(fmt, hmem):
                    _kernel32.GlobalFree(hmem)
        self._consumed.set()

    # -- game-loop side -----------------------------------------------------

    def publish(self, dib: bytes, wait_prev: float = 0.6) -> None:
        """Advertise a new frame, first waiting for the previous one to be
        consumed (WM_RENDERFORMAT seen) or wait_prev seconds, whichever
        comes first. Timing out just means the last paste never happened
        (dropped keystroke); overwriting is then safe by definition.
        """
        self._consumed.wait(wait_prev)
        deadline = time.perf_counter() + 0.5
        while _user32.GetOpenClipboardWindow() \
                and time.perf_counter() < deadline:
            time.sleep(0.005)  # someone (Paint) is mid-read: let them finish
        for attempt in range(5):
            if _user32.OpenClipboard(self._hwnd):
                try:
                    self._dib = dib
                    self._consumed.clear()
                    _user32.EmptyClipboard()
                    _user32.SetClipboardData(CF_DIB, None)  # the promise
                finally:
                    _user32.CloseClipboard()
                return
            time.sleep(0.02)
        raise RuntimeError("clipboard busy; frame dropped")

    def finalize(self) -> None:
        """Replace the delayed-render promise with real bytes (call on exit,
        so the last frame survives us and other apps can still paste it)."""
        data = self._dib
        if not data or not _user32.OpenClipboard(self._hwnd):
            return
        try:
            if _user32.GetClipboardOwner() == self._hwnd:
                _user32.EmptyClipboard()
                self._render(CF_DIB)
        finally:
            _user32.CloseClipboard()
