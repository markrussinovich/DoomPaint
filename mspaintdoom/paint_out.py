"""MS Paint as a framebuffer: clipboard DIB in, pasted onto the canvas.

Two paste paths, chosen at startup by a self-test:

* KeyPaster  — a synthetic Ctrl+V. Opens no menu, so it never diverts the
  player's keystrokes. Preferred, but synthetic keystrokes don't reach every
  Paint build reliably (some UWP/XAML versions drop them).
* MenuPaster — Paint's Edit>Paste via UI Automation. Rock-solid, but briefly
  opens the Edit menu each frame, which can swallow keystrokes. Fallback only.

Each new paste implicitly commits the previous floating selection, so no
explicit per-frame commit is needed; the final frame is left floating on exit.
"""
import ctypes
import io
import os
import re
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


# Extra room asked of fit_window beyond the display itself: covers the
# scrollbar strip (which vanishes once the canvas fits) plus a little margin.
_VIEW_SLACK_PX = 24


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


def focus_paint(hwnd: int, timeout: float = 5.0) -> bool:
    """Bring Paint to the foreground and confirm it, returning success.

    Retries until Paint is actually foreground or `timeout` elapses. A freshly
    launched window usually activates on the first try, but one we're *attaching*
    to (Paint was already open behind another window) often does not, and
    Windows' foreground lock rejects SetForegroundWindow from a background
    process until a synthetic Alt tap grants permission. Callers that then drive
    Paint (canvas priming, the Ctrl+V self-test) depend on this actually landing;
    a single best-effort attempt was what let the self-test flake to the slow
    menu paster when Paint started unfocused.
    """
    if win32gui.IsIconic(hwnd):
        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
    deadline = time.perf_counter() + timeout
    alt_tapped = False
    while True:
        if paint_is_foreground(hwnd):
            return True
        try:
            win32gui.SetForegroundWindow(hwnd)
        except win32gui.error:
            pass  # foreground lock — fall through to stronger measures
        if paint_is_foreground(hwnd):
            return True
        # Workaround #1: SwitchToThisWindow (alt-tab semantics). Targets our
        # window only, so it's safe to retry each iteration.
        ctypes.windll.user32.SwitchToThisWindow(hwnd, True)
        time.sleep(0.1)
        if paint_is_foreground(hwnd):
            return True
        # Workaround #2: a synthetic Alt tap grants SetForegroundWindow
        # permission past the foreground lock — but do it ONCE only. It's a
        # global keystroke, so repeating it every iteration would spam the menu
        # bar of whatever window actually has focus if Paint won't come forward.
        if not alt_tapped:
            alt_tapped = True
            sendinput.send_keys(
                (win32con.VK_MENU, False), (win32con.VK_MENU, True))
            try:
                win32gui.SetForegroundWindow(hwnd)
            except win32gui.error:
                pass
            if paint_is_foreground(hwnd):
                return True
        if time.perf_counter() >= deadline:
            return False
        time.sleep(0.2)


def paint_is_foreground(hwnd: int) -> bool:
    fg = win32gui.GetForegroundWindow()
    if not fg:
        return False
    root = win32gui.GetAncestor(fg, win32con.GA_ROOT) or fg
    return root == hwnd or _window_exe(root) == "mspaint.exe"


# Frames go out through an OLE IDataObject clipboard owner (see clipserve):
# publishing waits until the previous frame has actually been read (a GetData
# call on our data object) instead of guessing settle timers, and updates the
# frame bytes in place so we never race a rewrite into Paint's "Can't complete
# operation" error dialog.
_clip_server = None
_error_watchdog_wake = threading.Event()


def _clipboard() -> "clipserve.ClipboardServer":
    global _clip_server
    if _clip_server is None:
        _clip_server = clipserve.ClipboardServer()
    return _clip_server


def release_clipboard() -> None:
    """Stop the clipboard server, flushing real bytes onto the clipboard so the
    last frame stays pasteable after we exit (call before exiting)."""
    if _clip_server is not None:
        _clip_server.finalize()


def frame_to_dib(img: Image.Image) -> bytes:
    with io.BytesIO() as out:
        img.save(out, "BMP")
        return out.getvalue()[14:]  # strip BITMAPFILEHEADER -> CF_DIB payload


def encode_frame(frame, scale: int = 1) -> bytes:
    """numpy RGB frame -> CF_DIB bytes (optionally integer-upscaled)."""
    img = Image.fromarray(frame)
    if scale > 1:
        img = img.resize((img.width * scale, img.height * scale),
                         Image.Resampling.NEAREST)
    return frame_to_dib(img)


