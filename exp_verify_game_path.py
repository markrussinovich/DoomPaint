"""Verify the OLE-clipboard rewrite through the real game path.

Uses paint_out.frame_to_clipboard + KeyPaster (paste + Brushes commit)
against real Paint at game speed, then checks:
  1. frames land (canvas color tracks the published frames),
  2. no "Can't complete operation" dialog ever appears,
  3. an external clipboard write mid-game is reclaimed automatically,
  4. release_clipboard() leaves real CF_DIB bytes behind.
"""
import time

from PIL import Image

import win32clipboard
from mspaintdoom import capture, paint_out

COLORS = [(220, 30, 30), (30, 200, 60), (40, 80, 230), (230, 210, 40),
          (200, 40, 200), (40, 210, 210)]


def center_color(hwnd):
    img = capture.grab_window(hwnd)
    return None if img is None else img.getpixel((img.width // 2,
                                                  img.height // 2))


def close(rgb, target, tol=12):
    return rgb is not None and all(abs(a - b) <= tol
                                   for a, b in zip(rgb, target))


def main():
    hwnd = paint_out.launch_paint()
    paint_out.focus_paint(hwnd)
    if not paint_out.paint_is_foreground(hwnd):
        print("ABORT: Paint not foreground")
        return 1
    if paint_out.dismiss_error_dialog(hwnd):
        print("(dismissed leftover dialog)")
    paster = paint_out.KeyPaster(hwnd)

    # Phase 1: 80 frames, full game path (publish + Ctrl+V + commit).
    print("-- 80 frames at game speed --")
    t0 = time.perf_counter()
    for i in range(80):
        img = Image.new("RGB", (640, 400), COLORS[i % len(COLORS)])
        paint_out.frame_to_clipboard(img)
        paster.paste()
    dt = time.perf_counter() - t0
    time.sleep(0.5)
    final = center_color(hwnd)
    landed = any(close(final, COLORS[(79 - k) % len(COLORS)])
                 for k in range(3))
    print(f"{80 / dt:.1f} fps; final center={final}; "
          f"matches a recent frame: {landed}")
    dialog1 = paint_out.dismiss_error_dialog(hwnd)
    print(f"error dialog: {dialog1}")

    # Phase 2: steal the clipboard (like a user Ctrl+C elsewhere), then keep
    # publishing — the server should reclaim it and pastes should resume.
    print("-- clipboard theft + recovery --")
    win32clipboard.OpenClipboard()
    win32clipboard.EmptyClipboard()
    win32clipboard.SetClipboardText("stolen by another app")
    win32clipboard.CloseClipboard()
    time.sleep(0.3)
    for i in range(20):
        img = Image.new("RGB", (640, 400), COLORS[i % len(COLORS)])
        paint_out.frame_to_clipboard(img)
        try:
            paster.paste()
        except paint_out.PaintNotFocusedError:
            # like the game loop: refocus and keep going
            paint_out.focus_paint(hwnd)
            time.sleep(0.2)
    time.sleep(0.5)
    final2 = center_color(hwnd)
    recovered = any(close(final2, COLORS[(19 - k) % len(COLORS)])
                    for k in range(3))
    print(f"after theft: center={final2}; frames landing again: {recovered}")
    dialog2 = paint_out.dismiss_error_dialog(hwnd)
    print(f"error dialog: {dialog2}")

    # Phase 3: finalize -> real bytes must remain readable.
    paint_out.release_clipboard()
    win32clipboard.OpenClipboard()
    try:
        data = win32clipboard.GetClipboardData(win32clipboard.CF_DIB)
    finally:
        win32clipboard.CloseClipboard()
    print(f"post-exit clipboard CF_DIB: {len(data)} bytes")

    ok = landed and recovered and not dialog1 and not dialog2 \
        and len(data) > 100000
    print("\nVERDICT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
