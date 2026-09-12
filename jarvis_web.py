#!/usr/bin/env python3
"""
JARVIS Web Interface - FastAPI backend for voice visual JARVIS

WebSocket endpoint for real-time STT/TTS loop with animated orb visualization.
Browser captures mic -> sends audio (WebM/Opus) -> backend transcribes with Whisper
-> Gemini thinks -> edge-tts speaks -> audio sent back to browser.
"""

import os
import io
import sys
import json

# The console (and a redirected log) is cp1252 on Windows: a page read or a
# job note with "↘" or "ロ" crashed the print, and with it the announcement.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass
import time
import base64
import tempfile
import asyncio
import threading

from fastapi import FastAPI, Request, WebSocket
from fastapi.responses import HTMLResponse, JSONResponse
from starlette.websockets import WebSocketDisconnect

from voice_engine import VoiceEngine
from wake_engine import WakeEngine, wake_targets
from brain_gemini import JarvisBrain
from session_store import log as sstore_log
import intake
from intake import load_corpus, resolve_intent

# Layer 1 (Intake) corpus — portable data, reloaded once at boot.
INTAKE_CORPUS = load_corpus()
from config import (GEMINI_MODEL, ROUTER_BASE_URL, ROUTER_MODEL, WHISPER_MODEL,
                   GROQ_MODEL, GROQ_BASE_URL, CEREBRAS_MODEL, CEREBRAS_BASE_URL)

app = FastAPI(title="Cygnus Voice Interface")


# Browser origins allowed besides JARVIS's own (the phone's Tailscale https
# name). Comma-separated in .env.
_ALLOWED_ORIGINS = frozenset(
    o.strip().rstrip("/") for o in os.environ.get("JARVIS_ALLOWED_ORIGINS", "").split(",")
    if o.strip())


def _hostname(host_header: str) -> str:
    """A Host header without its port; IPv6 keeps its brackets."""
    host = host_header.strip().lower()
    if host.startswith("["):
        return host.split("]", 1)[0] + "]"
    return host.split(":", 1)[0]


# Names this server answers to. A DNS-rebinding page (evil.example pointed at
# 127.0.0.1) arrives with its own name in Host, and its Origin matches that
# Host, so the origin checks alone would let it in.
_ALLOWED_HOSTS = frozenset({"127.0.0.1", "localhost", "[::1]"} |
                           {_hostname(o.split("://", 1)[-1]) for o in _ALLOWED_ORIGINS})


@app.middleware("http")
async def _guard_apps_writes(request: Request, call_next):
    """Every request must name an allowed host. Apps-panel writes (rules,
    toggles, re-checks) come only from JARVIS's own pages: the server has no
    login and the handlers parse the body as JSON whatever its type, so
    without this any web page the user visits could post a rule (a
    text/plain POST skips the CORS preflight)."""
    if _hostname(request.headers.get("host", "")) not in _ALLOWED_HOSTS:
        return JSONResponse({"error": "unknown host"}, status_code=400)
    # What you said (shadow review) and your learned defaults stay on this
    # computer. Requests proxied by tailscale serve also come from 127.0.0.1,
    # so the phone passes this check; it is your own device.
    if request.url.path.startswith(("/shadow", "/prefs")) and \
            (request.client.host if request.client else "") not in ("127.0.0.1", "::1", "localhost"):
        return JSONResponse({"error": "Only available on this computer."}, status_code=403)
    if request.method in ("POST", "PUT", "PATCH", "DELETE") \
            and request.url.path.startswith(("/apps", "/shadow", "/prefs")):
        ctype = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        origin = request.headers.get("origin")
        same_origin = not origin or \
            origin.split("://", 1)[-1].rstrip("/") == request.headers.get("host", "")
        if ctype != "application/json" or not same_origin:
            return JSONResponse({"error": "forbidden"}, status_code=403)
    return await call_next(request)

# Connected voice clients (JARVIS HUD tabs) — the server-side wake engine alerts
# the local ones (see wake_targets) so a wake detected on the server opens the
# command window in the UI.
WS_CLIENTS = set()
# The subset that came in through the tailscale proxy (the phone). The PC's
# wake engine hears the PC's room, so its wakes must not open a capture on a
# phone somewhere else; those clients are tap-to-talk.
WS_REMOTE = set()
# The desktop app's window (it says hello on connect). While one is
# connected, server wakes go to it alone, so a Chrome tab stays quiet.
WS_DESKTOP = set()

# Global singletons (Whisper load is slow, do it once)
print("[JARVIS Web] Loading VoiceEngine (Whisper)...", flush=True)
voice_engine = VoiceEngine()
print(f"[JARVIS Web] Loading Brain ({ROUTER_MODEL})...", flush=True)
brain = JarvisBrain()
# The semantic router loads in the background (~5 s); until then it neither
# logs nor leads, and no reply waits for it.
import semantic_route
semantic_route.warm()
# 9router health: probe at boot, so the first cloud turn already skips dead
# models (router_health.py); it re-checks every 5 minutes after that.
import router_health
router_health.start()
import tools  # for the /apps/open endpoint (same dispatcher the brain uses)

# Server-side wake-word fallback. The browser's Web Speech WakeListener is primary,
# but it degrades when music/ambient noise bleeds into the mic — this catches the
# wake word with Whisper (verified robust to music) and tells the HUD to open its
# command window. Healthy=false just means "no server-side fallback"; the browser
# listener still works on its own.
# Post-TTS suspend window, tunable without code edits (JARVIS_WAKE_SUSPEND_MS).
try:
    _WAKE_SUSPEND_MS = max(0, int(os.environ.get("JARVIS_WAKE_SUSPEND_MS", "1500")))
except ValueError:
    print("[WakeEngine] bad JARVIS_WAKE_SUSPEND_MS, using 1500", flush=True)
    _WAKE_SUSPEND_MS = 1500
wake_engine = WakeEngine(voice_engine, suspend_ms=_WAKE_SUSPEND_MS)
# Longest the wake fallback stays quiet for one browser capture.
_WAKE_HOLD_MS = 30000

# The server's event loop, captured by websocket_endpoint. broadcast_wake runs on
# the WakeEngine worker thread, which has no loop of its own — asyncio.get_event_loop()
# there never schedules the send, so every server wake reached nobody.
_WS_LOOP = None
_WS_LOOP_WARN_TS = 0.0
# Cached set of website keys (populated at module load so /apps/open can route
# websites to open_site() without first hitting GET /apps).
_WEBSITE_KEYS = frozenset()
try:
    _wi, _ = _website_items()
    _WEBSITE_KEYS = frozenset(i["key"] for i in _wi)
except Exception:
    pass


def _wake_send_done(ws, fut):
    """Runs on the server loop once a wake send finishes; drops only sockets that failed."""
    if fut.cancelled():
        exc = asyncio.CancelledError()
    else:
        exc = fut.exception()
    if exc is not None:
        print(f"[WakeEngine] wake send failed, dropping client: {exc!r}", flush=True)
        WS_CLIENTS.discard(ws)


def broadcast_wake():
    """Called by the server wake engine when it hears 'jarvis'.

    Returns True when at least one send was scheduled, so the engine only starts
    its cooldown on a wake that actually went somewhere.
    """
    global _WS_LOOP_WARN_TS
    loop = _WS_LOOP
    if loop is None or not loop.is_running():
        now = time.time()
        if now - _WS_LOOP_WARN_TS >= 60.0:
            _WS_LOOP_WARN_TS = now
            print("[WakeEngine] wake heard but no server loop yet (no HUD connected); "
                  "clients kept", flush=True)
        return False
    clients = wake_targets(WS_CLIENTS, WS_REMOTE, WS_DESKTOP)
    target = "desktop app" if WS_DESKTOP.intersection(clients) else "local"
    print(f"[WakeEngine] broadcasting wake to {len(clients)} HUD client(s) "
          f"({target}; {len(WS_REMOTE)} remote skipped)", flush=True)
    payload = json.dumps({"type": "wake", "source": "server"})
    scheduled = 0
    for ws in clients:
        coro = ws.send_text(payload)
        try:
            fut = asyncio.run_coroutine_threadsafe(coro, loop)
        except Exception as e:
            coro.close()  # never scheduled — close it so it isn't reported unawaited
            print(f"[WakeEngine] wake schedule failed, dropping client: {e!r}", flush=True)
            WS_CLIENTS.discard(ws)
            continue
        fut.add_done_callback(lambda f, ws=ws: _wake_send_done(ws, f))
        scheduled += 1
    return scheduled > 0


wake_engine.set_callback(broadcast_wake)
wake_engine.set_error_callback(
    lambda msg: print(f"[WakeEngine] error: {msg}", flush=True))
wake_engine.start()

# Phase 2: pre-warm Hermes session at boot so the first voice query doesn't
# pay the full cold-start cost. Runs in a background thread to avoid blocking
# server startup. The session is persisted on disk and survives JARVIS restarts.
def _prewarm_hermes():
    try:
        from warm_harness import warm_session
        sid = warm_session(timeout=120)
        if sid:
            print(f"[JARVIS Web] Hermes warm session ready: {sid}", flush=True)
        else:
            print("[JARVIS Web] Hermes warm session: no session_id (will spawn fresh)", flush=True)
    except Exception as e:
        print(f"[JARVIS Web] Hermes warm-up failed: {e}", flush=True)

threading.Thread(target=_prewarm_hermes, daemon=True).start()

# Phase 8: refresh the web registry in the background ONLY if older than 7
# days (policy C). One stat call when fresh; voice resolution uses the current
# registry immediately either way.
try:
    import web_registry as _wr
    if _wr.maybe_refresh_async():
        print("[JARVIS Web] Web registry stale — background rescan started.", flush=True)
except Exception as _e:
    print(f"[JARVIS Web] Web registry check failed: {_e}", flush=True)

print("[JARVIS Web] Ready.", flush=True)

# Voice state feedback (Phase A): policy engine + cue cache. All spoken state
# updates (thinking cues, progress lines, terminal job announcements) route
# through voice_feedback.announce() so the user gets friendly feedback without
# overlapping/duplicate speech. The synthesizer is registered after tts_to_b64
# is defined below.
import voice_feedback as _vf
import voice_ducking

# Phase B: per-job milestone timers ("Still on it, sir.") for long background
# tasks — keyed by job id, cancelled when the job reaches a terminal state.
_TASK_MILESTONES: dict[str, asyncio.Task] = {}


class _ThinkingWatchdog:
    """Speaks 'Thinking, sir.' / 'Still working on it.' if a turn runs long."""

    def __init__(self, ws, vf):
        self._ws = ws
        self._vf = vf
        self._stop = asyncio.Event()

    def start(self):
        asyncio.get_running_loop().create_task(self._run())

    async def _run(self):
        try:
            await asyncio.sleep(_vf.THINKING_CUE_S)
            if self._stop.is_set():
                return
            await _send_cue(self._ws, "thinking", self._vf)
            await asyncio.sleep(max(1.0, _vf.THINKING_ESCALATE_S - _vf.THINKING_CUE_S))
            if self._stop.is_set():
                return
            await _send_cue(self._ws, "thinking_escalate", self._vf)
        except Exception:
            pass

    def stop(self):
        self._stop.set()


