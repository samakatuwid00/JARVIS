"""cygnus_smoke.py - try Cygnus end to end, the way you would talk to it.

Each case types commands over /ws (as the HUD does) and checks the reply and
the tools that actually ran (logs/audit.jsonl). Three tiers:

  safe   (default) answers, routing and safety gates - nothing on the
         machine changes (a delete is asked about and declined)
  live   reversible desktop actions: opens and closes Notepad and
         Calculator, opens GitHub and YouTube tabs, plays and pauses music
  agent  a Hermes/autonomous job that makes a sandbox folder, confirmed,
         checked on disk and removed again (slow, uses cloud models)

  python scripts/cygnus_smoke.py                   safe tier
  python scripts/cygnus_smoke.py --tier live       safe + live
  python scripts/cygnus_smoke.py --tier agent      all three
  python scripts/cygnus_smoke.py --only github     cases whose name matches

The turns land in the brain's conversation and the session log like any
other. Restart the server after pulling changes: it runs the code it
started with.
"""

import argparse
import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from cygnus_client import (Turn, ask, audit_mark, connect, find_server, hello, report_path,  # noqa: E402
                           tools_since)

TIERS = ("safe", "live", "agent")
# Tools that change something on the machine or hand work to an agent.
ACTING = {"open_application", "close_application", "open_site", "play_music", "play_spotify",
          "run_autonomous", "delegate_to_hermes", "delegate", "write_file", "search_web"}
SANDBOX = os.path.join(os.path.expanduser("~"), "Documents", f"cygnus-smoke-{time.strftime('%Y%m%d')}")


def _running(image):
    out = subprocess.run(["tasklist", "/FI", f"IMAGENAME eq {image}", "/NH"],
                         capture_output=True, text=True).stdout
    return image.lower() in out.lower()


