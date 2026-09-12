"""Step 3: the semantic router votes among its nearest labeled examples,
abstains below its minimum score, logs in shadow only, and is scored by
cross-validation that keeps near-duplicates in one fold. Embeddings are faked
so no test loads the real model."""

import numpy as np
import pytest

import routing_eval as re_
import semantic_route as sr

# Three directions of meaning; each text is a unit vector near one of them.
_AXES = {"music": [1, 0, 0], "close": [0, 1, 0], "chat": [0, 0, 1]}
_TEXTS = {"play a song": ("music", 0.0), "put on some music": ("music", 0.1),
          "play harry styles": ("music", 0.05), "close notepad": ("close", 0.0),
          "shut spotify": ("close", 0.1), "what is ai": ("chat", 0.0),
          "who are you": ("chat", 0.1), "tell me a joke": ("chat", 0.05),
          "blorp": (None, 0.0)}


def _fake_embed(texts):
    rows = []
    for t in texts:
        axis, tilt = _TEXTS[t]
        v = np.array(_AXES[axis] if axis else [1, 1, 1], dtype=float) + tilt
        rows.append(v / np.linalg.norm(v))
    return np.array(rows)


@pytest.fixture(autouse=True)
def fake_model(monkeypatch):
    monkeypatch.setattr(sr, "embed", _fake_embed)
    monkeypatch.setattr(sr, "_bank", None)


def test_nearest_examples_vote_and_weak_matches_abstain():
    router = sr.Router(["play a song", "put on some music", "close notepad", "what is ai"],
                       ["music", "music", "close_app", "chat"])
    assert router.route("play harry styles")[0] == "music"
    assert router.route("shut spotify")[0] == "close_app"
    label, score = router.route("blorp", min_score=0.9)
    assert label is None and score < 0.9
    assert sr.Router([], []).route(vector=np.zeros(3)) == (None, 0.0)


def test_shadow_is_off_by_default_in_tests_and_logs_when_on(monkeypatch):
    assert sr.shadow("play a song") is None
    monkeypatch.setattr(sr, "MODE", "shadow")
    monkeypatch.setattr(sr, "_confident_labels", lambda path=sr.GOLD: [
        {"text": "play a song", "route": "music", "act": "command"},
        {"text": "put on some music", "route": "music", "act": "command"},
        {"text": "what is ai", "route": "chat", "act": "question"}])
    out = sr.shadow("play harry styles")
    assert out["route"] == "music" and out["act"] == "command" and out["score"] > 0.9
    assert sr.shadow("   ") is None


def test_shadow_without_labels_stays_quiet(monkeypatch):
    monkeypatch.setattr(sr, "MODE", "shadow")
    monkeypatch.setattr(sr, "_confident_labels", lambda path=sr.GOLD: [])
    assert sr.shadow("play a song") is None


def test_near_duplicates_share_a_fold():
    vectors = _fake_embed(["play a song", "put on some music", "close notepad"])
    groups = re_._groups(vectors)
    assert groups[0] == groups[1] != groups[2]


def test_cross_validation_and_the_hybrid_table(monkeypatch):
    gold = [{"id": str(i), "text": t, "route": r, "act": "command"} for i, (t, r) in enumerate([
        ("play a song", "music"), ("put on some music", "music"), ("play harry styles", "music"),
        ("close notepad", "close_app"), ("shut spotify", "close_app"),
        ("what is ai", "chat"), ("who are you", "chat"), ("tell me a joke", "chat")])]
    cv = re_.cross_validate(gold)
    assert [row["id"] for row, _, _ in cv] == [g["id"] for g in gold]
    keyword = {g["id"]: "task" for g in gold}           # a keyword router that is always wrong
    table = re_.semantic_table(cv, keyword)
    t0, taken, right, hybrid = table[0]
    assert t0 == 0.0 and taken == len(gold)
    assert hybrid["hits"] == right                      # at 0 the semantic router takes everything
    assert table[-1][3]["hits"] <= hybrid["hits"]      # a higher minimum hands more to keywords


def test_a_turn_logs_the_semantic_pick(monkeypatch, tmp_path):
    import intent_router as ir
    monkeypatch.setattr(ir, "SHADOW_LOG", str(tmp_path / "shadow.jsonl"))
    monkeypatch.setattr(ir, "AUDIT_LOG", str(tmp_path / "audit.jsonl"))
    (tmp_path / "audit.jsonl").write_text("", encoding="utf-8")
    monkeypatch.setattr(sr, "shadow", lambda text: {"route": "music", "act": "command", "score": 0.93})
    started = ir.turn_started("play a song")
    rec = ir.turn_finished(started, "Playing.", backend="instant", background=False,
                           decided={"decision": {"type": "chat"}, "mode": "shadow"})
    assert rec["semantic"] == {"route": "music", "act": "command", "score": 0.93}