def frame_to_clipboard(img: Image.Image) -> bool:
    return _clipboard().publish(frame_to_dib(img))


def ctrl_v_paste(hwnd: int) -> None:
    """Synthetic Ctrl+V — only if Paint is foreground. Opens no menu."""
    if not paint_is_foreground(hwnd):
        raise PaintNotFocusedError
    # A held Shift (the run key) reaches Paint, and Paint reads modifiers
    # globally, so our injected V arrives as Ctrl+Shift+V — not the paste
    # accelerator — and the paste silently no-ops, freezing frames while you
    # run. Release any held Shift for the (atomic) chord, then re-press it so
    # running continues. The whole sequence is one SendInput batch, so the
    # window where Shift reads "up" to the game is negligible.
    shifts = keys.held_shift_vks()
    seq = [(vk, True) for vk in shifts]  # release held Shift(s)
    # If the player is already holding Ctrl (to fire), don't inject our own
    # Ctrl press/release around V — sending Ctrl-up would drop their held key.
    ctrl_down = keys.ctrl_physically_down()
    if ctrl_down:
        seq += [(ord("V"), False), (ord("V"), True)]
    else:
        seq += [(keys.VK_CONTROL, False), (ord("V"), False),
                (ord("V"), True), (keys.VK_CONTROL, True)]
    seq += [(vk, False) for vk in shifts]  # re-press Shift(s) to keep running
    sendinput.send_keys(*seq)
    if not ctrl_down:
        # Our Ctrl tap latches GetAsyncKeyState's 0x0001 bit; clear it so the
        # next input poll doesn't read it as the player firing.
        keys.consume_tap(keys.VK_CONTROL)


_VK_PAGE_UP = 0x21
_VK_PAGE_DOWN = 0x22


def _zoom_percent(win) -> int:
    text = win.child_window(
        auto_id="ZoomValuesComboBox",
        control_type="ComboBox").wrapper_object().selected_text()
    values = re.findall(r"\d+", text)
    if not values:
        raise ValueError(f"unrecognized Paint zoom value: {text!r}")
    return int(values[0])


def _zoom_step(hwnd: int, win, key: int) -> bool:
    if not paint_is_foreground(hwnd):
        return False
    before = _zoom_percent(win)
    ctrl_down = keys.ctrl_physically_down()
    if ctrl_down:
        sendinput.send_keys((key, False), (key, True))
    else:
        sendinput.send_keys(
            (keys.VK_CONTROL, False), (key, False),
            (key, True), (keys.VK_CONTROL, True))
        keys.consume_tap(keys.VK_CONTROL)
    for _ in range(40):
        time.sleep(0.05)
        if _zoom_percent(win) != before:
            return True
    return False


def _reset_zoom(hwnd: int, win) -> bool:
    if not paint_is_foreground(hwnd):
        return False
    before = _zoom_percent(win)
    if before == 100:
        return True
    ctrl_down = keys.ctrl_physically_down()
    if ctrl_down:
        sendinput.send_keys((ord("0"), False), (ord("0"), True))
    else:
        sendinput.send_keys(
            (keys.VK_CONTROL, False), (ord("0"), False),
            (ord("0"), True), (keys.VK_CONTROL, True))
        keys.consume_tap(keys.VK_CONTROL)
    deadline = time.perf_counter() + 2.0
    while time.perf_counter() < deadline:
        time.sleep(0.05)
        if _zoom_percent(win) == 100:
            return True
    return False


def _canvas_geometry(win, view_hwnd: int):
    image = win.child_window(
        auto_id="image", control_type="Group").wrapper_object().rectangle()
    view = win32gui.GetWindowRect(view_hwnd)
    return image, view


def _canvas_pixel_size(win) -> tuple[int, int]:
    text = win.child_window(
        auto_id="CanvasSizeTextBlock",
        control_type="Text").wrapper_object().window_text()
    size = re.findall(r"\d+", text)
    if len(size) < 2:
        raise ValueError(f"unrecognized Paint canvas size: {text!r}")
    return int(size[0]), int(size[1])


def _full_image_visible(win, image) -> bool:
    width, height = _canvas_pixel_size(win)
    zoom = _zoom_percent(win) / 100
    expected_w = round(width * zoom)
    expected_h = round(height * zoom)
    return image.width() >= expected_w - 2 \
        and image.height() >= expected_h - 2