def _eventually(test, seconds=8.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if test():
            return True
        time.sleep(0.5)
    return test()


def process_is(image, running):
    def check():
        if _eventually(lambda: _running(image) == running):
            return None
        return f"{image} is {'not ' if running else 'still '}running"
    return check


def sandbox_appears():
    return None if _eventually(lambda: os.path.isdir(SANDBOX), 240) else f"{SANDBOX} never appeared"


def remove_sandbox():
    shutil.rmtree(SANDBOX, ignore_errors=True)


def step(say, **expect):
    return {"say": say, **expect}


CASES = [
    # ---- safe: answers and routing, nothing on the machine changes ----
    {"name": "greeting", "tier": "safe", "steps": [
        step("hello cygnus", reply=r"hello|how may|assist", forbid=ACTING)]},
    {"name": "time", "tier": "safe", "steps": [
        step("what time is it", reply=r"\d{1,2}:\d{2}", forbid=ACTING)]},
    {"name": "math", "tier": "safe", "steps": [
        step("17 * 23", reply=r"391|three hundred (and )?ninety-one", forbid=ACTING)]},
    {"name": "own-work question", "tier": "safe", "steps": [
        step("What coding agent did you use to create the hello world website?",
             reject=r"https?://\S+ \[auto", forbid={"list_sites", "run_autonomous", "delegate_to_hermes"})]},
    {"name": "site registry", "tier": "safe", "steps": [
        step("what sites do you know?", tools={"list_sites"})]},
    {"name": "installed apps", "tier": "safe", "steps": [
        step("what apps do I have?", reply=r"\w", forbid=ACTING)]},
    {"name": "job status", "tier": "safe", "steps": [
        step("what's the status of the task?", reply=r"job|task|running|nothing|finished", forbid=ACTING)]},
    {"name": "memory recall", "tier": "safe", "steps": [
        step("what do you remember about me?", reply=r"\w{3}", forbid=ACTING)]},
    {"name": "capability question stays a question", "tier": "safe", "steps": [
        step("can you visit websites?", forbid=ACTING)]},
    {"name": "garbled speech is not acted on", "tier": "safe", "steps": [
        step("In the open, which was to do for the people.",
             forbid={"open_application", "open_site", "run_autonomous", "delegate_to_hermes"})]},
    {"name": "delete asks, no cancels", "tier": "safe", "steps": [
        step(r"delete the file C:\cygnus_smoke_missing\old.txt", reply=r"confirm",
             forbid={"write_file", "delegate_to_hermes"}),
        step("no", reply=r"cancel|won't|okay|alright")]},
    {"name": "taglish", "tier": "safe", "steps": [
        step("anong oras na ngayon?", reply=r"\w", forbid=ACTING)]},
    # ---- live: reversible desktop actions ----
    {"name": "notepad open and close", "tier": "live", "steps": [
        step("open notepad", tools={"open_application"}, check=process_is("Notepad.exe", True)),
        step("close notepad", tools={"close_application"}, check=process_is("Notepad.exe", False))]},
    {"name": "chain: two apps at once", "tier": "live", "steps": [
        step("open notepad and calculator", count={"open_application": 2},
             check=process_is("CalculatorApp.exe", True)),
        step("close notepad and calculator", count={"close_application": 2},
             check=process_is("CalculatorApp.exe", False))]},
    {"name": "github site", "tier": "live", "steps": [
        step("can you open github for me?", tools={"open_site"}, forbid={"open_application"},
             reject=r"error|could not")]},
    {"name": "site suffix", "tier": "live", "steps": [
        step("open youtube site", tools={"open_site"}, reject=r"no registered site")]},
    {"name": "music play and pause", "tier": "live", "steps": [
        step("play lo-fi on spotify", tools={"play_spotify", "play_music", "abilities.run"}, timeout=90),
        step("pause the music", tools={"stop_music", "stop_spotify", "abilities.run"})]},
    # ---- agent: a confirmed autonomous job in a sandbox folder ----
    {"name": "autonomous folder job", "tier": "agent", "cleanup": remove_sandbox, "steps": [
        step(f"create a folder called {os.path.basename(SANDBOX)} in my Documents", reply=r"confirm"),
        step("confirm", reply=r"on it|working|background|autonom", timeout=90, check=sandbox_appears)]},
]


def check_step(expect, turn, tools):
    """What is wrong with one turn, as a list of short reasons."""
    problems = [f"error: {turn.error}"] if turn.error else []
    reply = turn.reply or ""
    if expect.get("reply") and not re.search(expect["reply"], reply, re.I):
        problems.append(f"reply does not match /{expect['reply']}/")
    if expect.get("reject") and re.search(expect["reject"], reply, re.I):
        problems.append(f"reply matches /{expect['reject']}/")
    if expect.get("tools") and not set(expect["tools"]) & set(tools):
        problems.append(f"none of {sorted(expect['tools'])} ran (ran: {sorted(set(tools)) or 'nothing'})")
    forbidden = set(expect.get("forbid") or ()) & set(tools)
    if forbidden:
        problems.append(f"ran {sorted(forbidden)}, which it must not")
    for name, n in (expect.get("count") or {}).items():
        if tools.count(name) < n:
            problems.append(f"{name} ran {tools.count(name)}x, want {n}")
    return problems


async def run_step(ws, expect):
    import websockets
    mark = audit_mark()
    try:
        turn = await ask(ws, expect["say"], expect.get("timeout", 60))
    except websockets.ConnectionClosed as e:
        turn = Turn(said=expect["say"], error=f"connection closed: {e}")
    await asyncio.sleep(0.5)                      # tool audit lines land just after the reply
    tools = tools_since(mark)
    problems = check_step(expect, turn, tools)
    if expect.get("check"):
        problem = await asyncio.to_thread(expect["check"])
        if problem:
            problems.append(problem)
    return {"say": expect["say"], "reply": turn.reply, "error": turn.error, "ms": turn.ms,
            "backend": turn.telemetry.get("backend"), "intent": turn.telemetry.get("intent"),
            "tools": tools, "problems": problems, "late": turn.late}


def _print_step(case, result):
    mark = "PASS" if not result["problems"] else "FAIL"
    route = "/".join(x for x in (result["backend"], result["intent"]) if x) or "-"
    print(f"  {mark}  {case['name']}: {result['say']!r} -> {(result['reply'] or '')[:70]!r}"
          f"  [{route}, {result['ms']} ms]")
    for p in result["problems"]:
        print(f"        - {p}")


async def run(base, cases):
    """Each case on its own connection: a reply that arrives after its step
    timed out must not be read as the next case's reply."""
    results = []
    for case in cases:
        async with connect(base) as ws:
            await hello(ws, f"smoke-{case['name']}")
            for expect in case["steps"]:
                result = await run_step(ws, expect)
                _print_step(case, result)
                results.append({"case": case["name"], "tier": case["tier"], **result})
        if case.get("cleanup"):
            case["cleanup"]()
    return results


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--url", help="server base URL (default: 127.0.0.1:8001, then :8000)")
    ap.add_argument("--tier", choices=TIERS, default="safe")
    ap.add_argument("--only", help="run only cases whose name contains this")
    args = ap.parse_args(argv)
    base = find_server(args.url)
    tiers = TIERS[:TIERS.index(args.tier) + 1]
    cases = [c for c in CASES if c["tier"] in tiers and (not args.only or args.only in c["name"])]
    print(f"Cygnus at {base} - {len(cases)} cases, tiers: {', '.join(tiers)}")
    results = asyncio.run(run(base, cases))
    failed = [r for r in results if r["problems"]]
    path = report_path("smoke")
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"base": base, "tiers": tiers, "results": results}, f, indent=2, ensure_ascii=False)
    print(f"\n{len(results) - len(failed)} of {len(results)} steps passed; report: {path}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
