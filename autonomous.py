"""autonomous.py — Phase 15: supervised autonomous Hermes jobs.

A "goal" (multi-step task) runs as a background `hermes chat` process with an
autonomy contract embedded in the prompt. The contract forces Hermes to:
  1. plan before acting,
  2. append a status line after EVERY step to a per-job status file,
  3. end its reply with RESULT: done | failed (+ evidence summary).

This module supervises: spawns the process, tails the status file into
jobs.py (which broadcasts to the HUD), parses the final RESULT, and never
marks a job "done" without evidence lines (Phase 12/17 honesty rules).

Stdlib only. Status file is append-only text: .hermes-jobs/<jid>.status
"""

import os
import re
import shutil
import subprocess
import threading
import time

import jobs as jobreg

# --- config -----------------------------------------------------------------
MAX_TURNS = 40            # autonomous runs get a generous turn budget
POLL_INTERVAL = 2.0       # seconds between status-file polls
DEFAULT_TIMEOUT = 1800    # hard ceiling per goal (30 min)
RETRY_LIMIT = 0           # Phase 17 will raise this to 1

_JOBS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         ".hermes-jobs")

# jid -> {"proc": Popen, "status_path": str, "agent": str|None}
_RUNNING: dict[str, dict] = {}
_LOCK = threading.Lock()

# [STEP n] started|done|fail — what | evidence
_STEP_RE = re.compile(
    r"^\s*\[STEP\s+(\d+)\]\s+(started|done|fail)\b[—\-\|:]?\s*(.*)", re.I)
_RESULT_RE = re.compile(r"RESULT:\s*(done|failed)", re.I)

# Phase 17: regexes for INDEPENDENT artifact extraction from step lines.
_PATH_RE = re.compile(
    r"(?:[A-Za-z]:[\\/]|\\\\[^\\]+\\[^\\]+|[\\/])[^\s\"'|]+"
    r"|(?<![ \-])\b[\w\-.]+\.(?:txt|md|py|json|csv|html|png|jpg|pdf|docx|xlsx)\b"
    r"|/[a-z]/[^\s\"'|]+")  # also catch MSYS/git-bash /c/Users/... form


def _normalize_path(p: str) -> str:
    """Turn whatever form Hermes wrote into a real Windows path."""
    p = p.strip().rstrip(".,);")
    p = p.replace("/", "\\")
    # /c/Users/...  ->  C:\Users\...
    m = re.match(r"\\([a-z])\b(.*)", p, re.I)
    if m:
        p = f"{m.group(1).upper()}:{m.group(2)}"
    return p
_CONTENT_RE = re.compile(
    r"(?:contains?|says?|content|reads?)\s+[\"']?([^\"'\n]{1,120})[\"']?", re.I)

_CONTRACT_TEMPLATE = """AUTONOMY CONTRACT (mandatory):
You are working autonomously toward ONE goal. Follow exactly:
1. First, post your plan as numbered steps.
2. Execute steps one at a time. After EVERY step, append one line to the
   status file {status_path} using this shell pattern (create if missing):
     echo "[STEP <n>] done - <what you did> | <evidence>" >> "{status_path}"
   Use started/fail instead of done where appropriate. Evidence MUST be a
   checkable artifact: a file path, command output snippet, or URL — never
   a bare claim.
3. Verify each step's outcome yourself (read the file back, re-run the
   probe). If verification fails, mark that step fail and either fix it or
   stop honestly.
4. End your FINAL reply with exactly one line:
   RESULT: done
   or
   RESULT: failed - <reason>
   A RESULT: done without matching [STEP] done lines + evidence in the
   status file will be recorded as UNVERIFIED, not done.

GOAL: {goal}
"""


def status_path(jid: str) -> str:
    return os.path.join(_JOBS_DIR, f"{jid}.status")


def _contract(goal: str, spath: str) -> str:
    return _CONTRACT_TEMPLATE.format(status_path=spath.replace("\\", "/"),
                                     goal=goal)


def parse_status_line(line: str):
    """Return (step_no, state, rest) or None for non-status lines."""
    m = _STEP_RE.match(line or "")
    if not m:
        return None
    rest = re.sub(r"^[\s\-\—\–\|:]+", "", m.group(3))
    return int(m.group(1)), m.group(2).lower(), rest.strip()


def parse_result(reply: str):
    """Return 'done' | 'failed' | None from the final reply text."""
    m = None
    for m in _RESULT_RE.finditer(reply or ""):
        pass
    return m.group(1).lower() if m else None


def _tail_new_lines(path: str, offset: int):
    """Yield (new_offset, [lines...]) appended since byte offset."""
    try:
        size = os.path.getsize(path)
    except OSError:
        return offset, []
    if size <= offset:
        return offset, []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            f.seek(offset)
            data = f.read()
        return size, [ln for ln in data.splitlines() if ln.strip()]
    except OSError:
        return offset, []


