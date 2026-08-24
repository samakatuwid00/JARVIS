"""audit.py — structured JSONL audit log for every JARVIS tool call.

Phase 11a. Replaces the bare `[TOOL]` print lines as the queryable record:
  {ts, tool, args, duration_s, ok, result_summary, confirmed}

One line per execute_tool() invocation, appended to logs/audit.jsonl.
Prints stay (server.log remains human-readable); this is the machine record.
Stdlib only, fail-open: an audit write error must never break a tool call.
"""

import json
import os
import re
import threading
import time
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
AUDIT_DIR = os.path.join(BASE_DIR, "logs")
AUDIT_PATH = os.path.join(AUDIT_DIR, "audit.jsonl")

_LOCK = threading.Lock()
_MAX_ARGS_JSON = 300       # keep lines bounded; full args rarely matter later
_MAX_RESULT = 240


def _summarize_result(result) -> tuple[bool, str]:
    """(ok, first-line summary). [Error]/[Tool Error] prefixes mean failure."""
    text = str(result or "")
    first = text.strip().splitlines()[0][:200] if text.strip() else ""
    ok = not first.startswith(("[Error]", "[Tool Error]"))
    return ok, first


def log_call(tool: str, args: dict, duration_s: float, result,
             confirmed: bool = False) -> None:
    """Append one audit line. Never raises."""
    try:
        ok, summary = _summarize_result(result)
        entry = {
            "ts": datetime.now().isoformat(timespec="milliseconds"),
            "tool": tool,
            "args": json.dumps(args, default=str)[:_MAX_ARGS_JSON],
            "duration_s": round(float(duration_s), 2),
            "ok": ok,
            "result_summary": summary[:_MAX_RESULT],
            "confirmed": bool(confirmed),
        }
        line = json.dumps(entry, ensure_ascii=False)
        with _LOCK:
            os.makedirs(AUDIT_DIR, exist_ok=True)
            with open(AUDIT_PATH, "a", encoding="utf-8") as f:
                f.write(line + "\n")
    except Exception:
        pass  # audit must never break execution


def read_recent(n: int = 20) -> list[dict]:
    """Last n audit entries, oldest last. For 'what did you just do?'."""
    try:
        with open(AUDIT_PATH, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except FileNotFoundError:
        return []
    out = []
    for line in lines[-n:]:
        try:
            out.append(json.loads(line))
        except Exception:
            continue
    return out


def format_recent(n: int = 20) -> str:
    entries = read_recent(n)
    if not entries:
        return "No audit entries yet."
    marks = {True: "ok", False: "FAIL"}
    conf = {True: " [confirmed]", False: ""}
    return "\n".join(
        f"- {e['ts']} {e['tool']} ({e['duration_s']}s) "
        f"{marks.get(e['ok'], '?')}{conf.get(e['confirmed'], '')} — "
        f"{e['result_summary']}"
        for e in entries)
