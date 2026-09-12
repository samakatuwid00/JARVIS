"""cygnus_conversation.py - real conversations: follow-ups, repeats, overlaps.

A session is a scripted conversation whose later prompts depend on earlier
ones ("12 * 12" ... "and what is that plus 6?"). Four ways to run them:

  session        each conversation once, in order, on one socket
  --repeat K     each conversation K times: per turn, how often it passes,
                 whether it takes the same route every time, how many
                 different replies it gives, and its latency
  overlap        (always) a follow-up sent before the first reply is back,
                 and an instant command on a second socket while the first
                 waits on the cloud - does the fast one wait, do replies swap?
  --concurrent N N sessions at once, each giving its own code word in the
                 same moment and then asking for it back - the brain keeps
                 one conversation for every client, so this counts the
                 replies that come back with another session's word

  python scripts/cygnus_conversation.py                     safe sessions once
  python scripts/cygnus_conversation.py --repeat 3          consistency
  python scripts/cygnus_conversation.py --concurrent 3      simultaneous sessions
  python scripts/cygnus_conversation.py --tier live         + music, sites, apps

Follow-ups and code words use the cloud brain (a few model calls per run).
The turns join the brain's conversation like any other.
"""

import argparse
import asyncio
import json
import os
import re
import sys
import time
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from cygnus_client import ask, connect, find_server, hello, percentile, report_path  # noqa: E402
from cygnus_smoke import ACTING, run_step, step  # noqa: E402

WORDS = ("FALCON", "ORCHID", "GRANITE", "MERIDIAN", "LANTERN", "CASCADE", "TUNDRA", "SAFFRON")

SESSIONS = [
    {"name": "math follow-up", "tier": "safe", "turns": [
        step("12 * 12", reply=r"144"),
        step("and what is that plus 6?", reply=r"150|one hundred (and )?fifty", forbid=ACTING)]},
    {"name": "code word kept", "tier": "safe", "turns": [
        step("For this chat, the code word is FALCON. Just say OK.", reply=r"ok|falcon", forbid=ACTING),
        step("What code word did I give you?", reply=r"falcon", forbid=ACTING)]},
    {"name": "confirm, cancel, recall", "tier": "safe", "turns": [
        step(r"delete the file C:\cygnus_smoke_missing\old.txt", reply=r"confirm"),
        step("no", reply=r"cancel|won't|okay|alright"),
        step("what did I just ask you to do?", reply=r"delet|file|old\.txt", forbid=ACTING)]},
    {"name": "time then Taglish date", "tier": "safe", "turns": [
        step("what time is it", reply=r"\d{1,2}:\d{2}"),
        step("anong petsa ngayon?", reply=r"\d|setyembre|september|sabado|saturday", forbid=ACTING)]},
    {"name": "site list then which one", "tier": "safe", "turns": [
        step("what sites do you know?", tools={"list_sites"}),
        step("which of those do I visit most?", reply=r"irimsv", forbid=ACTING)]},
    {"name": "music session", "tier": "live", "turns": [
        step("play two ghosts by harry styles on spotify", tools={"play_spotify", "play_music", "abilities.run"},
             timeout=90),
        step("pause it", tools={"stop_music", "stop_spotify", "abilities.run"}),
        step("resume it", tools={"play_spotify", "play_music", "abilities.run"}),
        step("stop the music", tools={"stop_music", "stop_spotify", "abilities.run"})]},
    {"name": "open then search there", "tier": "live", "turns": [
        step("open youtube", tools={"open_site"}),
        step("search lofi beats there", tools={"execute_search", "search_web"})]},
    {"name": "misheard then corrected", "tier": "live", "turns": [
        step("open my face"),
        step("facebook I mean", tools={"open_site"})]},
    {"name": "apps chain", "tier": "live", "turns": [
        step("open notepad and calculator", count={"open_application": 2}),
        step("close notepad and calculator", count={"close_application": 2})]},
]


async def run_session(ws, session, pause):
    rows = []
    for expect in session["turns"]:
        rows.append(await run_step(ws, expect))
        await asyncio.sleep(pause)
    return rows


def _route(row):
    return "/".join(x for x in (row["backend"], row["intent"]) if x) or "-"


def _norm(reply):
    """A reply with its clock readings blanked, so 07:43 and 07:44 read alike."""
    return re.sub(r"\d{1,2}:\d{2}", "#", (reply or "").strip().lower())


