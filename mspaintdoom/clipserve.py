"""Clipboard owner backed by an OLE IDataObject — updated in place, never churned.

Paint reads a pasted image off the clipboard asynchronously, on its own
schedule. Rewriting the clipboard while that read is in flight makes it fail,
and Paint responds with a modal "Can't complete operation" dialog that drops
the paste. The raw-Win32 approach (a per-frame EmptyClipboard + SetClipboardData
to re-arm) triggers exactly that: emptying the clipboard for the next frame
frees the bytes out from under an in-progress read — intermittently, and more
often the faster we paste.

So we stop rewriting the clipboard. We own it once as an OLE data object
(OleSetClipboard with an IDataObject) and update the frame bytes it hands out
in place. The object is reference-counted, so a consumer's in-flight read stays
valid even as the next frame arrives — the race can't happen, and the dialog is
gone. Each GetData call on our object doubles as a positive "frame was consumed"
signal (the role WM_RENDERFORMAT played in the old delayed-render design), so
publish() still paces against real consumption. On shutdown, OleFlushClipboard
leaves the last frame pasteable.

The data object lives on a dedicated STA thread pumping messages, so the
cross-process GetData calls are serviced promptly.

If another app takes the clipboard (user copies something while alt-tabbed),
a WM_CLIPBOARDUPDATE listener flags the loss and the next publish() —
i.e. only once the game is actively pushing frames again — re-publishes our
object. Reclaiming on publish rather than on a timer means a copy the user
makes while the game is paused stays theirs.
"""
import atexit
import ctypes
import struct
import threading
import time
from ctypes import wintypes

import pythoncom
from win32com.server.exception import COMException
from win32com.server.util import wrap

_user32 = ctypes.WinDLL("user32", use_last_error=True)
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

CF_DIB = 8
_DVASPECT_CONTENT = 1
_TYMED_HGLOBAL = pythoncom.TYMED_HGLOBAL
_DATADIR_GET = 1
_DV_E_FORMATETC = -2147221404
_E_NOTIMPL = -2147467263
_OLE_E_ADVISENOTSUPPORTED = -2147221501
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


class _EnumFORMATETC:
    """Minimal IEnumFORMATETC advertising just CF_DIB (HGLOBAL)."""

    _com_interfaces_ = [pythoncom.IID_IEnumFORMATETC]
    _public_methods_ = ["Next", "Skip", "Reset", "Clone"]

    def __init__(self, fmts, index=0):
        self._fmts = fmts
        self._index = index

    def Next(self, count):
        result = self._fmts[self._index:self._index + count]
        self._index += len(result)
        return result

    def Skip(self, count):
        self._index += count
        return 0

    def Reset(self):
        self._index = 0
        return 0

    def Clone(self):
        return _new_enum(self._fmts, self._index)


def _new_enum(fmts, index=0):
    return wrap(_EnumFORMATETC(fmts, index), pythoncom.IID_IEnumFORMATETC)


class _DibDataObject:
    """IDataObject serving CF_DIB bytes from a provider callback.

    QueryGetData/EnumFormatEtc advertise only CF_DIB; GetData returns the
    current frame. Everything else returns a benign HRESULT so OLE treats it as
    "format/operation not supported" rather than a hard error.
    """

    _com_interfaces_ = [pythoncom.IID_IDataObject]
    _public_methods_ = ["GetData", "GetDataHere", "QueryGetData",
                        "GetCanonicalFormatEtc", "SetData", "EnumFormatEtc",
                        "DAdvise", "DUnadvise", "EnumDAdvise"]

    def __init__(self, provider):
        self._provider = provider  # zero-arg callable -> DIB bytes (or b"")

    def QueryGetData(self, fe):
        if fe[0] == CF_DIB and (fe[4] & _TYMED_HGLOBAL):
            return 0  # S_OK
        raise COMException(scode=_DV_E_FORMATETC)

    def GetData(self, fe):
        if fe[0] == CF_DIB and (fe[4] & _TYMED_HGLOBAL):
            data = self._provider()
            if data:
                medium = pythoncom.STGMEDIUM()
                medium.set(_TYMED_HGLOBAL, bytes(data))
                return medium
        raise COMException(scode=_DV_E_FORMATETC)

    def EnumFormatEtc(self, direction):
        if direction == _DATADIR_GET:
            return _new_enum([(CF_DIB, None, _DVASPECT_CONTENT, -1,
                               _TYMED_HGLOBAL)])
        raise COMException(scode=_E_NOTIMPL)

    def GetDataHere(self, *args):
        raise COMException(scode=_E_NOTIMPL)

    def GetCanonicalFormatEtc(self, *args):
        raise COMException(scode=_E_NOTIMPL)

    def SetData(self, *args):
        raise COMException(scode=_E_NOTIMPL)

    def DAdvise(self, *args):
        raise COMException(scode=_OLE_E_ADVISENOTSUPPORTED)

    def DUnadvise(self, *args):
        raise COMException(scode=_OLE_E_ADVISENOTSUPPORTED)

    def EnumDAdvise(self, *args):
        raise COMException(scode=_OLE_E_ADVISENOTSUPPORTED)