async def _send_cue(ws, kind: str, vf) -> None:
    line = vf.announce(kind)
    if line is None:
        return
    audio = await vf.cue_audio(line)
    try:
        await ws.send_text(json.dumps({
            "type": "cue", "text": line, "audio": audio, "priority": "low" if kind.startswith("thinking") else "normal"
        }))
    except Exception:
        pass


def _start_confirm_reminder(ws, vf) -> None:
    """Phase B: if a confirm gate is still pending after 5 min, remind once."""
    async def _run():
        try:
            await asyncio.sleep(300)
        except asyncio.CancelledError:
            return
        try:
            from tools import _PENDING_HERMES_CALL, _PENDING_DESTRUCTIVE
            pending = _PENDING_HERMES_CALL or _PENDING_DESTRUCTIVE.get("autonomous")
        except Exception:
            return
        if pending:
            await _send_cue(ws, "confirm_pending", vf)
    asyncio.get_running_loop().create_task(_run())


def _start_task_milestones(ws, vf, jid: str) -> None:
    """Phase B: speak a friendly 'still on it' line every 45s (max 3) while a
    background job is genuinely still running; cancelled on its terminal event."""
    _TASK_MILESTONES.pop(jid, None)  # cancel any stale timer for this job

    async def _run():
        lines = ["Still on it, sir.", "Making progress, sir.",
                 "This one is taking a while, sir."]
        try:
            for line in lines:
                await asyncio.sleep(45)
                import jobs as _jobs
                j = _jobs.get(jid)
                if not j or j.get("state") not in ("running", "queued"):
                    return
                spoken = vf.announce("milestone", line)
                if spoken:
                    audio = await vf.cue_audio(spoken)
                    await ws.send_text(json.dumps({
                        "type": "cue", "text": spoken, "audio": audio,
                        "priority": "normal"}))
        except asyncio.CancelledError:
            return
        except Exception:
            pass

    _TASK_MILESTONES[jid] = asyncio.get_running_loop().create_task(_run())


def _arm_phase_b(ws, vf, response_text) -> None:
    """Arm the per-turn Phase B timers from the raw think() response."""
    if not isinstance(response_text, str):
        return
    if ("HERMES_BACKGROUND:" in response_text
            or "AUTONOMOUS_BACKGROUND:" in response_text):
        ack = response_text.split(":", 1)[1].strip()
        m = _re.search(r"\(job ([0-9a-f]+)\)", ack)
        if m:
            _start_task_milestones(ws, vf, m.group(1))
    elif response_text.startswith("[NEEDS_CONFIRM"):
        _start_confirm_reminder(ws, vf)
    elif response_text.startswith("[NEEDS_PICK]"):
        # An ambiguous app name was NOT launched; JARVIS asked which one. Same
        # reminder treatment as a confirm — the turn is waiting on the user.
        _start_confirm_reminder(ws, vf)


def decode_audio_to_wav(audio_bytes: bytes) -> str:
    """
    Decode arbitrary browser audio (WebM/Opus, OGG/Opus, MP3, WAV...) to a
    16kHz mono WAV file on disk. Returns the temp .wav path.
    Uses PyAV to decode, soundfile/whisper expect WAV PCM.
    """
    import av
    import soundfile as sf
    import numpy as np

    tmp_wav = tempfile.NamedTemporaryFile(suffix=".wav", delete=False).name

    try:
        # Decode with PyAV
        container = av.open(io.BytesIO(audio_bytes))
        # Find best audio stream
        audio_stream = None
        for s in container.streams:
            if s.type == "audio":
                audio_stream = s
                break
        if audio_stream is None:
            raise ValueError("No audio stream found in uploaded data")

        resampler = av.audio.resampler.AudioResampler(
            format="s16",
            layout="mono",
            rate=16000,
        )

        frames = []
        for frame in container.decode(audio_stream):
            for resampled in resampler.resample(frame):
                # resampled.frame is planar s16 -> convert to numpy
                import numpy as np
                arr = resampled.to_ndarray()
                # shape (channels, samples) -> flatten mono already
                if arr.ndim > 1:
                    arr = arr[0]
                frames.append(arr.astype(np.float32) / 32768.0)

        if not frames:
            raise ValueError("Decoded audio is empty")

        audio_np = np.concatenate(frames, axis=0).astype(np.float32)

        sf.write(tmp_wav, audio_np, 16000, subtype="PCM_16")
        return tmp_wav

    except Exception as e:
        # Fallback: maybe it's already a WAV
        try:
            import soundfile as sf
            sf.read(io.BytesIO(audio_bytes))
            # It's a valid wav, just write through
            with open(tmp_wav, "wb") as f:
                f.write(audio_bytes)
            return tmp_wav
        except Exception:
            raise RuntimeError(f"Could not decode audio: {e}")


import re as _re

# The anti-AI-ism filter (machine tics, em dashes) lives in plain_reply.tidy
# with the rest of the outgoing-text cleanup, where it is tested.


def strip_ai_artifacts(text: str) -> str:
    """Strip the worst P0 AI-isms from a reply before it is spoken.

    Removes chatbot artifacts and filler openers entirely, and converts em dashes /
    double-hyphens to a comma so speech does not stumble on a dash. Leaves already-clean,
    brisk replies untouched. Does not alter meaning."""
    # Internal markers, job ids, Markdown and machine tics out (plain_reply.py).
    # Only here, where replies leave the server: brain.think's own return value
    # keeps its markers for the command chain.
    import plain_reply
    return plain_reply.tidy(text)


# ---------------------------------------------------------------------------
# Phase 16.6 REV A (2026-08-25): edge-tts RESTORED as PRIMARY — the user wants
# the original en-GB-Ryan voice back. Piper stays available but OPT-IN only:
# set JARVIS_TTS_ENGINE=piper to use it (real-time offline, RTF 0.12 measured).
_TTS_ENGINE = (os.environ.get("JARVIS_TTS_ENGINE") or "edge").strip().lower()
_PIPER_MODEL = os.path.join(os.path.expanduser("~"), ".jarvis-tts",
                            "en_GB-nem-medium.onnx")
_piper_voice = None
_KOKORO_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "models", "kokoro")
_KOKORO_TTS = None


def _get_piper():
    """Load the piper voice once; return None on any failure (edge fallback)."""
    global _piper_voice
    if _piper_voice is not None:
        return _piper_voice
    try:
        from piper import PiperVoice
        if os.path.exists(_PIPER_MODEL):
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
                _piper_voice = ex.submit(PiperVoice.load, _PIPER_MODEL) \
                    .result(timeout=60)
            return _piper_voice
    except Exception as e:
        print(f"[TTS] piper unavailable ({e}); using edge-tts fallback.")
        _piper_voice = None
    return None


def _get_kokoro():
    """Load the Kokoro ONNX TTS once; return None on any failure (edge fallback)."""
    global _KOKORO_TTS
    if _KOKORO_TTS is not None:
        return _KOKORO_TTS
    try:
        from kokoro_onnx import Kokoro
        model = os.path.join(_KOKORO_DIR, "kokoro-v1.0.onnx")
        voices = os.path.join(_KOKORO_DIR, "voices-v1.0.bin")
        if os.path.exists(model) and os.path.exists(voices):
            import numpy as _np
            _np_load = _np.load
            _np.load = lambda *a, **k: _np_load(*a, allow_pickle=True, **k)
            _KOKORO_TTS = Kokoro(model, voices)
            _np.load = _np_load

            # kokoro-onnx 0.4.7 (latest) feeds `speed` as int32 while
            # kokoro-v1.0.onnx declares it float, so every synth died with
            #   INVALID_ARGUMENT ... Actual: (tensor(int32)), expected: (tensor(float))
            # and JARVIS silently fell back to edge-tts — which is why
            # JARVIS_TTS_ENGINE=kokoro never actually took effect. Coerce each
            # feed to the dtype the graph declares. This is a no-op once the
            # library is fixed upstream, and adapts to either export variant.
            _sess = _KOKORO_TTS.sess
            _raw_run = _sess.run
            _want = {i.name: i.type for i in _sess.get_inputs()}

            def _run_coerced(output_names, input_feed, *a, **kw):
                fixed = {}
                for _name, _val in input_feed.items():
                    _t = _want.get(_name)
                    if _t == "tensor(float)":
                        fixed[_name] = _np.asarray(_val, dtype=_np.float32)
                    elif _t == "tensor(int64)":
                        fixed[_name] = _np.asarray(_val, dtype=_np.int64)
                    else:
                        fixed[_name] = _val
                return _raw_run(output_names, fixed, *a, **kw)

            _sess.run = _run_coerced
            print("[TTS] Kokoro (local, offline) ready — JARVIS_TTS_ENGINE=kokoro")
            return _KOKORO_TTS
        print(f"[TTS] kokoro model missing in {_KOKORO_DIR}; using edge-tts fallback.")
    except Exception as e:
        print(f"[TTS] kokoro unavailable ({e}); using edge-tts fallback.")
        _KOKORO_TTS = None
    return None


async def tts_to_b64(text: str) -> str:
    """Synthesize `text` and return base64 audio.

    DEFAULT: edge-tts MP3 (the original JARVIS voice). Opt-in piper WAV via
    JARVIS_TTS_ENGINE=piper; falls back to edge automatically on any failure.
    """
    text = strip_ai_artifacts(text)
    if _TTS_ENGINE == "kokoro":
        kokoro = _get_kokoro()
        if kokoro is not None:
            try:
                def _synth():
                    import re as _re
                    import numpy as _np2
                    samples, sr = kokoro.create(
                        text, voice="am_michael", speed=1.1, lang="en-us")
                    import io as _io
                    import soundfile as _sf
                    buf = _io.BytesIO()
                    _sf.write(buf, _np2.asarray(samples), sr, format="WAV")
                    return buf.getvalue()
                wav_bytes = await asyncio.to_thread(_synth)
                return base64.b64encode(wav_bytes).decode()
            except Exception as e:
                print(f"[TTS] kokoro synth failed ({e}); falling back to edge-tts.")
    if _TTS_ENGINE == "piper":
        voice = _get_piper()
        if voice is not None:
            try:
                import wave
                wav_path = tempfile.NamedTemporaryFile(suffix=".wav",
                                                       delete=False).name
                # CPU synth blocks ~0.1-0.5s: keep the event loop responsive.
                def _synth():
                    with wave.open(wav_path, "wb") as wf:
                        wf.setnchannels(1)
                        wf.setsampwidth(2)
                        wf.setframerate(22050)
                        voice.synthesize_wav(text, wf)
                await asyncio.to_thread(_synth)
                with open(wav_path, "rb") as f:
                    b64 = base64.b64encode(f.read()).decode()
                os.unlink(wav_path)
                return b64
            except Exception as e:
                print(f"[TTS] piper synth failed ({e}); falling back to edge-tts.")
    import edge_tts
    from config import TTS_VOICE, TTS_RATE, TTS_VOLUME

    # rate/volume were previously dropped here, so the web UI always spoke at
    # +0% no matter what config said — only the CLI path honoured TTS_RATE.
    communicate = edge_tts.Communicate(text, TTS_VOICE, rate=TTS_RATE, volume=TTS_VOLUME)
    mp3_path = tempfile.NamedTemporaryFile(suffix=".mp3", delete=False).name
    await communicate.save(mp3_path)
    try:
        with open(mp3_path, "rb") as f:
            return base64.b64encode(f.read()).decode()
    finally:
        os.unlink(mp3_path)


