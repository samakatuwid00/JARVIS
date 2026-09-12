"""cygnus_stress.py - how Cygnus holds up under load and bad input.

It runs against the server you use, so the defaults stay modest and never
call a cloud model:

  http     concurrent GETs of /status, /apps and /music/now
  instant  several /ws clients firing instant commands at once (time, math,
           greetings) - latency, errors, wrong answers; /status is polled
           throughout to see whether the server stays responsive
  burst    one client sends a batch without waiting, then collects replies
  edge     malformed and hostile input, each on a fresh socket; after each,
           the server must still answer /status and "what time is it"
  churn    many sockets opened and dropped at once
  cloud    (--cloud N, off by default) N clients ask the cloud brain at the
           same moment for a number only they were given - costs model
           calls, and shows whether concurrent turns cross in the one
           shared conversation

  python scripts/cygnus_stress.py
  python scripts/cygnus_stress.py --clients 8 --rounds 20 --cloud 3
"""

import argparse
import asyncio
import json
import os
import re
import sys
import time

import httpx

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from cygnus_client import ask, connect, find_server, percentile, report_path  # noqa: E402

# Instant commands (no cloud model) and what a right answer contains.
INSTANT = [("what time is it", r"\d{1,2}:\d{2}"), ("12 * 12", r"144|one hundred forty-four"),
           ("7 + 5", r"\b12\b|twelve"), ("hello", r"hello|assist"), ("thank you", r"welcome")]
HTTP_PATHS = ("/status", "/apps", "/music/now")


def latency_summary(ms):
    return {"n": len(ms), "p50_ms": percentile(ms, 0.5), "p95_ms": percentile(ms, 0.95),
            "max_ms": max(ms) if ms else None}


async def alive(base):
    """(status answers, a fresh socket answers an instant command)."""
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            status_ok = (await c.get(base + "/status")).status_code == 200
    except httpx.HTTPError:
        status_ok = False
    try:
        async with connect(base) as ws:
            turn = await ask(ws, "what time is it", 20)
        ws_ok = bool(turn.reply) and not turn.error
    except Exception:
        ws_ok = False
    return status_ok, ws_ok


async def phase_http(base, clients, rounds):
    ms, errors = [], 0
    async with httpx.AsyncClient(timeout=15) as c:
        async def one(path):
            nonlocal errors
            t0 = time.monotonic()
            try:
                r = await c.get(base + path)
                errors += r.status_code != 200
            except httpx.HTTPError:
                errors += 1
            ms.append(int((time.monotonic() - t0) * 1000))
        await asyncio.gather(*(one(HTTP_PATHS[i % len(HTTP_PATHS)]) for i in range(clients * rounds)))
    return {**latency_summary(ms), "errors": errors}


async def _poll_status(base, stop, ms):
    async with httpx.AsyncClient(timeout=15) as c:
        while not stop.is_set():
            t0 = time.monotonic()
            try:
                await c.get(base + "/status")
            except httpx.HTTPError:
                ms.append(15000)
            else:
                ms.append(int((time.monotonic() - t0) * 1000))
            await asyncio.sleep(0.5)


async def phase_instant(base, clients, rounds):
    ms, wrong, errors, status_ms = [], [], 0, []
    stop = asyncio.Event()

    async def client(k):
        nonlocal errors
        async with connect(base) as ws:
            for r in range(rounds):
                say, want = INSTANT[(k + r) % len(INSTANT)]
                turn = await ask(ws, say, 30)
                ms.append(turn.ms)
                if turn.error:
                    errors += 1
                elif not re.search(want, turn.reply or "", re.I):
                    wrong.append((say, (turn.reply or "")[:60]))
    poller = asyncio.create_task(_poll_status(base, stop, status_ms))
    await asyncio.gather(*(client(k) for k in range(clients)), return_exceptions=False)
    stop.set()
    await poller
    return {**latency_summary(ms), "errors": errors, "wrong": wrong[:10], "wrong_count": len(wrong),
            "status_while_loaded": latency_summary(status_ms)}


async def phase_burst(base, size):
    sums = [(f"{i} + {i}", str(2 * i)) for i in range(1, size + 1)]
    replies, t0 = [], time.monotonic()
    async with connect(base) as ws:
        for say, _ in sums:
            await ws.send(json.dumps({"type": "text", "text": say}))
        deadline = time.monotonic() + 30 + size * 2
        while len(replies) < size and time.monotonic() < deadline:
            try:
                msg = json.loads(await asyncio.wait_for(ws.recv(), deadline - time.monotonic()))
            except asyncio.TimeoutError:
                break
            if msg.get("type") == "response" and not (msg.get("proactive") or msg.get("deferred")):
                replies.append(msg.get("text") or "")
    in_order = all(re.search(rf"\b{want}\b", got) for (_, want), got in zip(sums, replies))
    return {"sent": size, "replies": len(replies), "in_order_and_right": in_order and len(replies) == size,
            "total_ms": int((time.monotonic() - t0) * 1000)}


# Hostile or malformed frames; each is sent on a fresh socket.
EDGE = [("malformed JSON", "{not json"), ("JSON that is not an object", "[1, 2, 3]"),
        ("unknown message type", json.dumps({"type": "bogus"})),
        ("text with no text", json.dumps({"type": "text"})),
        ("empty text", json.dumps({"type": "text", "text": "   "})),
        ("junk audio", json.dumps({"type": "audio", "data": "QUFB" * 2500})),
        ("binary frame", b"\x00\x01\x02\xff")]


