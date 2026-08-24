"""JARVIS session store (Tier-3 persistent memory).

JARVIS's OWN conversation transcript, kept separately from Hermes's
session_search (which indexes *Hermes's* chats, not JARVIS's). This lets
JARVIS recall within and across its own sessions — the missing piece that
Hermes's memory does not cover.

Design:
- One append-only JSONL file per calendar day: sessions/jarvis-YYYY-MM-DD.jsonl
- Each line: {"ts": ISO, "role": "user"|"assistant"|"tool"|"error", "text": ...}
- Rolling in-memory window (last N turns) for fast context re-injection.
- Gitignored + local-only (privacy: contains everything said to JARVIS).

No external deps; stdlib only so it can't break JARVIS startup.
"""
import os
import json
import datetime

_SESSIONS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sessions")
_WINDOW = 20  # last N turns held in memory

# In-memory rolling window (per-process; the JSONL is the durable source).
_recent: list[dict] = []


def _ensure_dir() -> None:
    try:
        os.makedirs(_SESSIONS_DIR, exist_ok=True)
    except OSError:
        pass


def _today_file() -> str:
    _ensure_dir()
    day = datetime.date.today().isoformat()
    return os.path.join(_SESSIONS_DIR, f"jarvis-{day}.jsonl")


def log(role: str, text: str) -> None:
    """Append one turn to the session store. Failures are swallowed so the
    session store can never crash a JARVIS turn."""
    rec = {
        "ts": datetime.datetime.now().isoformat(timespec="seconds"),
        "role": role,
        "text": (text or "")[:8000],  # bound size; full text lives in JSONL if needed
    }
    _recent.append(rec)
    if len(_recent) > _WINDOW:
        _recent.pop(0)
    try:
        with open(_today_file(), "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            line_no = _file_lines.get(_today_file(), 0) + 1
            _file_lines[_today_file()] = line_no
    except OSError:
        return
    # Phase 13: keep the FTS5 index current for fresh turns (fail-open).
    try:
        import session_index
        session_index.log_and_index(role, rec["text"], rec["ts"],
                                    _today_file(), line_no)
    except Exception:
        pass


# per-process line counts for log_and_index provenance (best-effort)
_file_lines: dict = {}


def context_window() -> list[dict]:
    """Return the rolling in-memory window (oldest first) for re-injection."""
    return list(_recent)


def recent_summary(n: int = 6) -> str:
    """Flatten the last n turns into a short text block for the router prompt."""
    lines = []
    for r in _recent[-n:]:
        role = r.get("role", "?")
        txt = (r.get("text") or "").replace("\n", " ")
        lines.append(f"{role}: {txt[:240]}")
    return "\n".join(lines)


if __name__ == "__main__":
    log("user", "test turn")
    log("assistant", "test reply")
    print("recent:", recent_summary())
    print("file would be:", _today_file())