class ClipboardServer:
    """Owns the clipboard as an OLE IDataObject on a dedicated STA thread."""

    def __init__(self):
        self._dib = b""
        self._last_good = _blank_dib()  # last non-empty frame actually served
        self._render_fn = None  # if set, called on demand to produce the DIB
        self._consumed = threading.Event()
        self._consumed.set()  # nothing pending yet
        self._lock = threading.Lock()
        self._ready = threading.Event()
        self._lost = False
        self._hwnd = None
        self._dataobj = None
        self._finalized = False
        self._proc = _WNDPROC(self._wndproc)  # keep alive: GC'd proc = crash
        self._thread = threading.Thread(target=self._pump, daemon=True,
                                        name="mspaintdoom-clip")
        self._thread.start()
        if not self._ready.wait(5.0) or not self._hwnd \
                or self._dataobj is None:
            raise RuntimeError("OLE clipboard server failed to start")
        atexit.register(self.finalize)

    # -- OLE (STA) thread ---------------------------------------------------

    def _provide(self):
        """Called on the STA thread when Paint reads CF_DIB: produce (and, in
        render-on-demand mode, generate) the freshest frame, and record that
        Paint consumed it.

        Never hands back an empty buffer. An empty read fails and pops Paint's
        dialog, which is exactly the cold-start case (the first Ctrl+V can beat
        the engine's first frame), so we fall back to the last good frame; and
        we only flag consumption when real bytes actually went out."""
        with self._lock:
            fn = self._render_fn
            data = fn() if fn is not None else self._dib
            if data:
                self._last_good = data
            else:
                data = self._last_good
            if data:
                self._consumed.set()  # consume edge is set under the same lock
        return data

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
            do = wrap(_DibDataObject(self._provide),
                      pythoncom.IID_IDataObject)
            if self._set_clipboard(do):
                self._dataobj = do
            _user32.AddClipboardFormatListener(self._hwnd)
        self._ready.set()
        if self._dataobj is None:
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
                    self._lost = not pythoncom.OleIsCurrentClipboard(
                        self._dataobj)
                except pythoncom.com_error:
                    self._lost = True
                return 0
            if msg == _WM_APP_REPUBLISH:
                if self._lost and self._set_clipboard(self._dataobj,
                                                      attempts=1):
                    self._lost = False
                return 0
            if msg == _WM_APP_FLUSH:
                try:
                    if pythoncom.OleIsCurrentClipboard(self._dataobj):
                        pythoncom.OleFlushClipboard()
                except pythoncom.com_error:
                    pass
                return 0
        except Exception:
            return 0  # never let an exception cross the ctypes boundary
        return _user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    # -- game-loop side -----------------------------------------------------

    def publish(self, dib: bytes = None, wait_prev: float = 0.25,
                render_fn=None) -> bool:
        """Advertise a new frame, first waiting for the previous one to be read
        by Paint (a GetData call) or wait_prev seconds, whichever comes first.
        Returns whether that previous frame was consumed.

        Pass `dib` for ready bytes, or `render_fn` (a zero-arg callable -> DIB
        bytes) for render-on-demand: the frame is produced only when Paint's
        GetData reads it, minimising displayed-frame age. Updating the data
        object in place needs no clipboard churn, so a read still in flight is
        never invalidated (that was the cause of Paint's clipboard-error dialog).
        """
        previous_consumed = self._consumed.wait(wait_prev)
        with self._lock:
            self._render_fn = render_fn
            if dib is not None:
                self._dib = dib
            self._consumed.clear()  # arm "not yet consumed" atomically with data
        if self._lost:  # someone else took the clipboard: reclaim it
            _user32.PostMessageW(self._hwnd, _WM_APP_REPUBLISH, 0, 0)
        return previous_consumed

    def finalize(self) -> None:
        """Snapshot the live object into real clipboard bytes. The flush is a
        synchronous SendMessage to the STA thread, so it has completed before
        this returns — the process can't exit with Paint's final read pointed
        at our dead data object (which pops the "Can't complete operation"
        dialog on quit)."""
        if self._finalized:
            return
        self._finalized = True
        if self._hwnd and self._dataobj is not None:
            _user32.SendMessageW(self._hwnd, _WM_APP_FLUSH, 0, 0)