def start_goal(goal_text: str, timeout: int = DEFAULT_TIMEOUT,
               on_done=None, progress_cb=None) -> tuple[str, str]:
    """Launch an autonomous Hermes job. Returns (ack_message, jid)."""
    goal_text = str(goal_text or "").strip()
    if not goal_text:
        return "[Error] No goal given.", ""

    from warm_harness import _HERMES_BIN, _child_env, warm_session
    sid = warm_session(timeout=min(timeout, 120))

    os.makedirs(_JOBS_DIR, exist_ok=True)
    jid = jobreg.create(goal_text[:200], tier="autonomous")
    spath = status_path(jid)
    open(spath, "a", encoding="utf-8").close()  # touch

    cmd = [_HERMES_BIN, "chat",
           "-q", _contract(goal_text, spath),
           "-Q"]
    if sid:
        cmd += ["--resume", sid]
    else:
        cmd += ["--pass-session-id"]
    cmd += ["--max-turns", str(MAX_TURNS)]

    try:
        timeout_i = max(60, min(int(timeout), 7200))
    except (TypeError, ValueError):
        timeout_i = DEFAULT_TIMEOUT
    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace",
            env=_child_env(),
            cwd=os.path.dirname(os.path.abspath(__file__)))
    except Exception as e:
        jobreg.update(jid, state="error", error=str(e)[:200])
        return f"[Error] Could not launch autonomous job: {e}", jid

    with _LOCK:
        _RUNNING[jid] = {"proc": proc, "status_path": spath}

    jobreg.update(jid, state="running", note="planning…")
    threading.Thread(target=_supervise, daemon=True,
                     args=(jid, goal_text, proc, timeout_i,
                           on_done, progress_cb),
                     name=f"autonomous-{jid}").start()

    ack = ("On it, sir — working autonomously now. I'll report each step "
           "as it lands and speak up when the goal is done.")
    return f"⟳ AUTONOMOUS_BACKGROUND:{ack}", jid


# ---------------------------------------------------------------------------
# Phase 17 slice: INDEPENDENT verification. The supervisor does NOT trust
# Hermes's "[STEP n] done - evidence" text — it re-checks the artifact itself.
# ---------------------------------------------------------------------------


def _find_paths(text: str) -> list[str]:
    """Extract plausible file/folder paths from a step line."""
    out, seen = [], set()
    for m in _PATH_RE.finditer(text or ""):
        p = _normalize_path(m.group(0))
        if p and p not in seen:
            seen.add(p)
            out.append(p)
    return out


def _verify_step(rest: str) -> tuple[bool, str]:
    """JARVIS independently checks a claimed step. Returns (ok, note)."""
    paths = _find_paths(rest)
    if not paths:
        return True, "no artifact to check (reasoning step)"
    checks = []
    all_ok = True
    for p in paths:
        expanded = os.path.expandvars(os.path.expanduser(p))
        exists = os.path.exists(expanded)
        checks.append(f"{'✓' if exists else '✗'} {p}")
        if not exists:
            all_ok = False
    cm = _CONTENT_RE.search(rest)
    if cm and paths:
        snippet = cm.group(1).strip().strip("\"'")
        try:
            data = open(os.path.expandvars(os.path.expanduser(paths[0])),
                        "r", encoding="utf-8", errors="replace").read()
            has = snippet.lower() in data.lower()
            checks.append(f"{'✓' if has else '✗'} content '{snippet[:40]}'")
            all_ok = all_ok and has
        except Exception:
            checks.append(f"✗ could not read {paths[0]}")
            all_ok = False
    return all_ok, " | ".join(checks)


