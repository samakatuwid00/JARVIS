"""dialogue_state.py - one place for what the conversation is about right now.

Phase 3 of the JARVIS Apps Intelligence design. Holds:
  - the open question JARVIS asked ("Which movie, sir?", "Make that the default?")
  - the last result on screen (a results page, an opened title, an app)
  - the app in front (read from the foreground window)
  - the last few turns (what was said, what JARVIS did)
and gives the understand-first router one snapshot() to read.

Older modules (the pending-name question in tools, the step runner) still keep
their own copies and mirror into this one; the switch-over (phase 5) retires
those copies. In-process only: lost on restart, like the conversation window.
"""

import threading
import time
from collections import deque

PENDING_TTL = 180    # seconds an unanswered question stays open
LAST_TTL = 900       # seconds "it" keeps pointing at the last result
MAX_TURNS = 8

_LOCK = threading.Lock()
_STATE = {"pending": None, "last": None, "turns": deque(maxlen=MAX_TURNS)}


def reset():
    with _LOCK:
        _STATE["pending"] = None
        _STATE["last"] = None
        _STATE["turns"].clear()


def ask(kind, question, payload=None, owner=""):
    """Record the question JARVIS just asked; the next reply may answer it."""
    with _LOCK:
        _STATE["pending"] = {"kind": kind, "question": question, "payload": payload or {},
                             "owner": owner, "ts": time.time()}


def pending(kind=None):
    """The open question (optionally only of one kind), or None once expired."""
    with _LOCK:
        p = _STATE["pending"]
        if not p or time.time() - p["ts"] > PENDING_TTL:
            _STATE["pending"] = None
            return None
        return dict(p) if kind in (None, p["kind"]) else None


def answered():
    with _LOCK:
        _STATE["pending"] = None


def remember_result(kind, **info):
    """What a command just left on screen: kind "results", "page" or "app"."""
    with _LOCK:
        _STATE["last"] = dict(info, kind=kind, ts=time.time())


def last_result(kind=None):
    with _LOCK:
        last = _STATE["last"]
        if not last or time.time() - last["ts"] > LAST_TTL:
            return None
        return dict(last) if kind in (None, last["kind"]) else None


def record_turn(user, reply, route=None):
    """One finished turn: what was said, what JARVIS answered, which path ran."""
    with _LOCK:
        _STATE["turns"].append({"ts": time.time(), "user": (user or "")[:300],
                                "reply": (reply or "")[:300], "route": route or {}})


def recent_turns(n=4):
    with _LOCK:
        return list(_STATE["turns"])[-n:]


def _foreground_pid():
    try:
        import ctypes
        from ctypes import wintypes
        hwnd = ctypes.windll.user32.GetForegroundWindow()
        pid = wintypes.DWORD()
        ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        return pid.value
    except Exception:
        return 0


def active_app():
    """Registry key of the app in front, matched by its program file, or None."""
    pid = _foreground_pid()
    if not pid:
        return None
    try:
        import os
        import app_abilities
        exe = os.path.basename(app_abilities._process_exe(pid))
        if not exe:
            return None
        for key, entry in app_abilities.load_apps()["apps"].items():
            if isinstance(entry, dict) and \
                    os.path.basename((entry.get("bin") or "").split(",")[0]).lower() == exe:
                return key
    except Exception:
        return None
    return None


def snapshot():
    """Everything the router should know about "now", as plain data."""
    last = last_result()
    return {
        "open_question": (pending() or {}).get("question"),
        "last_result": {k: v for k, v in (last or {}).items()
                        if k in ("kind", "title", "url", "app", "slot")} or None,
        "active_app": active_app(),
        "recent_turns": [{"user": t["user"], "reply": t["reply"]} for t in recent_turns()],
    }
