"""semantic_memory.py — structured fact store for JARVIS (SQLite + vectors).

Replaces the flat 40-bullet `Remembered` list as JARVIS's semantic memory.
Each fact carries temporal validity (supersede-on-contradiction, never delete),
provenance (source + reference), and reinforcement signals (hits, last_hit).

Storage: SQLite at memory/semantic.db
  - facts table: text + float32 embedding blob + validity window + provenance
  - facts_fts:   FTS5 keyword channel
Retrieval: hybrid — FTS5 keyword candidates fused with cosine (numpy scan;
           the fact count for a single-user agent is small enough that a real
           vector index is unnecessary — sqlite-vec is the drop-in upgrade if
           that ever changes).

Classification (AUDN pattern, deterministic v1):
  - NOOP     cosine >= 0.985 (same fact)         -> refresh timestamp/confidence
  - SUPERSEDE cosine >= 0.90 (restatement/change) -> old fact valid_to=now,
    linked via superseded_by; history is kept, never deleted
  - ADD      otherwise
All failures fail open: memory can never crash a JARVIS turn.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import datetime

_HERE = os.path.dirname(os.path.abspath(__file__))
_DB_PATH = os.environ.get("JARVIS_SEMANTIC_DB", os.path.join(_HERE, "memory", "semantic.db"))

NOOP_COS = 0.985
SUPERSEDE_COS = 0.90
STALE_DAYS = 30          # no reinforcement in 30d starts decaying confidence
DECAY_FLOOR = 0.25       # below this a stale fact stops being recalled

_lock = threading.Lock()
_conn: sqlite3.Connection | None = None
_embedder = None         # fn(text) -> list[float]; injected (tools model)


def set_embedder(fn) -> None:
    """Inject an embedding function (tools._get_embedding_model wrapper)."""
    global _embedder
    _embedder = fn


_classifier = None       # fn(new_text, old_text) -> same|update|different|None


def set_classifier(fn) -> None:
    """Inject an LLM arbiter for the ambiguous cosine band (0.80-0.985)."""
    global _classifier
    _classifier = fn


def _now() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


def _connect() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        os.makedirs(os.path.dirname(_DB_PATH), exist_ok=True)
        _conn = sqlite3.connect(_DB_PATH, check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.execute("""CREATE TABLE IF NOT EXISTS facts (
            id INTEGER PRIMARY KEY,
            text TEXT NOT NULL,
            vec BLOB,
            source TEXT DEFAULT 'remember',
            source_ref TEXT DEFAULT '',
            confidence REAL DEFAULT 1.0,
            hits INTEGER DEFAULT 0,
            last_hit TEXT,
            created TEXT,
            valid_from TEXT,
            valid_to TEXT,
            superseded_by INTEGER)""")
        # migration for DBs created before last_hit existed
        cols = {r[1] for r in _conn.execute("PRAGMA table_info(facts)")}
        if "last_hit" not in cols:
            _conn.execute("ALTER TABLE facts ADD COLUMN last_hit TEXT")
        _conn.execute("""CREATE VIRTUAL TABLE IF NOT EXISTS facts_fts
            USING fts5(text, content='facts', content_rowid='id')""")
        _conn.execute("""CREATE TRIGGER IF NOT EXISTS facts_ai AFTER INSERT ON facts BEGIN
            INSERT INTO facts_fts(rowid, text) VALUES (new.id, new.text); END""")
        _conn.commit()
    return _conn


def _embed(text: str):
    """Embedding vector as bytes, or None when no embedder is available."""
    if _embedder is None:
        return None
    try:
        import numpy as np
        v = _embedder(text)
        return np.asarray(v, dtype="float32").tobytes()
    except Exception:
        return None


def _cos(a_bytes, b_bytes) -> float:
    if not a_bytes or not b_bytes:
        return 0.0
    try:
        import numpy as np
        a = np.frombuffer(a_bytes, dtype="float32")
        b = np.frombuffer(b_bytes, dtype="float32")
        na, nb = float(np.linalg.norm(a)), float(np.linalg.norm(b))
        if na == 0 or nb == 0:
            return 0.0
        return float(np.dot(a, b) / (na * nb))
    except Exception:
        return 0.0


def _norm_text(t: str) -> str:
    return " ".join((t or "").lower().split())


def _fts_candidates(query: str, k: int = 8) -> list[int]:
    """Keyword candidates via FTS5 (best-effort; empty query/lock -> [])."""
    try:
        con = _connect()
        terms = " OR ".join(w for w in _norm_text(query).split() if len(w) > 2)[:200]
        if not terms:
            return []
        rows = con.execute(
            "SELECT rowid FROM facts_fts WHERE facts_fts MATCH ? LIMIT ?",
            (terms, k)).fetchall()
        return [r["rowid"] for r in rows]
    except Exception:
        return []


def add(text: str, source: str = "remember", source_ref: str = "",
        confidence: float = 1.0) -> dict:
    """Classify and store a fact. Returns {action, id, superseded}.

    action: 'add' | 'noop' | 'supersede'
    """
    text = (text or "").strip()
    if not text:
        return {"action": "noop", "id": None, "reason": "empty"}
    vec = _embed(text)
    with _lock:
        con = _connect()
        now = _now()
        # candidate set: nearest valid facts by vector, plus FTS keyword hits
        cands: dict[int, float] = {}
        if vec is not None:
            for r in con.execute(
                    "SELECT id, vec FROM facts WHERE valid_to IS NULL AND vec IS NOT NULL"):
                cands[r["id"]] = _cos(vec, r["vec"])
        for fid in _fts_candidates(_norm_text(text)):
            cands.setdefault(fid, 0.0)
        best, best_cos = None, 0.0
        for fid, cos in cands.items():
            if cos > best_cos:
                row = con.execute("SELECT text FROM facts WHERE id=?", (fid,)).fetchone()
                if row and _norm_text(row["text"]) == _norm_text(text):
                    best, best_cos = fid, 1.0
                elif cos > best_cos:
                    best, best_cos = fid, cos

        # NOOP: the same fact already stored — just reinforce it.
        norm_match = best is not None and _norm_text(
            con.execute("SELECT text FROM facts WHERE id=?",
                        (best,)).fetchone()["text"]) == _norm_text(text)
        if best is not None and (best_cos >= NOOP_COS or
                                 (best_cos >= NOOP_COS - 0.02 and norm_match)):
            con.execute("UPDATE facts SET hits=hits+1, last_hit=?, "
                        "confidence=MIN(1.0, confidence+0.05) WHERE id=?",
                        (now, best))
            con.commit()
            return {"action": "noop", "id": best, "reason": "already known"}

        # Ambiguous band (0.80-0.985): word-order paraphrases and restatements
        # sit here, where cosine alone can't tell "same fact" from "changed
        # fact". Arbitrate with the injected LLM classifier when available;
        # fall back to the deterministic supersede threshold.
        verdict = None
        if best is not None and 0.80 <= best_cos < NOOP_COS and _classifier is not None:
            old_row = con.execute("SELECT text FROM facts WHERE id=?",
                                  (best,)).fetchone()
            try:
                verdict = (_classifier(text, old_row["text"]) or "").strip().lower()
                if verdict not in ("same", "update", "different"):
                    verdict = None
            except Exception:
                verdict = None
        if verdict == "same":
            con.execute("UPDATE facts SET hits=hits+1, last_hit=? WHERE id=?",
                        (now, best))
            con.commit()
            return {"action": "noop", "id": best, "reason": "classifier: same"}
        if verdict == "different":
            supersede_now = False          # classifier says: distinct fact
        else:
            supersede_now = (verdict == "update") or (best_cos >= SUPERSEDE_COS)

        cur = con.execute(
            """INSERT INTO facts (text, vec, source, source_ref, confidence,
                                  hits, created, valid_from)
               VALUES (?,?,?,?,?,1,?,?)""",
            (text, vec, source, source_ref, confidence, now, now))
        new_id = cur.lastrowid

        # SUPERSEDE: a restatement/change of a known fact — retire the old one,
        # keep the history (valid_to set, superseded_by linked).
        superseded = None
        if best is not None and supersede_now:
            con.execute("UPDATE facts SET valid_to=?, superseded_by=? WHERE id=?",
                        (now, new_id, best))
            superseded = best

        con.commit()
        return {"action": "supersede" if superseded else "add",
                "id": new_id, "superseded": superseded, "cosine": round(best_cos, 3)}


def recall(query: str, k: int = 5) -> list[dict]:
    """Hybrid recall of VALID facts: FTS keyword + vector cosine, fused.

    Every returned fact is reinforced (hits+1, last_hit bumped) — recall is
    use, and use is what keeps a memory alive.
    """
    query = (query or "").strip()
    if not query:
        return []
    qvec = _embed(query)
    with _lock:
        con = _connect()
        rows = con.execute(
            """SELECT id, text, vec, source, source_ref, confidence, hits, created
               FROM facts
               WHERE valid_to IS NULL
                 AND (confidence IS NULL OR confidence >= ?)""",
            (DECAY_FLOOR,)).fetchall()
        fts_ids = set(_fts_candidates(query, k=10))
        scored = []
        for r in rows:
            cos = _cos(qvec, r["vec"]) if qvec is not None else 0.0
            kw = 1.0 if r["id"] in fts_ids else 0.0
            if cos <= 0 and kw == 0:
                continue
            score = 0.65 * cos + 0.35 * kw
            scored.append((score, r))
        scored.sort(key=lambda t: t[0], reverse=True)
        out = []
        now = _now()
        for score, r in scored[:k]:
            con.execute("UPDATE facts SET hits=hits+1, last_hit=? WHERE id=?",
                        (now, r["id"]))
            out.append({"id": r["id"], "text": r["text"], "score": round(score, 3),
                        "source": r["source"], "created": r["created"]})
        con.commit()
        return out


def current_facts(limit: int = 12) -> list[dict]:
    """All valid facts, most recently reinforced/created first (for injection)."""
    with _lock:
        con = _connect()
        rows = con.execute(
            """SELECT id, text, confidence, hits, created FROM facts
               WHERE valid_to IS NULL
               ORDER BY COALESCE(last_hit, created) DESC LIMIT ?""", (limit,)).fetchall()
        return [dict(r) for r in rows]


def context_block(limit: int = 12) -> str:
    """Formatted block for prompt injection; '' when the store is empty."""
    facts = current_facts(limit)
    if not facts:
        return ""
    lines = [f"- {f['text']}" for f in facts]
    return "[KNOWN FACTS]\n" + "\n".join(lines) + "\n"


def consolidate(stale_days: int = STALE_DAYS) -> str:
    """Hygiene pass: decay unreinforced facts below the floor out of recall.
    Also backfills missing embeddings when an embedder is registered (facts
    distilled by a process that had no model still get vectors here)."""
    with _lock:
        con = _connect()
        # backfill embeddings for vector-less facts
        if _embedder is not None:
            bare = con.execute(
                "SELECT id, text FROM facts WHERE vec IS NULL").fetchall()
            for r in bare:
                v = _embed(r["text"])
                if v:
                    con.execute("UPDATE facts SET vec=? WHERE id=?", (v, r["id"]))
        now = _now()
        cutoff = time.time() - stale_days * 86400
        rows = con.execute(
            """SELECT id, confidence, COALESCE(last_hit, created) AS last
               FROM facts WHERE valid_to IS NULL""").fetchall()
        decayed = 0
        for r in rows:
            try:
                last_ts = datetime.datetime.fromisoformat(r["last"]).timestamp()
            except Exception:
                continue
            if last_ts >= cutoff:
                continue
            new_conf = round(r["confidence"] * 0.85, 3)
            if new_conf < DECAY_FLOOR:
                con.execute("UPDATE facts SET valid_to=?, confidence=? WHERE id=?",
                            (now, new_conf, r["id"]))
            else:
                con.execute("UPDATE facts SET confidence=? WHERE id=?",
                            (new_conf, r["id"]))
            decayed += 1
        con.commit()
        valid = con.execute(
            "SELECT COUNT(*) AS n FROM facts WHERE valid_to IS NULL").fetchone()["n"]
        return f"decayed {decayed} stale fact(s); {valid} valid"


def dedup_pass(cos_threshold: float = 0.94) -> str:
    """Merge near-duplicate VALID facts.

    Exists because the first distillation ran without embeddings, leaving
    near-duplicates that could not be detected at insert time. The winner of a
    cluster is the most-reinforced fact (hits, then confidence, then newest);
    the losers are superseded by it (valid_to set, superseded_by linked) and
    their reinforcement transfers to the winner — nothing deleted.
    """
    import numpy as np
    with _lock:
        con = _connect()
        rows = con.execute(
            """SELECT id, text, vec, confidence, hits, created
               FROM facts WHERE valid_to IS NULL AND vec IS NOT NULL
               ORDER BY created ASC""").fetchall()
        vecs = [np.frombuffer(r["vec"], dtype="float32") for r in rows]
        alive = [True] * len(rows)
        now = _now()
        merged = 0
        for i in range(len(rows)):
            if not alive[i]:
                continue
            na = float(np.linalg.norm(vecs[i]))
            if na == 0:
                continue
            for j in range(i + 1, len(rows)):
                if not alive[j]:
                    continue
                nb = float(np.linalg.norm(vecs[j]))
                if nb == 0:
                    continue
                cos = float(np.dot(vecs[i], vecs[j]) / (na * nb))
                if cos < cos_threshold:
                    continue
                # winner: more reinforcement; ties -> higher confidence, then newer
                wi, wj = (i, j)
                if (rows[j]["hits"], rows[j]["confidence"], j) > \
                        (rows[i]["hits"], rows[i]["confidence"], i):
                    wi, wj = j, i
                con.execute("UPDATE facts SET valid_to=?, superseded_by=? WHERE id=?",
                            (now, rows[wi]["id"], rows[wj]["id"]))
                con.execute("UPDATE facts SET hits=hits + ? WHERE id=?",
                            (rows[wj]["hits"], rows[wi]["id"]))
                alive[wj] = False
                merged += 1
        con.commit()
        valid = con.execute(
            "SELECT COUNT(*) AS n FROM facts WHERE valid_to IS NULL").fetchone()["n"]
        return f"merged {merged} duplicate(s); {valid} valid facts remain"


def stats() -> dict:
    with _lock:
        con = _connect()
        total = con.execute("SELECT COUNT(*) AS n FROM facts").fetchone()["n"]
        valid = con.execute(
            "SELECT COUNT(*) AS n FROM facts WHERE valid_to IS NULL").fetchone()["n"]
        return {"total": total, "valid": valid}


if __name__ == "__main__":
    set_embedder(lambda t: [0.1] * 8)   # smoke run without the model
    print(add("smoke test fact", source="smoke"))
    print(recall("smoke"))
    print(consolidate())
    print(stats())
