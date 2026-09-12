"""semantic_route.py - route a command by what it means, not its keywords.

Step 3 of the semantic-routing plan, log-only. Every confident label in
eval/routing_gold.jsonl is an example; a new command takes the route its
nearest examples vote for, compared as model2vec sentence embeddings
(potion-multilingual-128M: ~0.1 ms a sentence on CPU, reads Tagalog). Below
MIN_SCORE it abstains and the keyword router keeps the turn.

JARVIS_SEMANTIC=lead (default) logs its pick next to each turn and lets a
confident pick for a route it earned lead (lead()); "shadow" only logs;
"off" skips both and never loads the model (~1 GB of server RAM).
"""

import json
import os
import threading

import numpy as np

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
GOLD = os.path.join(BASE_DIR, "eval", "routing_gold.jsonl")
MODEL = os.getenv("JARVIS_SEMANTIC_MODEL", "minishlab/potion-multilingual-128M")
MODE = os.getenv("JARVIS_SEMANTIC", "lead").strip().lower()
NEIGHBOURS = 5
MIN_SCORE = float(os.getenv("JARVIS_SEMANTIC_MIN", "0.6"))
# Routes where the semantic pick beat the keyword route under nested
# cross-validation (routing_eval --semantic, 2026-09-12), and the score a pick
# needs before it leads.
LEAD_ROUTES = {"app_action", "clarify", "job_status", "media_control", "open_site",
               "recall_memory", "task"}
LEAD_MIN_SCORE = 0.7

_model = None
_model_lock = threading.Lock()
_bank = None
_bank_lock = threading.Lock()


def _load_model():
    global _model
    with _model_lock:
        if _model is None:
            from model2vec import StaticModel
            _model = StaticModel.from_pretrained(MODEL)
    return _model


def embed(texts):
    """Unit-length sentence vectors, one row per text."""
    vectors = _load_model().encode(list(texts))
    return vectors / np.maximum(np.linalg.norm(vectors, axis=1, keepdims=True), 1e-9)


class Router:
    """Nearest-example voting over labeled utterances."""

    def __init__(self, texts, labels, vectors=None):
        self.labels = list(labels)
        self.vectors = embed(texts) if vectors is None else vectors

    def route(self, text=None, vector=None, min_score=MIN_SCORE):
        """(label, score): the label the nearest examples vote for, weighted by
        similarity, and its best similarity; label None below min_score."""
        if not self.labels:
            return None, 0.0
        v = embed([text])[0] if vector is None else vector
        sims = self.vectors @ v
        votes, best = {}, {}
        for i in np.argsort(-sims)[:NEIGHBOURS]:
            label, sim = self.labels[i], float(sims[i])
            votes[label] = votes.get(label, 0.0) + sim
            best[label] = max(best.get(label, -1.0), sim)
        label = max(votes, key=votes.get)
        return (label if best[label] >= min_score else None), best[label]


def _confident_labels(path=GOLD):
    rows = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if row.get("route") and not row.get("unsure"):
                    rows.append(row)
    except OSError:
        pass
    return rows


def _routers():
    """The route and dialogue-act routers built from the labels, once."""
    global _bank
    with _bank_lock:
        if _bank is None:
            rows = _confident_labels()
            texts = [r["text"] for r in rows]
            vectors = embed(texts) if rows else None
            _bank = {"route": Router(texts, [r["route"] for r in rows], vectors),
                     "act": Router(texts, [r.get("act") for r in rows], vectors)} if rows else {}
    return _bank


def warm():
    """Load the model and the examples in the background, at server start."""
    if MODE != "off":
        threading.Thread(target=_routers, daemon=True, name="semantic-warm").start()


def pick(text):
    """(route, score) of the nearest examples, or (None, 0.0) while the model
    is not loaded - never loads it on the caller's thread."""
    if MODE == "off" or not _bank or not (text or "").strip():
        return None, 0.0
    return _bank["route"].route(text, min_score=0.0)


def lead(text):
    """The trusted route for `text` when leading is on and the pick is
    confident, else None. Never loads the model on the caller's thread: until
    warm() or a logged turn has loaded it, nothing leads."""
    if MODE != "lead" or not _bank or not (text or "").strip():
        return None
    route, score = _bank["route"].route(text, min_score=LEAD_MIN_SCORE)
    return route if route in LEAD_ROUTES else None


def shadow(text):
    """{"route", "act", "score"} for the shadow log, or None when off, with
    no labels, or on an empty command. Loads the model on first use - call it
    off the reply path."""
    if MODE == "off" or not (text or "").strip():
        return None
    bank = _routers()
    if not bank:
        return None
    vector = embed([text])[0]
    route, score = bank["route"].route(vector=vector)
    act, _ = bank["act"].route(vector=vector, min_score=0.0)
    return {"route": route, "act": act, "score": round(score, 3)}
