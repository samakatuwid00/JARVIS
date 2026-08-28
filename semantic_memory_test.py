#!/usr/bin/env python3
"""Unit tests for semantic_memory: AUDN classification, recall fusion, decay."""
import math
import os
import sys
import tempfile

tmp = tempfile.mkdtemp(prefix="sm_test_")
os.environ["JARVIS_SEMANTIC_DB"] = os.path.join(tmp, "semantic.db")

import semantic_memory as sm  # noqa: E402

fails = []


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        fails.append(name)


# ---- precise fake embedder -------------------------------------------------
# unit vectors with controlled angles: identical text -> cos 1.0; the
# "september" restatement -> cos ~0.95 (SUPERSEDE band); other topics -> low.
_V = {}


def _unit(x, y):
    n = math.sqrt(x * x + y * y) or 1.0
    return [x / n, y / n]


_V["delivery is pickup only for now"] = _unit(1.0, 0.0)
_V["delivery is pickup only starting september"] = _unit(0.95, 0.312)   # cos ~0.95
_V["business sells musubi and graham balls"] = _unit(0.0, 1.0)
_V["music playlist for coding sessions"] = _unit(-1.0, 0.2)
_FALLBACK = _unit(0.7, 0.7)


def fake_embed(text):
    return _V.get(text.strip().lower(), _FALLBACK)


sm.set_embedder(fake_embed)

# ---- ADD ----
r1 = sm.add("delivery is pickup only for now", source="remember")
check("new fact adds", r1["action"] == "add")

# ---- NOOP: identical fact ----
r2 = sm.add("delivery is pickup only for now", source="remember")
check("identical fact -> noop", r2["action"] == "noop" and r2["id"] == r1["id"])

# ---- SUPERSEDE: restatement in the 0.90-0.985 cosine band ----
r3 = sm.add("delivery is pickup only starting september", source="remember")
check("restatement supersedes", r3["action"] == "supersede" and r3["superseded"] == r1["id"])
check("old fact retired", sm.stats()["valid"] == 1)

# ---- ADD: different topic coexists ----
r4 = sm.add("business sells musubi and graham balls", source="remember")
check("different topic adds", r4["action"] == "add")
check("two valid facts", sm.stats()["valid"] == 2)

# ---- recall: vector + keyword channels ----
hits = sm.recall("delivery pickup")
check("recall finds delivery fact", any("delivery" in h["text"] for h in hits))
hits = sm.recall("musubi business products")
check("recall finds business fact", any("musubi" in h["text"] for h in hits))

# ---- recall reinforces: hits bumped ----
before = [f for f in sm.current_facts(10) if "delivery" in f["text"]]
sm.recall("delivery pickup")
after = [f for f in sm.current_facts(10) if "delivery" in f["text"]]
check("recall reinforces hits", after and before and after[0]["hits"] >= before[0]["hits"])

# ---- context_block ----
block = sm.context_block()
check("context_block formats", block.startswith("[KNOWN FACTS]") and "delivery" in block)

# ---- superseded facts are NOT recalled ----
hits = sm.recall("delivery is pickup only for now")
check("superseded fact not recalled", all(h["id"] != r1["id"] for h in hits))

# ---- decay: stale facts drop out ----
sm._connect().execute(
    "UPDATE facts SET last_hit='2026-06-01T00:00:00', created='2026-06-01T00:00:00'")
sm._connect().commit()
report = sm.consolidate(stale_days=30)
check("consolidate decays stale facts", "decayed 2" in report)
confs = [r["confidence"] for r in sm._connect().execute(
    "SELECT confidence FROM facts WHERE id IN (?,?)", (r3["id"], r4["id"])).fetchall()]
check("stale confidence decayed below 1.0", confs and all(cc < 1.0 for cc in confs))

# ---- empty/garbage input ----
check("empty text noop", sm.add("")["action"] == "noop")