def _resize_handle_point(win, view_hwnd: int):
    image, (view_l, view_t, view_r, view_b) = _canvas_geometry(
        win, view_hwnd)
    if not _full_image_visible(win, image):
        return None
    point = image.right + 4, image.bottom + 4
    if view_l <= point[0] < view_r and view_t <= point[1] < view_b:
        return point
    return None


def _margin_click_point(win, view_hwnd: int):
    image, (view_l, view_t, view_r, view_b) = _canvas_geometry(
        win, view_hwnd)
    if not _full_image_visible(win, image):
        return None
    gap = 30
    mid_x = (image.left + image.right) // 2
    candidates = (
        (image.right + gap, image.top + gap),
        (image.left - gap, image.top + gap),
        (mid_x, image.bottom + gap),
        (mid_x, image.top - gap),
    )
    for x, y in candidates:
        if view_l + 10 <= x < view_r - 10 \
                and view_t + 10 <= y < view_b - 10:
            return x, y
    return None


def _click_screen_point(point) -> None:
    original = win32api.GetCursorPos()
    try:
        win32api.SetCursorPos(point)
        win32api.mouse_event(
            win32con.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
        win32api.mouse_event(
            win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
    finally:
        win32api.SetCursorPos(original)


def fit_canvas_zoom(hwnd: int) -> int | None:
    """Fit the canvas quickly, then snap down to an exact pixel ratio."""
    from pywinauto import Application
    from pywinauto.timings import Timings
    Timings.window_find_timeout = 1.0
    try:
        win = Application(backend="uia").connect(handle=hwnd) \
            .window(handle=hwnd)
        before = _zoom_percent(win)
        win.child_window(
            title="Fit to window", control_type="Button").invoke()
        deadline = time.perf_counter() + 2.0
        while _zoom_percent(win) == before \
                and time.perf_counter() < deadline:
            time.sleep(0.02)
        time.sleep(0.2)
        view = win32gui.FindWindowEx(hwnd, 0, "MSPaintView", None)
        current = _zoom_percent(win)
        width, height = _canvas_pixel_size(win)
        exact_ratio = width * current % 100 == 0 \
            and height * current % 100 == 0
        while not exact_ratio or _margin_click_point(win, view) is None:
            if not _zoom_step(hwnd, win, _VK_PAGE_DOWN):
                return None
            current = _zoom_percent(win)
            exact_ratio = width * current % 100 == 0 \
                and height * current % 100 == 0
        return current
    except Exception:
        return None


def prime_canvas(hwnd: int, target_width: int,
                 target_height: int) -> tuple[int, int, int] | None:
    """Drag to the smallest canvas possible below the target frame size."""
    from pywinauto import Application
    from pywinauto.timings import Timings
    Timings.window_find_timeout = 1.0
    try:
        win = Application(backend="uia").connect(handle=hwnd) \
            .window(handle=hwnd)
        original_zoom = _zoom_percent(win)
        width, height = _canvas_pixel_size(win)
        if (width, height) == (1, 1):
            return original_zoom, width, height
        if not paint_is_foreground(hwnd) \
                or win32api.GetAsyncKeyState(win32con.VK_LBUTTON) & 0x8000:
            return None
        view = win32gui.FindWindowEx(hwnd, 0, "MSPaintView", None)
        margin = _margin_click_point(win, view)
        if margin is None and _zoom_percent(win) > 100:
            if not _reset_zoom(hwnd, win):
                return None
            margin = _margin_click_point(win, view)
        while margin is None:
            if not _zoom_step(hwnd, win, _VK_PAGE_DOWN):
                return None
            margin = _margin_click_point(win, view)
        _click_screen_point(margin)
        time.sleep(0.1)
        start = _resize_handle_point(win, view)
        if start is None:
            return None
        image, _ = _canvas_geometry(win, view)
    except Exception:
        return None

    end = image.left + 1, image.top + 1
    original = win32api.GetCursorPos()
    pressed = False
    try:
        win32api.SetCursorPos(start)
        win32api.mouse_event(
            win32con.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
        pressed = True
        for step in range(1, 13):
            x = start[0] + (end[0] - start[0]) * step // 12
            y = start[1] + (end[1] - start[1]) * step // 12
            win32api.SetCursorPos((x, y))
            time.sleep(0.005)
    finally:
        if pressed:
            win32api.mouse_event(
                win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
        win32api.SetCursorPos(original)

    for _ in range(10):
        width, height = _canvas_pixel_size(win)
        if width < target_width and height < target_height:
            return original_zoom, width, height
        time.sleep(0.05)
    return None


class _Paster:
    """Paste frames into Paint. Each new paste implicitly commits the previous
    floating selection, so no explicit per-frame commit is needed."""

    def __init__(self, hwnd: int):
        from pywinauto import Application
        from pywinauto.timings import Timings
        Timings.window_find_timeout = 1.0
        self._hwnd = hwnd
        self._win = Application(backend="uia").connect(handle=hwnd) \
            .window(handle=hwnd)
        self._crop = self._win.child_window(
            auto_id="CropButton", control_type="Button").wrapper_object()
        self._fit_after_paste = False

    def fit_zoom_after_next_paste(self) -> None:
        self._fit_after_paste = True

    def _after_paste(self) -> None:
        if not self._fit_after_paste:
            return
        fit_canvas_zoom(self._hwnd)
        self._fit_after_paste = False

    def set_zoom(self, percent: int) -> bool:
        """Set Paint's view zoom via the status-bar slider (best-effort).

        Zoom is display-only: pastes stay 1:1 with the canvas, and Paint's
        integer zoom is nearest-neighbor — so a 640x400 canvas at 200% is
        pixel-identical to pasting doubled frames at 4x the clipboard cost.
        """
        try:
            slider = self._win.child_window(
                auto_id="ZoomSliderControl",
                control_type="Slider").wrapper_object()
            slider.set_value(percent)
            return abs(slider.value() - percent) < 1
        except Exception:
            return False

    def _viewport(self):
        """The canvas scroll viewport (wrapper, (l, t, r, b)) or None."""
        try:
            sv = self._win.child_window(
                auto_id="scrollViewer", control_type="Pane").wrapper_object()
            r = sv.element_info.rectangle
            return sv, (r.left, r.top, r.right, r.bottom)
        except Exception:
            return None

    def fit_window(self, width: int, height: int) -> bool:
        """Grow the Paint window until the canvas viewport can show a full
        width x height display, clamped to the monitor work area. Returns
        True when the whole display is visible.
        """
        found = self._viewport()
        if found is None:
            return False
        sv, (vl, vt, vr, vb) = found
        if vr - vl >= width and vb - vt >= height:
            return True
        if not win32gui.IsZoomed(self._hwnd):  # maximized = as big as it gets
            wl, wt, wr, wb = win32gui.GetWindowRect(self._hwnd)
            # window chrome around the viewport, plus slack for the scrollbar
            # strip that disappears once the canvas fits
            need_w = width + (vl - wl) + (wr - vr) + _VIEW_SLACK_PX
            need_h = height + (vt - wt) + (wb - vb) + _VIEW_SLACK_PX
            mon = win32api.MonitorFromWindow(
                self._hwnd, win32con.MONITOR_DEFAULTTONEAREST)
            wa_l, wa_t, wa_r, wa_b = win32api.GetMonitorInfo(mon)["Work"]
            new_w = min(need_w, wa_r - wa_l)
            new_h = min(need_h, wa_b - wa_t)
            x = max(wa_l, min(wl, wa_r - new_w))
            y = max(wa_t, min(wt, wa_b - new_h))
            win32gui.SetWindowPos(
                self._hwnd, 0, x, y, new_w, new_h,
                win32con.SWP_NOZORDER | win32con.SWP_NOACTIVATE)
            time.sleep(0.2)  # let Paint relayout before re-measuring
            found = self._viewport()
            if found is not None:
                sv, (vl, vt, vr, vb) = found
        if vr - vl >= width and vb - vt >= height:
            return True
        # Screen too small for the whole display: show its top-left corner,
        # where pastes anchor the frame.
        try:
            sv.iface_scroll.SetScrollPercent(0.0, 0.0)
        except Exception:
            pass
        return False

    def paste(self) -> None:
        if not self.paste_uncommitted():
            return
        if self._fit_after_paste:
            if not self._wait_for_selection(True):
                raise RuntimeError("Paint did not create a pasted selection")
            self._after_paste()

    def _selection_is_active(self) -> bool:
        return self._crop.is_enabled()

    def _wait_for_selection(self, active: bool,
                            timeout: float = 0.6) -> bool:
        deadline = time.perf_counter() + timeout
        while time.perf_counter() < deadline:
            if self._selection_is_active() == active:
                return True
            time.sleep(0.002)
        return False


class KeyPaster(_Paster):
    """Preferred: paste with Ctrl+V (no menu); the next paste commits it."""

    def paste_uncommitted(self) -> bool:
        ctrl_v_paste(self._hwnd)
        return True


class MenuPaster(_Paster):
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

def _window_changed(before: "Image.Image | None",
                    after: "Image.Image | None") -> bool:
    if before is None or after is None:
        return False
    a, b = np.asarray(before), np.asarray(after)
    if a.shape != b.shape:
        return True
    return float(np.mean(np.any(a != b, axis=2))) > 0.05


def key_paste_lands(hwnd: int, width: int = 640, height: int = 400,
                    attempts: int = 4) -> bool:
    """Self-test: does a synthetic Ctrl+V actually reach this Paint?

    A landed paste becomes a floating selection, which enables Paint's Crop
    button — a UI Automation signal that's robust to the window compositing and
    PrintWindow capture quirks a screenshot diff isn't. Retries a few times
    because the first paste after launch is often a warm-up no-op. Falls back to
    a pixel-diff check if the selection state can't be queried.
    """
    if not focus_paint(hwnd):
        return False
    try:
        from pywinauto import Application
        from pywinauto.timings import Timings
        Timings.window_find_timeout = 1.0
        win = Application(backend="uia").connect(handle=hwnd) \
            .window(handle=hwnd)
        crop = win.child_window(auto_id="CropButton",
                                control_type="Button").wrapper_object()
        if crop.is_enabled():
            # A selection is already active, so "selection appeared" can't tell
            # us anything; use the pixel-diff self-test instead.
            return _pixel_paste_lands(hwnd, width, height, attempts)
    except Exception:
        return _pixel_paste_lands(hwnd, width, height, attempts)
    swatch = Image.new("RGB", (width, height), (10, 220, 80))
    for _ in range(attempts):
        if not focus_paint(hwnd):
            return False
        frame_to_clipboard(swatch)
        try:
            ctrl_v_paste(hwnd)
        except PaintNotFocusedError:
            return False
        deadline = time.perf_counter() + 0.6
        while time.perf_counter() < deadline:
            try:
                if crop.is_enabled():  # a floating selection => paste landed
                    return True
            except Exception:
                break
            time.sleep(0.02)
    return False


def _pixel_paste_lands(hwnd: int, width: int, height: int,
                       attempts: int) -> bool:
    """Fallback self-test: diff the window pixels around a paste."""
    swatches = [Image.new("RGB", (width, height), c)
                for c in ((10, 220, 80), (220, 40, 140))]
    for i in range(attempts):
        if not focus_paint(hwnd):
            return False
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


def _dismiss_dialog(dlg) -> None:
    """Dismiss the clipboard-error dialog with a UIA click on its Close button.

    Deliberately NOT via an Escape keystroke: the player may be holding Ctrl to
    fire, and a synthetic Escape then becomes Ctrl+Esc — which pops the Windows
    Start menu. A UIA invoke sends no keys, so it's safe whatever's held down.
    """
    try:
        dlg.child_window(auto_id="CloseButton",
                         control_type="Button").invoke()
    except Exception:
        pass


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
        _dismiss_dialog(dlg)
        return True
    except Exception:
        return False


def start_error_watchdog(hwnd: int) -> None:
    """Start a watchdog that polls UIA only after a paste miss."""
    def run():
        import comtypes
        try:
            comtypes.CoInitialize()
        except OSError:
            pass
        while True:
            _error_watchdog_wake.wait()
            _error_watchdog_wake.clear()
            try:
                from pywinauto import Application
                win = Application(backend="uia").connect(handle=hwnd) \
                    .window(handle=hwnd)
            except Exception:
                continue
            dlg = win.child_window(title_re=".*complete operation.*",
                                   control_type="Window")
            deadline = time.perf_counter() + 2.0
            while time.perf_counter() < deadline:
                try:
                    if dlg.exists(timeout=0.01):
                        _dismiss_dialog(dlg)
                        print("  (clipboard-error dialog dismissed)")
                        break
                except Exception:
                    break
                if _error_watchdog_wake.wait(0.03):
                    _error_watchdog_wake.clear()
                    deadline = time.perf_counter() + 2.0

    threading.Thread(target=run, daemon=True,
                     name="mspaintdoom-dlg-watchdog").start()


def arm_error_watchdog() -> None:
    _error_watchdog_wake.set()


def push_frame(paster, render_fn) -> bool:
    """Advertise the freshest frame — produced only when Paint reads the
    clipboard (render_fn is a zero-arg thunk) — and paste it."""
    previous_consumed = _clipboard().publish(render_fn=render_fn)
    paster.paste()
    return previous_consumed
