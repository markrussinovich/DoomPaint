"""Map music, straight from the WAD.

ViZDoom strips ZDoom's music playback entirely (sound effects only), so the
soundtrack is reproduced here: the map's music lump is read out of the
Freedoom WAD — conveniently already standard MIDI, no MUS conversion needed —
dumped to a temp file, and looped through Windows' built-in MIDI sequencer
via MCI (winmm). No extra dependencies, and pausing the game pauses the tune.
"""
import atexit
import ctypes
import os
import struct
import tempfile
import threading
import time

_winmm = ctypes.WinDLL("winmm", use_last_error=True)

# Doom II-style maps use fixed track names; Doom 1-style maps (ExMy) use the
# map name itself (D_E1M1). Freedoom follows the same lump naming.
_DOOM2_TRACKS = (
    "RUNNIN", "STALKS", "COUNTD", "BETWEE", "DOOM", "THE_DA", "SHAWN",
    "DDTBLU", "IN_CIT", "DEAD", "STLKS2", "THEDA2", "DOOM2", "DDTBL2",
    "RUNNI2", "DEAD2", "STLKS3", "ROMERO", "SHAWN2", "MESSAG", "COUNT2",
    "DDTBL3", "AMPIE", "THEDA3", "ADRIAN", "MESSG2", "ROMER2", "TENSE",
    "SHAWN3", "OPENIN", "EVIL", "ULTIMA")


def music_lump_name(doom_map: str) -> str | None:
    m = doom_map.strip().upper()
    if m.startswith("MAP"):
        try:
            return "D_" + _DOOM2_TRACKS[int(m[3:]) - 1]
        except (ValueError, IndexError):
            return None
    return "D_" + m


def read_lump(wad_path: str, lump_name: str) -> bytes | None:
    """Minimal WAD directory walk; returns the lump bytes or None."""
    want = lump_name.upper().encode("ascii")
    with open(wad_path, "rb") as f:
        data = f.read()
    magic, count, dir_ofs = struct.unpack_from("<4sii", data, 0)
    if magic not in (b"IWAD", b"PWAD"):
        return None
    for i in range(count):
        pos, size, raw = struct.unpack_from("<ii8s", data, dir_ofs + 16 * i)
        if raw.rstrip(b"\x00") == want:
            return data[pos:pos + size]
    return None


# MUS controller number -> MIDI controller number (controller 0 is a program
# change, handled separately).
_MUS_CTRL_TO_MIDI = {1: 0x00, 2: 0x01, 3: 0x07, 4: 0x0A, 5: 0x0B,
                     6: 0x5B, 7: 0x5D, 8: 0x40, 9: 0x43}
# MUS system events -> MIDI channel-mode controllers.
_MUS_SYS_TO_MIDI = {10: 120, 11: 123, 12: 126, 13: 127, 14: 121}


def mus_to_midi(mus: bytes) -> bytes | None:
    """Convert a DMX MUS lump (vanilla doom.wad music) to a format-0 MIDI.

    MUS runs at 140 ticks/s; a MIDI division of 70 with the default tempo
    (500000 us/quarter) reproduces that exactly.
    """
    if not mus.startswith(b"MUS\x1a"):
        return None
    _, score_start = struct.unpack_from("<HH", mus, 4)
    pos = score_start
    track = bytearray()
    chan_map: dict[int, int] = {}   # MUS channel -> MIDI channel
    velocity: dict[int, int] = {}   # last velocity per MIDI channel
    delay = 0

    def emit(*evt: int) -> None:
        nonlocal delay
        v, out = delay, []
        out.append(v & 0x7F)
        v >>= 7
        while v:
            out.append(0x80 | (v & 0x7F))
            v >>= 7
        track.extend(reversed(out))
        track.extend(evt)
        delay = 0

    def midi_chan(mus_ch: int) -> int:
        if mus_ch == 15:
            return 9  # MUS percussion -> MIDI drum channel
        if mus_ch not in chan_map:
            n = len(chan_map)
            chan_map[mus_ch] = min(n if n < 9 else n + 1, 15)
        return chan_map[mus_ch]

    while pos < len(mus):
        head = mus[pos]
        pos += 1
        etype, ch = (head >> 4) & 7, midi_chan(head & 15)
        if etype == 0:                                   # release note
            emit(0x80 | ch, mus[pos] & 0x7F, 0x40)
            pos += 1
        elif etype == 1:                                 # play note
            nb = mus[pos]
            pos += 1
            if nb & 0x80:
                velocity[ch] = mus[pos] & 0x7F
                pos += 1
            emit(0x90 | ch, nb & 0x7F, velocity.get(ch, 100))
        elif etype == 2:                                 # pitch bend
            bend = mus[pos] * 64
            pos += 1
            emit(0xE0 | ch, bend & 0x7F, (bend >> 7) & 0x7F)
        elif etype == 3:                                 # system event
            m = _MUS_SYS_TO_MIDI.get(mus[pos] & 0x7F)
            pos += 1
            if m is not None:
                emit(0xB0 | ch, m, 0)
        elif etype == 4:                                 # controller change
            ctrl, val = mus[pos], min(mus[pos + 1], 0x7F)
            pos += 2
            if ctrl == 0:
                emit(0xC0 | ch, val)
            elif ctrl in _MUS_CTRL_TO_MIDI:
                emit(0xB0 | ch, _MUS_CTRL_TO_MIDI[ctrl], val)
        elif etype == 6:                                 # score end
            break
        else:                                            # 5/7: skip payload
            pos += 1
        if head & 0x80:                                  # delay follows
            d = 0
            while True:
                b = mus[pos]
                pos += 1
                d = (d << 7) | (b & 0x7F)
                if not (b & 0x80):
                    break
            delay += d

    emit(0xFF, 0x2F, 0x00)  # end of track
    return (b"MThd" + struct.pack(">IHHH", 6, 0, 1, 70)
            + b"MTrk" + struct.pack(">I", len(track)) + bytes(track))


