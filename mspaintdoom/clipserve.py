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
"""
import atexit
import threading

import pythoncom
import win32api
from win32com.server.exception import COMException
from win32com.server.util import wrap

CF_DIB = 8
_DVASPECT_CONTENT = 1
_TYMED_HGLOBAL = pythoncom.TYMED_HGLOBAL
_DATADIR_GET = 1
_DV_E_FORMATETC = -2147221404
_E_NOTIMPL = -2147467263
_OLE_E_ADVISENOTSUPPORTED = -2147221501
_WM_QUIT = 0x0012


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
        self._last_good = b""  # last non-empty frame actually served
        self._render_fn = None  # if set, called on demand to produce the DIB
        self._consumed = threading.Event()
        self._consumed.set()  # nothing pending yet
        self._lock = threading.Lock()
        self._ready = threading.Event()
        self._tid = None
        self._finalized = False
        threading.Thread(target=self._pump, daemon=True,
                         name="mspaintdoom-clip").start()
        if not self._ready.wait(5.0):
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
        fn = self._render_fn
        with self._lock:
            data = fn() if fn is not None else self._dib
            if data:
                self._last_good = data
            else:
                data = self._last_good
        if data:
            self._consumed.set()
        return data

    def _pump(self):
        self._tid = win32api.GetCurrentThreadId()
        pythoncom.OleInitialize()
        self._dataobj = wrap(_DibDataObject(self._provide),
                             pythoncom.IID_IDataObject)
        pythoncom.OleSetClipboard(self._dataobj)
        self._ready.set()
        pythoncom.PumpMessages()  # until WM_QUIT; delivers Paint's GetData
        try:
            pythoncom.OleFlushClipboard()  # leave the last frame pasteable
        except Exception:
            pass

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
        self._consumed.clear()
        return previous_consumed

    def finalize(self) -> None:
        """Stop the STA pump, which then flushes the last frame onto the
        clipboard so quitting doesn't leave an empty clipboard behind."""
        if self._finalized:
            return
        self._finalized = True
        if self._tid:
            try:
                win32api.PostThreadMessage(self._tid, _WM_QUIT, 0, 0)
            except Exception:
                pass
