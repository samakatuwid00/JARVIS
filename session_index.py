"""session_index.py — FTS5 full-text search over JARVIS's own session logs.

Phase 13. The JSONL files in sessions/ are the durable transcript; this module
builds/maintains an SQLite FTS5 index over them so JARVIS can answer
"what did I ask about X last week?" — something recent_summary(6) cannot do.

Design:
- index.db lives beside the JSONLs (gitignored with them).
- One FTS5 row per JSONL turn: (ts, role, text, file, line_no).
- Incremental: tracks indexed line count per file in a meta table; only new
  lines are read on refresh. log_and_index() appends to both in one call so
  fresh turns are searchable immediately.
- Stdlib only (sqlite3 ships FTS5). Fail-open: indexing errors never break a
  JARVIS turn.
"""

import json
import os
import sqlite3
import threading

_BASE = os.path.dirname(os.path.abspath(__file__))
_SESSIONS_DIR = os.path.join(_BASE, "sessions")
_DB_PATH = os.path.join(_SESSIONS_DIR, "index.db")

_LOCK = threading.Lock()


def _connect():
    con = sqlite3.connect(_DB_PATH, timeout=5)
    return con


def _ensure_schema(con):
    con.execute(
        "CREATE VIRTUAL TABLE IF NOT EXISTS turns USING fts5("
        "ts, role, text, file, line_no)")
    con.execute(
        "CREATE TABLE IF NOT EXISTS meta (file TEXT PRIMARY KEY, lines INTEGER)")
    con.commit()


def _indexed_count(con, fname):
    row = con.execute("SELECT lines FROM meta WHERE file=?", (fname,)).fetchone()
    return row[0] if row else 0


def _set_indexed(con, fname, n):
    con.execute(
        "INSERT INTO meta(file, lines) VALUES(?,?) "
        "ON CONFLICT(file) DO UPDATE SET lines=excluded.lines", (fname, n))


def _count_lines(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return sum(1 for line in f if line.strip())
    except OSError:
        return 0


def index_file(fname: str) -> int:
    """Index any not-yet-indexed lines of one JSONL file. Returns added rows."""
    path = os.path.join(_SESSIONS_DIR, fname)
    if not os.path.exists(path):
        return 0
    added = 0
    with _LOCK:
        con = _connect()
        try:
            _ensure_schema(con)
            done = _indexed_count(con, fname)
            with open(path, "r", encoding="utf-8") as f:
                for i, line in enumerate(f, 1):
                    if i <= done or not line.strip():
                        continue
                    try:
                        rec = json.loads(line)
                    except Exception:
                        continue
                    con.execute(
                        "INSERT INTO turns(ts, role, text, file, line_no) "
                        "VALUES(?,?,?,?,?)",
                        (str(rec.get("ts", "")), str(rec.get("role", "")),
                         str(rec.get("text", ""))[:8000], fname, i))
                    added += 1
                _set_indexed(con, fname, max(done,
                             _count_lines(path)))
            con.commit()
        finally:
            con.close()
    return added


def rebuild() -> int:
    """Index every session JSONL's new lines. Returns total rows added."""
    total = 0
    try:
        names = [f for f in os.listdir(_SESSIONS_DIR)
                 if f.startswith("jarvis-") and f.endswith(".jsonl")]
    except OSError:
        return 0
    for fname in sorted(names):
        total += index_file(fname)
    return total


def search_sessions(query: str, limit: int = 10):
    """FTS5 search over past turns. Returns list of dicts, newest first."""
    q = (query or "").strip()
    if not q:
        return []
    # Build a safe OR-of-terms match expression from the raw spoken query.
    terms = [t for t in
             __import__("re").findall(r"[\wÀ-ÿ]{2,}", q)]
    if not terms:
        return []
    match = " OR ".join(f'"{t}"' for t in terms[:8])
    with _LOCK:
        con = _connect()
        try:
            _ensure_schema(con)
            rows = con.execute(
                "SELECT ts, role, text, file, line_no FROM turns "
                "WHERE turns MATCH ? "
                "ORDER BY ts DESC LIMIT ?", (match, int(limit))).fetchall()
        finally:
            con.close()
    return [{"ts": r[0], "role": r[1], "text": r[2],
             "file": r[3], "line": r[4]} for r in rows]


def log_and_index(role: str, text: str, iso_ts: str, today_fname: str,
                  line_no_hint: int = None):
    """Append a just-written turn straight into the index (no re-scan).

    Called by session_store.log() AFTER its file append succeeds, passing the
    filename it wrote to and the 1-based line number of that new record.
    """
    if not line_no_hint:
        return
    try:
        with _LOCK:
            con = _connect()
            try:
                _ensure_schema(con)
                con.execute(
                    "INSERT INTO turns(ts, role, text, file, line_no) "
                    "VALUES(?,?,?,?,?)",
                    (iso_ts, role, (text or "")[:8000],
                     os.path.basename(today_fname), line_no_hint))
                done = _indexed_count(con, os.path.basename(today_fname))
                _set_indexed(con, os.path.basename(today_fname),
                             max(done, line_no_hint))
                con.commit()
            finally:
                con.close()
    except Exception:
        pass


def format_hits(hits) -> str:
    if not hits:
        return "No matching past conversations found."
    lines = []
    for h in hits:
        txt = " ".join((h["text"] or "").split())[:160]
        lines.append(f"- {h['ts']} [{h['role']}] {txt}")
    return "\n".join(lines)


if __name__ == "__main__":
    n = rebuild()
    print(f"Indexed {n} new turn(s).")
