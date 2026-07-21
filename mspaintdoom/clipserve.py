"""Live OLE clipboard owner: the clipboard is written once, then never again.

Every earlier scheme rewrote the clipboard per frame, and any rewrite that
landed inside Paint's paste popped its modal "Can't complete operation"
dialog: Win11 Paint reads through the WinRT DataTransfer layer, which aborts
when the clipboard changes mid-paste. Delayed rendering (WM_RENDERFORMAT)
narrowed the window — it signals when a paste STARTS reading — but nothing
signals when the paste FINISHES, so a guessed tail guard remained, and at
game speed it still occasionally lost the race.

This version removes the race instead of shrinking it. OleSetClipboard
publishes one live IDataObject at boot; each Ctrl+V makes Paint's paste call
our GetData through the OLE proxy, and we serve whatever frame is newest at
that instant. publish() is just a buffer swap — the clipboard itself is
never rewritten during play, its sequence number never changes, and the
dialog becomes structurally impossible. (Measured against Paint 11.2605:
consecutive pastes each read a fresh frame; 40 pastes at 30 ms spacing,
zero rewrites, zero dialogs.)

If another app takes the clipboard (user copies something while alt-tabbed),
a WM_CLIPBOARDUPDATE listener flags the loss and the next publish() —
i.e. only once the game is actively pushing frames again — re-publishes our
object. On shutdown, finalize() calls OleFlushClipboard so real bytes
outlive the process and other apps can still paste the last frame.
"""
import atexit
import ctypes
import struct
import threading
import time
from ctypes import wintypes

import pythoncom
import winerror
from win32com.server.exception import COMException
from win32com.server.util import NewEnum, wrap

_user32 = ctypes.WinDLL("user32", use_last_error=True)
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

CF_DIB = 8
_WM_CLIPBOARDUPDATE = 0x031D
_WM_APP_REPUBLISH = 0x8001
_WM_APP_FLUSH = 0x8002
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
        (_user32.AddClipboardFormatListener, wintypes.BOOL, (wintypes.HWND,)),
        (_user32.PostMessageW, wintypes.BOOL,
         (wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)),
        (_user32.SendMessageW, ctypes.c_ssize_t,
         (wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)),
        (_kernel32.GetModuleHandleW, wintypes.HMODULE, (wintypes.LPCWSTR,))):
    fn.restype, fn.argtypes = res, args


def _blank_dib(width: int = 640, height: int = 400) -> bytes:
    """A black CF_DIB, served if anything pastes before the first frame."""
    header = struct.pack("<IiiHHIIiiII", 40, width, height, 1, 24, 0,
                         width * height * 3, 0, 0, 0, 0)
    return header + b"\x00" * (width * height * 3)


class _FrameDataObject:
    """IDataObject serving the owner's newest frame on every GetData call.

    Paint's paste probes a couple of private OLE formats first (observed:
    two RegisterClipboardFormat ids, via GetData and GetDataHere) before
    reading CF_DIB; refusing those with the standard error codes is part of
    the normal protocol, not a failure.
    """

    _com_interfaces_ = [pythoncom.IID_IDataObject]
    _public_methods_ = ["GetData", "GetDataHere", "QueryGetData",
                        "GetCanonicalFormatEtc", "SetData", "EnumFormatEtc",
                        "DAdvise", "DUnadvise", "EnumDAdvise"]

    _FORMATS = [(CF_DIB, None, pythoncom.DVASPECT_CONTENT, -1,
                 pythoncom.TYMED_HGLOBAL)]

    def __init__(self, owner: "ClipboardServer"):
        self._owner = owner

    def GetData(self, fe):
        cf, _target, aspect, _index, tymed = fe
        if cf != CF_DIB or not (tymed & pythoncom.TYMED_HGLOBAL) \
                or not (aspect & pythoncom.DVASPECT_CONTENT):
            raise COMException(hresult=winerror.DV_E_FORMATETC)
        stg = pythoncom.STGMEDIUM()
        stg.set(pythoncom.TYMED_HGLOBAL, self._owner._dib)
        return stg

    def GetDataHere(self, fe):
        raise COMException(hresult=winerror.E_NOTIMPL)

    def QueryGetData(self, fe):
        cf, _target, _aspect, _index, tymed = fe
        if cf != CF_DIB or not (tymed & pythoncom.TYMED_HGLOBAL):
            raise COMException(hresult=winerror.DV_E_FORMATETC)
        return None

    def GetCanonicalFormatEtc(self, fe):
        raise COMException(hresult=winerror.DATA_S_SAMEFORMATETC)

    def SetData(self, fe, medium):
        raise COMException(hresult=winerror.E_NOTIMPL)

    def EnumFormatEtc(self, direction):
        if direction != pythoncom.DATADIR_GET:
            raise COMException(hresult=winerror.E_NOTIMPL)
        return NewEnum(self._FORMATS, iid=pythoncom.IID_IEnumFORMATETC)

    def DAdvise(self, fe, flags, sink):
        raise COMException(hresult=winerror.E_NOTIMPL)

    def DUnadvise(self, connection):
        raise COMException(hresult=winerror.E_NOTIMPL)

    def EnumDAdvise(self):
        raise COMException(hresult=winerror.E_NOTIMPL)


