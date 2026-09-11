"""Phase 4: the understand-first router in shadow mode. The model is faked;
logs live in tmp_path. Nothing it decides is executed."""

import json

import pytest

import app_abilities as aa
import dialogue_state as ds
import intent_router as ir

RULE = {"rule_id": "search_movie", "source_phrase": "search movie",
        "summary": "search hollymoviehd.cc in Brave"}
APPS = {
    "brave": {"name": "brave", "app_kind": "browser", "compiled_rules": [RULE],
              "abilities": aa.build_abilities("brave", {}, "browser")},
    "spotify": {"name": "spotify", "app_kind": "media",
                "abilities": aa.build_abilities("spotify", {}, "media")},
    "winword": {"name": "winword", "app_kind": "office",
                "abilities": aa.build_abilities("winword", {}, "office")},
    "asusx": {"name": "asusx", "app_kind": "noise", "hidden": True, "abilities": []},
}


@pytest.fixture(autouse=True)
def logs(tmp_path, monkeypatch):
    monkeypatch.setattr(ir, "SHADOW_LOG", str(tmp_path / "shadow.jsonl"))
    monkeypatch.setattr(ir, "LABELS_LOG", str(tmp_path / "labels.jsonl"))
    monkeypatch.setattr(ir, "AUDIT_LOG", str(tmp_path / "audit.jsonl"))
    (tmp_path / "audit.jsonl").write_text("", encoding="utf-8")
    ds.reset()
    return tmp_path


def _llm(reply, seen=None):
    def llm(prompt):
        if seen is not None:
            seen.append(prompt)
        return dict(reply), "fake-model"
    return llm


# ------------------------------------------------------------ shortlist --

def test_shortlist_uses_names_state_rules_and_kind_hints():
    assert ir.shortlist("pause spotify", {}, APPS)[0] == "spotify"
    assert "winword" in ir.shortlist("open word", {}, APPS)                 # display name
    assert ir.shortlist("search movie dune", {}, APPS)[0] == "brave"         # rule phrase
    assert "brave" in ir.shortlist("play it", {"last_result": {"app": "brave"}}, APPS)
    assert "asusx" not in ir.shortlist("open asusx", {}, APPS)              # hidden noise


def test_catalog_leaves_out_switched_off_and_never_abilities():
    apps = json.loads(json.dumps(APPS))
    apps["spotify"]["abilities"][3]["enabled"] = False                       # play_pause
    text = ir.catalog(["spotify", "brave"], apps)
    assert "spotify.play_pause" not in text and "spotify.next" in text
    assert "rule search_movie: when the user says \"search movie\"" in text


# ------------------------------------------------------------- decisions --

def test_validate_rejects_anything_not_in_the_shortlist():
    keys = ["spotify"]
    ok = ir.validate({"type": "ability", "app": "spotify", "id": "spotify.search",
                      "args": {"query": "lofi", "evil": "x"}}, keys, APPS)
    assert ok["type"] == "ability" and ok["args"] == {"query": "lofi"} and not ok["unsafe"]
    assert ir.validate({"type": "ability", "app": "spotify", "id": "spotify.format_disk"},
                       keys, APPS)["type"] == "invalid"
    assert ir.validate({"type": "rule", "app": "brave", "id": "search_movie"},
                       keys, APPS)["type"] == "invalid"                     # brave not shortlisted
    assert ir.validate({"type": "launch_missiles"}, keys, APPS)["type"] == "invalid"


def test_never_level_abilities_are_flagged_unsafe():
    apps = json.loads(json.dumps(APPS))
    apps["spotify"]["abilities"][4]["level"] = "never"                       # next
    d = ir.validate({"type": "ability", "app": "spotify", "id": "spotify.next"}, ["spotify"], apps)
    assert d["unsafe"] is True


