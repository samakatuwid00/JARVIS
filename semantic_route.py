"""semantic_route.py - route a command by what it means, not its keywords.

Step 3 of the semantic-routing plan, log-only. Every confident label in
eval/routing_gold.jsonl is an example; a new command takes the route its
nearest examples vote for, compared as model2vec sentence embeddings
(potion-multilingual-128M: ~0.1 ms a sentence on CPU, reads Tagalog). Below
MIN_SCORE it abstains and the keyword router keeps the turn.

JARVIS_SEMANTIC=shadow logs its pick next to each turn (intent_router's
shadow log); "off" skips it and never loads the model (~0.5 GB of RAM).
"""

import json
import os
import threading

import numpy as np

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
GOLD = os.path.join(BASE_DIR, "eval", "routing_gold.jsonl")
MODEL = os.getenv("JARVIS_SEMANTIC_MODEL", "minishlab/potion-multilingual-128M")
MODE = os.getenv("JARVIS_SEMANTIC", "shadow").strip().lower()
NEIGHBOURS = 5
MIN_SCORE = float(os.getenv("JARVIS_SEMANTIC_MIN", "0.6"))

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