# ---- keyword-only mode (no embedder) still works ----
sm.set_embedder(None)
r = sm.add("music playlist for coding sessions", source="remember")
check("keyword-only add works", r["action"] == "add")
hits = sm.recall("music playlist")
check("keyword-only recall works", any("music" in h["text"] for h in hits))

# ---- LLM arbitration in the ambiguous band (0.80-0.985) ----
# candidate at cos ~0.92 vs the seeded old fact: the deterministic rule alone
# cannot tell "same" from "changed" here — the classifier arbitrates.
_V["delivery is pickup only starting october"] = _unit(0.92, 0.392)

sm.set_embedder(fake_embed)


def _seed_old_delivery():
    """Seed one valid 'old' delivery fact directly (bypassing AUDN)."""
    con = sm._connect()
    now = sm._now()
    v = bytes(__import__("numpy").asarray(_unit(1.0, 0.0), dtype="float32"))
    con.execute("DELETE FROM facts WHERE text LIKE '%delivery%'")
    con.execute(
        """INSERT INTO facts (text, vec, source, source_ref, confidence, hits,
                              created, valid_from)
           VALUES ('delivery is pickup only for now', ?, 'remember',
                   'seed', 1.0, 1, ?, ?)""", (v, now, now))
    con.commit()


sm.set_classifier(lambda new, old: "same")     # LLM says: same fact
_seed_old_delivery()
r = sm.add("delivery is pickup only starting october", source="remember")
check("classifier 'same' -> noop", r["action"] == "noop" and r["reason"] == "classifier: same")

sm.set_classifier(lambda new, old: "update")   # LLM says: it replaces
_seed_old_delivery()
r = sm.add("delivery is pickup only starting october", source="remember")
check("classifier 'update' -> supersede", r["action"] == "supersede")

sm.set_classifier(lambda new, old: "different")  # LLM says: distinct fact
_seed_old_delivery()
r = sm.add("delivery is pickup only starting october", source="remember")
check("classifier 'different' -> add", r["action"] == "add")

sm.set_classifier(None)                        # no arbiter -> 0.92 >= 0.90 supersedes
_seed_old_delivery()
r = sm.add("delivery is pickup only starting october", source="remember")
check("no classifier -> deterministic supersede", r["action"] == "supersede")

# classifier errors fall back to deterministic (supersedes here)
sm.set_classifier(lambda new, old: (_ for _ in ()).throw(RuntimeError("router down")))
_seed_old_delivery()
r = sm.add("delivery is pickup only starting october", source="remember")
check("classifier failure -> deterministic supersede", r["action"] == "supersede")
sm.set_classifier(None)

# ---- dedup_pass merges coexisting near-duplicates ----
# reproduce the real-world case: insert a near-duplicate bypassing AUDN
# (as the vector-less first distillation effectively did)
import numpy as _np
con = sm._connect()
now = sm._now()
_dvec = _np.asarray(_unit(0.98, 0.199), dtype="float32").tobytes()
con.execute(
    """INSERT INTO facts (text, vec, source, source_ref, confidence, hits,
                          created, valid_from)
       VALUES ('Our delivery radius is limited to Naga City only', ?,
               'hygiene', 'jarvis-test:1-9', 1.0, 2, ?, ?)""",
    (_dvec, now, now))
con.commit()
before = sm.stats()["valid"]
report = sm.dedup_pass(0.94)
after = sm.stats()["valid"]
check("dedup merges near-duplicates", after < before and "merged 1" in report)
# winner keeps the merged reinforcement
winner_hits = [r["hits"] for r in con.execute(
    "SELECT hits FROM facts WHERE valid_to IS NULL AND text LIKE '%delivery%'")]
check("winner absorbed loser hits", any(h >= 2 for h in winner_hits))

print("\n" + ("ALL PASS" if not fails else f"{len(fails)} FAILURES: {fails}"))
sys.exit(1 if fails else 0)
