"""routing_eval: the keyword router is replayed offline, live turns map onto
the same route classes, and the score counts questions that were acted on."""

import json

import pytest

import routing_eval as re_


@pytest.mark.parametrize("text, route", [
    ("what time is it", "time"),
    ("yes", "confirm"),
    ("close notepad", "close_app"),
    ("What coding agent did you use to create this website?", "chat"),
    ("what sites do you know?", "list_sites"),
    ("what's the status of the task", "job_status"),
    ("create a website then open it", "task")])
def test_keyword_route_replays_the_local_order(text, route):
    assert re_.keyword_route(text) == route


def test_an_open_goes_where_the_registry_resolves(monkeypatch):
    import tools
    monkeypatch.setattr(tools, "resolve_open_target",
                        lambda text, force=None, browser=None:
                        {"notepad": ("app", "notepad"), "youtube": ("site", "youtube")}
                        .get(text, (None, text)))
    assert re_.keyword_route("open notepad") == "open_app"
    assert re_.keyword_route("jarvis open youtube") == "open_site"


@pytest.mark.parametrize("actual, route", [
    ({"type": "web_search"}, "web_search"),
    ({"type": "delegate", "id": "hermes"}, "task"),
    ({"type": "ability", "app": "notepad", "id": "open"}, "open_app"),
    ({"type": "ability", "app": "spotify", "id": "spotify.volume_down"}, "media_control"),
    ({"type": "ability", "app": "brave", "id": "brave.new_tab"}, "app_action"),
    ({"type": "ability", "app": "media", "id": "play_music"}, "music"),
    ({"type": "rule", "app": "brave", "id": "search_movie"}, "rule"),
    ({"type": "chat", "id": "list_sites"}, "list_sites"),
    ({"type": "chat", "id": "decline"}, "decline"),
    ({"type": "chat", "id": "general"}, "chat")])
def test_live_turns_map_onto_route_classes(actual, route):
    assert re_.live_route({"actual": actual}) == route


def test_score_counts_rules_as_right_and_flags_questions_acted_on():
    gold = [{"text": "search movie dune", "route": "web_search", "act": "command"},
            {"text": "which agent built it?", "route": "chat", "act": "about_own_work"},
            {"text": "what is ai", "route": "chat", "act": "question"}]
    s = re_.score([(gold[0], "rule"), (gold[1], "task"), (gold[2], "chat")])
    assert (s["n"], s["hits"]) == (3, 2)
    assert s["per_route"] == {"web_search": (1, 1), "chat": (2, 1)}
    assert s["acted_on"] == [("which agent built it?", "task")]
    assert s["confusion"][("chat", "task")] == 1


def test_live_scoring_uses_only_labeled_shadow_turns():
    gold = [{"text": "close spotify", "route": "close_app", "shadow_ids": ["a1"]}]
    shadow = [{"id": "a1", "actual": {"type": "ability", "app": "spotify", "id": "close"}},
              {"id": "zz", "actual": {"type": "chat", "id": "general"}}]
    s = re_.score_live(gold, shadow)
    assert (s["n"], s["hits"]) == (1, 1)


def test_reviewed_filter_and_main(tmp_path, capsys):
    path = tmp_path / "gold.jsonl"
    rows = [{"text": "what time is it", "route": "time", "act": "question", "reviewed": True},
            {"text": "hello jarvis", "route": "greeting", "act": "statement", "reviewed": False}]
    path.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
    assert len(re_.load_gold(str(path))) == 2
    assert len(re_.load_gold(str(path), reviewed_only=True)) == 1
    assert re_.main(["--gold", str(path), "--misses", "0"]) == 0
    out = capsys.readouterr().out
    assert "2 labeled utterances (1 reviewed" in out and "2/2 right" in out