class ClipboardServer:
    """Owns the clipboard via a live OLE data object on its own STA thread."""

    def __init__(self):
        self._dib = _blank_dib()
        self._lost = False
        self._ready = threading.Event()
        self._hwnd = None
        self._do = None
        self._proc = _WNDPROC(self._wndproc)  # keep alive: GC'd proc = crash
        threading.Thread(target=self._pump, daemon=True,
                         name="mspaintdoom-clip").start()
        if not self._ready.wait(5.0) or not self._hwnd or self._do is None:
            raise RuntimeError("clipboard server failed to start")
        atexit.register(self.finalize)

    # -- STA thread ----------------------------------------------------------

    def _pump(self):
        pythoncom.OleInitialize()
        cls = _WNDCLASSW()
        cls.lpfnWndProc = self._proc
        cls.hInstance = _kernel32.GetModuleHandleW(None)
        cls.lpszClassName = "MsPaintDoomClipSrv"
        _user32.RegisterClassW(ctypes.byref(cls))
        self._hwnd = _user32.CreateWindowExW(
            0, cls.lpszClassName, None, 0, 0, 0, 0, 0, _HWND_MESSAGE,
            None, cls.hInstance, None)
        if self._hwnd:
            do = wrap(_FrameDataObject(self), pythoncom.IID_IDataObject)
            if self._set_clipboard(do):
                self._do = do
            _user32.AddClipboardFormatListener(self._hwnd)
        self._ready.set()
        if self._do is None:
            return
        msg = wintypes.MSG()
        while _user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            _user32.TranslateMessage(ctypes.byref(msg))
            _user32.DispatchMessageW(ctypes.byref(msg))

    def _set_clipboard(self, do, attempts: int = 10) -> bool:
        for _ in range(attempts):
            try:
                pythoncom.OleSetClipboard(do)
                return True
            except pythoncom.com_error:
                pythoncom.PumpWaitingMessages()
                time.sleep(0.05)  # clipboard busy; retry shortly
        return False

    def _wndproc(self, hwnd, msg, wparam, lparam):
        try:
            if msg == _WM_CLIPBOARDUPDATE:
                # Fires for our own OleSetClipboard too; the check sorts it out.
                try:
                    self._lost = not pythoncom.OleIsCurrentClipboard(self._do)
                except pythoncom.com_error:
                    self._lost = True
                return 0
            if msg == _WM_APP_REPUBLISH:
                if self._lost and self._set_clipboard(self._do, attempts=1):
                    self._lost = False
                return 0
            if msg == _WM_APP_FLUSH:
                try:
                    if pythoncom.OleIsCurrentClipboard(self._do):
                        pythoncom.OleFlushClipboard()
                except pythoncom.com_error:
                    pass
                return 0
        except Exception:
            return 0  # never let an exception cross the ctypes boundary
        return _user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    # -- game-loop side ------------------------------------------------------

    def publish(self, dib: bytes) -> None:
        """Make dib the frame served to the next paste. Never blocks: this is
        a reference swap, not a clipboard write. Paint reads the newest frame
        at its own pace and skipped frames simply never leave this buffer."""
        self._dib = dib
        if self._lost:  # someone else took the clipboard: reclaim it
            _user32.PostMessageW(self._hwnd, _WM_APP_REPUBLISH, 0, 0)

    def finalize(self) -> None:
        """Snapshot the live object into real clipboard bytes (call on exit,
        so the last frame survives us and other apps can still paste it)."""
        if self._hwnd and self._do is not None:
            _user32.SendMessageW(self._hwnd, _WM_APP_FLUSH, 0, 0)
