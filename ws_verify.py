import asyncio, json, urllib.request
import websockets

BASE = "http://localhost:8000"

def http(path):
    with urllib.request.urlopen(BASE + path, timeout=20) as r:
        return r.status, r.read(200)

async def main():
    for p in ["/status", "/", "/hud.html"]:
        st, body = http(p)
        print(f"GET {p} -> {st}", body[:120] if p == "/status" else "")

    prompts = [
        "Jarvis, clear your throat and then tell me a short, natural good morning.",
        "Jarvis, what is 12 plus 13?",
    ]
    async with websockets.connect("ws://localhost:8000/ws", max_size=None) as ws:
        for p in prompts:
            await ws.send(json.dumps({"type": "text", "text": p, "speak": True}))
            while True:
                msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=120))
                if msg.get("type") == "response":
                    text = msg.get("text", "")
                    print("\n=== PROMPT:", p)
                    print("RESPONSE:", repr(text))
                    print("HAS_ASTERISK:", "*" in text, "| AUDIO_BYTES:", len(msg.get("audio") or ""))
                    break
                if msg.get("type") == "error":
                    print("ERROR:", msg)
                    break

asyncio.run(main())
