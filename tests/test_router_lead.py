"""Phase 5: the understand-first router leads. The model is faked; abilities
and rules are stubbed so nothing runs on the machine; logs live in tmp_path."""

import json

import pytest

import app_abilities as aa
import dialogue_state as ds
import intent_router as ir

RULE = {"rule_id": "search_movie", "source_phrase": "search movie", "enforcement": "action",
        "action": {"type": "search_site", "site": "hollymoviehd.cc", "url": "https://hollymoviehd.cc",
                   "search_url": "https://hollymoviehd.cc?s={query}", "browser": "brave"}}
APPS = {
    "brave": {"name": "brave", "app_kind": "browser", "category": "browser", "compiled_rules": [RULE],
              "abilities": aa.build_abilities("brave", {}, "browser")},
    "spotify": {"name": "spotify", "app_kind": "media",
                "abilities": aa.build_abilities("spotify", {}, "media")},
}


@pytest.fixture(autouse=True)
def logs(tmp_path, monkeypatch):
    monkeypatch.setattr(ir, "SHADOW_LOG", str(tmp_path / "shadow.jsonl"))
    monkeypatch.setattr(ir, "LABELS_LOG", str(tmp_path / "labels.jsonl"))
    monkeypatch.setattr(ir, "AUDIT_LOG", str(tmp_path / "audit.jsonl"))
    (tmp_path / "audit.jsonl").write_text("", encoding="utf-8")
    monkeypatch.setattr(ir, "LEAD_CONFIDENCE", 0.85)
    ds.reset()
    return tmp_path


def _llm(kind, app, id, confidence=0.95, args=None, question=None):
    reply = {"intent": {"action": "x", "target": "y"}, "confidence": confidence, "reason": "r",
             "decision": {"type": kind, "app": app, "id": id, "args": args or {}, "question": question}}
    return lambda prompt: (dict(reply), "fake-model")


def _runs(monkeypatch):
    ran = []
    monkeypatch.setattr(aa, "run_ability",
                        lambda key, aid, args=None: ran.append((key, aid, dict(args or {})))
                        or "Done: Volume down.")
    return ran


def test_a_confident_safe_ability_runs(monkeypatch):
    ran = _runs(monkeypatch)
    reply, rec = ir.route("turn the spotify volume down", {}, APPS,
                          llm=_llm("ability", "spotify", "spotify.volume_down"))
    assert reply == "Volume down, sir (Spotify)."
    assert ran == [("spotify", "spotify.volume_down", {})]
    assert rec["executed"] is True and rec["mode"] == "lead"


def test_low_confidence_falls_through(monkeypatch):
    ran = _runs(monkeypatch)
    reply, rec = ir.route("play my favorite", {}, APPS,
                          llm=_llm("ability", "brave", "brave.search_web", confidence=0.7,
                                   args={"query": "x"}))
    assert reply is None and ran == [] and rec["why_not"] == "low confidence"


def test_an_unknown_or_never_ability_never_runs(monkeypatch):
    ran = _runs(monkeypatch)
    assert ir.route("x", {}, APPS, llm=_llm("ability", "spotify", "spotify.nope"))[0] is None
    apps = {"cmd": {"name": "cmd", "app_kind": "terminal",
                    "abilities": aa.build_abilities("cmd", {}, "terminal")}}
    reply, rec = ir.route("run dir in cmd", {}, apps,
                          llm=_llm("ability", "cmd", "cmd.run_command", args={"command": "dir"}))
    assert reply is None and rec["why_not"] == "unsafe" and ran == []


def test_an_ask_ability_asks_first_then_yes_runs_it(monkeypatch):
    ran = _runs(monkeypatch)
    reply, rec = ir.route("play the video", {}, APPS, llm=_llm("ability", "brave", "brave.play_page"))
    assert reply == "Should I play the video on the page in Brave, sir?"
    assert ran == [] and rec["asked"] is True
    assert ds.pending("ability")["payload"]["id"] == "brave.play_page"
    assert ir.answer_pending_ability("what time is it") is None      # not an answer
    monkeypatch.setattr(aa, "load_apps", lambda: {"apps": APPS})
    assert ir.answer_pending_ability("yes").startswith("Volume down, sir")
    assert ran == [("brave", "brave.play_page", {})] and ds.pending("ability") is None


