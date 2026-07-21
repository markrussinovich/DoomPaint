"""Watches Paint for File>Save / File>Open so the game's actual ZDoom state
can ride along inside the screenshot PNG (see engine_vzd.DoomEngine.save_state
/ load_state and savepng.embed_save / extract_save).

Detecting *which file* Paint just saved/opened is the hard part: Paint's own
UI doesn't expose it directly, and scraping the common file dialog's controls
is fragile across classic vs. packaged Paint (different UIA trees, different
processes). Instead this piggybacks on a Windows shell behavior that isn't
specific to Paint at all: choosing a file through the standard Open/Save
common dialog registers it in the user's shell "Recent Items" folder — that
happens inside the shared dialog itself, not app-specific code, so it's the
same for classic mspaint.exe and the packaged Windows 11 Paint app. So:

1. A background thread watches for a top-level "Open" or "Save As" window
   owned by Paint (walking the owner chain, not matching by process — the
   packaged app can show the dialog from a different process than the main
   window) to appear, then disappear (user confirmed or cancelled).
2. On disappearance, it resolves the newest .lnk in Recent Items created
   after the dialog appeared, and follows its shortcut target to get the
   actual full file path Paint just touched.
3. For subsequent plain File>Save with no dialog (saving an already-named
   document), the last resolved path's mtime is polled instead.

Best-effort throughout: if any step fails (a future Windows/Paint version
changes this), the feature just doesn't trigger -- Paint's own File>Save and
File>Open keep working completely normally either way. Untested on real
Windows/Paint as of writing; verify the dialog titles and Recent Items
behavior still hold on your build before relying on this.
"""
import glob
import os
import threading
import time

import pythoncom
import win32con
import win32gui
from win32com.client import Dispatch
from win32com.shell import shell, shellcon

_DIALOG_TITLES = (("Save As", "save"), ("Open", "load"))
_POLL_INTERVAL = 0.15
_SETTLE_TIMEOUT = 2.0


def _owned_by(hwnd_dialog: int, hwnd_owner: int) -> bool:
    """True if hwnd_dialog is (transitively) owned by hwnd_owner."""
    seen: set[int] = set()
    h = hwnd_dialog
    for _ in range(4):  # owner chains are shallow; bound the walk regardless
        owner = win32gui.GetWindow(h, win32con.GW_OWNER)
        if not owner or owner in seen:
            return False
        if owner == hwnd_owner:
            return True
        seen.add(owner)
        h = owner
    return False


def _find_dialog(hwnd_owner: int, title: str) -> "int | None":
    found: list[int] = []
    title_lower = title.lower()

    def cb(hwnd, _):
        if win32gui.IsWindowVisible(hwnd) \
                and win32gui.GetWindowText(hwnd).lower() == title_lower \
                and _owned_by(hwnd, hwnd_owner):
            found.append(hwnd)
        return True

    win32gui.EnumWindows(cb, None)
    return found[0] if found else None


def _recent_items_dir() -> "str | None":
    try:
        return shell.SHGetFolderPath(0, shellcon.CSIDL_RECENT, None, 0)
    except Exception:
        return None


def _resolve_shortcut(lnk_path: str) -> "str | None":
    try:
        target = Dispatch("WScript.Shell").CreateShortCut(lnk_path).Targetpath
        return target or None
    except Exception:
        return None


def _newest_recent_target(after: float, exts=(".png",)) -> "str | None":
    """Path targeted by the newest .lnk in Recent Items touched at/after
    `after` (a time.time() timestamp), if it points at one of `exts`.

    Windows doesn't always bump a Recent Items shortcut's mtime for a
    *repeat* open/save of a file already at the front of the MRU list (the
    entry may just get silently reordered) -- a strict `mtime >= after`
    filter then finds nothing, even though the dialog genuinely just closed
    on that file. So this prefers a match at/after `after`, but falls back
    to the single most-recently-touched matching entry overall, bounded to
    a sane recency window, rather than giving up.
    """
    recent_dir = _recent_items_dir()
    if not recent_dir:
        return None
    fallback_floor = after - 60.0  # tolerate an untouched-mtime repeat pick
    best_after, best_after_mtime = None, after
    best_any, best_any_mtime = None, fallback_floor
    for lnk in glob.glob(os.path.join(recent_dir, "*.lnk")):
        try:
            mtime = os.path.getmtime(lnk)
        except OSError:
            continue
        if mtime < fallback_floor:
            continue
        target = _resolve_shortcut(lnk)
        if not target or not target.lower().endswith(exts):
            continue
        if mtime >= after and mtime >= best_after_mtime:
            best_after, best_after_mtime = target, mtime
        if mtime >= best_any_mtime:
            best_any, best_any_mtime = target, mtime
    return best_after or best_any