def _supervise(jid: str, goal: str, proc, timeout: int,
               on_done=None, progress_cb=None):
    """Watcher thread: tail status file → jobs notes; wait on process."""
    import warm_harness  # local import to avoid cycles at module load
    meta = _RUNNING.get(jid, {})
    spath = meta.get("status_path") or status_path(jid)
    offset = 0
    steps_done = 0          # Hermes-claimed done
    steps_verified = 0      # JARVIS independently confirmed
    steps_failed = 0
    failed_steps = []       # what Hermes claimed that JARVIS could not confirm
    deadline = time.time() + timeout

    def _progress(msg):
        if callable(progress_cb):
            try:
                progress_cb(msg)
            except Exception:
                pass

    while True:
        if time.time() > deadline:
            _finish(jid, proc, state="timeout",
                    summary=f"timed out after {timeout}s "
                            f"({steps_done} steps verified)",
                    on_done=on_done)
            return
        # process still alive?
        rc = proc.poll()
        offset, new_lines = _tail_new_lines(spath, offset)
        for ln in new_lines:
            parsed = parse_status_line(ln)
            if not parsed:
                continue
            n, st, rest = parsed
            steps_done += 1
            if st == "done":
                ok, note = _verify_step(rest)
                if ok:
                    steps_verified += 1
                    jobreg.update(jid, note=f"[STEP {n}] ✓ {rest[:90]} — JARVIS: {note}")
                else:
                    steps_failed += 1
                    failed_steps.append(rest.split("|")[0].strip()[:60])
                    jobreg.update(jid, note=f"[STEP {n}] ✗ CLAIM UNVERIFIED — {note}")
            elif st == "fail":
                steps_failed += 1
                failed_steps.append(rest.split("|")[0].strip()[:60])
                jobreg.update(jid, note=f"[STEP {n}] ✗ {rest[:120]}")
            else:
                jobreg.update(jid, note=f"[STEP {n}] … {rest[:120]}")
            _progress(f"[STEP {n}] {st}: {rest[:80]}")
        if rc is not None:
            break
        time.sleep(POLL_INTERVAL)

    out, err = proc.communicate()
    combined = (out or "") + "\n" + (err or "")
    result = parse_result(combined)

    # capture session id updates so the warm pool stays fresh
    try:
        new_sid = warm_harness._parse_session(combined)
        if new_sid:
            with warm_harness._lock:
                warm_harness._session_id = new_sid
                warm_harness._save_session(new_sid)
    except Exception:
        pass

    body = "\n".join(l for l in combined.splitlines()
                     if l.strip() and not l.startswith("session_id:")
                     and not l.startswith("Warning:")).strip()

    if result == "done":
        if steps_verified == 0:
            _finish(jid, proc, state="unverified",
                    summary=f"claimed done but JARVIS could not verify any step "
                            f"({steps_done} claimed, {steps_failed} failed check)",
                    result=body[-4000:], on_done=on_done)
        elif steps_failed > 0 or steps_verified < steps_done:
            # Name the step, so "VS Code is open" is never reported as a
            # success when the check found no VS Code window (2026-09-11).
            which = "; ".join(failed_steps[:3]) or "a step Hermes reported"
            _finish(jid, proc, state="unverified",
                    summary=(f"{steps_verified} of {steps_done} steps verified. "
                             f"I could not confirm: {which}."),
                    result=body[-4000:], on_done=on_done)
        else:
            _finish(jid, proc, state="done",
                    summary=(f"{steps_verified} step(s) verified by JARVIS"
                             + (f", {steps_failed} failed" if steps_failed else "")),
                    result=body[-4000:], on_done=on_done)
    elif result == "failed":
        reason = ""
        for ln in body.splitlines():
            m = _RESULT_RE.search(ln)
            if m:
                reason = ln[m.end():].lstrip(" -–—:")
                break
        _finish(jid, proc, state="error",
                summary=f"failed: {reason[:180] or 'reported failure'} "
                        f"({steps_done} steps verified)",
                result=body[-4000:], on_done=on_done)
    else:
        # no RESULT line at all — dishonest/incomplete run
        _finish(jid, proc, state="unverified",
                summary=f"no RESULT line ({steps_done} steps verified); "
                        "last output: " + (body[-140:] or "<empty>"),
                result=body[-4000:], on_done=on_done)


def _finish(jid, proc, *, state, summary, result=None, on_done=None):
    jobreg.update(jid, state=state, summary=summary, result=result)
    if callable(on_done):
        prefix = {"done": "Goal complete",
                  "unverified": "Finished but I could NOT verify it",
                  "timeout": "Timed out"}.get(state, "Failed")
        try:
            on_done(f"{prefix}, sir. {summary}")
        except Exception:
            pass
    with _LOCK:
        _RUNNING.pop(jid, None)


def cancel(jid: str) -> bool:
    """Kill a running autonomous job. Returns True if it was running."""
    with _LOCK:
        meta = _RUNNING.pop(jid, None)
    if not meta:
        return False
    proc = meta["proc"]
    try:
        proc.kill()
    except Exception:
        pass
    jobreg.update(jid, state="cancelled", summary="cancelled by user")
    return True


def active_jobs() -> list[str]:
    with _LOCK:
        return list(_RUNNING.keys())


def any_running() -> bool:
    """True if any autonomous job subprocess is still alive (watcher guard)."""
    with _LOCK:
        for meta in _RUNNING.values():
            proc = meta.get("proc")
            if proc is not None and proc.poll() is None:
                return True
    return False


def latest_status(jid: str | None = None, lines: int = 3) -> str:
    """Human-readable last status lines for voice ('what's the status?')."""
    if jid is None:
        js = active_jobs()
        jid = js[0] if js else None
    if jid is None:
        recents = jobreg.recent(limit=1)
        if recents:
            r = recents[0]
            return (f"Last job finished: {r.get('summary') or r.get('state')}. ")
        return "No autonomous jobs are running or recent."
    path = status_path(jid)
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            all_lines = [l.strip() for l in f if l.strip()]
    except OSError:
        all_lines = []
    tail = all_lines[-lines:] if all_lines else ["(status file empty — "
                                                 "still planning or stuck)"]
    return "; ".join(tail)


if __name__ == "__main__":
    # smoke: parser units only (no process spawn)
    assert parse_status_line("[STEP 1] done - wrote file | /tmp/x.txt") == \
        (1, "done", "wrote file | /tmp/x.txt")
    assert parse_status_line("[step 2] FAIL - bad thing | reason")[1] == "fail"
    assert parse_status_line("random chatter") is None
    assert parse_result("blah\nRESULT: done\n") == "done"
    assert parse_result("RESULT: failed - disk full") == "failed"
    assert parse_result("no marker here") is None
    print("autonomous.py self-check OK")