_vf.set_synthesizer(tts_to_b64)
if _TTS_ENGINE == "kokoro" and _get_kokoro() is not None:
    _vf.warm_cache()


def _duck_failsafe_loop():
    """Periodically restore ducked music if a speech-end message was lost."""
    while True:
        time.sleep(30)
        try:
            voice_ducking.check_failsafe()
        except Exception:
            pass


threading.Thread(target=_duck_failsafe_loop, daemon=True, name="duck-failsafe").start()

# Rolling counters so the HUD can show a backend mix and a fallback rate rather
# than a decorative gauge. Kept in memory only: this is a demo readout, not
# metrics anyone should depend on.
TURN_STATS = {"turns": 0, "by_backend": {}, "latencies": []}

# Model facts, read once from Ollama. Static for the process, so there is no
# reason to pay for it on every turn.
MODEL_META = {}


def _load_model_meta():
    """Size/params/quant of the local model, straight from Ollama."""
    try:
        import urllib.request
        from config import OLLAMA_MODEL
        req = urllib.request.Request(
            "http://localhost:11434/api/show",
            data=json.dumps({"name": OLLAMA_MODEL}).encode(),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as r:
            d = json.load(r)
        det = d.get("details", {}) or {}
        MODEL_META.update({
            "params": det.get("parameter_size"),
            "quant": det.get("quantization_level"),
            "family": det.get("family"),
        })
        for line in (d.get("parameters") or "").splitlines():
            if line.strip().startswith("num_ctx"):
                MODEL_META["num_ctx"] = int(line.split()[-1])
        try:
            with urllib.request.urlopen("http://localhost:11434/api/tags", timeout=10) as r:
                for m in json.load(r).get("models", []):
                    if m.get("name", "").startswith(OLLAMA_MODEL):
                        MODEL_META["size_gb"] = round(m.get("size", 0) / 1e9, 2)
                        break
        except Exception:
            pass
    except Exception as e:
        print(f"[JARVIS Web] model meta unavailable ({e})", flush=True)


_load_model_meta()


def _log_turn_timing(t):
    """One line per voice turn: where the wall clock actually went.

    Stages are cumulative timestamps; we print the deltas so a slow turn names
    its own culprit instead of needing a hand-run benchmark.
    """
    try:
        order = [("decode", "start"), ("stt", "decode"),
                 ("brain", "stt"), ("tts", "brain")]
        parts = []
        for name, prev in order:
            if name in t and prev in t:
                parts.append(f"{name} {t[name] - t[prev]:.2f}s")
        last = t.get("tts") or t.get("brain") or t.get("stt")
        total = f" | total {last - t['start']:.2f}s" if last else ""
        print(f"[TIMING] {' | '.join(parts)}{total}", flush=True)
    except Exception:
        pass  # instrumentation must never break a turn


async def send_telemetry(websocket: WebSocket):
    """Push the per-turn numbers the HUD panels read.

    Only what the backend actually reported: a field the brain did not record is
    simply absent, so the HUD leaves that row blank instead of showing a made-up
    figure next to real ones.
    """
    try:
        stats = dict(getattr(brain, "last_stats", {}) or {})
        backend = stats.get("backend") or brain.last_backend
        if backend:
            TURN_STATS["turns"] += 1
            TURN_STATS["by_backend"][backend] = TURN_STATS["by_backend"].get(backend, 0) + 1
        if stats.get("latency_ms"):
            TURN_STATS["latencies"].append(stats["latency_ms"])
            del TURN_STATS["latencies"][:-10]          # last 10 turns only

        lat = sorted(TURN_STATS["latencies"])
        stats["p50_ms"] = lat[len(lat) // 2] if lat else None
        stats["turns"] = TURN_STATS["turns"]
        stats["mix"] = TURN_STATS["by_backend"]
        # Real window from the model itself rather than a hardcoded 8192.
        stats["context_limit"] = MODEL_META.get("num_ctx") if backend == "ollama" else None
        stats["history_msgs"] = len(getattr(brain, "conversation", []) or [])
        stats["model_meta"] = MODEL_META
        stats["local_only"] = bool(getattr(brain, "_local_only", False))
        await websocket.send_text(json.dumps({"type": "telemetry", "stats": stats}))
    except Exception as e:
        print(f"[WS] telemetry error: {e}", flush=True)


def _known_names():
    """Lowercase names of the user's pinned apps and registered sites, for
    the transcript picker: the transcript that names one is the one meant."""
    names = set()
    try:
        import app_abilities
        import curate
        apps = app_abilities.load_apps()["apps"]
        for key in curate.registered_apps():
            entry = apps.get(key) or {}
            for n in (key, entry.get("name"), entry.get("display_name"),
                      app_abilities.DISPLAY_NAMES.get(key)):
                if n:
                    names.add(" ".join(n.lower().split()))
    except Exception:
        pass
    try:
        import web_registry
        for key, site in (web_registry.load_registry().get("sites") or {}).items():
            names.add(key.lower())
            names.update(a.lower() for a in site.get("aliases") or [])
    except Exception:
        pass
    return {n for n in names if len(n) >= 3}


def _job_cards(recent_s: float = 900) -> list[dict]:
    """Active jobs and jobs that ended in the last `recent_s` seconds, shaped
    like job events plus their plain card fields (plain_reply.card)."""
    import jobs
    import plain_reply
    now = time.time()
    rows, seen = [], set()
    for j in jobs.active() + jobs.recent(8):
        ended = (j.get("started") or 0) + (j.get("elapsed") or 0)
        if j["id"] in seen or (j.get("state") not in ("queued", "running", "waiting-on-confirm")
                               and now - ended > recent_s):
            continue
        seen.add(j["id"])
        ev = {"type": "job", "id": j["id"], "task": j.get("task", ""), "state": j.get("state"),
              "note": (j.get("progress") or [None])[-1], "error": j.get("error"),
              "summary": j.get("summary"), "elapsed": j.get("elapsed")}
        rows.append({**ev, **plain_reply.card(ev)})
    return rows


# Replies that could not be sent because the client's socket closed mid-turn
# (a phone reconnecting, 2026-09-12: "websocket.send after websocket.close"),
# kept per client and sent when that client connects again. Last 3, 10 min.
_UNDELIVERED: dict[str, list] = {}
_UNDELIVERED_TTL_S = 600


def _stash_reply(client: str | None, text: str) -> None:
    if not client or not text:
        return
    rows = [r for r in _UNDELIVERED.get(client, []) if time.time() - r[0] < _UNDELIVERED_TTL_S]
    _UNDELIVERED[client] = (rows + [(time.time(), strip_ai_artifacts(text))])[-3:]
    print(f"[WS] reply kept for {client} (socket closed): {text[:60]!r}", flush=True)


async def _flush_undelivered(websocket: WebSocket, client: str) -> None:
    rows = [r for r in _UNDELIVERED.pop(client, []) if time.time() - r[0] < _UNDELIVERED_TTL_S]
    for _ts, text in rows:
        try:
            await websocket.send_text(json.dumps({
                "type": "response", "text": "While you were reconnecting: " + text,
                "audio": None, "deferred": True}))
        except Exception:
            _UNDELIVERED.setdefault(client, []).append((_ts, text))
            return


async def deliver_result(websocket: WebSocket, text: str, speak: bool, client: str | None = None):
    """Phase 4: deliver a deferred Hermes answer (called from the background thread
    via run_coroutine_threadsafe once Hermes returns). Sends it as a fresh
    `response` event with TTS, so the voice client hears the result whenever it
    lands — without having blocked the mic meanwhile."""
    try:
        await websocket.send_text(json.dumps({"type": "status", "state": "speaking"}))
        tts_b64 = await tts_to_b64(text) if speak else None
        text = strip_ai_artifacts(text)  # clean displayed text too, not just audio
        # JARVIS is about to talk — mute the server wake listener briefly so it
        # doesn't hear its own voice and re-trigger a capture. (Music is not TTS,
        # so this does NOT mute it during user speech — the whole point of the
        # fallback is to survive music.)
        try:
            wake_engine.suspend()
        except Exception:
            pass
        await websocket.send_text(json.dumps({
            "type": "response", "text": text, "audio": tts_b64, "deferred": True
        }))
        await send_telemetry(websocket)
        await websocket.send_text(json.dumps({"type": "status", "state": "idle"}))
    except Exception as e:
        print(f"[WS] deliver_result error: {e}", flush=True)
        _stash_reply(client, text)



def _no_cache(resp):
    """HUD pages must never be served from browser cache — a stale page makes
    the user see an old HUD after every redesign (bit us 2026-08-28)."""
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp


@app.get("/")
async def get():
    html_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "jarvis_hud_v3.html")
    with open(html_path, "r", encoding="utf-8") as f:
        html = f.read()
    return _no_cache(HTMLResponse(content=html, status_code=200))


@app.get("/manifest.webmanifest")
async def get_manifest():
    """Lets Android Chrome install the HUD to the home screen (standalone,
    no browser bar). Icons are served below."""
    return JSONResponse({
        "name": "Cygnus", "short_name": "Cygnus", "start_url": "/",
        "display": "standalone", "background_color": "#05070c",
        "theme_color": "#05070c",
        "icons": [{"src": "/icon-192.png", "sizes": "192x192", "type": "image/png",
                   "purpose": "any maskable"},
                  {"src": "/icon-512.png", "sizes": "512x512", "type": "image/png",
                   "purpose": "any maskable"}],
    }, media_type="application/manifest+json")


@app.get("/icon-{size}.png")
async def get_icon(size: int):
    from fastapi.responses import FileResponse
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", f"icon-{size}.png")
    if not os.path.exists(path):
        return JSONResponse({"error": "no such icon"}, status_code=404)
    return FileResponse(path, media_type="image/png")


@app.get("/hud.html")
async def get_hud():
    # The untouched HUD artifact (served into an iframe)
    html_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "hud_artifact.html")
    with open(html_path, "r", encoding="utf-8") as f:
        html = f.read()
    return _no_cache(HTMLResponse(content=html, status_code=200))


DELEGATE_REGISTRY_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "delegate_registry.json")


def _website_items():
    """Registered sites from web_registry.json as /apps items.

    Returns (items, hidden_count). Sites carry kind="website" and a `url`, so
    the panel can badge them and offer "Open Site" instead of a binary launch.
    """
    global _WEBSITE_KEYS
    items, hidden = [], 0
    try:
        import web_registry as wr
        sites = (wr.load_registry() or {}).get("sites", {})
    except Exception:
        return [], 0
    for key, site in (sites or {}).items():
        if not isinstance(site, dict):
            continue
        if site.get("hidden"):
            hidden += 1
            continue
        items.append({
            "name": site.get("name") or key,
            "key": key,
            "bin": "",
            "url": site.get("url", ""),
            "host": site.get("host", ""),
            "visits": site.get("visits", 0),
            "category": "website",
            "kind": "website",
            "broken": not bool(site.get("url")),
            "enabled": site.get("enabled", True),
            "adapter": "browser",
            "registered": site.get("registered", True),
            "hidden": False,
            "rule_drafts": site.get("rule_drafts") or [],
            "compiled_rules": site.get("compiled_rules") or [],
        })
    _WEBSITE_KEYS = frozenset(i["key"] for i in items)
    return items, hidden


