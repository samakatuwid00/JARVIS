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
from brain_gemini import JarvisBrain
from config import (GEMINI_MODEL, ROUTER_BASE_URL, ROUTER_MODEL, WHISPER_MODEL,
                   GROQ_MODEL, GROQ_BASE_URL, CEREBRAS_MODEL, CEREBRAS_BASE_URL)

app = FastAPI(title="JARVIS Voice Interface")

# Global singletons (Whisper load is slow, do it once)
print("[JARVIS Web] Loading VoiceEngine (Whisper)...", flush=True)
voice_engine = VoiceEngine()
print(f"[JARVIS Web] Loading Brain ({ROUTER_MODEL})...", flush=True)
brain = JarvisBrain()
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


async def tts_to_b64(text: str) -> str:
    """Synthesize `text` with edge-tts and return base64 mp3."""
    import edge_tts
    from config import TTS_VOICE

    communicate = edge_tts.Communicate(text, TTS_VOICE)
    mp3_path = tempfile.NamedTemporaryFile(suffix=".mp3", delete=False).name
    await communicate.save(mp3_path)
    try:
        with open(mp3_path, "rb") as f:
            return base64.b64encode(f.read()).decode()
    finally:
        os.unlink(mp3_path)


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
    print("[WS] client connected", flush=True)

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

                    await websocket.send_text(json.dumps({
                        "type": "transcript", "text": text.strip()
                    }))
                    await websocket.send_text(json.dumps({"type": "status", "state": "thinking"}))

                    # Off the event loop: a turn that drives the browser can run for
                    # a minute, and blocking here stalls the WebSocket for its duration.
                    response = await asyncio.to_thread(brain.think, text.strip())

                    await websocket.send_text(json.dumps({"type": "status", "state": "speaking"}))

                    # TTS with edge-tts -> base64 mp3
                    tts_b64 = await tts_to_b64(response)

                    await websocket.send_text(json.dumps({
                        "type": "response",
                        "text": response,
                        "audio": tts_b64
                    }))
                    await websocket.send_text(json.dumps({"type": "status", "state": "idle"}))

                except Exception as e:
                    print(f"[WS] audio error: {e}", flush=True)
                    await websocket.send_text(json.dumps({
                        "type": "error", "text": str(e)
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
                    response = await asyncio.to_thread(brain.think, user_text)
                    tts_b64 = None
                    if speak:
                        await websocket.send_text(json.dumps({"type": "status", "state": "speaking"}))
                        tts_b64 = await tts_to_b64(response)
                    await websocket.send_text(json.dumps({
                        "type": "response", "text": response, "audio": tts_b64
                    }))
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
    except Exception as e:
        print(f"[WS] error: {e}", flush=True)


@app.get("/status")
async def get_status():
    """Report the backend actually in use, not a hardcoded guess."""
    if brain._use_router:
        model, endpoint = ROUTER_MODEL, ROUTER_BASE_URL
    elif brain._use_groq:
        model, endpoint = GROQ_MODEL, GROQ_BASE_URL
    elif brain._use_cerebras:
        model, endpoint = CEREBRAS_MODEL, CEREBRAS_BASE_URL
    else:
        model, endpoint = GEMINI_MODEL, "google-genai"
    return {
        "status": "running",
        "brain": model,
        "endpoint": endpoint,
        "fallback": GEMINI_MODEL if brain._use_router else None,
        "last_backend": brain.last_backend,
        "voice": f"whisper:{WHISPER_MODEL}",
    }


if __name__ == "__main__":
    import uvicorn
    print("[JARVIS Web] Starting on http://localhost:8000  (ws://localhost:8000/ws)")
    uvicorn.run(app, host="0.0.0.0", port=8000)
