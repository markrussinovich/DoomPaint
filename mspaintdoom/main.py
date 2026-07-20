"""MS Paint Doom: real engine, Paint canvas as the monitor.

Usage:  python -m mspaintdoom.main [--map E1M1] [--wad 1|2] [--scale N]
                                   [--skill 1-5] [--no-sound]
"""
import argparse
import os
import re
import sys
import threading
import time

from . import keys, paint_out
from . import music as music_mod
from .engine_vzd import TICRATE, DoomEngine
from .music import MusicPlayer

MAX_TICS_PER_FRAME = 7  # cap catch-up so slow pastes slow the game, not warp it


# Delayed clipboard rendering tells us directly whether Paint requested the
# previous frame. Only demote after a sustained run of dropped Ctrl+V pastes.
PASTE_MISSES_BEFORE_FALLBACK = 8


class OnDemandRenderer:
    """Produce the frame at the moment Paint reads the clipboard.

    The engine is only ever touched from ONE thread (ViZDoom is not
    thread-safe):
      * pull  -> engine.step runs inside the clipboard render callback, so the
                 displayed frame is generated the instant Paint reads it
                 (lowest latency, but the simulation only advances on reads).
      * push  -> a dedicated engine thread free-runs at the tic rate; the read
                 callback just returns the most recent pre-encoded frame
                 (smooth simulation, latency ~= one tic).
    """

    def __init__(self, engine, scale, mode, max_tics):
        self._engine = engine
        self._scale = scale
        self._mode = mode
        self._max_tics = max_tics
        self._action = [0] * 9
        self._last = time.perf_counter()
        self._paused = False
        self._reset_sound = False
        self._latest_dib = b""
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._new_frame = threading.Event()  # set when a fresh tic frame exists
        if mode == "push":
            try:
                self._latest_dib = self._step_encode()  # seed so the very
                self._new_frame.set()                    # first read is never
            except Exception:                            # empty (cold-start)
                pass
            threading.Thread(target=self._push_loop, daemon=True,
                             name="mspaintdoom-engine").start()

    def set_action(self, action) -> None:
        self._action = action

    def set_paused(self, paused: bool) -> None:
        if self._paused and not paused:
            self._last = time.perf_counter()  # don't bank time spent paused
        self._paused = paused

    def request_reset_sound(self) -> None:
        self._reset_sound = True

    def stop(self) -> None:
        self._stop.set()

    def _step_encode(self) -> bytes:
        now = time.perf_counter()
        tics = min(self._max_tics, max(1, round((now - self._last) * TICRATE)))
        self._last = now
        if self._reset_sound:
            self._reset_sound = False
            self._engine.reset_sound()
        frame = self._engine.step(self._action, tics)
        return paint_out.encode_frame(frame, self._scale)  # immutable bytes

    def render_fn(self) -> bytes:
        if self._mode == "pull":
            return self._step_encode()
        with self._lock:
            return self._latest_dib

    def wait_new_frame(self, timeout: float) -> bool:
        """Block until the engine has produced a new frame (i.e. a tic
        advanced), so the paste loop never submits faster than the simulation
        actually moves — the tic rate is the cap, with no separate constant to
        keep in sync. In pull mode the frame is rendered inside Paint's read, so
        every read is inherently fresh and this returns at once."""
        if self._mode != "push":
            return True
        got = self._new_frame.wait(timeout)
        self._new_frame.clear()
        return got

    def _push_loop(self) -> None:
        period = 1.0 / TICRATE
        nxt = time.perf_counter()
        while not self._stop.is_set():
            if not self._paused:
                try:
                    dib = self._step_encode()
                    with self._lock:
                        self._latest_dib = dib
                    self._new_frame.set()  # a tic advanced; a fresh frame is up
                except Exception:
                    pass
            nxt += period
            slack = nxt - time.perf_counter()
            if slack > 0:
                time.sleep(slack)
            else:
                nxt = time.perf_counter()


