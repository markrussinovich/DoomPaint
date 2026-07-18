"""Capture the current Paint window to the given path (occlusion-proof)."""
import sys

from mspaintdoom import capture, paint_out

hwnd = paint_out.find_paint()
if not hwnd:
    raise SystemExit("no Paint window")
img = capture.grab_window(hwnd)
if img is None:
    raise SystemExit("capture failed (minimized?)")
img.save(sys.argv[1])
print("saved", sys.argv[1])
