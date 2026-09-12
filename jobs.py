"""jobs.py — unified background-job registry for JARVIS (Phase: async specialists).

One registry that every delegation tier reports into, so "what are you working
on?" has one answer regardless of whether the work runs on a local tool, an
OpenCode specialist, or the Hermes harness.

Design:
  - In-memory dict for O(1) status reads; append-only JSONL log for durability.
  - Events are pushed through an optional callback (jarvis_web wires this to
    the HUD WebSocket) at every state transition — running/progress/done/
    error/timeout. Voice announcements ride the same events.
  - Stdlib only, thread-safe via a single lock; jobs survive process restarts
    as history (running jobs from a dead process are marked 'error' on load).
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
import uuid
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_LOG_PATH = Path(os.getenv("JARVIS_JOBS_LOG", _HERE / "logs" / "jobs.jsonl"))

_lock = threading.Lock()
_jobs: dict[str, dict] = {}
_listeners: list = []          # callables(event: dict) -> None
_loaded = False

# queued / waiting-on-confirm jobs never reach a terminal state on their own,
# so without expiry they pile up in active() forever (real case: 26 stale
# waiting-on-confirm jobs from a broken confirm latch). Anything in these two
# states older than the TTL is marked timeout on the next status read. Ten
# minutes: a confirm is answered right away or not at all, and an hour of
# "waiting for you to say confirm" for long-forgotten asks was noise in
# Active Tasks and in "status?" (2026-09-12).
STALE_TTL_SECONDS = float(os.getenv("JARVIS_JOB_STALE_TTL", "600"))
_STALE_STATES = ("queued", "waiting-on-confirm")


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def _append_log(rec: dict) -> None:
    try:
        _LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass  # registry must never break a turn over log IO


def _load_history() -> None:
    """Load past jobs once per process; mark interrupted runs as errored."""
    global _loaded
    if _loaded:
        return
    _loaded = True
    if not _LOG_PATH.exists():
        return
    try:
        with open(_LOG_PATH, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                jid = rec.get("id")
                if not jid:
                    continue
                prev = _jobs.get(jid)
                # last record wins, except the step notes: each record carries
                # one, and keeping only the last lost which agent a job used.
                merged = {**(prev or {}), **rec}
                notes = list((prev or {}).get("progress") or [])
                if rec.get("note"):
                    notes.append(rec["note"])
                merged["progress"] = notes[-20:]
                _jobs[jid] = merged
    except Exception:
        return
    for j in _jobs.values():
        if j.get("state") == "running":
            j["state"] = "error"
            j["error"] = "interrupted by restart"


# ---------------------------------------------------------------------------
# Listeners (HUD WebSocket, voice announcements)
# ---------------------------------------------------------------------------

def add_listener(fn) -> None:
    with _lock:
        if fn not in _listeners:
            _listeners.append(fn)


def remove_listener(fn) -> None:
    """Drop a listener (a HUD connection that closed must stop hearing jobs)."""
    with _lock:
        if fn in _listeners:
            _listeners.remove(fn)


def _emit(job: dict) -> None:
    event = {
        "type": "job",
        "id": job["id"],
        "task": job.get("task", ""),
        "tier": job.get("tier", ""),
        "agent": job.get("agent"),
        "state": job.get("state"),
        "note": (job.get("progress") or [None])[-1],
        "error": job.get("error"),
        "summary": job.get("summary"),
        "elapsed": round(time.time() - job["started"], 1) if job.get("started") else None,
    }
    for fn in list(_listeners):
        try:
            fn(event)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def _expire_stale() -> None:
    """Mark stale queued / waiting-on-confirm jobs as timeout (read-side sweep)."""
    now = time.time()
    with _lock:
        stale = [jid for jid, j in _jobs.items()
                 if j.get("state") in _STALE_STATES
                 and (j.get("started") or 0) and now - j["started"] > STALE_TTL_SECONDS]
    for jid in stale:
        update(jid, state="timeout",
               note=f"expired — no confirm within {int(STALE_TTL_SECONDS // 60)} min")


def create(task: str, tier: str, agent: str | None = None,
           background: bool = True) -> str:
    """Register a new job; returns its id. Emits a 'queued' event."""
    _load_history()
    jid = uuid.uuid4().hex[:8]
    rec = {
        "id": jid,
        "task": task,
        "tier": tier,
        "agent": agent,
        "state": "queued",
        "background": bool(background),
        "started": time.time(),
        "progress": [],
        "result": None,
        "summary": None,
        "error": None,
    }
    with _lock:
        _jobs[jid] = rec
    _append_log({"id": jid, "ts": time.time(), **{k: rec[k] for k in
                 ("task", "tier", "agent", "state")}})
    _emit(rec)
    return jid


def update(jid: str, *, state: str | None = None, note: str | None = None,
           result: str | None = None, summary: str | None = None,
           error: str | None = None) -> None:
    """Transition a job; emits on every change."""
    _load_history()
    with _lock:
        job = _jobs.get(jid)
        if not job:
            return
        if state:
            job["state"] = state
        if note:
            job.setdefault("progress", []).append(note)
            job["progress"] = job["progress"][-20:]
        if result is not None:
            job["result"] = result
        if summary is not None:
            job["summary"] = summary
        if error is not None:
            job["error"] = error
        # Elapsed time freezes at the first terminal transition.
        if state in ("done", "error", "timeout") and job.get("started"):
            job["elapsed"] = round(time.time() - job["started"], 1)
        snapshot = dict(job)
    # Persist the FULL record on every transition so a fresh process
    # (status poll, HUD reconnect, post-restart query) sees result,
    # summary, error and elapsed - not just the last state flip.
    rec = {"id": jid, "ts": time.time(),
           "state": snapshot["state"],
           "task": snapshot.get("task"),
           "tier": snapshot.get("tier"),
           "agent": snapshot.get("agent"),
           "started": snapshot.get("started"),
           "elapsed": snapshot.get("elapsed")}
    if note is not None:
        rec["note"] = note
    if result is not None:
        rec["result"] = result
    if summary is not None:
        rec["summary"] = summary
    if error is not None:
        rec["error"] = error
    _append_log(rec)
    _emit(snapshot)


def get(jid: str) -> dict | None:
    _load_history()
    with _lock:
        job = _jobs.get(jid)
        return dict(job) if job else None


def active() -> list[dict]:
    """Currently queued/running/waiting-on-confirm jobs, oldest first."""
    _load_history()
    _expire_stale()     # was defined but never called: weeks-old confirms stayed "active"
    with _lock:
        rows = [dict(j) for j in _jobs.values()
                if j.get("state") in ("queued", "running", "waiting-on-confirm")]
    rows.sort(key=lambda j: j.get("started") or 0)
    return rows

def waiting() -> list[dict]:
    """Jobs awaiting confirm, oldest first."""
    _load_history()
    _expire_stale()
    with _lock:
        rows = [dict(j) for j in _jobs.values() if j.get("state") == "waiting-on-confirm"]
    rows.sort(key=lambda j: j.get("started") or 0)
    return rows


def recent(limit: int = 10) -> list[dict]:
    """Most recent jobs of any state, newest first."""
    _load_history()
    with _lock:
        rows = sorted(_jobs.values(), key=lambda j: j.get("started") or 0)
    return [dict(j) for j in rows[-limit:]][::-1]


# Words a question about past work shares with the job it means - not the
# question's own framing ("what coding agent did you use to create ...").
_FRAME_WORDS = {"what", "which", "who", "how", "when", "where", "why", "did", "does", "you", "your",
                "use", "used", "the", "this", "that", "these", "those", "and", "for", "with", "from",
                "was", "were", "are", "its", "create", "created", "make", "made", "build", "built",
                "coding", "agent", "tool", "can", "please", "jarvis", "cygnus", "sir", "just", "earlier",
                "today", "one", "job", "task", "work", "have", "has", "had"}


def content_words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", (text or "").lower())
            if len(w) >= 3 and w not in _FRAME_WORDS}


def find_work(query: str, limit: int = 3) -> list[dict]:
    """Past jobs whose task shares the most words with `query`, best first:
    "the hello world website" finds the job that built it hours ago, long
    after it left the newest three."""
    words = content_words(query)
    if not words:
        return []
    _load_history()
    with _lock:
        rows = [dict(j) for j in _jobs.values()]
    need = 1 if len(words) == 1 else 2
    scored = [(len(words & content_words(j.get("task") or "")), j.get("started") or 0, j) for j in rows]
    scored = sorted((s for s in scored if s[0] >= need), key=lambda s: (s[0], s[1]), reverse=True)
    return [j for _, _, j in scored[:limit]]


def format_jobs(jobs: list[dict], max_notes: int = 6) -> str:
    """Jobs as plain facts - task, who ran it, each step, the outcome."""
    lines = []
    for j in jobs:
        when = time.strftime("%H:%M", time.localtime(j.get("started") or 0))
        who = j.get("agent") or j.get("tier") or "unknown"
        lines.append(f"- {when} job {j['id']} ({who}), state {j.get('state')}: "
                     f"{(j.get('task') or '')[:120]}")
        for note in (j.get("progress") or [])[-max_notes:]:
            lines.append(f"    {note[:200]}")
        if j.get("summary"):
            lines.append(f"    outcome: {' '.join(j['summary'].split())[:200]}")
    return "\n".join(lines)


def recent_work(limit: int = 3, max_notes: int = 6, exclude=()) -> str:
    """The latest jobs (minus `exclude` ids) as plain facts, so "what did you
    use to build that?" is answered from the record rather than guessed."""
    return format_jobs([j for j in recent(limit + len(exclude)) if j["id"] not in exclude][:limit],
                       max_notes)


def status_line() -> str:
    """Human one-liner for voice: 'what are you working on?'"""
    act = active()
    if not act:
        return "Nothing running right now, sir."
    parts = []
    for j in act[:3]:
        mins = (time.time() - (j.get("started") or time.time())) / 60
        label = f"{j.get('agent') or j.get('tier')}: {j['task'][:40]}"
        parts.append(f"{label} ({mins:.0f} min in)" if mins >= 1 else label)
    more = f", plus {len(act)-3} more" if len(act) > 3 else ""
    return f"{len(act)} task{'s' if len(act)!=1 else ''} running: " + "; ".join(parts) + more + "."
