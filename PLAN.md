# MS Paint Doom — Plan

Run the real Doom engine and use **Microsoft Paint as the monitor**: every frame is
pasted into the Paint canvas as an actual document edit, so Paint's undo history
becomes a demo recording (Ctrl+Z steps backward through the game).

## Architecture

```
┌─────────────┐   frame (numpy RGB)   ┌──────────────┐   DIB on clipboard    ┌──────────┐
│ Doom engine  │ ────────────────────▶ │ paint_out.py │ ─────Ctrl+V──────────▶ │ MS Paint │
│ (real engine)│                       └──────────────┘                        │  canvas  │
│              │ ◀──────────────────── keys (GetAsyncKeyState polling) ◀────── │ (focused)│
└─────────────┘                                                                └──────────┘
```

### 1. Engine

Two candidates, tried in order:

- **cydoomgeneric** (preferred): Python bindings for `doomgeneric`, the canonical
  "port Doom anywhere" fork of the original engine. Full game — menus, HUD, demos.
  Caveat: builds from source with Cython, needs MSVC on Windows.
- **vizdoom** (fallback): ZDoom-based engine with prebuilt Windows wheels and a
  bundled Freedoom WAD. Frame buffers and actions exposed programmatically; less
  "authentic" (no title screen menu flow) but bulletproof to install.

### 2. Display: Paint as framebuffer

- Launch/attach to `mspaint.exe`, find its window via Win32.
- Per frame: downscale/upscale to a fixed size (target 640×400), write to the
  Windows clipboard as a DIB, then send Ctrl+V followed by Esc (deselect) to Paint.
- **Focus guard**: before any synthetic keystroke, verify the foreground window is
  Paint; if the user has switched apps, pause instead of typing into whatever is
  focused. This is a hard safety rule for the input injector.
- Expected throughput: clipboard+paste round-trip ≈ 30–100 ms → **~5–15 fps**.
  Genuinely playable, hilariously so.

### 3. Input

- Poll `GetAsyncKeyState` (global, focus-independent) each tick for:
  arrows / WASD (move+turn), Ctrl (fire), Space (use), Shift (run),
  Enter/Esc/Y/N (menus, cydoomgeneric only).
- Translate press/release edges into the engine's key-event queue.
- Only forward keys while Paint is the foreground window, so gameplay input and
  real typing elsewhere can't cross streams. Panic key: **F12 quits**.

### 4. WAD (game data)

Use **Freedoom** (BSD-licensed, freely redistributable) rather than commercial
Doom assets: `freedoom1.wad` (~20 MB) from the official Freedoom GitHub release.
vizdoom bundles `freedoom2.wad`, making this step free if the fallback is used.

### 5. Stretch: "art mode"

One frame, painted with Paint's *own tools* — synthetic mouse strokes with the
pencil at low resolution (e.g. 64×40 grid, one flood-filled block per cell).
Roughly a minute per frame; exists purely for the bit. Not in v1 scope unless
time permits.

## Project layout

```
C:\Source\MsPaintDoom\
├── PLAN.md
├── README.md
├── requirements.txt
├── run.bat                  # create venv if missing, install, launch
├── wad\freedoom1.wad        # game data (downloaded, not committed)
└── mspaintdoom\
    ├── __init__.py
    ├── main.py              # arg parsing, engine selection, loop
    ├── engine_cdg.py        # cydoomgeneric wrapper
    ├── engine_vzd.py        # vizdoom wrapper
    ├── paint_out.py         # Paint window mgmt, clipboard DIB, paste
    └── keys.py              # GetAsyncKeyState polling → key events
```

## Test plan

1. Smoke: engine produces frames headlessly (dump one frame to PNG).
2. Paint round-trip: paste a synthetic test pattern into Paint, screenshot desktop,
   confirm it landed on the canvas.
3. End-to-end: run 10–15 s of gameplay/demo loop in Paint, screenshot twice,
   confirm frames advance.
4. Undo party trick: Ctrl+Z a few times, confirm the game "rewinds".

## Known risks

- cydoomgeneric build failure on Windows → vizdoom fallback (decision baked in).
- Win11 Paint paste quirks (floating selection, canvas auto-grow) → send Esc after
  paste; pre-size canvas on first frame.
- Paste latency worse than expected → drop target size to 320×200.
- The loop steals keystrokes only when Paint is focused by design; F12 always stops.
