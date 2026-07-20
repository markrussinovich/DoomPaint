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

import numpy as np

from . import capture, keys, paint_out
from . import music as music_mod
from .engine_vzd import TICRATE, DoomEngine
from .music import MusicPlayer

MAX_TICS_PER_FRAME = 7  # cap catch-up so slow pastes slow the game, not warp it

# Ctrl+V pastes can silently stop registering in some Paint builds even after
# passing the boot self-test. Every N frames, compare the Paint window to the
# previous check; if the engine kept producing new frames but the canvas froze
# for STRIKES consecutive checks, demote to the (slower, reliable) menu paster.
PASTE_CHECK_EVERY = 10
PASTE_STALE_STRIKES = 2


def run() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--map", default="E1M1", help="E1M1.. (wad 1), MAP01.. (wad 2)")
    ap.add_argument("--wad", type=int, choices=(1, 2), default=1,
                    help="1=Freedoom Phase 1, 2=Phase 2")
    ap.add_argument("--scale", type=int, default=1, choices=(1, 2),
                    help="on-screen upscale of the 640x400 frame (done via "
                         "Paint's view zoom when available, so it's free)")
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
    args = ap.parse_args()

    # Session log: what the game actually saw, for post-mortem diagnosis.
    log_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "last_run.log")
    session_log = open(log_path, "w", buffering=1, encoding="utf-8")

    def log(msg: str) -> None:
        session_log.write(f"[{time.strftime('%H:%M:%S')}] {msg}\n")

    log(f"boot args={vars(args)}")

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
                        sound=not args.no_sound, game_wad=game_wad)
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
    hwnd = paint_out.launch_paint()
    paint_out.focus_paint(hwnd)

    frame0 = engine.step([0] * 9, 1)  # warm up one tic
    if paint_out.dismiss_error_dialog(hwnd):
        print("Dismissed a leftover Paint error dialog.")
    paint_out.start_error_watchdog(hwnd)
    # Prefer Ctrl+V (opens no menu, never diverts the player's keystrokes); fall
    # back to the Edit>Paste menu only if synthetic keys don't reach this Paint.
    # Either way, commit is via the Brushes tool, never a synthetic Esc.
    if paint_out.key_paste_lands(hwnd):
        paster = paint_out.KeyPaster(hwnd)
        print("Paste mode: Ctrl+V (no menus)")
    else:
        paster = paint_out.MenuPaster(hwnd)
        print("Paste mode: UIA menu fallback (Ctrl+V didn't register here)")

    # Scale via Paint's own view zoom when we can: zoom is display-only and
    # nearest-neighbor, so a 640x400 canvas at 200% looks pixel-identical to
    # pasting doubled frames — but each paste moves 4x less data, so scale 2
    # runs at scale 1 framerate.
    paste_scale = args.scale
    if args.scale > 1:
        if paster.set_zoom(args.scale * 100):
            paste_scale = 1
            print(f"Scaling via Paint's {args.scale * 100}% view zoom "
                  "(frames paste at 1x — full framerate)")
            log(f"view zoom {args.scale * 100}%; pasting 1x frames")
        else:
            print("  (zoom control not found — pasting scaled frames)")
            log("zoom unavailable; pasting scaled frames")

    # Size the canvas to exactly the pasted frame, so frames fill it edge to
    # edge with no leftover white margins.
    canvas_w = frame0.shape[1] * paste_scale
    canvas_h = frame0.shape[0] * paste_scale
    if paster.size_canvas(canvas_w, canvas_h):
        print(f"Canvas sized to the Doom display ({canvas_w}x{canvas_h})")
        log(f"canvas sized to {canvas_w}x{canvas_h}")
    else:
        print("  (couldn't size the canvas — frames will still auto-grow it)")
        log("canvas sizing failed")

    # Make sure the whole display (canvas x zoom) is visible in the window.
    disp_w = frame0.shape[1] * args.scale
    disp_h = frame0.shape[0] * args.scale
    if paster.fit_window(disp_w, disp_h):
        log(f"window fits the {disp_w}x{disp_h} display")
    else:
        print(f"  (screen too small to show the whole {disp_w}x{disp_h} "
              "display — showing its top-left)")
        log("window could not fit the display")

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
    stale_strikes = 0
    engine_moved = False
    prev_frame = None
    check_img = None
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
                last = time.perf_counter()  # don't bank time while paused
                time.sleep(0.10)
                continue
            if was_paused:
                log("resumed (Paint focused)")
                was_paused = False
            music.resume()

            action = keys.poll_action()
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
            tics = min(MAX_TICS_PER_FRAME, max(1, round((now - last) * TICRATE)))
            last = now
            frame = engine.step(action, tics)
            if prev_frame is not None and not np.array_equal(frame, prev_frame):
                engine_moved = True
            prev_frame = frame
            try:
                paint_out.push_frame(hwnd, frame, paster, scale=paste_scale)
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
                        engine.reset_sound()
                    fires_in_window = 0
            if total_frames % PASTE_CHECK_EVERY == 0:
                img = capture.grab_window(hwnd)
                if img is not None:
                    if engine_moved and paint_out.window_stale(check_img, img):
                        # Frames aren't landing. Usual suspect: the modal
                        # "Can't complete operation" clipboard-error dialog.
                        if paint_out.dismiss_error_dialog(hwnd):
                            print("  (dismissed Paint's clipboard-error "
                                  "dialog)")
                            stale_strikes = 0
                        elif isinstance(paster, paint_out.KeyPaster):
                            stale_strikes += 1
                            if stale_strikes >= PASTE_STALE_STRIKES:
                                print("Ctrl+V pastes stopped landing — "
                                      "switching to the UIA menu paster.")
                                paster = paint_out.MenuPaster(hwnd)
                                stale_strikes = 0
                    else:
                        stale_strikes = 0
                    check_img = img
                    engine_moved = False
            if frames % 50 == 0:
                dt = time.perf_counter() - stat_t0
                print(f"  {frames / dt:4.1f} fps (paint-side)")
                frames, stat_t0 = 0, time.perf_counter()
    except KeyboardInterrupt:
        return 0
    finally:
        music.stop()
        engine.close()
        paint_out.release_clipboard()  # leave real bytes, not a dead promise
        print("Doom has left the canvas.")


if __name__ == "__main__":
    sys.exit(run())