def _mci(cmd: str) -> str:
    buf = ctypes.create_unicode_buffer(128)
    err = _winmm.mciSendStringW(cmd, buf, 128, None)
    if err:
        raise OSError(f"MCI error {err}: {cmd}")
    return buf.value


def set_session_volume(level: float, pid: int | None = None,
                       exe: str | None = None) -> bool:
    """Set volume (and unmute) on the WASAPI sessions of a process, matched
    by pid or exe name. Windows persists per-app volumes across runs, so a
    session once left at 0 (a crashed pause, a long-ago mixer tweak) stays
    silent forever unless someone puts it back. Returns True if any session
    matched."""
    hit = False
    try:
        import comtypes
        from pycaw.pycaw import AudioUtilities, ISimpleAudioVolume
        try:
            comtypes.CoInitialize()
        except OSError:
            pass
        for s in AudioUtilities.GetAllSessions():
            p = s.Process
            if not p:
                continue
            if (pid is not None and p.pid == pid) or \
                    (exe is not None and p.name().lower() == exe.lower()):
                try:
                    v = s._ctl.QueryInterface(ISimpleAudioVolume)
                    v.SetMasterVolume(level, None)
                    v.SetMute(0, None)
                    hit = True
                except Exception:
                    pass
    except Exception:
        pass  # no pycaw / no session yet: callers treat this as best-effort
    return hit


def _set_my_session_volume(level: float) -> bool:
    """Volume of THIS process's WASAPI session (where the MIDI synth plays)."""
    return set_session_volume(level, pid=os.getpid())


class SessionPeakSampler:
    """Continuously track the loudest recent peak of a process's audio
    session (on the default device). Lets the game notice that its own
    engine is rendering silence and repair it."""

    def __init__(self, exe: str = "vizdoom.exe"):
        self._exe = exe.lower()
        self.max_peak = 0.0
        threading.Thread(target=self._run, daemon=True,
                         name="mspaintdoom-peaks").start()

    def _run(self):
        try:
            import comtypes
            from pycaw.pycaw import AudioUtilities, IAudioMeterInformation
            try:
                comtypes.CoInitialize()
            except OSError:
                pass
        except Exception:
            return
        while True:
            try:
                for s in AudioUtilities.GetAllSessions():
                    p = s.Process
                    if p and p.name().lower() == self._exe:
                        v = s._ctl.QueryInterface(
                            IAudioMeterInformation).GetPeakValue()
                        if v > self.max_peak:
                            self.max_peak = v
            except Exception:
                pass
            time.sleep(0.12)

    def take(self) -> float:
        """Max peak since the last call, and reset the window."""
        v = self.max_peak
        self.max_peak = 0.0
        return v


def default_device_id() -> str | None:
    """Windows default render endpoint id (to detect mid-game switches)."""
    try:
        import comtypes
        from pycaw.constants import EDataFlow
        from pycaw.pycaw import AudioUtilities
        try:
            comtypes.CoInitialize()
        except OSError:
            pass
        return AudioUtilities.GetDeviceEnumerator() \
            .GetDefaultAudioEndpoint(EDataFlow.eRender.value, 1).GetId()
    except Exception:
        return None