def _capability_items():
    """Delegates from delegate_registry.json as /apps items (kind="capability").

    These are not launchable — they are what JARVIS can hand a task to — so the
    panel renders them read-only.
    """
    try:
        with open(DELEGATE_REGISTRY_PATH, "r", encoding="utf-8") as f:
            delegates = (json.load(f) or {}).get("delegates", [])
    except Exception:
        return []
    items = []
    for d in delegates or []:
        if not isinstance(d, dict) or not d.get("id"):
            continue
        did = d["id"]
        items.append({
            "name": did,
            # Namespaced so a delegate id can never collide with an app key.
            "key": "capability:%s" % did,
            "id": did,
            "bin": "",
            "category": "capability",
            "kind": "capability",
            "handles": d.get("handles") or [],
            "description": d.get("description", ""),
            "priority": d.get("priority", 0),
            "tools": d.get("tools") or [],
            "broken": False,
            "enabled": d.get("enabled", True),
            "adapter": d.get("kind", "local"),
            "registered": True,
            "hidden": False,
            "rule_drafts": [],
            "compiled_rules": [],
        })
    items.sort(key=lambda i: (-i["priority"], i["name"]))
    return items


@app.get("/apps")
async def get_apps():
    """The app registry for the HUD's Apps modal.

    Returns two views of the same scan:
    - `curated`: registered + not hidden — the short list the panel opens on.
    - `all`: every detected app that is not hidden — the raw scan, so the user
      can find something the curation missed (and hide the bloatware).
    `apps` stays as an alias of `curated` for older callers.

    Both views also carry the registered websites (kind="website") and the
    delegate capabilities (kind="capability"), so the panel is one surface for
    everything JARVIS can act on.
    """
    import os as _os
    from machine_capabilities import load_registry
    # Phase 8: opt-in flags (registered/enabled) come from curate.py
    try:
        from curate import is_registered, is_hidden
    except Exception:
        is_registered = lambda k: False
        is_hidden = lambda k: False
    websites, site_hidden = _website_items()
    capabilities = _capability_items()
    extras = websites + capabilities
    reg = load_registry()
    if not reg:
        # A fresh machine: no apps yet. The panel offers Add app / Full scan.
        return JSONResponse({"apps": list(extras), "curated": list(extras),
                             "all": list(extras), "count": len(extras),
                             "curated_count": len(extras),
                             "all_count": len(extras),
                             "hidden_count": site_hidden,
                             "website_count": len(websites),
                             "capability_count": len(capabilities),
                             "websites": websites,
                             "capabilities": capabilities})
    curated = []
    all_apps = []
    hidden_count = 0
    for key, entry in reg.get("apps", {}).items():
        if not isinstance(entry, dict):
            continue
        if is_hidden(key):
            hidden_count += 1
            continue
        binp = entry.get("bin") or ""
        registered = is_registered(key)
        item = {
            "name": entry.get("name", key),
            "key": key,
            "bin": binp,
            "category": entry.get("category", "other"),
            "kind": entry.get("kind", "gui"),
            "broken": bool(binp) and not _os.path.exists(binp),
            "enabled": entry.get("enabled", True),
            "adapter": entry.get("kind"),
            "registered": registered,
            "hidden": False,
            "rule_drafts": entry.get("rule_drafts") or [],
            "compiled_rules": entry.get("compiled_rules") or [],
            "abilities": entry.get("abilities") or [],
            "app_kind": entry.get("app_kind"),
            "deep_scan": entry.get("deep_scan"),
        }
        all_apps.append(item)
        if registered:
            curated.append(item)
    sort_key = lambda a: (a["category"], a["name"])
    curated.sort(key=sort_key)
    all_apps.sort(key=sort_key)
    # Websites and capabilities are curated by construction: they appear in
    # both views, after the installed apps.
    curated += extras
    all_apps += extras
    hidden_count += site_hidden
    return JSONResponse({
        "count": len(curated),
        "curated_count": len(curated),
        "all_count": len(all_apps),
        "hidden_count": hidden_count,
        "website_count": len(websites),
        "capability_count": len(capabilities),
        "generated": reg.get("generated"),
        "curated": curated,
        "all": all_apps,
        "websites": websites,
        "capabilities": capabilities,
        "apps": curated,
    })


@app.post("/apps/open")
async def post_apps_open(message: Request):
    """Open an app by registry key from the HUD modal. Same chain as voice."""
    try:
        body = await message.json()
    except Exception:
        return JSONResponse({"error": "bad json"}, status_code=400)
    key = (body.get("key") or "").strip()
    if not key:
        return JSONResponse({"error": "missing key"}, status_code=400)
    loop = asyncio.get_running_loop()
    # Websites carry kind="website" + url; open them via open_site, not open_application
    # (which expects a binary and would fail on an empty bin with WinError 2).
    if key in _WEBSITE_KEYS:
        result = await loop.run_in_executor(None, lambda: tools.open_site(key))
    else:
        result = await loop.run_in_executor(
            None, lambda: tools.execute_tool("open_application", {"app": key}))
    ok = not result.startswith("[Error]")
    return JSONResponse({"ok": ok, "result": result})


@app.post("/apps/toggle")
async def post_apps_toggle(message: Request):
    """Enable or disable a registered app in the registry."""
    try:
        body = await message.json()
    except Exception:
        return JSONResponse({"error": "bad json"}, status_code=400)
    key = (body.get("key") or "").strip()
    enable = bool(body.get("enabled", True))
    if not key:
        return JSONResponse({"error": "missing key"}, status_code=400)
    from machine_capabilities import REGISTRY_PATH
    reg_path = REGISTRY_PATH
    if not os.path.exists(reg_path):
        return JSONResponse({"error": "no registry"}, status_code=400)
    with open(reg_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    apps = data.setdefault("apps", {})
    if key not in apps:
        return JSONResponse({"error": "unknown app"}, status_code=400)
    apps[key]["enabled"] = enable
    tmp = reg_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp, reg_path)
    return JSONResponse({"ok": True, "key": key, "enabled": enable})


