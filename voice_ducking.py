"""voice_ducking.py — Phase C: duck music while JARVIS speaks (Windows Core Audio).

Ducks per-process audio sessions (Spotify) to a low volume for the duration of
JARVIS's speech and restores the original levels afterwards. Browsers are
deliberately NOT ducked: the HUD plays JARVIS's voice through the browser, so a
browser-wide duck would silence JARVIS itself.

Failsafe: duck() arms a max-duration timer; if the matching speech-end never
arrives (client crash, lost message), volume is restored automatically.
"""
from __future__ import annotations

import threading
import time

# Processes whose audio gets ducked when JARVIS speaks.
_DUCK_PROCESSES = ("spotify.exe",)
DUCK_VOLUME = 0.15          # fraction of full volume while ducked
FAILSAFE_S = 180.0          # absolute max duck duration

_lock = threading.Lock()
_ducked = False             # True between duck() and unduck()
_originals: dict = {}       # session instance -> original volume
_failsafe_at = 0.0


def _iter_duck_sessions():
    """Yield (session_ctl, simple_volume, process_name) for duckable sessions."""
    try:
        from pycaw.pycaw import AudioUtilities
    except Exception:
        return
    try:
        sessions = AudioUtilities.GetAllSessions()
    except Exception:
        return
    for s in sessions:
        try:
            name = (s.ProcessName() or "").lower()
        except Exception:
            continue
        if name in _DUCK_PROCESSES:
            yield s


def duck() -> bool:
    """Duck music processes. Returns True if any session was ducked."""
    global _ducked, _failsafe_at
    with _lock:
        if _ducked:
            _failsafe_at = time.monotonic() + FAILSAFE_S
            return True
        saved = {}
        for s in _iter_duck_sessions():
            try:
                vol = getattr(s, "SimpleAudioVolume", None)
                if vol is None:
                    vol = s._ctl.QueryInterface(_simple_volume())
                saved[id(s)] = (vol, vol.GetMasterVolume())
                vol.SetMasterVolume(DUCK_VOLUME, None)
            except Exception:
                continue
        if not saved:
            return False
        _originals.update(saved)
        _ducked = True
        _failsafe_at = time.monotonic() + FAILSAFE_S
        return True


def unduck() -> None:
    """Restore original volumes (no-op if not currently ducked)."""
    global _ducked
    with _lock:
        if not _ducked:
            return
        for vol, level in list(_originals.values()):
            try:
                vol.SetMasterVolume(level, None)
            except Exception:
                continue
        _originals.clear()
        _ducked = False


def check_failsafe() -> None:
    """Undo ducking if the failsafe window expired (call periodically)."""
    with _lock:
        expired = _ducked and time.monotonic() > _failsafe_at
    if expired:
        unduck()


def _simple_volume():
    from pycaw.pycaw import ISimpleAudioVolume
    return ISimpleAudioVolume


def _reset_for_tests() -> None:
    global _ducked, _failsafe_at
    with _lock:
        _originals.clear()
        _ducked = False
        _failsafe_at = 0.0