def watch_default_device(on_change) -> None:
    """Call on_change() whenever the default output device changes.

    Why it matters: the MIDI music stream auto-migrates to a new default,
    but the engine's OpenAL stream keeps the device it opened at boot — so
    plugging in / switching to a headset mid-game silently strands the sound
    effects on the old device.
    """
    def run():
        last = default_device_id()
        while True:
            time.sleep(2.0)
            cur = default_device_id()
            if cur and last and cur != last:
                on_change()
            last = cur or last

    threading.Thread(target=run, daemon=True,
                     name="mspaintdoom-devwatch").start()


def audio_output_status() -> str | None:
    """'<device name>: <master volume>%' for the default output, or None."""
    try:
        import comtypes
        from pycaw.pycaw import AudioUtilities
        try:
            comtypes.CoInitialize()
        except OSError:
            pass
        dev = AudioUtilities.GetSpeakers()
        vol = dev.EndpointVolume.GetMasterVolumeLevelScalar()
        muted = bool(dev.EndpointVolume.GetMute())
        name = getattr(dev, "FriendlyName", None) or "default output"
        return f"{name} at {vol:.0%}" + (" [MUTED]" if muted else "")
    except Exception:
        return None


class MusicPlayer:
    """Loop one MIDI track via MCI, with pause/resume."""

    _ALIAS = "mspaintdoom_music"

    def __init__(self, volume: float = 0.4):
        # The GS Wavetable synth renders much hotter than the engine's sound
        # effects; its session volume is the music's mixing fader.
        self._volume = max(0.0, min(1.0, volume))
        self._lock = threading.Lock()
        self._open = False
        self._paused = False
        self._vol_ok = False
        self._resume_pos = "0"
        self._stop_evt = threading.Event()
        self._path = os.path.join(tempfile.gettempdir(),
                                  f"mspaintdoom_{os.getpid()}.mid")

    def start(self, wad_path: str, doom_map: str) -> bool:
        name = music_lump_name(doom_map)
        data = read_lump(wad_path, name) if name else None
        if data and data.startswith(b"MUS\x1a"):
            data = mus_to_midi(data)  # vanilla-Doom WADs store MUS, not MIDI
        if not data or not data.startswith(b"MThd"):
            return False
        with open(self._path, "wb") as f:
            f.write(data)
        try:
            with self._lock:
                _mci(f'open "{self._path}" type sequencer alias {self._ALIAS}')
                _mci(f"play {self._ALIAS}")
                self._open = True
        except OSError:
            return False
        # Set the music fader (also repairs a persisted volume from any
        # earlier run).
        self._vol_ok = _set_my_session_volume(self._volume)
        threading.Thread(target=self._loop_watch, daemon=True,
                         name="mspaintdoom-music").start()
        atexit.register(self.stop)
        return True

    def _loop_watch(self):
        # MCI sequencers don't auto-repeat; rewind when the track ends.
        while not self._stop_evt.wait(1.0):
            with self._lock:
                if not self._open or self._paused:
                    continue
                if not self._vol_ok:
                    # Session may only appear once the synth starts rendering.
                    self._vol_ok = _set_my_session_volume(self._volume)
                try:
                    if _mci(f"status {self._ALIAS} mode") == "stopped":
                        _mci(f"seek {self._ALIAS} to start")
                        _mci(f"play {self._ALIAS}")
                except OSError:
                    pass

    def pause(self):
        # MCI "pause" freezes the sequence but sends no note-offs, so a held
        # organ chord drones on forever — and closing/reopening the sequencer
        # instead costs ~5 s on some machines. So: pause the sequence AND mute
        # this process's audio session (the MIDI synth renders into it;
        # ViZDoom's sound effects live in vizdoom.exe's session, unaffected).
        with self._lock:
            if self._open and not self._paused:
                try:
                    _mci(f"pause {self._ALIAS}")
                except OSError:
                    pass
                _set_my_session_volume(0.0)
                self._paused = True

    def resume(self):
        with self._lock:
            if self._open and self._paused:
                try:
                    _mci(f"play {self._ALIAS}")  # continues from position
                except OSError:
                    pass
                _set_my_session_volume(self._volume)
                self._paused = False

    def stop(self):
        self._stop_evt.set()
        with self._lock:
            if self._open:
                try:
                    _mci(f"close {self._ALIAS}")
                except OSError:
                    pass
                self._open = False
                # Never leave the persisted per-app volume at the paused 0 —
                # Windows would remember it and silence every future run.
                _set_my_session_volume(1.0)
        try:
            os.unlink(self._path)
        except OSError:
            pass
