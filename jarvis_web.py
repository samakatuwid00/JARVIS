#!/usr/bin/env python3
"""
JARVIS Web Interface - FastAPI backend for voice visual JARVIS

WebSocket endpoint for real-time STT/TTS loop with animated orb visualization.
Browser captures mic -> sends audio (WebM/Opus) -> backend transcribes with Whisper
-> Gemini thinks -> edge-tts speaks -> audio sent back to browser.
"""

import os
import io
import json
import time
import base64
import tempfile
import asyncio
import threading

from fastapi import FastAPI, Request, WebSocket
from fastapi.responses import HTMLResponse, JSONResponse
from starlette.websockets import WebSocketDisconnect

from voice_engine import VoiceEngine
from wake_engine import WakeEngine
from brain_gemini import JarvisBrain
from session_store import log as sstore_log
import intake
from intake import load_corpus, resolve_intent

# Layer 1 (Intake) corpus — portable data, reloaded once at boot.
INTAKE_CORPUS = load_corpus()
from config import (GEMINI_MODEL, ROUTER_BASE_URL, ROUTER_MODEL, WHISPER_MODEL,
                   GROQ_MODEL, GROQ_BASE_URL, CEREBRAS_MODEL, CEREBRAS_BASE_URL)

app = FastAPI(title="JARVIS Voice Interface")

# Connected voice clients (JARVIS HUD tabs) — the server-side wake engine alerts
# all of them so a wake detected on the server opens the command window in the UI.
WS_CLIENTS = set()

# Global singletons (Whisper load is slow, do it once)
print("[JARVIS Web] Loading VoiceEngine (Whisper)...", flush=True)
voice_engine = VoiceEngine()
print(f"[JARVIS Web] Loading Brain ({ROUTER_MODEL})...", flush=True)
brain = JarvisBrain()
import tools  # for the /apps/open endpoint (same dispatcher the brain uses)

# Server-side wake-word fallback. The browser's Web Speech WakeListener is primary,
# but it degrades when music/ambient noise bleeds into the mic — this catches the
# wake word with Whisper (verified robust to music) and tells the HUD to open its
# command window. Healthy=false just means "no server-side fallback"; the browser
# listener still works on its own.
wake_engine = WakeEngine(voice_engine)


def broadcast_wake():
    """Called by the server wake engine when it hears 'jarvis'."""
    print("[WakeEngine] broadcasting wake to HUD client(s)", flush=True)
    dead = set()
    for ws in list(WS_CLIENTS):
        try:
            asyncio.run_coroutine_threadsafe(
                ws.send_text(json.dumps({"type": "wake", "source": "server"})),
                asyncio.get_event_loop())
        except Exception:
            dead.add(ws)
    WS_CLIENTS.difference_update(dead)


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
            await _send_cue(ws, "confirm_pending")
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

# Lightweight P0 anti-AI-ism filter (mirrors the avoid-ai-writing skill's quick-wins).
# Regex-only, microseconds of work, so it adds no latency to JARVIS's spoken replies.
# It runs on every reply right before TTS (see tts_to_b64). It does NOT rewrite prose
# (the full skill is for long-form); it only strips the worst machine tics the voice
# model might still emit.
_AI_ARTIFACT_PATTERNS = [
    # chatbot pleasantries (punctuation inside the match so trailing \b isn't needed)
    _re.compile(r"\b(great question[!.]?|i hope this helps[!.]?|absolutely[!.]?|"
                r"certainly[!.]?|you'?re welcome[!.]?|feel free to (?:ask|reach out)|"
                r"let me know if you need anything)\b", _re.IGNORECASE),
    # filler openers
    _re.compile(r"\b(let's explore|let's dive in|let's break this down|let me break this down)\b",
                _re.IGNORECASE),
    # significance inflation on routine facts
    _re.compile(r"\b(a pivotal moment|a game-?changer|a watershed moment|the future looks bright|"
                r"only time will tell)\b", _re.IGNORECASE),
]
_EM_DASH = _re.compile(r"—|--")  # em dash and double-hyphen


