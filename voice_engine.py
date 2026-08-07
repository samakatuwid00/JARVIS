# Voice Engine - Wake word detection, STT, TTS
import asyncio
import io
import tempfile
import threading
import time
import queue
import sys

import numpy as np
import sounddevice as sd
import soundfile as sf
from faster_whisper import WhisperModel
from faster_whisper.vad import VadOptions
import edge_tts

from config import (
    WHISPER_MODEL, WHISPER_DEVICE, WHISPER_COMPUTE_TYPE,
    WHISPER_CPU_THREADS, WHISPER_INITIAL_PROMPT,
    WAKE_WORD, WAKE_WORD_TIMEOUT,
    TTS_VOICE, TTS_RATE, TTS_VOLUME
)

# Cleaner utterance boundaries than the VAD defaults.
VAD_PARAMETERS = VadOptions(
    min_speech_duration_ms=250,
    speech_pad_ms=400,
    threshold=0.5
)


class VoiceEngine:
    """Handles all voice I/O: wake word, speech-to-text, text-to-speech."""

    def __init__(self):
        print("[JARVIS] Loading Whisper model...")
        self.whisper = WhisperModel(
            WHISPER_MODEL,
            device=WHISPER_DEVICE,
            compute_type=WHISPER_COMPUTE_TYPE,
            cpu_threads=WHISPER_CPU_THREADS
        )
        print(f"[JARVIS] Whisper ready ({WHISPER_MODEL} on {WHISPER_DEVICE})")

        self.sample_rate = 16000
        self.is_listening = False
        self.audio_queue = queue.Queue()

    def listen_for_wake_word(self, timeout=WAKE_WORD_TIMEOUT):
        """Record audio and check if it contains the wake word."""
        print(f"[JARVIS] Listening for '{WAKE_WORD}'...")
        audio = self._record_audio(duration=timeout)
        if audio is None:
            return False

        text = self._transcribe(audio).lower().strip()
        if text:
            print(f"[YOU] {text}")

        # Check for wake word (fuzzy match)
        wake_words = ["jarvis", "hey jarvis", "ok jarvis", "hello jarvis"]
        return any(w in text for w in wake_words)

    def listen_for_command(self, timeout=8.0):
        """Record and transcribe a voice command."""
        print("[JARVIS] Listening...")
        audio = self._record_audio(duration=timeout)
        if audio is None:
            return None

        text = self._transcribe(audio).strip()
        if text:
            print(f"[YOU] {text}")
        return text if text else None

    def speak(self, text):
        """Convert text to speech and play it."""
        print(f"[JARVIS] {text}")
        asyncio.run(self._tts_play(text))

    def speak_async(self, text):
        """Speak in a separate thread (non-blocking)."""
        thread = threading.Thread(target=self.speak, args=(text,), daemon=True)
        thread.start()
        return thread

    def _record_audio(self, duration=5.0, silence_threshold=500, silence_duration=1.5):
        """Record audio from microphone with silence detection."""
        frames = []
        silent_chunks = 0
        chunk_size = int(self.sample_rate * 0.1)  # 100ms chunks
        max_chunks = int(duration / 0.1)

        try:
            with sd.InputStream(
                samplerate=self.sample_rate,
                channels=1,
                dtype='int16',
                blocksize=chunk_size
            ) as stream:
                for _ in range(max_chunks):
                    data, overflowed = stream.read(chunk_size)
                    audio_chunk = data[:, 0]
                    frames.append(audio_chunk)

                    # Silence detection
                    if np.abs(audio_chunk).mean() < silence_threshold:
                        silent_chunks += 1
                        if silent_chunks > silence_duration / 0.1:
                            break
                    else:
                        silent_chunks = 0

        except Exception as e:
            print(f"[JARVIS] Audio error: {e}")
            return None

        if not frames:
            return None

        audio = np.concatenate(frames)

        # Skip if too short or too quiet
        if len(audio) < self.sample_rate * 0.5:
            return None
        if np.abs(audio).mean() < 100:
            return None

        return audio

    def _transcribe(self, audio):
        """Transcribe audio to text using Whisper."""
        # Convert to float32 for Whisper
        audio_float = audio.astype(np.float32) / 32768.0

        segments, info = self.whisper.transcribe(
            audio_float,
            language="en",
            beam_size=1,
            vad_filter=True,
            vad_parameters=VAD_PARAMETERS,
            initial_prompt=WHISPER_INITIAL_PROMPT,
            condition_on_previous_text=False
        )

        text = " ".join(segment.text for segment in segments)
        return text.strip()

    async def _tts_play(self, text):
        """Generate and play TTS audio using edge-tts."""
        communicate = edge_tts.Communicate(
            text,
            TTS_VOICE,
            rate=TTS_RATE,
            volume=TTS_VOLUME
        )

        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
            tmp_path = f.name

        await communicate.save(tmp_path)

        # Play the audio
        try:
            data, samplerate = sf.read(tmp_path)
            sd.play(data, samplerate)
            sd.wait()
        except Exception as e:
            print(f"[JARVIS] Playback error: {e}")
        finally:
            import os
            os.unlink(tmp_path)

    @staticmethod
    def _normalize(audio_float, target_rms=0.15, peak_ceiling=0.95):
        """Bring quiet speech up to a consistent level so Whisper hears it clearly."""
        if audio_float.size == 0:
            return audio_float

        rms = float(np.sqrt(np.mean(np.square(audio_float, dtype=np.float64))))
        peak = float(np.max(np.abs(audio_float)))
        if peak <= 1e-6:
            return audio_float  # digital silence, nothing to scale

        if rms < 1e-6:
            gain = peak_ceiling / peak  # near-silent: peak-normalize instead
        else:
            gain = min(target_rms / rms, peak_ceiling / peak)

        return (audio_float * gain).astype(np.float32)

    def _transcribe_from_file(self, file_path):
        """Transcribe audio from a file using Whisper."""
        import soundfile as sf
        audio, sr = sf.read(file_path)
        if len(audio.shape) > 1:
            audio = audio[:, 0]  # mono
        # Resample if needed
        if sr != self.sample_rate:
            import scipy.signal
            audio = scipy.signal.resample(audio, int(len(audio) * self.sample_rate / sr))
        audio_float = audio.astype(np.float32)
        if audio_float.max() > 1.0:
            audio_float = audio_float / 32768.0
        audio_float = self._normalize(audio_float)

        segments, info = self.whisper.transcribe(
            audio_float,
            language="en",
            beam_size=1,
            vad_filter=True,
            vad_parameters=VAD_PARAMETERS,
            initial_prompt=WHISPER_INITIAL_PROMPT,
            condition_on_previous_text=False
        )

        text = " ".join(segment.text for segment in segments)
        return text.strip()


class TextInterface:
    """Fallback text-based interface for environments without microphone."""

    def __init__(self):
        print("[JARVIS] Text mode (no microphone)")

    def listen_for_wake_word(self, timeout=None):
        """In text mode, always ready."""
        return True

    def listen_for_command(self, timeout=None):
        """Get command from text input."""
        try:
            cmd = input("\n🎤 You: ").strip()
            return cmd if cmd else None
        except (EOFError, KeyboardInterrupt):
            return None

    def speak(self, text):
        """Print text response."""
        print(f"\n🤖 Jarvis: {text}")

    def speak_async(self, text):
        self.speak(text)
        return threading.Thread()
