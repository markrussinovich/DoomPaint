"""MS Paint as a framebuffer: clipboard DIB in, pasted onto the canvas.

Two paste paths, chosen at startup by a self-test:

* KeyPaster  — a synthetic Ctrl+V. Opens no menu, so it never diverts the
  player's keystrokes. Preferred, but synthetic keystrokes don't reach every
  Paint build reliably (some UWP/XAML versions drop them).
* MenuPaster — Paint's Edit>Paste via UI Automation. Rock-solid, but briefly
  opens the Edit menu each frame, which can swallow keystrokes. Fallback only.

Both COMMIT the pasted floating selection by switching to the Brushes tool via
UI Automation — never with a synthetic Esc, because Esc while the player holds
Ctrl to fire becomes Ctrl+Esc and opens the Windows Start menu.
"""
import ctypes
import io
import os
import subprocess
import threading
import time

import numpy as np
import win32api
import win32con
import win32gui
import win32process
from PIL import Image

from . import capture, clipserve, keys, sendinput


class PaintNotFocusedError(RuntimeError):
    pass


def _window_exe(hwnd: int) -> str:
    try:
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        h = win32api.OpenProcess(
            win32con.PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        try:
            return os.path.basename(win32process.GetModuleFileNameEx(h, 0)).lower()
        finally:
            win32api.CloseHandle(h)
    except win32api.error:
        return ""


def _is_paint_window(hwnd: int) -> bool:
    if not win32gui.IsWindowVisible(hwnd):
        return False
    title = win32gui.GetWindowText(hwnd)
    # Main window is "<doc> - Paint"; menu flyouts etc. are other top-level
    # mspaint.exe windows and must not match.
    if not title.endswith("Paint"):
        return False
    return _window_exe(hwnd) == "mspaint.exe"


def find_paint() -> int | None:
    found: list[int] = []

    def cb(hwnd, _):
        if _is_paint_window(hwnd):
            found.append(hwnd)
        return True

    win32gui.EnumWindows(cb, None)
    return found[0] if found else None


def launch_paint(timeout: float = 20.0) -> int:
    hwnd = find_paint()
    if hwnd:
        return hwnd
    subprocess.Popen(["mspaint"], shell=False)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        hwnd = find_paint()
        if hwnd:
            time.sleep(1.0)  # let the canvas finish initializing
            return hwnd
        time.sleep(0.25)
    raise RuntimeError("Paint window did not appear")


def focus_paint(hwnd: int) -> None:
    if win32gui.IsIconic(hwnd):
        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
    try:
        win32gui.SetForegroundWindow(hwnd)
    except win32gui.error:
        pass  # foreground lock — fall through to stronger measures
    time.sleep(0.1)
    if paint_is_foreground(hwnd):
        return
    # Foreground lock workaround #1: SwitchToThisWindow (alt-tab semantics).
    ctypes.windll.user32.SwitchToThisWindow(hwnd, True)
    time.sleep(0.2)
    if paint_is_foreground(hwnd):
        return
    # Workaround #2: a synthetic Alt tap grants SetForegroundWindow permission.
    sendinput.send_keys((win32con.VK_MENU, False), (win32con.VK_MENU, True))
    try:
        win32gui.SetForegroundWindow(hwnd)
    except win32gui.error:
        pass
    time.sleep(0.2)


def paint_is_foreground(hwnd: int) -> bool:
    fg = win32gui.GetForegroundWindow()
    if not fg:
        return False
    root = win32gui.GetAncestor(fg, win32con.GA_ROOT) or fg
    return root == hwnd or _window_exe(root) == "mspaint.exe"


# Frames go out through a delayed-rendering clipboard owner (see clipserve):
# publishing waits until Paint has actually READ the previous frame
# (WM_RENDERFORMAT) instead of guessing settle timers, so Paint never races
# our rewrite into its "Can't complete operation" error dialog.
_clip_server = None


def _clipboard() -> "clipserve.ClipboardServer":
    global _clip_server
    if _clip_server is None:
        _clip_server = clipserve.ClipboardServer()
    return _clip_server


def release_clipboard() -> None:
    """Swap the delayed-render promise for real bytes (call before exiting)."""
    if _clip_server is not None:
        _clip_server.finalize()


def frame_to_clipboard(img: Image.Image) -> None:
    with io.BytesIO() as out:
        img.save(out, "BMP")
        dib = out.getvalue()[14:]  # strip BITMAPFILEHEADER -> CF_DIB payload
    _clipboard().publish(dib)


def ctrl_v_paste(hwnd: int) -> None:
    """Synthetic Ctrl+V — only if Paint is foreground. Opens no menu."""
    if not paint_is_foreground(hwnd):
        raise PaintNotFocusedError
    # If the player is already holding Ctrl (to fire), don't inject our own
    # Ctrl press/release around V — sending Ctrl-up would drop their held key.
    if keys.ctrl_physically_down():
        sendinput.send_keys((ord("V"), False), (ord("V"), True))
    else:
        sendinput.send_keys((keys.VK_CONTROL, False), (ord("V"), False),
                            (ord("V"), True), (keys.VK_CONTROL, True))
        # Our Ctrl tap latches GetAsyncKeyState's 0x0001 bit; clear it so the
        # next input poll doesn't read it as the player firing.
        keys.consume_tap(keys.VK_CONTROL)


class _BrushesCommitter:
    """Shared UIA setup: the Brushes tool button used to commit a floating paste.

    Selecting a tool flattens the pasted floating selection onto the canvas
    without drawing anything (drawing needs a mouse stroke) and without opening
    a menu or sending a keystroke — so it can't drag the frame with arrow keys,
    can't leave a selection mini-toolbar, and can't form a bad key-chord.

    The button vanishes from the UIA tree if the user hides Paint's toolbar,
    so commit is best-effort: a missing button means frames paste uncommitted
    (each paste flattens the previous one anyway), never a crash. We re-probe
    periodically so re-showing the toolbar restores commits mid-game.
    """

    _REPROBE_EVERY = 25  # frames between probes while the button is missing

    def __init__(self, hwnd: int):
        from pywinauto import Application
        from pywinauto.timings import Timings
        # Default element-find timeout is 5 s; at game speed a failed lookup
        # must cost a dropped frame, not a multi-second stall.
        Timings.window_find_timeout = 1.0
        self._hwnd = hwnd
        self._win = Application(backend="uia").connect(handle=hwnd) \
            .window(handle=hwnd)
        self._misses = 0
        if self._probe() is None:
            print("  (Brushes button not found — toolbar hidden? Frames will "
                  "paste uncommitted; re-show the toolbar to restore per-frame "
                  "undo steps)")

    def _probe(self):
        try:
            self._brushes = self._win.child_window(
                auto_id="BrushesSplitButton",
                control_type="RadioButton").wrapper_object()
        except Exception:
            self._brushes = None
        return self._brushes

    def _commit(self) -> None:
        if self._brushes is None:
            self._misses += 1
            if self._misses % self._REPROBE_EVERY or self._probe() is None:
                return
        try:
            self._brushes.invoke()
        except Exception:
            # Stale element (toolbar toggled, UI rebuilt): re-resolve, retry.
            if self._probe() is None:
                return
            try:
                self._brushes.invoke()
            except Exception:
                self._brushes = None

    def size_canvas(self, width: int, height: int) -> bool:
        """Make the canvas exactly width x height (the Doom display size).

        Pasting a display-sized frame auto-grows a smaller canvas, and the
        floating selection then covers exactly the target rect at the origin —
        so cropping the canvas to that selection also trims the white margins
        of a larger pre-existing canvas. Best-effort: if it fails, the first
        game frame still grows the canvas to fit; it just may keep margins.
        """
        frame_to_clipboard(Image.new("RGB", (width, height)))
        try:
            if not self.paste_uncommitted():
                return False
        except PaintNotFocusedError:
            return False
        time.sleep(0.2)  # let the floating selection register before cropping
        if not self._crop_to_selection():
            return False
        self._commit()
        return True

    def _crop_to_selection(self) -> bool:
        for locator in ({"auto_id": "CropButton", "control_type": "Button"},
                        {"title": "Crop", "control_type": "Button"}):
            try:
                self._win.child_window(**locator).wrapper_object().invoke()
                return True
            except Exception:
                continue
        # Toolbar hidden? Ctrl+Shift+X crops too, and boot (before the game
        # loop starts polling) is a safe moment for a synthetic chord.
        try:
            sendinput.send_keys(
                (keys.VK_CONTROL, False), (keys.VK_SHIFT, False),
                (ord("X"), False), (ord("X"), True),
                (keys.VK_SHIFT, True), (keys.VK_CONTROL, True))
        except OSError:
            return False
        keys.consume_tap(keys.VK_CONTROL)
        keys.consume_tap(keys.VK_SHIFT)
        return True


class KeyPaster(_BrushesCommitter):
    """Preferred: paste with Ctrl+V (no menu), commit by switching tools."""

    def paste_uncommitted(self) -> bool:
        ctrl_v_paste(self._hwnd)
        return True

    def paste(self) -> None:
        self.paste_uncommitted()
        self._commit()


class MenuPaster(_BrushesCommitter):
    """Fallback: paste via the Edit>Paste menu item (briefly opens the menu)."""

    def __init__(self, hwnd: int):
        super().__init__(hwnd)
        self._edit = self._win.child_window(title="Edit",
                                            control_type="MenuItem")

    def paste_uncommitted(self) -> bool:
        # Menu automation is racy at game speed; retry, and drop the frame
        # rather than crash if the menu never cooperates this tick.
        for _ in range(3):
            try:
                self._edit.expand()
                self._win.child_window(title="Paste",
                                       control_type="MenuItem").invoke()
                return True
            except Exception:
                try:
                    self._edit.collapse()
                except Exception:
                    pass
                time.sleep(0.05)
        return False

    def paste(self) -> None:
        if self.paste_uncommitted():
            self._commit()


def _window_changed(before: "Image.Image | None",
                    after: "Image.Image | None") -> bool:
    if before is None or after is None:
        return False
    a, b = np.asarray(before), np.asarray(after)
    if a.shape != b.shape:
        return True
    return float(np.mean(np.any(a != b, axis=2))) > 0.05


def key_paste_lands(hwnd: int, attempts: int = 4) -> bool:
    """Self-test: does a synthetic Ctrl+V actually paint the canvas here?

    Pastes alternating solid colours and checks the window changed. Runs a few
    times because the first paste after launch is often a warm-up no-op.
    """
    if not paint_is_foreground(hwnd):
        return False
    swatches = [Image.new("RGB", (640, 400), c)
                for c in ((10, 220, 80), (220, 40, 140))]
    for i in range(attempts):
        before = capture.grab_window(hwnd)
        frame_to_clipboard(swatches[i % 2])
        try:
            ctrl_v_paste(hwnd)
        except PaintNotFocusedError:
            return False
        time.sleep(0.3)
        if _window_changed(before, capture.grab_window(hwnd)):
            return True
    return False


def window_stale(before: "Image.Image | None",
                 after: "Image.Image | None") -> bool:
    """Window content is essentially unchanged (pastes aren't landing)."""
    if before is None or after is None:
        return False  # can't tell; don't accuse the paster
    a, b = np.asarray(before), np.asarray(after)
    if a.shape != b.shape:
        return False
    return float(np.mean(np.any(a != b, axis=2))) < 0.005


def dismiss_error_dialog(hwnd: int) -> bool:
    """Close Paint's modal "Can't complete operation" clipboard-error dialog.

    While it's up, every paste silently fails, so the game looks frozen even
    though the loop is running. Returns True if a dialog was dismissed.
    """
    try:
        from pywinauto import Application
        win = Application(backend="uia").connect(handle=hwnd) \
            .window(handle=hwnd)
        dlg = win.child_window(title_re=".*complete operation.*",
                               control_type="Window")
        if not dlg.exists(timeout=0.2):
            return False
        dlg.child_window(auto_id="CloseButton",
                         control_type="Button").invoke()
        return True
    except Exception:
        return False


def start_error_watchdog(hwnd: int) -> None:
    """Background thread: dismiss the "Can't complete operation" dialog
    within ~0.4 s of it appearing. The in-loop staleness check still exists
    as backup, but this one doesn't wait for frames to visibly freeze."""
    def run():
        import comtypes
        try:
            comtypes.CoInitialize()
        except OSError:
            pass
        try:
            from pywinauto import Application
            win = Application(backend="uia").connect(handle=hwnd) \
                .window(handle=hwnd)
        except Exception:
            return
        dlg = win.child_window(title_re=".*complete operation.*",
                               control_type="Window")
        # Hot loop on purpose: a check costs ~20 ms, so at this cadence the
        # dialog is gone within ~150 ms of appearing — a blink, not a popup.
        while True:
            try:
                if dlg.exists(timeout=0.01):
                    dlg.child_window(auto_id="CloseButton",
                                     control_type="Button").invoke()
                    print("  (clipboard-error dialog dismissed)")
            except Exception:
                pass
            time.sleep(0.1)

    threading.Thread(target=run, daemon=True,
                     name="mspaintdoom-dlg-watchdog").start()


def push_frame(hwnd: int, frame, paster, scale: int = 1) -> None:
    img = Image.fromarray(frame)
    if scale > 1:
        img = img.resize((img.width * scale, img.height * scale),
                         Image.Resampling.NEAREST)
    frame_to_clipboard(img)
    paster.paste()