@app.post("/apps/rules")
async def post_apps_rules(message: Request):
    """Store rule drafts (and optional compiled rules) for a registered app."""
    try:
        body = await message.json()
    except Exception:
        return JSONResponse({"error": "bad json"}, status_code=400)
    key = (body.get("key") or "").strip()
    if not key:
        return JSONResponse({"error": "missing key"}, status_code=400)
    drafts = body.get("rule_drafts") or []
    # Key presence, not truthiness: an explicit `compiled_rules: []` commits an
    # empty set (wipes the rules); omitting it only stores drafts.
    compiled = body.get("compiled_rules")
    from machine_capabilities import REGISTRY_PATH
    reg_path = REGISTRY_PATH
    if not os.path.exists(reg_path):
        return JSONResponse({"error": "no registry"}, status_code=400)
    with open(reg_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    apps = data.setdefault("apps", {})
    if key not in apps:
        return JSONResponse({"error": "unknown app"}, status_code=400)
    if isinstance(compiled, list):
        import rules_compiler
        rules_compiler.commit_rules(key, compiled, reg_path, accept=True)
    else:
        apps[key]["rule_drafts"] = drafts
        tmp = reg_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        os.replace(tmp, reg_path)
    return JSONResponse({"ok": True, "key": key})


@app.post("/apps/rules/clear")
async def post_apps_rules_clear(message: Request):
    """Remove one committed rule (`rule_id`) or, when it is absent, every rule.

    Body: {"key": "spotify", "rule_id": "keep_it_quiet"}  — rule_id optional.
    """
    try:
        body = await message.json()
    except Exception:
        return JSONResponse({"error": "bad json"}, status_code=400)
    key = (body.get("key") or "").strip()
    rule_id = (body.get("rule_id") or "").strip()
    if not key:
        return JSONResponse({"error": "missing key"}, status_code=400)
    if not _known_app(key):
        return JSONResponse({"error": "unknown app"}, status_code=400)
    from machine_capabilities import REGISTRY_PATH
    import rules_compiler
    try:
        removed, remaining = rules_compiler.remove_rule(key, rule_id, REGISTRY_PATH)
    except KeyError:
        return JSONResponse({"error": "unknown app"}, status_code=400)
    if rule_id and not removed:
        return JSONResponse({"error": "unknown rule_id", "key": key, "rule_id": rule_id},
                            status_code=404)
    return JSONResponse({"ok": True, "key": key, "removed": removed, "remaining": remaining})


@app.post("/apps/register")
async def post_apps_register(message: Request):
    """Phase 8: opt an app in (or out) of the curated registry."""
    try:
        body = await message.json()
    except Exception:
        return JSONResponse({"error": "bad json"}, status_code=400)
    key = (body.get("key") or "").strip()
    registered = bool(body.get("registered", True))
    enabled = bool(body.get("enabled", True))
    if not key:
        return JSONResponse({"error": "missing key"}, status_code=400)
    import curate
    ok = curate.mark_registered(key, registered=registered, enabled=enabled)
    if not ok:
        return JSONResponse({"error": "unknown app"}, status_code=400)
    return JSONResponse({"ok": True, "key": key,
                         "registered": registered, "enabled": enabled})


@app.post("/apps/compile")
async def post_apps_compile(message: Request):
    """Compile with JARVIS: turn the drafts into a proposed rule set with AI.

    Each draft line may hold a command and what it should do ('search movies
    in brave -> search hollymoviehd.cc'). rules_ai reads the intent (router,
    then local Ollama); lines no model can read keep the keyword parser. A
    line that already has a structured rule is kept, and an older plain rule
    is recompiled with its saved action so the site it named is not lost.
    Returned `notes` say what JARVIS guessed. User owns the commit
    (POST /apps/rules).
    """
    try:
        body = await message.json()
    except Exception:
        return JSONResponse({"error": "bad json"}, status_code=400)
    key = (body.get("key") or "").strip()
    drafts = body.get("rule_drafts") or []
    if not key or not drafts:
        return JSONResponse({"error": "missing key or drafts"}, status_code=400)

    import rules_ai
    from machine_capabilities import REGISTRY_PATH
    entry = {}
    try:
        with open(REGISTRY_PATH, "r", encoding="utf-8") as f:
            entry = (json.load(f).get("apps") or {}).get(key) or {}
    except Exception:
        pass
    import rules_sim

    def _compile():
        say = rules_sim.reporter(key)
        try:
            say("Understanding your rules...", 10)
            rules, notes = rules_ai.compile_drafts(
                key, drafts, entry.get("compiled_rules") or [], entry)
            rules = rules_sim.simulate_all(key, rules, entry, progress=say)
            searches = [r for r in rules if (r.get("action") or {}).get("type") == "search_site"]
            if searches and all((r.get("verification") or {}).get("status") == "verified"
                                for r in searches):
                notes = [n for n in notes if "guessed" not in n]
            return rules, notes
        finally:
            rules_sim.finish_progress(key)

    # Model calls and real page checks take seconds: keep them off the event loop.
    proposed, notes = await asyncio.to_thread(_compile)
    return JSONResponse({
        "ok": True,
        "key": key,
        "proposed": proposed,
        "questions": notes,
        "notes": notes,
        "proposal_markdown": rules_ai.propose_markdown(key, proposed, notes),
    })


def _known_app(key):
    """True when `key` names an app in the on-disk registry."""
    from machine_capabilities import REGISTRY_PATH
    if not os.path.exists(REGISTRY_PATH):
        return False
    with open(REGISTRY_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)
    return key in (data.get("apps") or {})


@app.post("/apps/rules/begin")
async def post_apps_rules_begin(message: Request):
    """Phase 4: start voice rule authoring for an app.

    Body: {"app_key": "spotify", "phrase": "no explicit stuff and keep it quiet"}
    Returns either a clarification question or a proposal awaiting confirmation.
    Nothing is written here — the user owns the commit (/apps/rules/clarify).
    """
    try:
        body = await message.json()
    except Exception:
        return JSONResponse({"error": "bad json"}, status_code=400)
    key = (body.get("app_key") or body.get("key") or "").strip()
    phrase = (body.get("phrase") or "").strip()
    if not key:
        return JSONResponse({"error": "missing app_key"}, status_code=400)
    if not phrase:
        return JSONResponse({"error": "missing phrase"}, status_code=400)
    if not _known_app(key):
        return JSONResponse({"error": "unknown app"}, status_code=400)

    import rules_voice
    step = await asyncio.to_thread(rules_voice.begin_rule_setup, key, phrase)
    if step.get("status") == "error":
        return JSONResponse(step, status_code=400)
    return JSONResponse(dict(step, ok=True))


@app.post("/apps/rules/clarify")
async def post_apps_rules_clarify(message: Request):
    """Phase 4: answer one clarification question, or confirm the proposal.

    Body: {"app_key": "spotify", "rule_id": "keep_it_quiet", "answer": "under 40%"}
    An empty `rule_id` means "accept the proposal as-is". Once nothing is open
    the finalized rule set is committed to the registry.
    """
    try:
        body = await message.json()
    except Exception:
        return JSONResponse({"error": "bad json"}, status_code=400)
    key = (body.get("app_key") or body.get("key") or "").strip()
    rule_id = (body.get("rule_id") or "").strip()
    answer = (body.get("answer") or "").strip()
    if not key:
        return JSONResponse({"error": "missing app_key"}, status_code=400)
    if not answer:
        return JSONResponse({"error": "missing answer"}, status_code=400)

    import rules_voice
    step = await asyncio.to_thread(rules_voice.handle_clarification, key, rule_id, answer)
    if step.get("status") == "error":
        return JSONResponse(step, status_code=400)
    return JSONResponse(dict(step, ok=True))


@app.post("/apps/rules/test")
async def post_apps_rules_test(message: Request):
    """Run a command rule once, for real, before or after saving it.

    Body: {"app_key": "brave", "sample": "Dune", "rule_id": ""} — an empty
    rule_id means the proposal in progress. Opens the browser exactly like
    the spoken command would, so the user sees where the rule lands.
    """
    try:
        body = await message.json()
    except Exception:
        return JSONResponse({"error": "bad json"}, status_code=400)
    key = (body.get("app_key") or body.get("key") or "").strip()
    rid = (body.get("rule_id") or "").strip()
    sample = (body.get("sample") or "").strip()
    if not key:
        return JSONResponse({"error": "missing app_key"}, status_code=400)
    import rules_engine
    import rules_voice
    rule = None
    pend = rules_voice.pending_setup(key)
    if not rid and pend:
        rule = pend.get("proposed_rule")
    if rule is None and rid:
        rule = next((r for r in rules_voice.list_rules(key).get("rules") or []
                     if r.get("rule_id") == rid), None)
    if not rule:
        return JSONResponse({"error": "no rule to test"}, status_code=404)
    action = rules_engine.action_of(rule, key, rules_voice._app_entry(key))
    if not action:
        return JSONResponse({"error": "this rule doesn't open anything to test"},
                            status_code=400)
    if action.get("type") in ("search_site", "steps") and not sample:
        import rules_sim
        sample = rules_sim.sample_for(rule)
    result = await asyncio.to_thread(tools.run_rule_action, rule, key, action, sample)
    return JSONResponse({"ok": not result.startswith("[Error]"), "result": result,
                         "url": rules_engine.build_action_url(action, sample)})


def _apps_job(progress_key, work):
    """Run a slow Apps job (scan, AI sorting, launching an app) with progress
    published under progress_key for the panel's progress bar."""
    import rules_sim
    try:
        return work(rules_sim.reporter(progress_key))
    finally:
        rules_sim.finish_progress(progress_key)


async def _json_body(message):
    try:
        return await message.json()
    except Exception:
        return None


@app.post("/apps/add")
async def post_apps_add(message: Request):
    """Add one app by name or program path: register it, work out what it is,
    create its predefined rules, and deep-scan it (opening it once)."""
    body = await _json_body(message)
    query = ((body or {}).get("query") or "").strip()
    if not query:
        return JSONResponse({"error": "Type an app name or its program path."}, status_code=400)
    import app_abilities
    out = await asyncio.to_thread(
        _apps_job, "apps", lambda say: app_abilities.add_app(query, progress=say))
    return JSONResponse(dict(out, ok="error" not in out), status_code=400 if "error" in out else 200)


@app.post("/apps/fullscan")
async def post_apps_fullscan(message: Request):
    """Find every launchable app on this computer, hide noise, register the rest.
    Keeps every rule and choice the user already made."""
    import app_abilities
    out = await asyncio.to_thread(
        _apps_job, "apps", lambda say: app_abilities.full_scan(progress=say))
    return JSONResponse(dict(out, ok=True))


@app.post("/apps/abilities/generate")
async def post_apps_abilities_generate(message: Request):
    """Create predefined rules for all registered apps (or body.keys), without
    launching anything."""
    body = await _json_body(message) or {}
    keys = body.get("keys") if isinstance(body.get("keys"), list) else None
    import app_abilities
    out = await asyncio.to_thread(
        _apps_job, "apps", lambda say: app_abilities.generate(keys=keys, progress=say))
    return JSONResponse(dict(out, ok=True))


@app.post("/apps/abilities/scan")
async def post_apps_abilities_scan(message: Request):
    """Deep-scan one app's window ("Scan this app"). launch=true opens it if
    needed and closes it again afterwards."""
    body = await _json_body(message) or {}
    key = (body.get("key") or "").strip()
    if not key:
        return JSONResponse({"error": "missing key"}, status_code=400)
    import app_abilities
    launch = bool(body.get("launch", True))
    out = await asyncio.to_thread(
        _apps_job, key, lambda say: app_abilities.scan_app(key, launch=launch, progress=say))
    return JSONResponse(dict(out, ok="error" not in out), status_code=400 if "error" in out else 200)


@app.post("/apps/abilities/update")
async def post_apps_abilities_update(message: Request):
    """The user's on/off switch and consent level for one ability."""
    body = await _json_body(message) or {}
    import app_abilities
    ability = app_abilities.set_choice((body.get("key") or "").strip(),
                                       (body.get("id") or "").strip(),
                                       enabled=body.get("enabled"), level=body.get("level"))
    if ability is None:
        return JSONResponse({"error": "unknown ability"}, status_code=404)
    return JSONResponse({"ok": True, "ability": ability})


@app.post("/apps/abilities/test")
async def post_apps_abilities_test(message: Request):
    """Run one ability for real from the panel. `value` fills the ability's
    one needed detail (a website, a folder, a search), when it has one."""
    body = await _json_body(message) or {}
    key, aid = (body.get("key") or "").strip(), (body.get("id") or "").strip()
    import app_abilities
    entry = app_abilities.load_apps()["apps"].get(key) or {}
    ability = next((a for a in entry.get("abilities") or [] if a.get("id") == aid), None)
    if ability is None:
        return JSONResponse({"error": "unknown ability"}, status_code=404)
    args = {}
    needs = list((ability.get("needs") or {}).keys())
    if needs and str(body.get("value") or "").strip():
        args[needs[0]] = str(body["value"]).strip()[:300]
    result = await asyncio.to_thread(app_abilities.run_ability, key, aid, args)
    return JSONResponse({"ok": not result.startswith("[Error]"), "result": result})


@app.post("/apps/abilities/scan-all")
async def post_apps_scan_all(message: Request):
    """Start "Deep scan all" in the background: open, read and close each
    registered app not scanned yet, one at a time. Returns at once; progress
    is under key "apps-scan-all"."""
    import threading
    import app_abilities
    if not app_abilities.claim_scan_all():
        return JSONResponse({"error": "A deep scan of all apps is already running."}, status_code=409)
    total = len(app_abilities._scan_all_targets())
    threading.Thread(target=lambda: _apps_job(
        "apps-scan-all", lambda say: app_abilities.deep_scan_all(progress=say, claimed=True)),
        daemon=True).start()
    return JSONResponse({"ok": True, "started": True, "total": total})


@app.post("/apps/abilities/scan-all/cancel")
async def post_apps_scan_all_cancel(message: Request):
    """Stop after the app being scanned now; the next run resumes from there."""
    import app_abilities
    app_abilities.cancel_scan_all()
    return JSONResponse({"ok": True})


@app.get("/apps/abilities/scan-all")
async def get_apps_scan_all():
    import app_abilities
    return JSONResponse(dict(app_abilities.scan_all_status(), ok=True))


@app.get("/shadow/review")
async def get_shadow_review(all: int = 0):
    """Shadow mode: where the understand-first router would have acted
    differently from JARVIS, newest first, with readiness stats."""
    import intent_router
    return JSONResponse({"ok": True, "items": intent_router.review(disagreements_only=not all),
                         "stats": intent_router.stats()})


@app.post("/shadow/label")
async def post_shadow_label(message: Request):
    """The user's verdict on one shadow turn: "old", "new" or "neither"."""
    body = await _json_body(message) or {}
    import intent_router
    try:
        intent_router.label(str(body.get("id") or ""), str(body.get("verdict") or ""))
    except ValueError:
        return JSONResponse({"error": "verdict must be old, new or neither"}, status_code=400)
    return JSONResponse({"ok": True, "stats": intent_router.stats()})


@app.get("/prefs")
async def get_prefs():
    """Defaults JARVIS learned from use and you agreed to."""
    import preferences
    return JSONResponse({"ok": True, "defaults": preferences.all_defaults()})


@app.post("/prefs/forget")
async def post_prefs_forget(message: Request):
    body = await _json_body(message) or {}
    import preferences
    if not preferences.forget(str(body.get("topic") or "")):
        return JSONResponse({"error": "unknown default"}, status_code=404)
    return JSONResponse({"ok": True})


@app.get("/apps/rules/progress")
async def get_apps_rules_progress(key: str = ""):
    """What rule setup is doing right now, for the panel's progress bar:
    {"stage": "Checking https://...", "pct": 70, "done": false}."""
    import rules_sim
    return JSONResponse(dict(rules_sim.get_progress((key or "").strip()), ok=True))


@app.post("/apps/rules/recheck")
async def post_apps_rules_recheck(message: Request):
    """Simulate a saved rule again in a hidden browser and save what it finds.

    Body: {"key": "brave", "rule_id": "search_a_movie"}. A broken search
    address is replaced by a working one when the check finds it (the site's
    own search box, a common pattern, or the AI's repair).
    """
    try:
        body = await message.json()
    except Exception:
        return JSONResponse({"error": "bad json"}, status_code=400)
    key = (body.get("key") or body.get("app_key") or "").strip()
    rid = (body.get("rule_id") or "").strip()
    if not key or not rid:
        return JSONResponse({"error": "missing key or rule_id"}, status_code=400)
    import rules_compiler
    import rules_engine
    import rules_sim
    import rules_voice
    from machine_capabilities import REGISTRY_PATH
    entry = rules_voice._app_entry(key)
    rule = next((r for r in entry.get("compiled_rules") or [] if r.get("rule_id") == rid), None)
    if not rule:
        return JSONResponse({"error": "unknown rule_id"}, status_code=404)
    rule = dict(rule)
    if not isinstance(rule.get("action"), dict):
        # An older plain rule that names a site gets its derived action saved.
        derived = rules_engine.action_of(rule, key, entry)
        if derived:
            rule["action"] = derived
            rule["enforcement"] = "action"

    def _run():
        say = rules_sim.reporter(key)
        try:
            say("Re-checking your rule...", 10)
            return rules_sim.simulate(key, rule, entry, progress=say,
                                      user_text=rule.get("action_text")
                                      or rule.get("intent") or rule.get("source_phrase", ""))
        finally:
            rules_sim.finish_progress(key)

    new_rule, report = await asyncio.to_thread(_run)
    rules_compiler.commit_rules(key, [new_rule], REGISTRY_PATH, accept=True, merge=True)
    return JSONResponse({"ok": True, "key": key, "status": report["status"],
                         "steps": report["steps"], "judge": report["judge"],
                         "rule": new_rule})


@app.get("/apps/rules/list")
async def get_apps_rules_list(key: str = ""):
    """Phase 4: the rules currently committed for an app, human-readable."""
    key = (key or "").strip()
    if not key:
        return JSONResponse({"error": "missing key"}, status_code=400)
    import rules_voice
    out = rules_voice.list_rules(key)
    if out.get("status") == "error":
        return JSONResponse(out, status_code=400)
    return JSONResponse(dict(out, ok=True))


@app.get("/apps/registry")
async def get_apps_registry():
    """Full registry with registered/enabled/hidden flags, grouped for the
    Add-or-Remove-Programs-style panel. Returns:
    - installed (registered + enabled)
    - available (detected, not registered)
    - hidden (explicitly hidden by user)
    """
    from machine_capabilities import load_registry
    from curate import registered_apps, detected_but_unregistered, is_registered, is_enabled
    reg = load_registry()
    if not reg:
        return JSONResponse({"error": "no registry", "installed": [], "available": [], "hidden": [], "count": 0})
    apps = reg.get("apps", {})
    installed = []
    available = []
    hidden = []
    for key, entry in apps.items():
        if not isinstance(entry, dict):
            continue
        binp = entry.get("bin") or ""
        item = {
            "name": entry.get("name", key),
            "key": key,
            "bin": binp,
            "category": entry.get("category", "other"),
            "kind": entry.get("kind", "gui"),
            "broken": bool(binp) and not os.path.exists(binp),
            "enabled": entry.get("enabled", True),
            "hidden": entry.get("hidden", False),
            "confidence": entry.get("confidence", "medium"),
            "rule_drafts": entry.get("rule_drafts") or [],
            "compiled_rules": entry.get("compiled_rules") or [],
        }
        if entry.get("hidden", False):
            hidden.append(item)
        elif is_enabled(key):
            installed.append(item)
        elif is_registered(key):
            installed.append(item)
        else:
            available.append(item)
    installed.sort(key=lambda a: (a["category"], a["name"]))
    available.sort(key=lambda a: (a["category"], a["name"]))
    hidden.sort(key=lambda a: (a["category"], a["name"]))
    return JSONResponse({
        "count": len(installed) + len(available) + len(hidden),
        "installed": installed,
        "available": available,
        "hidden": hidden,
        "generated": reg.get("generated"),
    })


@app.post("/apps/hide")
async def post_apps_hide(message: Request):
    """Hide an app from the registry (won't show in installed or available).
    The user can un-hide it later via the panel."""
    try:
        body = await message.json()
    except Exception:
        return JSONResponse({"error": "bad json"}, status_code=400)
    key = (body.get("key") or "").strip()
    # `hidden` is the documented field; `hide` stays accepted for older callers.
    hide = bool(body.get("hidden", body.get("hide", True)))
    if not key:
        return JSONResponse({"error": "missing key"}, status_code=400)
    from machine_capabilities import REGISTRY_PATH
    if not os.path.exists(REGISTRY_PATH):
        return JSONResponse({"error": "no registry"}, status_code=400)
    with open(REGISTRY_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)
    apps = data.setdefault("apps", {})
    if key not in apps:
        return JSONResponse({"error": "unknown app"}, status_code=400)
    apps[key]["hidden"] = bool(hide)
    apps[key]["hidden_by"] = "user"      # a Full scan leaves this choice alone
    if hide and apps[key].get("registered"):
        apps[key]["registered"] = False  # unregistering on hide
        apps[key]["pinned_by"] = "user"
    tmp = REGISTRY_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp, REGISTRY_PATH)
    return JSONResponse({"ok": True, "key": key, "hidden": hide})