def consistency(runs):
    """Per turn over repeated runs of one session: pass rate, the share of
    runs on the commonest route, distinct replies, and latency."""
    out = []
    for i, turns in enumerate(zip(*runs)):
        routes = Counter(_route(t) for t in turns)
        ms = [t["ms"] for t in turns]
        out.append({"turn": i + 1, "say": turns[0]["say"], "runs": len(turns),
                    "pass_rate": sum(not t["problems"] for t in turns) / len(turns),
                    "route": routes.most_common(1)[0][0],
                    "route_stable": routes.most_common(1)[0][1] / len(turns),
                    "distinct_replies": len({_norm(t["reply"]) for t in turns}),
                    "p50_ms": percentile(ms, 0.5), "p95_ms": percentile(ms, 0.95)})
    return out


async def _next_reply(ws, turn, timeout):
    """Keep reading `turn`'s socket for the reply after the one it holds -
    two commands sent back to back are answered in turn."""
    start = time.monotonic()
    turn.reply = None
    while time.monotonic() - start < timeout:
        try:
            msg = json.loads(await asyncio.wait_for(ws.recv(), timeout - (time.monotonic() - start)))
        except asyncio.TimeoutError:
            break
        if msg.get("type") == "response" and not (msg.get("proactive") or msg.get("deferred")):
            turn.reply = msg.get("text") or ""
            break
    turn.ms += int((time.monotonic() - start) * 1000)
    if turn.reply is None:
        turn.error = f"no second reply within {timeout:.0f}s"
    return turn


async def overlap(base):
    """A follow-up sent before the first reply, and an instant command on a
    second socket while the first waits on the cloud."""
    async with connect(base) as ws:
        await hello(ws, "overlap-queued")
        await ws.send(json.dumps({"type": "text", "text": "12 * 12"}))
        follow = await ask(ws, "and what is that plus 6?", 90)       # sent at once, answered in turn
        if follow.reply and re.search(r"144", follow.reply) and not follow.error:
            follow = await _next_reply(ws, follow, 90)                # that was the first one's reply
    queued = {"follow_up_reply": (follow.reply or "")[:80], "ms": follow.ms,
              "kept_context": bool(re.search(r"150|fifty", follow.reply or "", re.I))}

    async def slow():
        async with connect(base) as ws:
            await hello(ws, "overlap-slow")
            return await ask(ws, "In two sentences, what is a black hole?", 120)

    async def fast():
        await asyncio.sleep(0.5)
        async with connect(base) as ws:
            await hello(ws, "overlap-fast")
            return await ask(ws, "what time is it", 120)
    s, f = await asyncio.gather(slow(), fast())
    side = {"slow_ms": s.ms, "fast_ms": f.ms, "fast_waited_for_slow": f.ms > 3000,
            "fast_has_time": bool(re.search(r"\d{1,2}:\d{2}", f.reply or "")),
            "slow_mentions_time": bool(re.search(r"\d{1,2}:\d{2}\s*(am|pm)", s.reply or "", re.I)),
            "slow_reply": (s.reply or "")[:80]}
    return {"queued_follow_up": queued, "cloud_and_instant_side_by_side": side}


async def concurrent_sessions(base, n):
    """Each session gives its own word in the same moment, then asks for it."""
    words = WORDS[:n]
    sockets = [await connect(base).__aenter__() for _ in words]
    for ws, w in zip(sockets, words):
        await hello(ws, f"concurrent-{w.lower()}")
    try:
        await asyncio.gather(*(ask(ws, f"For this chat, the code word is {w}. Just say OK.", 120)
                               for ws, w in zip(sockets, words)))
        recalls = await asyncio.gather(*(ask(ws, "What code word did I give you?", 120) for ws in sockets))
    finally:
        for ws in sockets:
            await ws.close()
    rows = []
    for w, turn in zip(words, recalls):
        said = set(re.findall(r"[A-Z]{5,}", (turn.reply or "").upper())) & set(words)
        rows.append({"word": w, "got_own": w in said, "got_others": sorted(said - {w}),
                     "ms": turn.ms, "reply": (turn.reply or "")[:80]})
    return {"sessions": rows, "kept_own": sum(r["got_own"] for r in rows),
            "crossed": sum(bool(r["got_others"]) for r in rows)}


