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
import base64
import tempfile
import asyncio
import threading

from fastapi import FastAPI, WebSocket
from fastapi.responses import HTMLResponse
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

print("[JARVIS Web] Ready.", flush=True)


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


async def tts_to_b64(text: str) -> str:
    """Synthesize `text` with edge-tts and return base64 mp3."""
    import edge_tts
    from config import TTS_VOICE, TTS_RATE, TTS_VOLUME

    text = strip_ai_artifacts(text)
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


@app.get("/")
async def get():
    html_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "jarvis_visual.html")
    with open(html_path, "r", encoding="utf-8") as f:
        html = f.read()
    return HTMLResponse(content=html, status_code=200)


@app.get("/hud.html")
async def get_hud():
    # The untouched HUD artifact (served into an iframe)
    html_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "hud_artifact.html")
    with open(html_path, "r", encoding="utf-8") as f:
        html = f.read()
    return HTMLResponse(content=html, status_code=200)


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    WS_CLIENTS.add(websocket)
    print("[WS] client connected", flush=True)

    # Progress queue: tools push mid-task status here from the think() thread,
    # and a background coroutine pops them and speaks them over the WS.
    import queue as _queue
    _progress_q = _queue.Queue()

    async def _progress_reader():
        """Consume progress updates from tools and speak them as they arrive."""
        loop = asyncio.get_running_loop()
        while True:
            # Run the blocking get() in a thread so it doesn't stall the event loop.
            msg = await loop.run_in_executor(None, _progress_q.get)
            if msg is None:
                break
            try:
                tts_b64 = await tts_to_b64(msg)
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
                    # Fixes bad voice/grammar before the router sees it, and
                    # offers "did you mean?" chips when confidence is low so a
                    # mis-hear becomes one tap instead of a wrong action.
                    intent = resolve_intent(text.strip(), INTAKE_CORPUS)
                    if intent.needs_confirmation() and intent.candidates:
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
                else:
                    await websocket.send_text(json.dumps({
                        "type": "error", "text": f"unknown command: {name}"
                    }))

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
        # Server-side wake-word fallback health (music-proof). 'healthy': mic live
        # and listening; false just means the fallback is off — the browser's own
        # Web Speech WakeListener still works on its own.
        "wake_server": {
            "healthy": bool(getattr(wake_engine, "healthy", False)),
            "error": getattr(wake_engine, "last_err", "") or None,
        },
    }


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("JARVIS_PORT", "8000"))
    print(f"[JARVIS Web] Starting on http://localhost:{port}  (ws://localhost:{port}/ws)")
    uvicorn.run(app, host="0.0.0.0", port=port)
