"""Warm Hermes harness — persistent session against local Hermes.

Replaces per-request subprocess spawns with a reused session:
  1. First call:  hermes chat -q task -Q --pass-session-id → captures session_id
  2. Subsequent:  hermes chat -q task -Q --resume SESSION_ID → warm resume

Session is persisted on disk (Hermes SQLite), so it survives JARVIS restarts.
On boot, warm_session() pre-warms with a trivial query to eliminate first-turn lag.

Usage:
    from warm_harness import warm_send, warm_session
    result = warm_send("open notepad", timeout=300, max_turns=15)
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

_HERMES_BIN = shutil.which("hermes") or "hermes"
_TOOLSETS = (
    "web,browser,terminal,file,code_execution,vision,video,image_gen,"
    "video_gen,bfl,x_search,tts,skills,todo,memory,session_search,clarify,"
    "delegation,cronjob,computer_use"
)
_SOURCE = "tool"

# Persisted session ID — survives module reloads within the same process.
_session_id: str | None = None
_lock = threading.Lock()

# Where we cache the session_id on disk so a JARVIS restart can pick it up.
_CACHE_DIR = Path(__file__).parent / ".warm_harness"
_SESSION_CACHE = _CACHE_DIR / "session_id.txt"

# Regex to extract session_id from hermes chat -Q output.
_SESSION_RE = re.compile(r"session_id:\s*(\S+)")


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _child_env() -> dict:
    """Environment for the hermes subprocess — prepend venv Scripts so
    child python3 resolves correctly (turbovec ABI fix)."""
    env = dict(os.environ)
    venv_scripts = os.path.join(
        os.path.dirname(os.path.dirname(_HERMES_BIN)), "Scripts"
    )
    if os.path.isfile(os.path.join(venv_scripts, "python3.exe")):
        env["PATH"] = venv_scripts + os.pathsep + env.get("PATH", "")
    return env


def _run(cmd: list[str], timeout: int) -> tuple[str, str, int]:
    """Run hermes subprocess, return (stdout, stderr, returncode)."""
    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
        encoding="utf-8",
        errors="replace",
        env=_child_env(),
    )
    return proc.stdout or "", proc.stderr or "", proc.returncode


def _parse_session(output: str) -> str | None:
    """Extract session_id from hermes -Q output."""
    for line in output.splitlines():
        m = _SESSION_RE.search(line)
        if m:
            return m.group(1)
    return None


def _parse_reply(output: str) -> str:
    """Strip session_id lines, warning lines, and resume notices from hermes output."""
    lines = []
    for ln in output.splitlines():
        s = ln.strip()
        if not s:
            continue
        if s.startswith("session_id:"):
            continue
        if s.startswith("Warning:"):
            continue
        if "Resumed session" in s:
            continue
        if "Reached maximum iterations" in s:
            continue
        lines.append(s)
    out = "\n".join(lines).strip()
    if out.startswith("Hermes reports:"):
        out = out[len("Hermes reports:"):].lstrip()
    return out


def _save_session(sid: str) -> None:
    """Persist session_id to disk."""
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    _SESSION_CACHE.write_text(sid, encoding="utf-8")


def _load_cached_session() -> str | None:
    """Load session_id from disk cache (survives restarts)."""
    try:
        if _SESSION_CACHE.exists():
            sid = _SESSION_CACHE.read_text(encoding="utf-8").strip()
            if sid:
                return sid
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def warm_session(timeout: int = 300) -> str | None:
    """Ensure a warm Hermes session exists. Returns the session_id.

    Strategy:
      1. Use in-memory cache (fast path).
      2. Fall back to disk cache (survives JARVIS restart).
      3. If no cached session, start a fresh one with a trivial query.

    The trivial query ("say ok") forces Hermes to fully initialize (load tools,
    build system prompt, connect to model provider) so the FIRST real user
    query doesn't pay the full cold-start cost.
    """
    global _session_id

    with _lock:
        # 1. In-memory
        if _session_id:
            return _session_id

        # 2. Disk cache
        cached = _load_cached_session()
        if cached:
            _session_id = cached
            return _session_id

        # 3. Fresh session — warm it with a trivial query
        cmd = [
            _HERMES_BIN, "chat",
            "-q", "say ok",
            "-Q",
            "--pass-session-id",
            "--max-turns", "1",
            "-t", _TOOLSETS,
            "--source", _SOURCE,
        ]
        stdout, stderr, rc = _run(cmd, timeout)
        combined = stdout + "\n" + stderr
        sid = _parse_session(combined)
        if sid:
            _session_id = sid
            _save_session(sid)
            return _session_id

        # Failed to get session_id — caller will fall back to fresh spawn
        return None


def warm_send(
    task: str,
    timeout: int = 300,
    max_turns: int = 15,
    progress_cb=None,
) -> str:
    """Send a task to Hermes via the warm session.

    Returns the parsed reply text. Falls back to fresh spawn if session
    is unavailable. If `progress_cb` is callable, it is invoked at milestones
    ("Delegating to Hermes...", "Hermes finished.") so the caller can stream
    status to the user while the (blocking) subprocess runs.
    """
    global _session_id

    task = str(task or "").strip()
    if not task:
        return "[Error] No task given."

    if callable(progress_cb):
        try:
            progress_cb(f"Delegating to Hermes: {task[:80]}")
        except Exception:
            pass

    with _lock:
        sid = _session_id

    # If no session yet, try to warm one
    if not sid:
        sid = warm_session(timeout=min(timeout, 120))

    # Attempt with session
    if sid:
        cmd = [
            _HERMES_BIN, "chat",
            "-q", task,
            "-Q",
            "--resume", sid,
            "--max-turns", str(max_turns),
            "-t", _TOOLSETS,
            "--source", _SOURCE,
        ]
        try:
            stdout, stderr, rc = _run(cmd, timeout)
            combined = stdout + "\n" + stderr

            # Check if session was actually resumed
            if "Resumed session" in combined or rc == 0:
                # Update session_id (hermes may reassign on resume)
                new_sid = _parse_session(combined)
                if new_sid and new_sid != sid:
                    with _lock:
                        _session_id = new_sid
                        _save_session(new_sid)

                reply = _parse_reply(combined)
                if reply:
                    if callable(progress_cb):
                        try:
                            progress_cb("Hermes finished.")
                        except Exception:
                            pass
                    return reply
                if rc != 0:
                    # Session may be stale — clear and retry fresh
                    with _lock:
                        _session_id = None
                        try:
                            _SESSION_CACHE.unlink(missing_ok=True)
                        except Exception:
                            pass
            else:
                # Resume failed — session stale, clear and retry fresh
                with _lock:
                    _session_id = None
                    try:
                        _SESSION_CACHE.unlink(missing_ok=True)
                    except Exception:
                        pass
        except subprocess.TimeoutExpired:
            return (
                f"[Error] Hermes did not finish within {timeout}s. "
                "The task may be too large to delegate from a voice turn."
            )
        except Exception as e:
            return f"[Error] Could not launch Hermes: {type(e).__name__}: {e}"

    # Fallback: fresh spawn (no session)
    cmd = [
        _HERMES_BIN, "chat",
        "-q", task,
        "-Q",
        "--pass-session-id",
        "--max-turns", str(max_turns),
        "-t", _TOOLSETS,
        "--source", _SOURCE,
    ]
    try:
        stdout, stderr, rc = _run(cmd, timeout)
        combined = stdout + "\n" + stderr

        # Capture session_id for next call
        new_sid = _parse_session(combined)
        if new_sid:
            with _lock:
                _session_id = new_sid
                _save_session(new_sid)

        reply = _parse_reply(combined)
        if rc != 0 and not reply:
            return f"[Error] Hermes exited with code {rc} and said nothing."
        if not reply:
            return "Hermes finished but returned no output."
        # Truncate long replies for voice
        if len(reply) > 1500:
            reply = reply[:1500] + " …(truncated)"
        if callable(progress_cb):
            try:
                progress_cb("Hermes finished.")
            except Exception:
                pass
        return reply
    except subprocess.TimeoutExpired:
        return (
            f"[Error] Hermes did not finish within {timeout}s. "
            "The task may be too large to delegate from a voice turn."
        )
    except Exception as e:
        return f"[Error] Could not launch Hermes: {type(e).__name__}: {e}"


def warm_redirect(instruction: str, timeout: int = 300, max_turns: int = 5) -> str | None:
    """Mid-flight redirect: inject a new instruction into the warm session.

    Equivalent to speaking a correction while Hermes is working — appends a
    new user turn to the same persistent session via `--resume`. Returns the
    Hermes reply, or None if no warm session exists.
    """
    global _session_id
    instruction = str(instruction or "").strip()
    if not instruction:
        return None
    with _lock:
        sid = _session_id
    if not sid:
        return None
    cmd = [
        _HERMES_BIN, "chat",
        "-q", instruction,
        "-Q",
        "--resume", sid,
        "--max-turns", str(max_turns),
        "-t", _TOOLSETS,
        "--source", _SOURCE,
    ]
    try:
        stdout, stderr, rc = _run(cmd, timeout)
        combined = stdout + "\n" + stderr
        # session may be reassigned on resume
        new_sid = _parse_session(combined)
        if new_sid and new_sid != sid:
            with _lock:
                _session_id = new_sid
                _save_session(new_sid)
        return _parse_reply(combined) or None
    except Exception:
        return None


def warm_reset() -> None:
    """Clear the warm session (e.g., after idle timeout)."""
    global _session_id
    with _lock:
        _session_id = None
        try:
            _SESSION_CACHE.unlink(missing_ok=True)
        except Exception:
            pass


def warm_status() -> dict:
    """Return status info about the warm session."""
    with _lock:
        return {
            "session_id": _session_id,
            "cached": bool(_session_id),
            "cache_file": str(_SESSION_CACHE),
            "cache_exists": _SESSION_CACHE.exists(),
        }


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    print("=== Warm Harness Self-Test ===")
    print(f"Hermes binary: {_HERMES_BIN}")
    print(f"Toolsets: {_TOOLSETS[:60]}...")

    t0 = time.time()
    sid = warm_session(timeout=120)
    t1 = time.time()
    print(f"\nWarm session: {sid} (took {t1-t0:.1f}s)")

    t2 = time.time()
    r1 = warm_send("say hello", timeout=60, max_turns=1)
    t3 = time.time()
    print(f"Reply 1: {r1[:80]}... ({t3-t2:.1f}s)")

    t4 = time.time()
    r2 = warm_send("what did I just say?", timeout=60, max_turns=1)
    t5 = time.time()
    print(f"Reply 2: {r2[:80]}... ({t5-t4:.1f}s)")

    print(f"\nStatus: {warm_status()}")
    print("=== DONE ===")