def _print_run(session, rows):
    for row in rows:
        mark = "PASS" if not row["problems"] else "FAIL"
        print(f"  {mark}  {session['name']}: {row['say']!r} -> {(row['reply'] or '')[:64]!r}"
              f"  [{_route(row)}, {row['ms']} ms]")
        for p in row["problems"]:
            print(f"        - {p}")


async def run(base, args, sessions):
    report = {"sessions": {}, "consistency": {}}
    for session in sessions:
        runs = []
        for k in range(args.repeat):
            # A fresh connection per run: a late reply from a timed-out turn
            # must not be read as the next run's reply.
            async with connect(base) as ws:
                await hello(ws, f"conv-{session['name']}-{k}")
                rows = await run_session(ws, session, args.pause)
            if k == 0 or any(r["problems"] for r in rows):
                _print_run(session, rows)
            runs.append(rows)
        report["sessions"][session["name"]] = runs
        if args.repeat > 1:
            report["consistency"][session["name"]] = consistency(runs)
    report["overlap"] = await overlap(base)
    if args.concurrent:
        report["concurrent"] = await concurrent_sessions(base, args.concurrent)
    return report


def summary(report):
    """Plain findings a person should look at."""
    found = []
    for name, runs in report["sessions"].items():
        failing = sorted({r["say"] for rows in runs for r in rows if r["problems"]})
        if failing:
            found.append(f"{name}: failed on {', '.join(repr(s) for s in failing)}")
    for name, turns in report["consistency"].items():
        for t in turns:
            if t["route_stable"] < 1:
                found.append(f"{name} turn {t['turn']}: route changed between runs "
                             f"({t['route_stable']:.0%} on {t['route']})")
    ov = report["overlap"]
    if not ov["queued_follow_up"]["kept_context"]:
        found.append("a follow-up sent before the first reply lost its context")
    side = ov["cloud_and_instant_side_by_side"]
    if side["fast_waited_for_slow"]:
        found.append(f"an instant command waited {side['fast_ms']} ms behind another client's cloud turn")
    if side["slow_mentions_time"]:
        found.append("the cloud reply picked up the other client's time question")
    conc = report.get("concurrent")
    if conc and conc["crossed"]:
        found.append(f"{conc['crossed']} of {len(conc['sessions'])} simultaneous sessions got another "
                     "session's code word back")
    if conc and conc["kept_own"] < len(conc["sessions"]):
        found.append(f"only {conc['kept_own']} of {len(conc['sessions'])} simultaneous sessions "
                     "recalled their own code word")
    return found


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--url")
    ap.add_argument("--tier", choices=("safe", "live"), default="safe")
    ap.add_argument("--repeat", type=int, default=1)
    ap.add_argument("--concurrent", type=int, default=0, help=f"simultaneous sessions (max {len(WORDS)})")
    ap.add_argument("--pause", type=float, default=1.0, help="seconds between turns, like a person")
    ap.add_argument("--only")
    args = ap.parse_args(argv)
    args.concurrent = min(args.concurrent, len(WORDS))
    base = find_server(args.url)
    tiers = ("safe",) if args.tier == "safe" else ("safe", "live")
    sessions = [s for s in SESSIONS if s["tier"] in tiers and (not args.only or args.only in s["name"])]
    print(f"Cygnus at {base} - {len(sessions)} sessions x {args.repeat}, "
          f"concurrent {args.concurrent or 'off'}")
    report = asyncio.run(run(base, args, sessions))
    for name, turns in report["consistency"].items():
        print(f"\n== consistency: {name}")
        for t in turns:
            print(f"  turn {t['turn']}: pass {t['pass_rate']:.0%}, route {t['route']} "
                  f"({t['route_stable']:.0%}), {t['distinct_replies']} distinct replies, "
                  f"p50 {t['p50_ms']} ms / p95 {t['p95_ms']} ms")
    print("\n== overlap\n  " + json.dumps(report["overlap"], ensure_ascii=False)[:600])
    if report.get("concurrent"):
        print("\n== concurrent sessions")
        for r in report["concurrent"]["sessions"]:
            print("  " + json.dumps(r, ensure_ascii=False))
    found = summary(report)
    path = report_path("conversation")
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"base": base, "args": vars(args), "report": report, "findings": found},
                  f, indent=2, ensure_ascii=False)
    print("\n== findings\n" + ("\n".join(f"  - {x}" for x in found) or "  none"))
    print(f"report: {path}")
    return 1 if found else 0


if __name__ == "__main__":
    raise SystemExit(main())