def strip_ai_artifacts(text: str) -> str:
    """Strip the worst P0 AI-isms from a reply before it is spoken.

    Removes chatbot artifacts and filler openers entirely, and converts em dashes /
    double-hyphens to a comma so speech does not stumble on a dash. Leaves already-clean,
    brisk replies untouched. Does not alter meaning."""
    if not text:
        return text
    for pat in _AI_ARTIFACT_PATTERNS:
        text = pat.sub("", text)
    # em dash / double-hyphen -> comma (read naturally)
    text = _EM_DASH.sub(",", text)
    # tidy the spaces left by removed phrases: collapse 2+ spaces, and " ," -> ","
    text = _re.sub(r"\s{2,}", " ", text)
    text = _re.sub(r"\s+,", ",", text)
    text = text.strip()
    # drop a leading comma/space that a removed opener may have left
    text = _re.sub(r"^[\s,]+", "", text)
    return text


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


async def deliver_result(websocket: WebSocket, text: str, speak: bool):
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



def _no_cache(resp):
    """HUD pages must never be served from browser cache — a stale page makes
    the user see an old HUD after every redesign (bit us 2026-08-28)."""
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp


@app.get("/")
async def get():
    html_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "jarvis_visual.html")
    with open(html_path, "r", encoding="utf-8") as f:
        html = f.read()
    return _no_cache(HTMLResponse(content=html, status_code=200))


@app.get("/hud.html")
async def get_hud():
    # The untouched HUD artifact (served into an iframe)
    html_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "hud_artifact.html")
    with open(html_path, "r", encoding="utf-8") as f:
        html = f.read()
    return _no_cache(HTMLResponse(content=html, status_code=200))


