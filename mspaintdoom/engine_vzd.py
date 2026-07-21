"""Doom engine wrapper (ViZDoom + bundled Freedoom WADs)."""
import os
import shutil
import tempfile
import threading
import time

import numpy as np
import vizdoom as vzd
import win32con
import win32gui

# Order must match keys._ACTION_KEYS.
BUTTONS = (
    vzd.Button.MOVE_FORWARD,
    vzd.Button.MOVE_BACKWARD,
    vzd.Button.TURN_LEFT,
    vzd.Button.TURN_RIGHT,
    vzd.Button.ATTACK,
    vzd.Button.USE,
    vzd.Button.SPEED,
    vzd.Button.MOVE_LEFT,
    vzd.Button.MOVE_RIGHT,
)

TICRATE = 35  # Doom's fixed simulation rate, tics per second

# Selectable render resolutions. Smaller frames paste into Paint dramatically
# faster (Paint's per-frame composite cost is pixel-bound): 320x200 — Doom's
# authentic native resolution — roughly triples the achievable frame rate over
# 640x400 with lower latency, at the cost of a chunkier (but period-accurate)
# picture that Paint's fit-zoom scales up to the same on-screen size.
RESOLUTIONS = {
    "320x200": "RES_320X200",
    "320x240": "RES_320X240",
    "640x400": "RES_640X400",
    "640x480": "RES_640X480",
}


def _suppress_engine_window(stop: threading.Event) -> None:
    """Hide ViZDoom's window the moment it's created.

    set_window_visible(False) is honored only after init: the engine still
    creates and briefly shows its native window during startup, which flashes
    on screen. Shove it off-screen and hide it as soon as it exists.
    """
    while not stop.is_set():
        hwnd = win32gui.FindWindow("ViZDoomMainWindow", None)
        if hwnd:
            win32gui.SetWindowPos(
                hwnd, 0, -32000, -32000, 0, 0,
                win32con.SWP_NOSIZE | win32con.SWP_NOZORDER
                | win32con.SWP_NOACTIVATE | win32con.SWP_HIDEWINDOW)
        time.sleep(0.005)


class DoomEngine:
    def __init__(self, wad: int = 1, doom_map: str = "E1M1", skill: int = 3,
                 sound: bool = True, game_wad: str | None = None,
                 resolution: str = "640x400"):
        self._game = g = vzd.DoomGame()
        wad_dir = os.path.dirname(vzd.__file__)
        # game_wad overrides the bundled Freedoom (e.g. shareware doom1.wad —
        # ViZDoom's engine fork accepts it alongside its pk3).
        self.wad_path = game_wad or os.path.join(wad_dir, f"freedoom{wad}.wad")
        g.set_doom_game_path(self.wad_path)
        g.set_doom_map(doom_map)
        g.set_doom_skill(skill)
        # Create the engine window off-screen: window_visible(False) is only
        # honored after init, so without this the window flashes at startup.
        # Pin sfx volume explicitly — ZDoom persists cvars to _vizdoom.ini on
        # exit, so a stray +snd_sfxvolume 0 from a past run would otherwise
        # mute every session after it, silently. Max it out: the MIDI music
        # renders much hotter than the engine's effects, so SFX need all the
        # headroom they can get (music is balanced via its session volume).
        g.add_game_args("+win_x -32000 +win_y -32000 +snd_sfxvolume 1")
        g.set_screen_resolution(
            getattr(vzd.ScreenResolution, RESOLUTIONS[resolution]))
        g.set_screen_format(vzd.ScreenFormat.RGB24)
        g.set_window_visible(False)
        g.set_mode(vzd.Mode.PLAYER)
        g.set_render_hud(True)
        g.set_render_weapon(True)
        g.set_episode_timeout(0)
        for b in BUTTONS:
            g.add_available_button(b)
        g.set_sound_enabled(sound)
        hide_done = threading.Event()
        hider = threading.Thread(target=_suppress_engine_window,
                                 args=(hide_done,), daemon=True)
        hider.start()
        # ZDoom resolves its savegame (and _vizdoom.ini config) directory
        # from the process's current working directory at init() time — no
        # command-line flag (-savedir / +save_dir) overrides it, and it's
        # cached rather than re-read per save, so a temporary chdir bracketing
        # just the init() call isolates saves into their own directory
        # without disturbing the caller's cwd for anything else (all game/WAD
        # paths above are already absolute, so this doesn't affect loading).
        self._save_dir = tempfile.mkdtemp(prefix="doompaint_save_")
        orig_cwd = os.getcwd()
        try:
            os.chdir(self._save_dir)
            try:
                try:
                    g.init()
                except vzd.ViZDoomErrorException:
                    if not sound:
                        raise
                    # Audio backend can be missing (e.g. no output device); retry.
                    print("  (audio init failed — running without sound effects)")
                    g.set_sound_enabled(False)
                    g.init()
                time.sleep(0.2)  # window may show a beat after init returns
            finally:
                hide_done.set()
        finally:
            os.chdir(orig_cwd)
        self._last_frame = self._grab()

    def _grab(self) -> np.ndarray:
        state = self._game.get_state()
        if state is not None:
            self._last_frame = state.screen_buffer
        return self._last_frame

    def step(self, action: list[int], tics: int) -> np.ndarray:
        """Advance the simulation `tics` tics under `action`; return the frame."""
        if self._game.is_episode_finished():
            self._game.new_episode()  # death or level end: restart map
        self._game.make_action(list(action), max(1, tics))
        return self._grab()

    def save_state(self, slot: int = 1) -> bytes:
        """Snapshot full engine state via ZDoom's native savegame.

        Unlike the pasted frame, this is a real save: position, health,
        ammo/inventory, level, and monster/door/item state, all restorable
        with load_state(). Returns the raw .zds bytes (a small zip archive)
        so the caller can stash them anywhere (e.g. embedded in a PNG).
        """
        path = os.path.join(self._save_dir, f"{slot}.zds")
        if os.path.exists(path):
            os.remove(path)
        self._game.send_game_command(f"save {slot} doompaint")
        noop = [0] * len(BUTTONS)
        deadline = time.monotonic() + 2.0
        while not os.path.exists(path) and time.monotonic() < deadline:
            self._game.make_action(noop, 1)
        if not os.path.exists(path):
            raise RuntimeError("ZDoom did not produce a save file")
        with open(path, "rb") as f:
            data = f.read()
        os.remove(path)
        return data

    def load_state(self, data: bytes, slot: int = 1) -> np.ndarray:
        """Restore engine state from bytes previously returned by save_state().

        Position and velocity are both restored, so gameplay resumes exactly
        as it was mid-motion (a few tics of momentum settling afterward is
        correct behavior, not a bug).
        """
        path = os.path.join(self._save_dir, f"{slot}.zds")
        with open(path, "wb") as f:
            f.write(data)
        self._game.send_game_command(f"load {slot}")
        noop = [0] * len(BUTTONS)
        for _ in range(3):  # let the load actually take effect
            self._game.make_action(noop, 1)
        os.remove(path)
        return self._grab()

    def reset_sound(self) -> None:
        """Re-initialize the engine's sound system (ZDoom's snd_reset).

        The engine's OpenAL init can come up silent (device race at boot);
        this rebuilds it in-place without touching game state.
        """
        try:
            self._game.send_game_command("snd_reset")
        except Exception:
            pass

    def close(self) -> None:
        self._game.close()
        shutil.rmtree(self._save_dir, ignore_errors=True)
