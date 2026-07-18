"""Quiet end-to-end display test: no focus stealing, no global keystrokes.

clipboard -> MenuPaster (UIA Edit>Paste) -> PrintWindow capture, before/after.
"""
import time

import numpy as np
from PIL import Image

from mspaintdoom import capture, paint_out

frame = Image.open(r"C:\Source\MsPaintDoom\smoke_frame.png").convert("RGB")
hwnd = paint_out.launch_paint()

before = capture.grab_window(hwnd)
before.save(r"C:\Source\MsPaintDoom\cap_before.png")

paint_out.frame_to_clipboard(frame)
paster = paint_out.MenuPaster(hwnd)
t0 = time.perf_counter()
paster.paste()
dt1 = (time.perf_counter() - t0) * 1000
time.sleep(0.6)

after = capture.grab_window(hwnd)
after.save(r"C:\Source\MsPaintDoom\cap_after.png")

a, b = np.asarray(before), np.asarray(after)
changed = (a.shape != b.shape) or float(np.mean(np.any(a != b, axis=2))) > 0.05
print(f"paste invoke: {dt1:.0f} ms; canvas changed: {changed}")

# Second paste to measure steady-state cost.
t0 = time.perf_counter()
paster.paste()
print(f"second paste: {(time.perf_counter() - t0) * 1000:.0f} ms")
time.sleep(0.4)
capture.grab_window(hwnd).save(r"C:\Source\MsPaintDoom\cap_after2.png")