def run() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--map", default="E1M1", help="E1M1.. (wad 1), MAP01.. (wad 2)")
    ap.add_argument("--wad", type=int, choices=(1, 2), default=1,
                    help="1=Freedoom Phase 1, 2=Phase 2")
    ap.add_argument("--scale", type=int, default=1, choices=(1, 2),
                    help="integer upscale of the 640x400 frame")
    ap.add_argument("--res", choices=("320x200", "320x240", "640x400",
                                      "640x480"), default="640x400",
                    help="engine render resolution (default 640x400). "
                         "320x200 is Doom's native res and pastes ~2.5x faster "
                         "(up to the 35 Hz tic rate), but square pixels look "
                         "stretched; 320x240 is the aspect-correct 4:3 view "
                         "(~20 fps); 640x* are sharp but slow (~11-13 fps)")
    ap.add_argument("--skill", type=int, default=3, choices=range(1, 6))
    ap.add_argument("--no-sound", action="store_true",
                    help="disable all audio (effects and music)")
    ap.add_argument("--no-music", action="store_true",
                    help="disable the looping map music, keep sound effects")
    ap.add_argument("--music-volume", type=int, default=40, metavar="0-100",
                    help="music loudness relative to sound effects "
                         "(default 40; the MIDI synth runs hot)")
    ap.add_argument("--music-wad", default=None, metavar="PATH",
                    help="WAD to take the soundtrack from — point it at a "
                         "commercial doom.wad you own for the original "
                         "tracks (wad\\doom.wad / wad\\doom2.wad are "
                         "auto-detected). Game data stays Freedoom.")
    ap.add_argument("--render-mode", choices=("eager", "pull", "push"),
                    default="push",
                    help="when to produce each frame: 'push' (default) "
                         "free-runs the engine on its own thread and serves "
                         "the freshest frame on Paint's clipboard read; 'pull' "
                         "runs the engine inside the read (lowest latency, but "
                         "the simulation only advances on reads); 'eager' "
                         "encodes at submit time (legacy behaviour)")
    args = ap.parse_args()

    # Session log: what the game actually saw, for post-mortem diagnosis.
    log_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "last_run.log")
    session_log = open(log_path, "w", buffering=1, encoding="utf-8")

    def log(msg: str) -> None:
        session_log.write(f"[{time.strftime('%H:%M:%S')}] {msg}\n")

    log(f"boot args={vars(args)}")
    log("pacing: engine-tic + Paint-readiness (no rate cap)")

    # Game data: prefer the real (shareware) DOOM WAD when it covers the
    # requested map — shareware is episode 1 only; Freedoom fills the rest.
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    shareware = os.path.join(repo, "wad", "doom1.wad")
    game_wad = None
    if args.wad == 1 and args.map.upper().startswith("E1") \
            and os.path.exists(shareware):
        game_wad = shareware
        print("Booting DOOM (the real shareware DOOM.WAD)...")
    else:
        print("Booting Doom (this is real Doom; the WAD is Freedoom)...")
    log(f"game wad: {game_wad or 'freedoom'}")
    engine = DoomEngine(wad=args.wad, doom_map=args.map, skill=args.skill,
                        sound=not args.no_sound, game_wad=game_wad,
                        resolution=args.res)
    device_changed_at = [0.0]
    if not args.no_sound:
        status = music_mod.audio_output_status()
        if status:
            print(f"Audio out: {status}")
            pct = re.search(r"at (\d+)%", status)
            if "MUTED" in status or (pct and int(pct.group(1)) == 0):
                print("  (that output is muted/zero — you won't hear a thing)")
            elif pct and int(pct.group(1)) < 25:
                print("  (heads up: master volume is quite low)")
        # Windows persists per-app volumes; repair the engine's SFX session
        # in case a past run (or a mixer tweak) left it silenced.
        music_mod.set_session_volume(1.0, exe="vizdoom.exe")
        # All streams should follow a default-device change automatically
        # (music via winmm; effects via the upgraded OpenAL-Soft 1.24, which
        # tracks the default). Note it anyway, with the recovery path.
        def on_device_change():
            device_changed_at[0] = time.perf_counter()
            print("  (default audio device changed — audio should follow; "
                  "if sound effects vanish, F12 and relaunch run.bat)")
        music_mod.watch_default_device(on_device_change)

    music = MusicPlayer(volume=args.music_volume / 100)
    if not (args.no_sound or args.no_music):
        music_wad = args.music_wad
        if not music_wad:
            wad_dir = os.path.join(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                "wad")
            names = ("doom.wad", "doom1.wad") if args.wad == 1 \
                else ("doom2.wad",)
            music_wad = engine.wad_path
            for name in names:
                local = os.path.join(wad_dir, name)
                if os.path.exists(local):
                    music_wad = local
                    break

        # Opening the MIDI sequencer can take seconds; don't hold up boot.
        def music_boot():
            if music.start(music_wad, args.map):
                which = ("original soundtrack"
                         if music_wad != engine.wad_path else "map track")
                print(f"  (music: looping the {which} via Windows MIDI)")
            elif music_wad != engine.wad_path \
                    and music.start(engine.wad_path, args.map):
                print("  (music: track missing in the music WAD; using the "
                      "Freedoom one)")
            else:
                print("  (music: no track found for this map)")
        threading.Thread(target=music_boot, daemon=True,
                         name="music-boot").start()
    print("Finding MS Paint...")
    print("Paste pacing: engine-tic paced (scales to your hardware)")
    hwnd = paint_out.launch_paint()
    paint_out.focus_paint(hwnd)

    frame0 = engine.step([0] * 9, 1)  # warm up one tic
    if paint_out.dismiss_error_dialog(hwnd):
        print("Dismissed a leftover Paint error dialog.")
    paint_out.start_error_watchdog(hwnd)
    disp_w = frame0.shape[1] * args.scale
    disp_h = frame0.shape[0] * args.scale
    canvas_prime = paint_out.prime_canvas(hwnd, disp_w, disp_h)
    if canvas_prime is not None:
        original_zoom, primed_w, primed_h = canvas_prime
        print(f"Canvas primed at {primed_w}x{primed_h} "
              f"(first paste expands to "
              f"{disp_w}x{disp_h})")
        log(f"canvas primed at {primed_w}x{primed_h} for "
            f"{disp_w}x{disp_h} frames "
            f"(original zoom: {original_zoom}%)")
    else:
        original_zoom = None
        print("  (couldn't prime the canvas — frames will still auto-grow it)")
        log("canvas priming failed")

    # Prefer Ctrl+V (opens no menu, never diverts the player's keystrokes); fall
    # back to the Edit>Paste menu only if synthetic keys don't reach this Paint.
    # A subsequent paste commits the previous floating selection automatically.
    key_paste_works = paint_out.key_paste_lands(hwnd, disp_w, disp_h)
    if key_paste_works:
        paster = paint_out.KeyPaster(hwnd)
        print("Paste mode: Ctrl+V (no menus)")
    else:
        paster = paint_out.MenuPaster(hwnd)
        print("Paste mode: UIA menu fallback (Ctrl+V didn't register here)")
    if original_zoom is not None:
        if key_paste_works:
            restored_zoom = paint_out.fit_canvas_zoom(hwnd)
            if restored_zoom is not None:
                print(f"Canvas fitted at {restored_zoom}%")
                log(f"canvas zoom fitted to {restored_zoom}%")
        else:
            paster.fit_zoom_after_next_paste()

    # Capture game keys before Paint sees them: a stray arrow key in Paint
    # dismisses the paste menu / drags the pasted selection and stalls frames.
    if keys.install_hook(lambda: paint_out.paint_is_foreground(hwnd)):
        print("Input capture: on (game keys won't reach Paint while playing)")
    else:
        print("Input capture: unavailable — keys may leak into Paint")

    print()
    print("  DOOM IS RUNNING IN MS PAINT.")
    print(f"  Controls: {keys.CONTROLS_HELP}")
    print("  Keep Paint focused to play; alt-tab pauses the game.")
    print("  Party trick: Ctrl+Z in Paint rewinds time.")
    print()

    frames = 0
    dropped = 0
    last_action = None
    was_paused = False
    total_frames = 0
    fires_in_window = 0
    sound_resets = 0
    sfx_sampler = None if args.no_sound else music_mod.SessionPeakSampler()
    paste_misses = 0
    renderer = None
    if args.render_mode != "eager":
        renderer = OnDemandRenderer(engine, args.scale, args.render_mode,
                                    MAX_TICS_PER_FRAME)
        print(f"Render mode: {args.render_mode} (frame produced on Paint's "
              f"clipboard read)")
    # No frame timer: the loop is gated purely by engine tics + Paint readiness
    # (see the loop body), so it self-scales to the hardware.
    stat_t0 = time.perf_counter()
    last = time.perf_counter()
    try:
        while True:
            if keys.quit_requested():
                print("F12 — quitting.")
                return 0
            if not (paint_out.paint_is_foreground(hwnd)
                    or os.environ.get("MSPAINTDOOM_NOFOCUS")):
                if not was_paused:
                    log("paused (Paint lost focus)")
                    was_paused = True
                music.pause()
                if renderer:
                    renderer.set_paused(True)
                last = time.perf_counter()  # don't bank time while paused
                time.sleep(0.10)
                continue
            if was_paused:
                log("resumed (Paint focused)")
                was_paused = False
            music.resume()
            if renderer:
                renderer.set_paused(False)

            action = keys.poll_action()
            if renderer:
                renderer.set_action(action)
            if action[4] and not (last_action and last_action[4]):
                fires_in_window += 1
            if action != last_action:
                pressed = [n for n, a in zip(
                    ("fwd", "back", "left", "right", "FIRE", "use", "run",
                     "strafeL", "strafeR"), action) if a]
                log(f"input: {'+'.join(pressed) or '(none)'}")
                if os.environ.get("MSPAINTDOOM_DEBUG"):
                    print(f"  input: {'+'.join(pressed) or '(none)'}")
                last_action = action
            now = time.perf_counter()
            if renderer is None:
                tics = min(MAX_TICS_PER_FRAME,
                           max(1, round((now - last) * TICRATE)))
                last = now
                frame = engine.step(action, tics)
            else:
                frame = None  # produced on demand when Paint reads
            # Pace on the two things that actually gate a new frame — no timer:
            #   (b) the engine advanced a tic (a fresh frame exists), and
            #   (a) Paint finished reading the previous frame (publish() waits
            #       on its GetData below).
            # Their combination self-scales to the hardware: min(tic rate,
            # Paint's composite rate), with no rate constant to keep in sync.
            if renderer is not None and not renderer.wait_new_frame(0.25):
                continue  # engine produced nothing new (paused/stalled)
            try:
                previous_consumed = paint_out.push_frame(
                    hwnd, frame, paster, scale=args.scale,
                    render_fn=(renderer.render_fn if renderer else None))
            except paint_out.PaintNotFocusedError:
                continue  # user tabbed away mid-frame; loop back to pause
            except Exception as e:
                # Clipboard contention, UIA hiccup, Paint UI mid-rebuild:
                # drop the frame and keep playing, never crash the game.
                dropped += 1
                if dropped % 25 == 1:
                    print(f"  (frame dropped: {type(e).__name__}: {e})")
                continue

            frames += 1
            total_frames += 1
            if total_frames % 50 == 0:
                log(f"{total_frames} frames pushed")
                # Self-heal: firing with a dead-silent engine session means
                # the engine's audio init came up broken — rebuild it.
                if sfx_sampler is not None:
                    peak = sfx_sampler.take()
                    log(f"engine sfx peak this window: {peak:.3f} "
                        f"(fires: {fires_in_window})")
                    settling = time.perf_counter() - device_changed_at[0] < 15
                    if fires_in_window >= 3 and peak < 0.01 \
                            and not settling and sound_resets < 5:
                        sound_resets += 1
                        log(f"engine silent despite firing — snd_reset "
                            f"#{sound_resets}")
                        print("  (engine audio is silent — resetting the "
                              "engine sound system)")
                        if renderer:
                            renderer.request_reset_sound()
                        else:
                            engine.reset_sound()
                    fires_in_window = 0
            if isinstance(paster, paint_out.KeyPaster):
                if previous_consumed:
                    paste_misses = 0
                else:
                    paste_misses += 1
                    paint_out.arm_error_watchdog()
                    log(f"Ctrl+V clipboard miss {paste_misses}/"
                        f"{PASTE_MISSES_BEFORE_FALLBACK}")
                    if paste_misses >= PASTE_MISSES_BEFORE_FALLBACK:
                        if paint_out.dismiss_error_dialog(hwnd):
                            print("  (dismissed Paint's clipboard-error "
                                  "dialog; retrying Ctrl+V)")
                            paste_misses = 0
                        else:
                            print("Ctrl+V missed "
                                  f"{PASTE_MISSES_BEFORE_FALLBACK} "
                                  "consecutive pastes — switching to the "
                                  "UIA menu paster.")
                            paster = paint_out.MenuPaster(hwnd)
                            paste_misses = 0
            if frames % 50 == 0:
                dt = time.perf_counter() - stat_t0
                print(f"  {frames / dt:4.1f} pastes/s submitted")
                frames, stat_t0 = 0, time.perf_counter()
    except KeyboardInterrupt:
        return 0
    finally:
        try:
            paster.commit_existing_selection()
            paster.wait_ready()
        except Exception as e:
            print(f"  (couldn't commit final frame: {e})")
        if renderer:
            renderer.stop()
        music.stop()
        # Finalize while the engine is still alive: in pull mode this renders
        # one last real frame on demand instead of leaving a dead promise.
        paint_out.release_clipboard()
        engine.close()
        print("Doom has left the canvas.")


if __name__ == "__main__":
    sys.exit(run())