def test_no_drops_the_asked_ability(monkeypatch):
    ran = _runs(monkeypatch)
    ir.route("play the video", {}, APPS, llm=_llm("ability", "brave", "brave.play_page"))
    assert ir.answer_pending_ability("no thanks") == "Okay, sir. I won't."
    assert ran == [] and ds.pending("ability") is None


def test_a_missing_detail_is_asked_for(monkeypatch):
    monkeypatch.setattr(aa, "run_ability",
                        lambda key, aid, args=None: "[Error] \"Search Spotify\" needs a query.")
    reply, rec = ir.route("search on spotify", {}, APPS, llm=_llm("ability", "spotify", "spotify.search"))
    assert reply == "\"Search Spotify\" needs a query, sir. Which?" and rec["executed"]


def test_a_confident_rule_runs_with_the_named_detail(monkeypatch):
    import tools
    ran = []
    monkeypatch.setattr(tools, "run_rule_action",
                        lambda rule, owner, action, slot="", entry=None:
                        ran.append((owner, rule["rule_id"], slot)) or "Searching hollymoviehd.cc for Dune in Brave.")
    reply, rec = ir.route("find me Dune on my movie site", {}, APPS,
                          llm=_llm("rule", "brave", "search_movie", args={"movie": "Dune"}))
    assert reply.startswith("Searching hollymoviehd.cc for Dune") and ran == [("brave", "search_movie", "Dune")]


def test_chat_and_web_search_fall_through(monkeypatch):
    ran = _runs(monkeypatch)
    assert ir.route("who am I", {}, APPS, llm=_llm("chat", None, None))[0] is None
    assert ir.route("cats", {}, APPS, llm=_llm("web_search", None, None))[0] is None
    assert ran == []


def test_worth_asking_skips_chat_with_no_app_in_it():
    assert ir.worth_asking("what is a factory pattern?", {}, APPS) is False
    assert ir.worth_asking("turn the spotify volume down", {}, APPS) is True
    assert ir.worth_asking("yes", {"open_question": "Which one?"}, APPS) is True


def test_a_led_turn_is_logged_once_without_a_second_model_call(logs, monkeypatch):
    _runs(monkeypatch)
    started = ir.turn_started("turn the spotify volume down")
    reply, rec = ir.route("turn the spotify volume down", started["snapshot"], APPS,
                          llm=_llm("ability", "spotify", "spotify.volume_down"))

    def no_llm(prompt):
        raise AssertionError("decided twice")
    ir.turn_finished(started, reply, backend="router-lead", llm=no_llm, background=False, decided=rec)
    record = json.loads((logs / "shadow.jsonl").read_text(encoding="utf-8"))
    assert record["mode"] == "lead" and record["executed"] and record["agree"] is True
    assert ir.stats()["led"] == 1 and ir.stats()["mode"] == ir.MODE


def test_registry_opens_stay_on_the_fast_lane(monkeypatch):
    import brain_gemini
    import tools
    monkeypatch.setattr(tools, "resolve_open_target",
                        lambda text, force=None, browser=None:
                        ("app", "notepad") if text == "notepad" else (None, text))
    assert brain_gemini._fast_lane_opens("open notepad") is True
    assert brain_gemini._fast_lane_opens("jarvis open new tab on brave browser") is False
    assert brain_gemini._fast_lane_opens("turn the spotify volume down") is False


def test_the_mode_switch_reads_the_environment(monkeypatch):
    import importlib
    monkeypatch.setenv("JARVIS_ROUTER", "shadow")
    mod = importlib.reload(ir)
    assert mod.MODE == "shadow" and mod.ENABLED
    monkeypatch.setenv("JARVIS_ROUTER", "off")
    assert importlib.reload(ir).MODE == "off" and not ir.ENABLED
    monkeypatch.delenv("JARVIS_ROUTER")
    importlib.reload(ir)
