"""Experiment: can a live OLE IDataObject eliminate clipboard rewrites?

Publishes ONE IDataObject via OleSetClipboard whose GetData returns a
different-colored 640x400 CF_DIB on every call, then sends Paint a series of
Ctrl+V pastes. If each paste calls our GetData (fresh frame served at read
time), the clipboard never needs rewriting mid-game -> the "Can't complete
operation" dialog becomes structurally impossible.

Logs every QueryGetData/GetData request (format, tymed) so we can see exactly
which path Paint's paste takes, and whether Windows caches the first render.
"""
import io
import time

import pythoncom
import winerror
from PIL import Image
from win32com.server.exception import COMException
from win32com.server.util import NewEnum, wrap

from mspaintdoom import capture, paint_out

CF_DIB = 8
CF_NAMES = {1: "CF_TEXT", 2: "CF_BITMAP", 3: "CF_METAFILEPICT", 8: "CF_DIB",
            14: "CF_ENHMETAFILE", 17: "CF_DIBV5"}
COLORS = [(220, 30, 30), (30, 200, 60), (40, 80, 230), (230, 210, 40),
          (200, 40, 200), (40, 210, 210)]

request_log = []  # (t, kind, cf, tymed)


def cfname(cf):
    return CF_NAMES.get(cf, f"cf#{cf}")


def make_dib(i: int) -> bytes:
    img = Image.new("RGB", (640, 400), COLORS[i % len(COLORS)])
    with io.BytesIO() as out:
        img.save(out, "BMP")
        return out.getvalue()[14:]


class FrameDataObject:
    _com_interfaces_ = [pythoncom.IID_IDataObject]
    _public_methods_ = ["GetData", "GetDataHere", "QueryGetData",
                        "GetCanonicalFormatEtc", "SetData", "EnumFormatEtc",
                        "DAdvise", "DUnadvise", "EnumDAdvise"]

    def __init__(self):
        self.n = 0
        self.fe = [(CF_DIB, None, pythoncom.DVASPECT_CONTENT, -1,
                    pythoncom.TYMED_HGLOBAL)]

    def GetData(self, fe):
        cf, target, aspect, index, tymed = fe
        request_log.append((time.perf_counter(), "GetData", cf, tymed))
        if cf != CF_DIB or not (tymed & pythoncom.TYMED_HGLOBAL):
            raise COMException(hresult=winerror.DV_E_FORMATETC)
        stg = pythoncom.STGMEDIUM()
        stg.set(pythoncom.TYMED_HGLOBAL, make_dib(self.n))
        self.n += 1
        return stg

    def GetDataHere(self, fe):
        request_log.append((time.perf_counter(), "GetDataHere", fe[0], fe[4]))
        raise COMException(hresult=winerror.E_NOTIMPL)

    def QueryGetData(self, fe):
        cf, target, aspect, index, tymed = fe
        request_log.append((time.perf_counter(), "QueryGetData", cf, tymed))
        if cf != CF_DIB:
            raise COMException(hresult=winerror.DV_E_FORMATETC)
        return None

    def GetCanonicalFormatEtc(self, fe):
        raise COMException(hresult=winerror.DATA_S_SAMEFORMATETC)

    def SetData(self, fe, medium):
        raise COMException(hresult=winerror.E_NOTIMPL)

    def EnumFormatEtc(self, direction):
        if direction != pythoncom.DATADIR_GET:
            raise COMException(hresult=winerror.E_NOTIMPL)
        return NewEnum(self.fe, iid=pythoncom.IID_IEnumFORMATETC)

    def DAdvise(self, fe, flags, sink):
        raise COMException(hresult=winerror.E_NOTIMPL)

    def DUnadvise(self, connection):
        raise COMException(hresult=winerror.E_NOTIMPL)

    def EnumDAdvise(self):
        raise COMException(hresult=winerror.E_NOTIMPL)


def pump(seconds: float) -> None:
    end = time.perf_counter() + seconds
    while time.perf_counter() < end:
        pythoncom.PumpWaitingMessages()
        time.sleep(0.005)


def center_color(hwnd):
    img = capture.grab_window(hwnd)
    if img is None:
        return None
    return img.getpixel((img.width // 2, img.height // 2))


def main():
    pythoncom.OleInitialize()
    inner = FrameDataObject()
    do = wrap(inner, pythoncom.IID_IDataObject)
    pythoncom.OleSetClipboard(do)
    print("OleSetClipboard done (one-time). No further clipboard writes.")

    hwnd = paint_out.launch_paint()
    paint_out.focus_paint(hwnd)
    pump(0.5)
    if not paint_out.paint_is_foreground(hwnd):
        print("ABORT: could not bring Paint to the foreground.")
        return 1
    if paint_out.dismiss_error_dialog(hwnd):
        print("(dismissed a leftover error dialog before starting)")

    baseline = len(request_log)
    print(f"requests during setup (history broker etc.): {baseline}")

    # Phase 1: slow pastes, verify each paste lands a DIFFERENT frame.
    print("\n-- phase 1: 6 pastes @ 400 ms --")
    seen = []
    for i in range(6):
        before = len(request_log)
        try:
            paint_out.ctrl_v_paste(hwnd)
        except paint_out.PaintNotFocusedError:
            print("ABORT: Paint lost focus mid-test.")
            return 1
        pump(0.4)
        col = center_color(hwnd)
        seen.append(col)
        new = request_log[before:]
        print(f"paste {i}: {len(new)} clipboard requests "
              f"{[(k, cfname(cf)) for _, k, cf, _ in new]}, center={col}")

    distinct = len(set(seen))
    print(f"distinct canvas colors across 6 pastes: {distinct}")

    # Phase 2: game-speed pastes.
    print("\n-- phase 2: 40 pastes @ 30 ms --")
    before = len(request_log)
    for i in range(40):
        try:
            paint_out.ctrl_v_paste(hwnd)
        except paint_out.PaintNotFocusedError:
            print("ABORT: Paint lost focus mid-test.")
            return 1
        pump(0.03)
    pump(1.0)  # drain stragglers
    getdatas = sum(1 for _, k, cf, _ in request_log[before:]
                   if k == "GetData" and cf == CF_DIB)
    print(f"GetData(CF_DIB) calls for 40 fast pastes: {getdatas}")

    dialog = paint_out.dismiss_error_dialog(hwnd)
    print(f"error dialog appeared: {dialog}")

    print(f"\ntotal GetData(CF_DIB) served: {inner.n}")
    print("full request log (after setup):")
    t0 = request_log[baseline][0] if len(request_log) > baseline else 0
    for t, kind, cf, tymed in request_log[baseline:]:
        print(f"  +{t - t0:7.3f}s {kind:13s} {cfname(cf):16s} tymed={tymed}")

    pythoncom.OleFlushClipboard()  # leave real bytes behind
    print("\nVERDICT:")
    if dialog:
        print("  dialog still reproduced — approach does NOT eliminate it")
    elif distinct >= 5 and getdatas >= 35:
        print("  every paste read a fresh frame with ZERO clipboard rewrites")
    elif distinct <= 2:
        print("  Windows cached the first render — frames freeze; not viable")
    else:
        print("  partial/ambiguous — read the log above")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
