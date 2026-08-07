#!/usr/bin/env python3
"""One-shot verification for the v2 HUD swap: HTTP routes + full WS voice loop."""

import asyncio
import base64
import json
import os
import tempfile

import httpx
import websockets
import edge_tts

BASE = "http://localhost:8000"
PHRASE = "Jarvis, what is 8 times 7?"


async def http_checks():
    async with httpx.AsyncClient(timeout=30) as c:
        for path in ("/", "/hud.html", "/status"):
            r = await c.get(BASE + path)
            print(f"GET {path} -> {r.status_code} ({len(r.content)} bytes)")
            assert r.status_code == 200, path
        r = await c.get(BASE + "/hud.html")
        body = r.text
        assert "Bundled Page" in body, "no Bundled Page title"
        assert '__bundler/template' in body and '__bundler/manifest' in body
        # The only "Error unpacking" occurrence must be the bundler's own catch
        # handler literal — anything else means a baked-in failure message.
        assert body.count("Error unpacking") == 1
        assert "setStatus('Error unpacking: ' + err.message);" in body
        print("hud.html: Bundled Page + template + manifest present, no unpack error")


async def synth_b64():
    mp3 = tempfile.NamedTemporaryFile(suffix=".mp3", delete=False).name
    await edge_tts.Communicate(PHRASE, "en-GB-RyanNeural").save(mp3)
    with open(mp3, "rb") as f:
        b = base64.b64encode(f.read()).decode()
    os.unlink(mp3)
    print(f"synthesized test utterance: {len(b)} b64 chars")
    return b


async def ws_check(b64):
    async with websockets.connect("ws://localhost:8000/ws", max_size=None) as ws:
        await ws.send(json.dumps({"type": "audio", "data": b64}))
        transcript = response = None
        while True:
            m = json.loads(await asyncio.wait_for(ws.recv(), timeout=120))
            t = m.get("type")
            if t == "status":
                print(f"  status: {m.get('state')}")
            elif t == "transcript":
                transcript = m["text"]
                print(f"  transcript: {transcript!r}")
            elif t == "error":
                raise SystemExit(f"WS error: {m.get('text')}")
            elif t == "response":
                response = m
                print(f"  response text: {m.get('text')!r}")
                print(f"  response audio: {len(m.get('audio') or '')} b64 chars")
                break
        assert transcript and transcript.strip(), "empty transcript"
        assert response.get("text"), "empty response text"
        assert response.get("audio"), "empty response audio"
        print("WS loop OK: transcript -> response(+audio)")


async def main():
    await http_checks()
    await ws_check(await synth_b64())
    sz = os.path.getsize("hud_artifact.html")
    print(f"hud_artifact.html size = {sz} (expected 600327)")
    assert sz == 600327


asyncio.run(main())
