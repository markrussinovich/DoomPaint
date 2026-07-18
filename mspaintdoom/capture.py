"""Capture a window's content even when occluded (PrintWindow + DWM)."""
import ctypes

import win32con
import win32gui
import win32ui
from PIL import Image

_PW_RENDERFULLCONTENT = 0x00000002


def grab_window(hwnd: int) -> Image.Image | None:
    if win32gui.IsIconic(hwnd):
        return None
    left, top, right, bottom = win32gui.GetWindowRect(hwnd)
    w, h = right - left, bottom - top
    if w <= 0 or h <= 0:
        return None
    hwnd_dc = win32gui.GetWindowDC(hwnd)
    mfc_dc = win32ui.CreateDCFromHandle(hwnd_dc)
    save_dc = mfc_dc.CreateCompatibleDC()
    bmp = win32ui.CreateBitmap()
    try:
        bmp.CreateCompatibleBitmap(mfc_dc, w, h)
        save_dc.SelectObject(bmp)
        ok = ctypes.windll.user32.PrintWindow(hwnd, save_dc.GetSafeHdc(),
                                             _PW_RENDERFULLCONTENT)
        if not ok:
            return None
        info = bmp.GetInfo()
        img = Image.frombuffer(
            "RGB", (info["bmWidth"], info["bmHeight"]),
            bmp.GetBitmapBits(True), "raw", "BGRX", 0, 1)
        return img
    finally:
        win32gui.DeleteObject(bmp.GetHandle())
        save_dc.DeleteDC()
        mfc_dc.DeleteDC()
        win32gui.ReleaseDC(hwnd, hwnd_dc)