@app.get("/apps.html")
async def get_apps_panel():
    html_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "apps_panel.html")
    with open(html_path, "r", encoding="utf-8") as f:
        html = f.read()
    return _no_cache(HTMLResponse(content=html, status_code=200))


def _ws_origin_allowed(websocket: WebSocket) -> bool:
    """Browsers skip CORS for WebSockets, so without this any web page open
    on a device that can reach the server could connect to /ws and drive the
    tools. The Host must be one of _ALLOWED_HOSTS (HTTP middleware does not
    run for WebSockets). Then accept JARVIS's own pages (Origin matches
    Host), the JARVIS_ALLOWED_ORIGINS list, and non-browser clients that
    send no Origin (ws_verify.py)."""
    host = websocket.headers.get("host", "")
    if _hostname(host) not in _ALLOWED_HOSTS:
        return False
    origin = websocket.headers.get("origin")
    if not origin:
        return True
    if origin.split("://", 1)[-1].rstrip("/") == host:
        return True
    return origin.rstrip("/") in _ALLOWED_ORIGINS


def _ws_is_remote(websocket: WebSocket) -> bool:
    """A proxied client (tailscale serve adds X-Forwarded-For) or one that
    addressed the server by anything but its loopback name is not in this
    room."""
    if websocket.headers.get("x-forwarded-for"):
        return True
    return _hostname(websocket.headers.get("host", "")) not in ("127.0.0.1", "localhost", "[::1]")


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    global _WS_LOOP
    if not _ws_origin_allowed(websocket):
        print(f"[WS] rejected origin {websocket.headers.get('origin')!r}", flush=True)
        await websocket.close(code=1008)
        return
    await websocket.accept()
    _WS_LOOP = asyncio.get_running_loop()
    WS_CLIENTS.add(websocket)
    if _ws_is_remote(websocket):
        WS_REMOTE.add(websocket)
        print(f"[WS] remote client connected (host={websocket.headers.get('host')!r}, "
              f"via={websocket.headers.get('x-forwarded-for')!r})", flush=True)
    else:
        print("[WS] client connected", flush=True)
    # Whose conversation this socket's turns join: the phone's or this PC's
    # browser, unless "hello" names the desktop window or a session of its own.
    client_key = "remote" if _ws_is_remote(websocket) else "local"
    await _flush_undelivered(websocket, client_key)     # a reply this client missed

    async def _send_response(payload: dict):
        """Send a turn's reply; if the socket closed mid-turn, keep it for the
        client's next connection instead of losing it."""
        try:
            await websocket.send_text(json.dumps(payload))
        except Exception:
            _stash_reply(client_key, payload.get("text") or "")
            raise
    # Per-connection feedback policy: each client decides (and hears) its own
    # cues; the synthesizer and audio cache are shared module-wide.
    vf = _vf.Policy()
    vf.set_synthesizer(tts_to_b64)

    # Capture THIS connection's loop once: job events fire from worker threads
    # where asyncio.get_event_loop() raises RuntimeError, which silently killed
    # every job->HUD broadcast and proactive announcement.
    _ws_loop = asyncio.get_running_loop()

    # Progress queue: tools push mid-task status here from the think() thread,
    # and a background coroutine pops them and speaks them over the WS.
    import queue as _queue
    _progress_q = _queue.Queue()

    # Job registry -> HUD bridge + Phase 8 proactive voice (speak completions unprompted).
    import jobs as _jobs
    def _on_job_event(event):
        try:
            loop = _ws_loop
            if loop.is_running():
                asyncio.run_coroutine_threadsafe(
                    _broadcast_job(event), loop)
                # Phase 8: proactive voice for terminal states
                try:
                    import memory_hygiene as _mh
                    line = _mh.proactive_job_announcement(event)
                    if line:
                        # Route through the policy engine (high priority, but
                        # still deduped) so terminal announcements never double
                        # up with progress-channel speech.
                        spoken = vf.announce(f"job_{event.get('state')}", line)
                        if spoken:
                            asyncio.run_coroutine_threadsafe(_speak_proactive(spoken), loop)
                    # Phase B: autonomous step milestones ("Step one done.").
                    if event.get("state") == "running":
                        step = _vf.step_line(event.get("note") or "")
                        if step:
                            spoken = vf.announce("milestone", step)
                            print(f"[FEEDBACK] step cue -> {spoken!r}", flush=True)
                            if spoken:
                                asyncio.run_coroutine_threadsafe(
                                    _speak_proactive(spoken), loop)
                    # Cancel this job's milestone timer once it finishes.
                    if event.get("state") in ("done", "error", "timeout", "unverified"):
                        t = _TASK_MILESTONES.pop(event.get("id"), None)
                        if t:
                            t.cancel()
                except Exception:
                    pass
        except RuntimeError:
            pass

    async def _broadcast_job(event):
        try:
            # Plain title / label / tone / detail for the Active Tasks card;
            # the raw fields stay for anything that reads them.
            import plain_reply
            await websocket.send_text(json.dumps({**event, **plain_reply.card(event)}))
        except Exception:
            pass

    # Which model is answering, said when it changes for this client: kept by
    # client in router_health (notice_for), so a phone that reconnects does not
    # hear "Big Pickle ... is answering" again (the owner's ask, 2026-09-12).
    try:
        import router_health as _rh
    except Exception:
        _rh = None

    async def _say_model_notice():
        """Show and speak who is answering, when it changed since the last reply."""
        if _rh is None:
            return
        try:
            st = dict(getattr(brain, "last_stats", {}) or {})
            backend = st.get("backend") or brain.last_backend
            _note_answering_model(st, backend)        # the top bar keeps it between turns
            line = _rh.notice_for(client_key, st.get("model"), backend)
            if line:
                tts_b64 = await tts_to_b64(line)
                await websocket.send_text(json.dumps({"type": "progress", "text": line, "audio": tts_b64}))
        except Exception as e:
            print(f"[WS] model notice failed: {e}", flush=True)

    async def _speak_proactive(text: str):
        from starlette.websockets import WebSocketState
        if websocket.client_state != WebSocketState.CONNECTED:
            return
        try:
            print(f"[SPEAK_PROACTIVE] entering: {text!r}", flush=True)
            tts_b64 = await tts_to_b64(text)
            await websocket.send_text(json.dumps({"type": "response", "text": text, "audio": tts_b64, "proactive": True}))
            print(f"[SPEAK_PROACTIVE] sent ({len(tts_b64 or '')} b64)", flush=True)
        except Exception as e:
            print(f"[SPEAK_PROACTIVE] failed: {type(e).__name__}: {e}", flush=True)

    try:
        _jobs.add_listener(_on_job_event)
    except Exception:
        pass

    # Phase 8: start hygiene loop once per process (6h interval, daemon)
    try:
        import memory_hygiene as _mh2
        if not getattr(_mh2, "_started", False):
            _mh2.start_hygiene_loop(interval_hours=6)
            _mh2._started = True
    except Exception:
        pass

    async def _progress_reader():
        """Consume progress updates from tools and speak them as they arrive."""
        loop = asyncio.get_running_loop()
        while True:
            # Run the blocking get() in a thread so it doesn't stall the event loop.
            msg = await loop.run_in_executor(None, _progress_q.get)
            if msg is None:
                break
            try:
                # Policy engine decides whether this milestone is worth SPEAKING
                # (throttled/deduped); the HUD caption updates either way.
                spoken = vf.announce("progress", msg)
                tts_b64 = await tts_to_b64(spoken) if spoken is not None else None
                await websocket.send_text(json.dumps({
                    "type": "progress", "text": msg, "audio": tts_b64
                }))
            except Exception:
                # WS closed while task still running — drain the queue silently
                # so the think() thread doesn't block on a full queue.
                break

    def _push_progress(msg):
        """Called from the think() thread to enqueue a progress update."""
        _progress_q.put(msg)

    try:
        while True:
            data = await websocket.receive_text()
            message = json.loads(data)
            mtype = message.get("type")

            if mtype == "ping":
                await websocket.send_text(json.dumps({"type": "pong"}))

            elif mtype == "hello":
                if message.get("client") == "desktop":
                    WS_DESKTOP.add(websocket)
                    client_key = "desktop"
                    print("[WS] desktop app window connected", flush=True)
                if message.get("session"):
                    client_key = "session:" + str(message["session"])[:64]
                if client_key not in ("remote", "local"):
                    await _flush_undelivered(websocket, client_key)

            elif mtype == "speech":
                # Phase C: duck music while JARVIS speaks, restore when done.
                # Failsafe auto-unduck guards against a lost "end" message.
                state = (message.get("state") or "").strip().lower()
                try:
                    if state == "start":
                        await asyncio.to_thread(voice_ducking.duck)
                    elif state == "end":
                        await asyncio.to_thread(voice_ducking.unduck)
                except Exception as e:
                    print(f"[DUCK] {state} failed: {e}", flush=True)

            elif mtype == "listening":
                # The HUD is recording a command: it owns the mic, so the
                # server-side wake fallback holds off (capped, in case "end"
                # is lost) instead of decoding over the user's voice.
                try:
                    if message.get("state") == "start":
                        wake_engine.hold(_WAKE_HOLD_MS)
                    else:
                        wake_engine.release()
                except Exception:
                    pass

            elif mtype == "audio":
                # Browser sent a recorded clip (base64 WAV or WebM/Opus blob).
                # mode == "scan" -> transcribe only (client-side wake-word check),
                # no Gemini call, no TTS. Anything else keeps the original behaviour.
                audio_b64 = message.get("data", "")
                scan = message.get("mode") == "scan"
                # The browser's own WakeListener fired to produce this clip, so the
                # server-side wake engine must not also open a capture for the same
                # "jarvis". Tell it the wake was already handled client-side.
                try:
                    wake_engine.note_browser_wake()
                except Exception:
                    pass
                if not audio_b64:
                    await websocket.send_text(json.dumps({"type": "error", "text": "empty audio"}))
                    continue

                if not scan:
                    await websocket.send_text(json.dumps({"type": "status", "state": "transcribing"}))

                # Stage timing. Without this the only way to find out where a
                # turn goes is to stop the server and benchmark by hand.
                _t = {"start": time.time()}

                # The wake loop and this turn share 16 cores. Let the turn have
                # them: the browser's own WakeListener still covers wake words,
                # and a wake fired mid-turn is ignored by the HUD anyway.
                try:
                    wake_engine.pause()
                except Exception:
                    pass

                try:
                    audio_bytes = base64.b64decode(audio_b64)
                    wav_path = decode_audio_to_wav(audio_bytes)
                    _t["decode"] = time.time()

                    # Transcribe with Whisper, checked against the browser's live
                    # transcript when the HUD sent one (transcript_pick).
                    hint = "" if scan else " ".join(str(message.get("hint") or "").split())[:300]
                    source = "whisper"
                    if hint:
                        text, source, heard, conf = voice_engine.transcribe_with_hint(
                            wav_path, hint, known=_known_names())
                        print(f"[STT] whisper: {heard[:120]!r} (conf {conf:.2f}) | browser: "
                              f"{hint[:120]!r} -> {source}", flush=True)
                    else:
                        text = voice_engine._transcribe_from_file(wav_path)
                    _t["stt"] = time.time()
                    print(f"[STT] raw{' (scan)' if scan else ''}: {text!r}", flush=True)
                    os.unlink(wav_path)

                    if source == "unsure" and not scan:
                        # Neither transcript is trustworthy: ask, never answer
                        # a sentence nobody said.
                        line = "I didn't catch that, sir. Could you say it again?"
                        await websocket.send_text(json.dumps({
                            "type": "response", "text": line, "audio": await tts_to_b64(line)}))
                        await websocket.send_text(json.dumps({"type": "status", "state": "idle"}))
                        continue

                    if scan:
                        await websocket.send_text(json.dumps({
                            "type": "scan", "text": (text or "").strip()
                        }))
                        continue

                    if not text or not text.strip():
                        await websocket.send_text(json.dumps({
                            "type": "status", "state": "idle",
                            "text": "(no speech detected)"
                        }))
                        continue

                    # ---- Layer 1: Intake normalization + confirm gate ----
                    # Fixes bad voice/grammar before the router sees it.
                    # Clarify ONLY when we have an uncertain ENTITY guess
                    # ("play talor swift" -> did you mean taylor swift?).
                    # A verb with NO entity ('open' clipped by music) routes
                    # to the brain normally — it resolves apps far better
                    # than the tiny corpus, and asking 'did you mean open,
                    # the beatles?' for a clipped command is worse than trying.
                    intent = resolve_intent(text.strip(), INTAKE_CORPUS)
                    if (intent.needs_confirmation() and intent.candidates
                            and intent.entity is not None):
                        await websocket.send_text(json.dumps({
                            "type": "clarify",
                            "heard": text.strip(),
                            "echo": intent.echo,
                            "candidates": intent.candidates,
                        }))
                        await websocket.send_text(json.dumps({
                            "type": "status", "state": "idle",
                            "text": intent.echo
                        }))
                        continue

                    await websocket.send_text(json.dumps({
                        "type": "transcript", "text": text.strip()
                    }))
                    await websocket.send_text(json.dumps({"type": "status", "state": "thinking"}))
                    _wd = _ThinkingWatchdog(websocket, vf)
                    _wd.start()

                    loop = asyncio.get_running_loop()

                    # Start the progress reader for mid-task voice updates.
                    reader_task = asyncio.create_task(_progress_reader())

                    def on_hermes_done(result):
                        # Called from the background Hermes thread; hop back to the
                        # event loop to ship the answer to the client.
                        asyncio.run_coroutine_threadsafe(
                            deliver_result(websocket, result, speak=True, client=client_key), loop)

                    # Off the event loop: a turn that drives the browser can run for
                    # a minute, and blocking here stalls the WebSocket for its duration.
                    response = await asyncio.to_thread(
                        brain.think, text.strip(), on_hermes_done, _push_progress, client=client_key)
                    _t["brain"] = time.time()
                    _wd.stop()
                    _arm_phase_b(websocket, vf, response)

                    # The transcript already went out before think() started (that is
                    # the point — the user sees their words while JARVIS works).
                    # Re-sending it here made every voice turn emit two identical
                    # transcript messages, which the HUD would render as a duplicate
                    # user line now that it logs them.
                    sstore_log("user", text.strip())

                    # Signal the progress reader that think() is done.
                    _progress_q.put(None)
                    await reader_task

                    # Phase 4: if Hermes was delegated in the background, think()
                    # returns the "working" ack immediately. Send it, free the mic
                    # (state idle), and let deliver_result push the real answer later.
                    if isinstance(response, str) and response.startswith("⟳ HERMES_BACKGROUND:"):
                        ack = response.split(":", 1)[1].strip()
                        ack = strip_ai_artifacts(ack)
                        sstore_log("assistant", ack)
                        tts_b64 = await tts_to_b64(ack)
                        await websocket.send_text(json.dumps({
                            "type": "response", "text": ack, "audio": tts_b64, "deferred": False
                        }))
                        await websocket.send_text(json.dumps({"type": "status", "state": "idle"}))
                        continue

                    sstore_log("assistant", response)
                    await _say_model_notice()
                    await websocket.send_text(json.dumps({"type": "status", "state": "speaking"}))
                    tts_b64 = await tts_to_b64(response)
                    _t["tts"] = time.time()
                    _log_turn_timing(_t)
                    try:
                        wake_engine.suspend()
                    except Exception:
                        pass
                    await _send_response({
                        "type": "response",
                        "text": strip_ai_artifacts(response),
                        "audio": tts_b64
                    })
                    await send_telemetry(websocket)
                    await websocket.send_text(json.dumps({"type": "status", "state": "idle"}))

                except Exception as e:
                    print(f"[WS] audio error: {e}", flush=True)
                    await websocket.send_text(json.dumps({
                        "type": "error", "text": str(e)
                    }))
                finally:
                    try:
                        wake_engine.resume()
                    except Exception:
                        pass

            elif mtype == "command":
                # Client-side slash commands. Kept off the "text" path on
                # purpose: routing "/clear" through the brain would spend a whole
                # local turn deciding what to do with it, and the model might
                # answer instead of clearing anything.
                name = (message.get("name") or "").strip().lower()
                if name == "clear":
                    brain.reset(client=client_key)
                    TURN_STATS["turns"] = 0
                    TURN_STATS["by_backend"].clear()
                    TURN_STATS["latencies"].clear()
                    print("[WS] context cleared", flush=True)
                    await websocket.send_text(json.dumps({
                        "type": "cleared",
                        "text": "Context cleared. Starting fresh."
                    }))
                    await send_telemetry(websocket)
                    await websocket.send_text(json.dumps({"type": "status", "state": "idle"}))
                elif name == "redirect":
                    # Phase 4: mid-flight redirect — inject a new instruction into
                    # the warm Hermes session without starting a fresh task.
                    redirect_text = (message.get("text") or "").strip()
                    if not redirect_text:
                        await websocket.send_text(json.dumps({
                            "type": "error", "text": "redirect: no instruction given"
                        }))
                        continue
                    try:
                        from warm_harness import warm_redirect
                        await websocket.send_text(json.dumps({
                            "type": "status", "state": "thinking"
                        }))
                        loop = asyncio.get_running_loop()
                        result = await asyncio.to_thread(warm_redirect, redirect_text)
                        if result is None:
                            await websocket.send_text(json.dumps({
                                "type": "error",
                                "text": "No active Hermes session to redirect."
                            }))
                        else:
                            sstore_log("assistant", result)
                            await websocket.send_text(json.dumps({
                                "type": "response", "text": result,
                                "audio": await tts_to_b64(result)
                            }))
                    except Exception as e:
                        await websocket.send_text(json.dumps({
                            "type": "error", "text": f"redirect failed: {e}"
                        }))
                    await websocket.send_text(json.dumps({"type": "status", "state": "idle"}))
                else:
                    await websocket.send_text(json.dumps({
                        "type": "error", "text": f"unknown command: {name}"
                    }))

            elif mtype == "cue":
                # Spoken session cue (wake / close). The client requests a
                # short line ("what is it, sir?" / "standing by, sir") and the
                # server TTS-es it so JARVIS audibly signals listening state.
                cue_text = (message.get("text") or "").strip()
                if not cue_text:
                    continue
                try:
                    tts_b64 = await tts_to_b64(cue_text)
                    await websocket.send_text(json.dumps({
                        "type": "cue", "text": cue_text, "audio": tts_b64
                    }))
                except Exception as e:
                    print(f"[WS] cue TTS failed: {e}", flush=True)

            elif mtype == "text":
                # Direct text command. TTS audio only when "speak": true
                # (wake-word client uses this when the command arrived in the
                # same breath as "Jarvis", so it never re-records).
                user_text = message.get("text", "").strip()
                if not user_text:
                    continue
                speak = bool(message.get("speak"))
                await websocket.send_text(json.dumps({"type": "transcript", "text": user_text}))
                await websocket.send_text(json.dumps({"type": "status", "state": "thinking"}))
                _wd = _ThinkingWatchdog(websocket, vf)
                _wd.start()
                try:
                    loop = asyncio.get_running_loop()

                    # Start the progress reader: tools will push mid-task status
                    # and this coroutine speaks them as they arrive.
                    reader_task = asyncio.create_task(_progress_reader())

                    def on_hermes_done(result):
                        asyncio.run_coroutine_threadsafe(
                            deliver_result(websocket, result, speak, client=client_key), loop)

                    response = await asyncio.to_thread(
                        brain.think, user_text, on_hermes_done, _push_progress, client=client_key)
                    _wd.stop()
                    _arm_phase_b(websocket, vf, response)
                    sstore_log("user", user_text)

                    # Signal the progress reader that think() is done, then
                    # drain any remaining messages it queued.
                    _progress_q.put(None)
                    await reader_task

                    # Phase 4: background Hermes delegation — ack now, answer later.
                    if isinstance(response, str) and response.startswith("⟳ HERMES_BACKGROUND:"):
                        ack = response.split(":", 1)[1].strip()
                        ack = strip_ai_artifacts(ack)
                        sstore_log("assistant", ack)
                        tts_b64 = await tts_to_b64(ack) if speak else None
                        await websocket.send_text(json.dumps({
                            "type": "response", "text": ack, "audio": tts_b64, "deferred": False
                        }))
                        await websocket.send_text(json.dumps({"type": "status", "state": "idle"}))
                        continue

                    sstore_log("assistant", response)
                    await _say_model_notice()
                    tts_b64 = None
                    if speak:
                        await websocket.send_text(json.dumps({"type": "status", "state": "speaking"}))
                        tts_b64 = await tts_to_b64(response)
                        try:
                            wake_engine.suspend()
                        except Exception:
                            pass
                    await _send_response({
                        "type": "response", "text": strip_ai_artifacts(response), "audio": tts_b64
                    })
                    await send_telemetry(websocket)
                    await websocket.send_text(json.dumps({"type": "status", "state": "idle"}))
                except Exception as e:
                    print(f"[WS] text error: {e}", flush=True)
                    await websocket.send_text(json.dumps({"type": "error", "text": str(e)}))

            else:
                await websocket.send_text(json.dumps({
                    "type": "error", "text": f"unknown message type: {mtype}"
                }))

    except WebSocketDisconnect:
        print("[WS] client disconnected", flush=True)
        WS_CLIENTS.discard(websocket)
    except Exception as e:
        print(f"[WS] error: {e}", flush=True)
        WS_CLIENTS.discard(websocket)
    finally:
        WS_REMOTE.discard(websocket)
        WS_DESKTOP.discard(websocket)
        # This connection's job listener must not outlive it: stale listeners
        # spoke every finished job into closed sockets (5x after 5 reconnects).
        try:
            _jobs.remove_listener(_on_job_event)
        except Exception:
            pass