def _wait_file_settled(path: str, timeout: float = _SETTLE_TIMEOUT) -> bool:
    """Block until `path` stops changing size/mtime for ~0.2s (Paint has
    finished writing it), or the file simply already exists and is idle."""
    deadline = time.monotonic() + timeout
    last_stat, stable_since = None, None
    while time.monotonic() < deadline:
        try:
            st = os.stat(path)
            stat_key = (st.st_size, st.st_mtime)
        except OSError:
            time.sleep(0.1)
            continue
        if stat_key == last_stat:
            stable_since = stable_since or time.monotonic()
            if time.monotonic() - stable_since > 0.2:
                return True
        else:
            last_stat, stable_since = stat_key, None
        time.sleep(0.1)
    return os.path.exists(path)


class SaveLoadWatcher:
    """Background detector; the main loop drains pending saves/loads each
    frame and performs the actual engine save/load itself, since ViZDoom's
    DoomGame isn't safe to touch from a second thread concurrently with the
    main loop's own engine.step() calls.
    """

    def __init__(self, hwnd: int):
        self._hwnd = hwnd
        self._lock = threading.Lock()
        self._known_path: "str | None" = None
        self._known_mtime: "float | None" = None
        self._pending_save: "str | None" = None
        self._pending_load: "str | None" = None
        self._stop = threading.Event()
        threading.Thread(target=self._run, daemon=True,
                         name="mspaintdoom-saveload-watch").start()

    def _run(self) -> None:
        try:
            pythoncom.CoInitialize()
        except OSError:
            pass
        while not self._stop.is_set():
            try:
                self._poll_dialogs()
                self._poll_known_path()
            except Exception:
                pass  # a watcher hiccup must never take down the game loop
            time.sleep(_POLL_INTERVAL)

    def _poll_dialogs(self) -> None:
        for title, kind in _DIALOG_TITLES:
            if self._stop.is_set():
                return
            if _find_dialog(self._hwnd, title) is None:
                continue
            opened_at = time.time()
            while _find_dialog(self._hwnd, title) is not None \
                    and not self._stop.is_set():
                time.sleep(_POLL_INTERVAL)
            # Let the shell register the Recent Items entry. For a brand-new
            # Save As, that registration can lag behind the dialog closing
            # (Windows adds it once the app finishes writing the file), so
            # retry for a few seconds rather than checking once and giving up
            # -- a single early miss here used to silently drop every save.
            deadline = time.monotonic() + _SETTLE_TIMEOUT
            path = None
            while time.monotonic() < deadline and not self._stop.is_set():
                path = _newest_recent_target(opened_at)
                if path:
                    break
                time.sleep(_POLL_INTERVAL)
            if path is None:
                continue  # cancelled, or Recent Items didn't pick it up
            if kind == "save":
                _wait_file_settled(path)
            with self._lock:
                self._known_path = path
                self._known_mtime = None  # re-baselined by _poll_known_path
                if kind == "save":
                    self._pending_save = path
                else:
                    self._pending_load = path

    def _poll_known_path(self) -> None:
        with self._lock:
            path = self._known_path
        if not path:
            return
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            return
        with self._lock:
            if self._known_mtime is None:
                self._known_mtime = mtime
            elif mtime > self._known_mtime:
                self._known_mtime = mtime
                self._pending_save = path

    def take_pending_save(self) -> "str | None":
        with self._lock:
            path, self._pending_save = self._pending_save, None
            return path

    def take_pending_load(self) -> "str | None":
        with self._lock:
            path, self._pending_load = self._pending_load, None
            return path

    def note_written(self, path: str) -> None:
        """Call right after rewriting `path` ourselves (embedding save data
        into it) so the next mtime poll doesn't mistake our own write for
        the user saving again -- without this, embedding changes the file's
        mtime, which re-queues a pending save, whose embed changes the mtime
        again, forever.
        """
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            return
        with self._lock:
            if self._known_path == path:
                self._known_mtime = mtime

    def close(self) -> None:
        self._stop.set()