def test_delegate_backends_are_replayed(monkeypatch):
    import tools
    monkeypatch.setattr(tools, "resolve_open_target", lambda text, force=None, browser=None: (None, text))
    monkeypatch.setattr(tools, "_detect_backend", lambda t: "desktop")
    assert re_.keyword_route("Can you open Spotify?") == "open_app"
    monkeypatch.setattr(tools, "_detect_backend", lambda t: "music")
    assert re_.keyword_route("Play, columns cut, dancing on my own, Jarvis.") == "music"


def test_a_spoken_answer_counts_for_instant_routes():
    assert re_.hit("chat", "math") and re_.hit("chat", "greeting")
    assert not re_.hit("chat", "time") and not re_.hit("chat", "clarify")


@pytest.mark.parametrize("entries, actual", [
    ([{"caller": "execute_tool:play_spotify", "task": "{}"}], {"type": "ability", "app": "media",
                                                                "id": "play_spotify"}),
    ([{"caller": "delegate_to_hermes", "task": "x"}], {"type": "delegate", "app": None,
                                                       "id": "delegate_to_hermes"}),
    ([{"caller": "execute_tool:web_browse", "task": "{}"}], {"type": "ability", "app": None,
                                                             "id": "web_browse"})])
def test_actual_route_knows_the_tools_it_missed(entries, actual):
    import intent_router
    assert intent_router.actual_route(entries) == actual


def test_old_turns_are_re_read_from_the_audit_trail():
    records = [{"id": "a", "ts": "2026-09-12 10:00:05", "agree": False,
                "actual": {"type": "chat", "id": None},
                "decision": {"type": "ability", "app": "spotify", "id": "spotify.play"}},
               {"id": "b", "ts": "2026-09-12 10:01:00", "agree": False,
                "actual": {"type": "delegate", "id": "hermes"}, "decision": {"type": "chat"}},
               {"id": "c", "ts": "2026-09-12 10:01:30", "agree": True,
                "actual": {"type": "chat", "id": None}, "decision": {"type": "chat"}}]
    audit = [{"ts": "2026-09-12T10:00:03.100", "caller": "execute_tool:play_spotify", "task": "{}"},
             {"ts": "2026-09-12T10:00:40.000", "caller": "execute_tool:get_memory_context"},
             {"ts": "2026-09-12T10:01:10.000", "caller": "execute_tool:open_application",
              "task": "{\"app\": \"notepad\"}"}]
    rebuilt = re_.rebuild_actual(records, audit)
    assert rebuilt[0]["actual"]["id"] == "play_spotify"
    assert rebuilt[1]["actual"] == {"type": "delegate", "id": "hermes"}   # kept as logged
    assert rebuilt[2]["actual"] == {"type": "chat", "id": None}           # a neighbour's open is not re-read
    assert re_.live_route(rebuilt[0]) == "music"
    assert [r["id"] for r in re_.false_disagreements(records, rebuilt)] == []


def test_live_semantic_picks_are_scored_and_differences_listed():
    gold = [{"text": "what is my name", "route": "recall_memory", "act": "question",
             "shadow_ids": ["a"]}]
    shadow = [{"id": "a", "user": "what is my name", "actual": {"type": "chat", "id": None},
               "semantic": {"route": "recall_memory", "score": 0.93}},
              {"id": "b", "user": "open github", "actual": {"type": "ability", "id": "open_site"},
               "semantic": {"route": "open_site", "score": 0.75}},
              {"id": "c", "user": "what is cygnus", "actual": {"type": "chat", "id": None},
               "semantic": {"route": None, "score": 0.35}},
              {"id": "d", "user": "no pick logged", "actual": {"type": "chat", "id": None}}]
    s = re_.score_live_semantic(gold, shadow)
    assert (s["turns"], s["labeled"]) == (3, 1)
    assert s["semantic"]["hits"] == 1 and s["live"]["hits"] == 0
    assert [d[0] for d in s["differ"]] == ["what is my name", "what is cygnus"]
    out = re_.report_live_semantic(s)
    assert "semantic recall_memory (0.93) / JARVIS chat" in out