# The model that last answered a turn. A Hermes or tool turn records no model,
# so the top bar fell back to the configured one ("Gemini 3.8 Flash") while
# Big Pickle was the one answering (2026-09-12).
_LAST_ANSWERED = {"model": None}


def _note_answering_model(stats: dict, backend: str | None) -> None:
    from config import OLLAMA_MODEL
    if (stats or {}).get("model"):
        _LAST_ANSWERED["model"] = stats["model"]
    elif backend == "ollama":
        _LAST_ANSWERED["model"] = OLLAMA_MODEL


@app.get("/status")
async def get_status():
    """Report the backend actually in use, not a hardcoded guess."""
    from config import OLLAMA_MODEL, OLLAMA_BASE_URL
    # Ollama was missing from this chain entirely, so with local-only on (every
    # cloud flag False) it fell through to the Gemini branch and reported
    # brain=gemini-2.5-flash / endpoint=google-genai while actually running the
    # local model. That readout is why JARVIS looked like it kept switching.
    # Gemini is intentionally disabled (its key is reserved for the user's
    # portfolio), so it is never the resolved model/endpoint.
    if getattr(brain, "_local_only", False) or getattr(brain, "_prefer_local", False):
        model, endpoint = OLLAMA_MODEL, OLLAMA_BASE_URL
    elif brain._use_router:
        model, endpoint = ROUTER_MODEL, ROUTER_BASE_URL
    elif brain._use_cerebras:
        model, endpoint = CEREBRAS_MODEL, CEREBRAS_BASE_URL
    elif brain._use_groq:
        model, endpoint = GROQ_MODEL, GROQ_BASE_URL
    elif getattr(brain, "_use_ollama", False):
        model, endpoint = OLLAMA_MODEL, OLLAMA_BASE_URL
    else:
        model, endpoint = OLLAMA_MODEL, OLLAMA_BASE_URL
    # "brain" used to report the configured router model even when a different
    # backend answered, so the HUD showed brain=ag/claude-sonnet-4-6 next to
    # answered-by=ollama and looked self-contradictory. Report what actually ran.
    _st = getattr(brain, "last_stats", {}) or {}
    _note_answering_model(_st, _st.get("backend") or getattr(brain, "last_backend", None))
    actual = _st.get("model") or _LAST_ANSWERED["model"]
    # Route reflects the actual chain: router -> cerebras -> groq -> local.
    # Gemini is intentionally omitted (reserved for portfolio work).
    if getattr(brain, "_local_only", False):
        route = "local only"
    else:
        hops = []
        if getattr(brain, "_prefer_local", False):
            hops.append("local")
        if brain._use_router:
            hops.append("router")
        if brain._use_cerebras:
            hops.append("cerebras")
        if brain._use_groq:
            hops.append("groq")
        if getattr(brain, "_use_ollama", False) and not getattr(brain, "_prefer_local", False):
            hops.append("local")
        route = " > ".join(hops)
    return {
        "status": "running",
        "brain": actual or model,
        # The same model as the user would name it, for the top bar.
        "brain_label": __import__("router_health").friendly(actual or model),
        "configured": model,
        "local_only": bool(getattr(brain, "_local_only", False)),
        "route": route,
        "endpoint": endpoint,
        # Gemini is off; the final real fallback is the local model.
        "fallback": None if getattr(brain, "_local_only", False) else OLLAMA_MODEL,
        "last_backend": brain.last_backend,
        "voice": f"whisper:{WHISPER_MODEL}",
        "tts": _TTS_ENGINE,
        "feedback_mode": _vf.current_mode(),
        # Background job table (all tiers) for HUD/programmatic polling.
        "jobs_active": __import__("jobs").active(),
        "jobs_recent": __import__("jobs").recent(8),
        "jobs_status_line": __import__("jobs").status_line(),
        # The same cards the job events carry, so a page that just opened
        # shows what is running and what ended in the last 15 minutes.
        "job_cards": _job_cards(),
        # Server-side wake-word fallback health (music-proof). 'healthy': mic live
        # and listening; false just means the fallback is off — the browser's own
        # Web Speech WakeListener still works on its own.
        "wake_server": {
            "healthy": bool(getattr(wake_engine, "healthy", False)),
            "error": getattr(wake_engine, "last_err", "") or None,
        },
    }