@app.get("/apps")
async def get_apps():
    """The app registry for the HUD's Apps modal.

    Returns launchable apps grouped for display: name, category, path,
    broken flag. Sorted by category then name. No auth needed — local only,
    and the registry is already gitignored.
    """
    import os as _os
    from machine_capabilities import load_registry
    # Phase 8: opt-in flags (registered/enabled) come from curate.py
    try:
        from curate import is_registered, is_enabled
    except Exception:
        is_registered = lambda k: False
        is_enabled = lambda k: False
    reg = load_registry()
    if not reg:
        return JSONResponse({"error": "no registry", "apps": [], "count": 0})
    out = []
    for key, entry in reg.get("apps", {}).items():
        binp = entry.get("bin") or ""
        out.append({
            "name": entry.get("name", key),
            "key": key,
            "bin": binp,
            "category": entry.get("category", "other"),
            "kind": entry.get("kind", "gui"),
            "broken": bool(binp) and not _os.path.exists(binp),
            "enabled": entry.get("enabled", True),
            "adapter": entry.get("kind"),
            "registered": is_registered(key),
            "rule_drafts": entry.get("rule_drafts") or [],
            "compiled_rules": entry.get("compiled_rules") or [],
        })
    out.sort(key=lambda a: (a["category"], a["name"]))
    return JSONResponse({"count": len(out), "generated": reg.get("generated"), "apps": out})


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
    compiled = body.get("compiled_rules") or []
    from machine_capabilities import REGISTRY_PATH
    reg_path = REGISTRY_PATH
    if not os.path.exists(reg_path):
        return JSONResponse({"error": "no registry"}, status_code=400)
    with open(reg_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    apps = data.setdefault("apps", {})
    if key not in apps:
        return JSONResponse({"error": "unknown app"}, status_code=400)
    if compiled:
        import rules_compiler
        rules_compiler.commit_rules(key, compiled, reg_path, accept=True)
    else:
        apps[key]["rule_drafts"] = drafts
        tmp = reg_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        os.replace(tmp, reg_path)
    return JSONResponse({"ok": True, "key": key})


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
    """Phase 3.5 wizard: turn raw rule drafts into a proposed ruleset.

    Runs rules_compiler.parse_scaffold, then auto-resolves ambiguous clauses
    with a SAFE DEFAULT answer (volume -> 60% all sessions; else -> the phrase
    as intent, all sessions) so the UI receives a complete proposed ruleset.
    The inferred `questions` are returned so the user can correct JARVIS's
    guess before accepting. User owns the commit (POST /apps/rules).
    """
    try:
        body = await message.json()
    except Exception:
        return JSONResponse({"error": "bad json"}, status_code=400)
    key = (body.get("key") or "").strip()
    drafts = body.get("rule_drafts") or []
    if not key or not drafts:
        return JSONResponse({"error": "missing key or drafts"}, status_code=400)

    import rules_compiler as rc
    phrase = " and ".join(drafts)
    parsed = rc.parse_scaffold(key, phrase)
    candidates = parsed["candidate_rules"]

    # Auto-resolve ambiguous clauses with a safe default; capture the question
    # so the UI can surface "JARVIS assumed X — correct me if wrong".
    answers = {}
    questions = []
    for r in candidates:
        if r.get("needs_clarification"):
            low = (r.get("source_phrase") or "").lower()
            if any(m in low for m in ("quiet", "loud", "soft", "low", "calm", "chill")):
                answers[r["rule_id"]] = f"under 60% {key} volume all sessions"
            else:
                answers[r["rule_id"]] = f"{r['source_phrase']}, all sessions"
            if r.get("clarification_question"):
                questions.append(r["clarification_question"])

    proposed = rc.apply_clarifications(key, candidates, answers)

    # Phase 3.5 guard: a draft like "never X even if I ask" is a HARD block,
    # not a soft preference. Promote any rule the escalation detector flags so
    # we never silently downgrade a user's explicit "must not" into a soft nudge.
    for r in proposed:
        if rc.detect_hard_escalation(r) and r.get("enforcement") != "hard":
            r["enforcement"] = "hard"
            r["intent"] = (r.get("intent") or "") + " (hard-escalated: user said never/even if I ask)"

    return JSONResponse({
        "ok": True,
        "key": key,
        "proposed": proposed,
        "questions": questions,
        "proposal_markdown": rc.propose_ruleset(key, proposed),
    })


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
    hide = bool(body.get("hide", True))
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
    if hide and apps[key].get("registered"):
        apps[key]["registered"] = False  # unregistering on hide
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


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    WS_CLIENTS.add(websocket)
    print("[WS] client connected", flush=True)
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
            await websocket.send_text(json.dumps(event))
        except Exception:
            pass

    async def _speak_proactive(text: str):
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

                try:
                    audio_bytes = base64.b64decode(audio_b64)
                    wav_path = decode_audio_to_wav(audio_bytes)

                    # Transcribe with Whisper
                    text = voice_engine._transcribe_from_file(wav_path)
                    os.unlink(wav_path)

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
                            deliver_result(websocket, result, speak=True), loop)

                    # Off the event loop: a turn that drives the browser can run for
                    # a minute, and blocking here stalls the WebSocket for its duration.
                    response = await asyncio.to_thread(
                        brain.think, text.strip(), on_hermes_done, _push_progress)
                    _wd.stop()
                    _arm_phase_b(websocket, vf, response)

                    await websocket.send_text(json.dumps({"type": "transcript", "text": text.strip()}))
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
                    await websocket.send_text(json.dumps({"type": "status", "state": "speaking"}))
                    tts_b64 = await tts_to_b64(response)
                    try:
                        wake_engine.suspend()
                    except Exception:
                        pass
                    await websocket.send_text(json.dumps({
                        "type": "response",
                        "text": strip_ai_artifacts(response),
                        "audio": tts_b64
                    }))
                    await send_telemetry(websocket)
                    await websocket.send_text(json.dumps({"type": "status", "state": "idle"}))

                except Exception as e:
                    print(f"[WS] audio error: {e}", flush=True)
                    await websocket.send_text(json.dumps({
                        "type": "error", "text": str(e)
                    }))

            elif mtype == "command":
                # Client-side slash commands. Kept off the "text" path on
                # purpose: routing "/clear" through the brain would spend a whole
                # local turn deciding what to do with it, and the model might
                # answer instead of clearing anything.
                name = (message.get("name") or "").strip().lower()
                if name == "clear":
                    brain.reset()
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
                            deliver_result(websocket, result, speak), loop)

                    response = await asyncio.to_thread(
                        brain.think, user_text, on_hermes_done, _push_progress)
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
                    tts_b64 = None
                    if speak:
                        await websocket.send_text(json.dumps({"type": "status", "state": "speaking"}))
                        tts_b64 = await tts_to_b64(response)
                        try:
                            wake_engine.suspend()
                        except Exception:
                            pass
                    await websocket.send_text(json.dumps({
                        "type": "response", "text": response, "audio": tts_b64
                    }))
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
    actual = (getattr(brain, "last_stats", {}) or {}).get("model")
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
    print(f"[JARVIS Web] Starting on http://localhost:{port}  (ws://localhost:{port}/ws)")
    uvicorn.run(app, host="0.0.0.0", port=port)
