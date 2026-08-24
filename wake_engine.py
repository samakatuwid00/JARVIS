"""Server-side wake-word listener for JARVIS.

Why this exists
---------------
The browser's Web Speech API (WakeListener in jarvis_visual.html) is the PRIMARY
wake detector, but it is fragile:
  * Chrome/Edge-only (no Firefox).
  * It is a cloud/on-device speech engine that degrades badly when music/ambient
    noise bleeds into the mic — exactly the "JARVIS won't hear me over music"
    symptom. The browser just never fires the wake, and there is no fallback.

This engine provides that fallback. It runs a continuous microphone capture in
the *server* process (which already has Whisper loaded) and transcribes short
rolling windows with Whisper, which we empirically verified hears "jarvis" cleanly
even over a 2x music bed. When it detects the wake word it calls a callback so
the server can tell the browser to open its command window.

Design notes / pitfalls
-----------------------
* It must NOT react to JARVIS's own TTS. We pass a `suspend()` window after each
  speak so the engine goes silent while JARVIS is talking. Music is NOT TTS, so
  music does not falsely suspend the listener (that would defeat the whole point).
* Debounced: once it fires it won't fire again for WAKE_COOLDOWN_MS, so one
  "jarvis" doesn't open twenty captures.
* The browser already does its own wake detection; to avoid double capture the
  server only acts when no browser-side wake has recently happened. The server
  tracks the last browser-wake time via `note_browser_wake()` (called from the WS
  handler when the browser's own listener fires).
* Whisper is CPU-heavy; we only transcribe every WINDOW_S seconds and keep the
  buffer small. On a quiet room this is a few Whisper calls/min.
"""

import time
import queue
import threading

import numpy as np
import sounddevice as sd

WAKE_WORDS = ['jarvis', 'jarviss', 'jarvus', 'jervis', 'javis', 'jarvez', 'charvis', 'jarv']


def wake_word_in(text):
    """Mirror of the HUD's wakeWordIn() so server + browser agree on what 'jarvis' is."""
    if not text:
        return False
    import re
    t = re.sub(r'[.,!?;:]+$', '', (text or '').lower()).strip()
    if t == 'jarvis':
        return True
    for w in WAKE_WORDS:
        if t == w:
            return True
        if re.search(r'(^|\s)' + w + r'(\s|$)', t):
            return True
    return False


class WakeEngine:
    def __init__(self, voice_engine, sample_rate=16000,
                 window_s=2.5, step_s=1.0, cooldown_ms=4000,
                 suspend_ms=1500, device=None):
        self.ve = voice_engine
        self.sr = sample_rate
        self.window_s = window_s
        self.step_s = step_s
        self.cooldown = cooldown_ms / 1000.0
        self.suspend_s = suspend_ms / 1000.0
        self.device = device

        self._q = queue.Queue()
        self._buf = np.zeros(0, dtype=np.float32)
        self._last_fire = 0.0
        self._suspend_until = 0.0
        self._last_browser_wake = 0.0
        self._last_audio_ts = 0.0
        self._running = False
        self._thread = None
        self._stream = None
        self._callback = None  # called when wake detected: callback()
        self._on_error = None  # called on fatal error: on_error(msg)
        self.healthy = False
        self.last_err = ''

    # -- external signals ---------------------------------------------------
    def set_callback(self, cb):
        self._callback = cb

    def set_error_callback(self, cb):
        self._on_error = cb

    def suspend(self, ms=None):
        """Mute the listener briefly (e.g. right after JARVIS speaks TTS)."""
        s = (ms or self.suspend_ms * 1000) / 1000.0
        self._suspend_until = max(self._suspend_until, time.time() + s)

    def note_browser_wake(self):
        """The browser's own Web Speech listener fired — don't double-trigger."""
        self._last_browser_wake = time.time()

    # -- lifecycle ----------------------------------------------------------
    def start(self):
        if self._running:
            return
        try:
            self._stream = sd.InputStream(
                device=self.device, samplerate=self.sr, channels=1,
                dtype='float32', blocksize=int(self.sr * 0.1),
                callback=self._audio_cb)
            self._stream.start()
            self._running = True
            self.healthy = True
            self.last_err = ''
            self._thread = threading.Thread(target=self._loop, daemon=True)
            self._thread.start()
            print("[WakeEngine] started (server-side wake fallback)", flush=True)
        except Exception as e:
            self.healthy = False
            self.last_err = str(e)
            print(f"[WakeEngine] FAILED to start: {e}", flush=True)
            if self._on_error:
                self._on_error(str(e))

    def stop(self):
        self._running = False
        try:
            if self._stream:
                self._stream.stop()
                self._stream.close()
        except Exception:
            pass
        self._stream = None

    def _audio_cb(self, indata, frames, time_info, status):
        try:
            self._q.put(np.copy(indata[:, 0]))
            self._last_audio_ts = time.time()
        except Exception:
            pass

    # -- capture + detect loop ---------------------------------------------
    def _loop(self):
        step_samples = int(self.sr * self.step_s)
        window_samples = int(self.sr * self.window_s)
        while self._running:
            try:
                chunk = self._q.get(timeout=1.0)
            except queue.Empty:
                # No audio arriving? Mark unhealthy only if it's been a while.
                if time.time() - self._last_audio_ts > 5.0:
                    self.healthy = False
                    self.last_err = 'no microphone audio received'
                continue
            self.healthy = True
            self.last_err = ''
            self._buf = np.concatenate([self._buf, chunk])
            # Keep at most one window + a step of history.
            cap = window_samples + step_samples
            if self._buf.shape[0] > cap:
                self._buf = self._buf[-cap:]

            now = time.time()
            if now - self._last_fire < self.cooldown:
                continue
            if now < self._suspend_until:
                continue                      # JARVIS is (or just was) speaking
            if self._buf.shape[0] < window_samples:
                continue
            if now - self._last_browser_wake < 2.0:
                continue                      # browser already handled it

            window = self._buf[-window_samples:].astype(np.float32)
            # Skip near-silent windows (room tone) — no point burning Whisper.
            if np.abs(window).mean() < 0.002:
                continue
            try:
                text = self.ve._transcribe(window)
            except Exception as e:
                self.last_err = f'transcribe error: {e}'
                continue
            if wake_word_in(text):
                print(f"[WakeEngine] wake word detected: {text!r}", flush=True)
                self._last_fire = now
                if self._callback:
                    try:
                        self._callback()
                    except Exception as e:
                        print(f"[WakeEngine] callback error: {e}", flush=True)
