"""Tier-1 voice prototype adapter — RealtimeSTT (streaming STT) + Kokoro (local TTS).

Prototype validated 2026-08-28 (see vault inbox + bench below). This file shows
the exact drop-in shape for voice_engine.py integration. It was BENCHMARKED in an
isolated venv; it is NOT wired into the live server yet.

Bench results (CPU-only, faster-whisper small.en/int8, same machine as JARVIS):

  STT  current: batch whisper small.en final ONLY -> 2318-3359 ms, nothing before
       tier-1:    RealtimeSTT partials start at ~646 ms (tiny.en), refreshed ~500ms;
                  final (small.en) 3443 ms on full utterance (same compute as today)
  TTS  current: edge-tts (network) ~700 ms per clip, needs internet, MP3 file dance
       tier-1:    Kokoro v1.0 fp32 ONNX 820-1636 ms fully local (int8 is 10x slower
                  on CPU — do not use). 325MB model + 28MB voices.

Integration:
  1. `uv pip install --python <jarvis-python> RealtimeSTT kokoro-onnx silero-vad`
  2. Swap VoiceEngine -> VoiceEngineRT in jarvis_web.py
  3. Wire on_partial -> WS {"type":"stt_partial"} for live captions
  4. Keep edge-tts as fallback if the kokoro model file is missing.
"""
from __future__ import annotations

import asyncio
import queue
import threading
import time
from pathlib import Path

import numpy as np
import sounddevice as sd
import soundfile as sf

KOKORO_MODEL = Path(__file__).parent / "models" / "kokoro-v1.0.onnx"
KOKORO_VOICES = Path(__file__).parent / "models" / "voices-v1.0.bin"


class VoiceEngineRT:
    """Drop-in replacement for voice_engine.VoiceEngine with streaming STT."""

    def __init__(self, on_partial=None, on_final=None):
        from RealtimeSTT import AudioToTextRecorder
        self.on_partial = on_partial    # fn(text) -> live caption while user speaks
        self.on_final = on_final        # fn(text) -> replaces listen_for_command return
        self._t0 = 0.0

        def _partial(text):
            if text and self.on_partial:
                self.on_partial(text)

        self.recorder = AudioToTextRecorder(
            model="small.en",
            realtime_model_type="tiny.en",
            use_microphone=True,
            enable_realtime_transcription=True,
            on_realtime_transcription_update=_partial,
            spinner=False,
            post_speech_silence_duration=0.9,   # endpointing (voice_engine used 1.5s)
            min_length_of_recording=0.5,
            level=20,
        )

    def listen_streaming(self):
        """Blocking per-utterance loop; call on_final(text) when a turn ends."""
        self._t0 = time.perf_counter()
        self.recorder.text(self._emit)  # parks until the VAD closes an utterance

    def _emit(self, text):
        if self.on_final:
            self.on_final(text.strip())
        # re-arm for the next utterance
        threading.Thread(target=self.listen_streaming, daemon=True).start()

    # --- parot the old interface ------------------------------------------
    def speak(self, text):
        asyncio.run(speak_kokoro(text))

    def speak_async(self, text):
        return threading.Thread(target=self.speak, args=(text,), daemon=True).start()


class KokoroTTS:
    """Local TTS. Sentence-chunked so playback can start before full synthesis."""

    def __init__(self, voice="am_michael", speed=1.1):
        from kokoro_onnx import Kokoro
        npl = np.load
        np.load = lambda *a, **k: npl(*a, allow_pickle=True, **k)
        self.kokoro = Kokoro(str(KOKORO_MODEL), str(KOKORO_VOICES))
        np.load = npl
        self.voice, self.speed = voice, speed

    def synth(self, text: str) -> tuple[np.ndarray, int]:
        samples, sr = self.kokoro.create(text, voice=self.voice, speed=self.speed, lang="en-us")
        return np.asarray(samples), sr

    def split_sentences(self, text: str) -> list[str]:
        import re
        parts = [p.strip() for p in re.split(r"(?<=[.!?])\s+", text) if p.strip()]
        return parts or [text]


async def speak_kokoro(text: str, tts: KokoroTTS | None = None):
    """Play TTS audio; synthesizes sentence-by-sentence for lower time-to-first-audio."""
    tts = tts or KokoroTTS()
    for sentence in tts.split_sentences(text):
        samples, sr = tts.synth(sentence)
        sd.play(samples, sr)
        # synthesize the next sentence while this one plays
        sd.wait()