def test_decide_sends_state_and_catalog_to_the_model():
    seen = []
    reply = {"decision": {"type": "rule", "app": "brave", "id": "search_movie", "args": {}},
             "intent": {"action": "search", "target": "Dune"}, "confidence": 0.9, "reason": "rule fits"}
    decision, intent, conf, reason, model = ir.decide(
        "search movie Dune", {"open_question": None}, APPS, llm=_llm(reply, seen))
    assert decision == {"type": "rule", "app": "brave", "id": "search_movie", "args": {}}
    assert conf == 0.9 and model == "fake-model"
    assert "rule search_movie" in seen[0] and "CONVERSATION NOW" in seen[0]


# --------------------------------------------------- what JARVIS actually did --

def test_actual_route_reads_this_turns_audit_entries():
    assert ir.actual_route([{"tool": "rules.action", "args": json.dumps(
        {"rule": "search_movie", "owner": "brave"})}]) == \
        {"type": "rule", "app": "brave", "id": "search_movie"}
    assert ir.actual_route([{"caller": "execute_tool:open_application",
                             "task": json.dumps({"app": "Spotify"})}]) == \
        {"type": "ability", "app": "spotify", "id": "open"}
    assert ir.actual_route([], backend="hermes")["type"] == "delegate"
    assert ir.actual_route([], backend="instant", stats={"intent": "time"}) == \
        {"type": "chat", "app": None, "id": "time"}


def test_agreement_rules():
    assert ir.agree({"type": "rule", "id": "search_movie"}, {"type": "rule", "id": "search_movie"})
    assert not ir.agree({"type": "rule", "id": "other"}, {"type": "rule", "id": "search_movie"})
    assert ir.agree({"type": "ability", "app": "spotify", "id": "spotify.open"},
                    {"type": "ability", "app": "spotify", "id": "open"})
    assert not ir.agree({"type": "ability", "app": "brave", "id": "brave.open"},
                        {"type": "ability", "app": "spotify", "id": "open"})
    assert ir.agree({"type": "chat"}, {"type": "chat", "id": "time"})
    # a rule named like the action is still not the same decision as the ability
    assert not ir.agree({"type": "rule", "app": "chrome", "id": "open_my_facebook"},
                        {"type": "ability", "app": "chrome", "id": "open"})
    assert not ir.agree({"type": "invalid"}, {"type": "chat"})


# ------------------------------------------------------------ the turn log --

def test_a_turn_is_logged_in_shadow_and_never_executed(logs, monkeypatch):
    monkeypatch.setattr(aa, "load_apps", lambda: {"apps": APPS})
    monkeypatch.setattr(aa, "run_ability",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("executed")))
    monkeypatch.setattr(ds, "active_app", lambda: None)
    started = ir.turn_started("search movie Dune")
    (logs / "audit.jsonl").write_text(json.dumps(
        {"tool": "rules.action", "args": json.dumps({"rule": "search_movie", "owner": "brave"})}) + "\n",
        encoding="utf-8")
    reply = {"decision": {"type": "rule", "app": "brave", "id": "search_movie"}, "confidence": 0.8}
    record = ir.turn_finished(started, "Searching hollymoviehd.cc for Dune in Brave.",
                              backend="instant", llm=_llm(reply), background=False)
    assert record["agree"] is True and record["actual"]["id"] == "search_movie"
    assert ds.recent_turns()[-1]["user"] == "search movie Dune"
    assert json.loads((logs / "shadow.jsonl").read_text(encoding="utf-8"))["id"] == record["id"]


def test_review_labels_and_readiness(logs):
    rows = [{"id": "a", "agree": True, "decision": {}}, {"id": "b", "agree": False, "decision": {}},
            {"id": "c", "agree": False, "decision": {"unsafe": True}}]
    (logs / "shadow.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    assert [r["id"] for r in ir.review()] == ["c", "b"]
    ir.label("b", "new")
    ir.label("b", "old")                                   # the latest verdict counts
    with pytest.raises(ValueError):
        ir.label("c", "maybe")
    s = ir.stats()
    assert s["turns"] == 3 and s["agreed"] == 1 and s["old_right"] == 1
    assert s["unsafe"] == 1 and s["ready"] is False
    assert ir.review()[1]["verdict"] == "old"