# Messages every client receives whatever it sent (job events, spoken cues).
_BROADCAST = {"job", "cue", "telemetry", "progress", "wake"}


async def _edge_one(base, frame, wait=15):
    """What the socket did with the frame: its reply ('error', 'response',
    'idle'), 'silent', or 'closed'. Broadcast job events are not replies."""
    try:
        async with connect(base) as ws:
            await ws.send(frame)
            deadline = time.monotonic() + wait
            while time.monotonic() < deadline:
                try:
                    msg = json.loads(await asyncio.wait_for(ws.recv(), deadline - time.monotonic()))
                except asyncio.TimeoutError:
                    break
                kind = msg.get("type")
                if kind in ("error", "response") or (kind == "status" and msg.get("state") == "idle"):
                    return "idle" if kind == "status" else kind
            return "silent"
    except Exception as e:
        return f"closed ({type(e).__name__})"


async def phase_edge(base):
    rows = []
    for name, frame in EDGE:
        outcome = await _edge_one(base, frame)
        status_ok, ws_ok = await alive(base)
        rows.append({"input": name, "socket": outcome, "server_alive": status_ok and ws_ok})
    return rows


async def phase_churn(base, n):
    async def one():
        try:
            async with connect(base):
                pass
            return True
        except Exception:
            return False
    opened = sum(await asyncio.gather(*(one() for _ in range(n))))
    status_ok, ws_ok = await alive(base)
    return {"sockets": n, "opened": opened, "server_alive": status_ok and ws_ok}


async def phase_cloud(base, n):
    async def client(k):
        token = 1000 + 37 * k
        async with connect(base) as ws:
            turn = await ask(ws, f"Reply with only this number and nothing else: {token}", 120)
        mine = str(token) in (turn.reply or "")
        others = [t for t in (1000 + 37 * j for j in range(n)) if t != token and str(t) in (turn.reply or "")]
        return {"token": token, "ms": turn.ms, "error": turn.error, "got_own": mine,
                "got_other_clients": others, "reply": (turn.reply or "")[:80]}
    rows = await asyncio.gather(*(client(k) for k in range(n)))
    return {"clients": rows, "crossed": sum(bool(r["got_other_clients"]) for r in rows)}


def _show(name, data):
    print(f"\n== {name}")
    for row in (data if isinstance(data, list) else [data]):
        print("  " + json.dumps(row, ensure_ascii=False)[:300])


async def run(base, args):
    out = {}
    out["http"] = await phase_http(base, args.clients, args.rounds)
    _show("http", out["http"])
    out["instant"] = await phase_instant(base, args.clients, args.rounds)
    _show("instant", out["instant"])
    out["burst"] = await phase_burst(base, args.burst)
    _show("burst", out["burst"])
    out["edge"] = await phase_edge(base)
    _show("edge", out["edge"])
    out["churn"] = await phase_churn(base, args.clients * 5)
    _show("churn", out["churn"])
    if args.cloud:
        out["cloud"] = await phase_cloud(base, args.cloud)
        _show("cloud", out["cloud"])
    return out


def verdicts(out):
    """Plain findings from the numbers: what a person should look at."""
    found = []
    if out["http"]["errors"]:
        found.append(f"{out['http']['errors']} HTTP requests failed")
    inst = out["instant"]
    if inst["errors"] or inst["wrong_count"]:
        found.append(f"instant commands: {inst['errors']} errors, {inst['wrong_count']} wrong answers")
    if (inst["status_while_loaded"]["max_ms"] or 0) > 3000:
        found.append(f"/status took up to {inst['status_while_loaded']['max_ms']} ms under load")
    if not out["burst"]["in_order_and_right"]:
        found.append(f"burst: {out['burst']['replies']} of {out['burst']['sent']} replies, not all right/in order")
    found += [f"edge '{r['input']}' left the server unresponsive" for r in out["edge"] if not r["server_alive"]]
    found += [f"edge '{r['input']}' closed the socket" for r in out["edge"] if r["socket"].startswith("closed")]
    if not out["churn"]["server_alive"]:
        found.append("socket churn left the server unresponsive")
    if out.get("cloud", {}).get("crossed"):
        found.append(f"cloud: {out['cloud']['crossed']} replies carried another client's number")
    return found


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--url")
    ap.add_argument("--clients", type=int, default=4)
    ap.add_argument("--rounds", type=int, default=10)
    ap.add_argument("--burst", type=int, default=8)
    ap.add_argument("--cloud", type=int, default=0, help="clients for the cloud phase (costs model calls)")
    args = ap.parse_args(argv)
    base = find_server(args.url)
    print(f"Cygnus at {base} - {args.clients} clients x {args.rounds} rounds, burst {args.burst}, "
          f"cloud {args.cloud or 'off'}")
    out = asyncio.run(run(base, args))
    found = verdicts(out)
    path = report_path("stress")
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"base": base, "args": vars(args), "results": out, "findings": found}, f, indent=2)
    print("\n== findings")
    print("\n".join(f"  - {x}" for x in found) or "  none")
    print(f"report: {path}")
    return 1 if found else 0


if __name__ == "__main__":
    raise SystemExit(main())