@app.post("/voice_feedback")
async def post_voice_feedback(message: Request):
    """HUD toggle: switch the voice-feedback mode at runtime."""
    try:
        body = await message.json()
    except Exception:
        return JSONResponse({"error": "bad json"}, status_code=400)
    try:
        mode = _vf.set_mode(body.get("mode") or "")
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    print(f"[FEEDBACK] mode -> {mode}", flush=True)
    return {"ok": True, "mode": mode}


@app.get("/music/now")
async def get_music_now():
    """Now-playing metadata for the HUD card. Idle when no music window is open."""
    import music_agent
    try:
        info = await asyncio.to_thread(music_agent.now_playing)
    except Exception as e:
        print(f"[MUSIC] now_playing failed: {e}", flush=True)
        info = {"idle": True}
    return info


@app.post("/music")
async def post_music(message: Request):
    """Music controls from the HUD card. stop_yt pauses/navigates YT Music away;
    spotify_toggle presses the media play/pause key (toggles Spotify)."""
    try:
        body = await message.json()
    except Exception:
        return JSONResponse({"error": "bad json"}, status_code=400)
    action = (body.get("action") or "").strip().lower()
    if action == "stop_yt":
        import music_agent
        out = await asyncio.to_thread(music_agent.stop_music)
        return {"ok": True, "action": action, "result": out[:200]}
    if action == "spotify_toggle":
        from tools import stop_spotify
        out = await asyncio.to_thread(stop_spotify)
        return {"ok": True, "action": action, "result": out[:200]}
    return JSONResponse({"error": f"unknown action: {action}"}, status_code=400)


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("JARVIS_PORT", "8000"))
    # This computer only: the tools open apps and drive the browser with no
    # login. Remote devices come in through a proxy on this machine
    # (tailscale serve); JARVIS_HOST=0.0.0.0 re-exposes it on the LAN, and the
    # LAN address must then be listed in JARVIS_ALLOWED_ORIGINS as well.
    host = os.environ.get("JARVIS_HOST", "127.0.0.1")
    print(f"[JARVIS Web] Starting on http://{host}:{port}  (ws://{host}:{port}/ws)")
    uvicorn.run(app, host=host, port=port)
