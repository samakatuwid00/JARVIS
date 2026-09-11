"""Action rules: AI compile (rules_ai), runtime matching (rules_engine),
voice setup (rules_voice), and the spoken path (tools.try_rule_action).

The model is always faked; registries and the audit log live in tmp_path.
"""

import json

import pytest

import rules_ai
import rules_compiler as rc
import rules_engine
import rules_voice

LEGACY_BRAVE_RULE = {
    "rule_id": "search_a_movie_in_brave",
    "intent": "it will search a movie on hollymoviehd.cc based on the given name",
    "enforcement": "soft",
    "adapter_check": "",
    "scope": "all_sessions",
    "source_phrase": "search a movie in brave",
    "needs_clarification": False,
    "clarification_question": None,
}


def _apps(rules, **entry):
    return {"brave": dict({"category": "browser", "compiled_rules": rules}, **entry)}


def _fake_llm(reply, seen=None):
    def llm(prompt):
        if seen is not None:
            seen.append(prompt)
        return (dict(reply), "fake-model") if reply is not None else (None, "down")
    return llm


# ------------------------------------------------------------ runtime match --

@pytest.mark.parametrize("spoken, slot", [
    ("search movies Dune in brave browser", "Dune"),
    ("Search a movie in Brave.", ""),
    ("search movies in brave browser, Jarvis.", ""),
    ("search a movie named The Lord of the Rings in brave", "Lord of the Rings"),
    ("search dune movie in brave", "dune"),
    ("search a movie Dune on brave", "Dune"),
])
def test_legacy_rule_matches_spoken_variants(spoken, slot):
    m = rules_engine.match_action(spoken, apps=_apps([LEGACY_BRAVE_RULE]))
    assert m is not None
    assert m["slot"] == slot
    assert m["action"]["site"] == "hollymoviehd.cc"
    assert m["action"]["browser"] == "brave"


@pytest.mark.parametrize("spoken", [
    "search dune on google",
    "search, e-movie, hungry, Jarvis.",
    "open brave",
    "play a movie in brave",
])
def test_unrelated_commands_do_not_match(spoken):
    assert rules_engine.match_action(spoken, apps=_apps([LEGACY_BRAVE_RULE])) is None


def test_name_containing_trigger_word_stays_whole():
    rule = dict(LEGACY_BRAVE_RULE, source_phrase="search movie in brave")
    m = rules_engine.match_action("search movie coming in hot in brave",
                                  apps=_apps([rule]))
    assert m["slot"] == "coming in hot"


def test_disabled_app_and_block_rules_never_run():
    assert rules_engine.match_action(
        "search a movie Dune in brave",
        apps=_apps([LEGACY_BRAVE_RULE], enabled=False)) is None
    block = dict(LEGACY_BRAVE_RULE, enforcement="hard",
                 intent="never search hollymoviehd.cc")
    assert rules_engine.match_action("search a movie Dune in brave",
                                     apps=_apps([block])) is None


def test_build_action_url_encodes_the_name():
    action = rules_engine.action_of(LEGACY_BRAVE_RULE, "brave", {"category": "browser"})
    assert rules_engine.build_action_url(action, "The Odyssey") == \
        "https://hollymoviehd.cc/?s=The+Odyssey"
    assert rules_engine.build_action_url(action, "") == "https://hollymoviehd.cc"


# ----------------------------------------------------------------- compile --

def test_compile_keeps_user_site_and_drops_invented_one():
    reply = {"type": "search_site", "trigger": "search movies in brave browser",
             "triggers": ["find a movie in brave", "search brave", "look up movies on brave"],
             "site": "evil.example", "search_url": "https://evil.example/find?q={query}",
             "slot": "movie name", "browser": "brave"}
    rule, notes = rules_ai.compile_rule(
        "brave", "search movies in brave browser",
        "search the movie name on hollymoviehd.cc", entry={"category": "browser"},
        llm=_fake_llm(reply))
    a = rule["action"]
    assert a["site"] == "hollymoviehd.cc"
    assert a["search_url"] == "https://hollymoviehd.cc/?s={query}"
    assert a["search_url_guessed"] is True
    assert a["browser"] == "brave"
    assert rule["enforcement"] == "action"
    assert rule["triggers"][0] == "search movies in brave browser"
    assert "search brave" not in rule["triggers"]  # would capture every search
    assert "find a movie in brave" in rule["triggers"]
    assert any("guessed" in n for n in notes)
    # the gate ignores runnable rules: opening Brave stays silent
    assert rules_engine.check("brave", {"entry": {"compiled_rules": [rule]}}) == \
        {"action": "allow"}


def test_model_search_url_is_used_but_flagged_as_a_guess():
    reply = {"type": "search_site", "site": "hollymoviehd.cc",
             "search_url": "https://hollymoviehd.cc/search/{query}", "slot": "movie name"}
    rule, notes = rules_ai.compile_rule("brave", "search movies in brave",
                                        "search hollymoviehd.cc", llm=_fake_llm(reply))
    assert rule["action"]["search_url"] == "https://hollymoviehd.cc/search/{query}"
    assert rule["action"]["search_url_guessed"] is True
    assert any("Test" in n for n in notes)


def test_search_url_typed_by_user_is_not_a_guess():
    reply = {"type": "search_site", "site": "hollymoviehd.cc",
             "search_url": "https://hollymoviehd.cc/search/{query}", "slot": "movie name"}
    rule, notes = rules_ai.compile_rule(
        "brave", "search movies in brave",
        "open hollymoviehd.cc/search/ with the movie name", llm=_fake_llm(reply))
    assert rule["action"]["search_url_guessed"] is False
    assert not notes


def test_compile_without_model_falls_back_to_keywords():
    rule, notes = rules_ai.compile_rule("brave", "search a movie in brave",
                                        "search it on hollymoviehd.cc",
                                        entry={"category": "browser"}, llm=_fake_llm(None))
    assert rule["compiled_by"] == "keywords"
    assert rule["action"]["type"] == "search_site"
    assert rule["action"]["slot"] == "movie name"
    assert any("unavailable" in n for n in notes)
    # no model and no site: the caller keeps its legacy path
    assert rules_ai.compile_rule("spotify", "keep it quiet", "",
                                 llm=_fake_llm(None))[0] is None


def test_compile_asks_for_a_site_when_none_given():
    reply = {"type": "search_site", "site": None, "slot": "song name"}
    rule, _ = rules_ai.compile_rule("brave", "search songs in brave", "search for songs",
                                    llm=_fake_llm(reply))
    assert rule["needs_clarification"] is True
    assert "website" in rule["clarification_question"]


def test_compile_block_rule_is_scoped_to_its_action():
    reply = {"type": "block", "applies_to": "play"}
    rule, _ = rules_ai.compile_rule("spotify", "never play explicit tracks", "",
                                    llm=_fake_llm(reply))
    assert rule["enforcement"] == "hard"
    assert rule["adapter_check"].startswith("pre_play:")
    # a play-scoped block must not stop Spotify from opening
    assert rules_engine.check("spotify", {"action": "open",
                                          "entry": {"compiled_rules": [rule]}}) == \
        {"action": "allow"}


def test_split_draft():
    assert rules_ai.split_draft("search movies in brave -> search hollymoviehd.cc") == \
        ("search movies in brave", "search hollymoviehd.cc")
    assert rules_ai.split_draft("when I say search movies, search hollymoviehd.cc") == \
        ("search movies", "search hollymoviehd.cc")
    assert rules_ai.split_draft("never play explicit") == ("never play explicit", "")


def test_compile_drafts_keeps_the_site_of_an_older_rule():
    seen = []
    reply = {"type": "search_site", "site": "hollymoviehd.cc", "slot": "movie name"}
    rules, _ = rules_ai.compile_drafts("brave", ["search a movie in brave"],
                                       [LEGACY_BRAVE_RULE], {"category": "browser"},
                                       llm=_fake_llm(reply, seen))
    assert rules[0]["action"]["site"] == "hollymoviehd.cc"
    assert "hollymoviehd.cc" in seen[0]  # the saved action reached the model
    # an already structured rule is kept without another model call
    again, _ = rules_ai.compile_drafts("brave", ["search a movie in brave"], rules,
                                       {"category": "browser"}, llm=_fake_llm(None, seen))
    assert again[0] is rules[0] and len(seen) == 1


# ------------------------------------------------------------------ commit --

def _registry(tmp_path, rules):
    path = tmp_path / "app_registry.json"
    path.write_text(json.dumps({"apps": _apps(rules)}), encoding="utf-8")
    return path


def test_commit_merge_adds_and_default_replaces(tmp_path):
    path = _registry(tmp_path, [LEGACY_BRAVE_RULE])
    new = dict(LEGACY_BRAVE_RULE, rule_id="open_hd", source_phrase="open hd movies")
    rc.commit_rules("brave", [new], path, accept=True, merge=True)
    ids = [r["rule_id"] for r in json.loads(path.read_text())["apps"]["brave"]["compiled_rules"]]
    assert ids == ["search_a_movie_in_brave", "open_hd"]
    rc.commit_rules("brave", [new], path, accept=True)
    ids = [r["rule_id"] for r in json.loads(path.read_text())["apps"]["brave"]["compiled_rules"]]
    assert ids == ["open_hd"]


# ------------------------------------------------------------- voice setup --

def test_voice_setup_asks_for_the_action_then_adds_the_rule(tmp_path, monkeypatch):
    path = _registry(tmp_path, [dict(LEGACY_BRAVE_RULE, rule_id="other_rule",
                                     source_phrase="open hd movies")])
    seen = []
    reply = {"type": "search_site", "site": "hollymoviehd.cc", "slot": "movie name",
             "triggers": ["find movies in brave"]}
    monkeypatch.setattr(rules_ai, "_ask_llm", _fake_llm(reply, seen))

    step = rules_voice.begin_rule_setup("brave", "search movies in brave browser",
                                        registry_path=str(path))
    assert step["status"] == "clarify"
    assert step["question"] == 'What should JARVIS do when you say "search movies in brave browser"?'

    step = rules_voice.handle_clarification("brave", step["rule_id"],
                                            "search the movie name on hollymoviehd.cc",
                                            registry_path=str(path))
    assert step["status"] == "proposal" and step["testable"] is True
    assert "hollymoviehd.cc" in step["summary"]

    step = rules_voice.handle_clarification("brave", "", "use the site's search page",
                                            registry_path=str(path))
    assert step["status"] == "proposal"
    assert any("CORRECTION FROM THE USER" in p for p in seen)

    step = rules_voice.handle_clarification("brave", "", "yes", registry_path=str(path))
    assert step["status"] == "committed" and step["total"] == 2
    saved = json.loads(path.read_text())["apps"]["brave"]["compiled_rules"]
    assert [r["rule_id"] for r in saved] == ["other_rule", "search_movies_in_brave_browser"]


def test_voice_setup_does_not_split_a_command(tmp_path, monkeypatch):
    path = _registry(tmp_path, [])
    monkeypatch.setattr(rules_ai, "_ask_llm", _fake_llm(None))
    step = rules_voice.begin_rule_setup("brave", "search a movie and play it",
                                        registry_path=str(path))
    assert step["status"] == "clarify"
    assert "search a movie and play it" in step["question"]
    rules_voice.cancel_setup("brave")


# -------------------------------------------------------------- spoken path --

@pytest.fixture
def spoken(tmp_path, monkeypatch):
    import audit
    import tools
    path = _registry(tmp_path, [LEGACY_BRAVE_RULE])
    monkeypatch.setattr(rules_engine, "APP_REGISTRY_PATH", str(path))
    monkeypatch.setattr(audit, "AUDIT_DIR", str(tmp_path))
    monkeypatch.setattr(audit, "AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    opened = []
    monkeypatch.setattr(tools, "_open_url_in_browser",
                        lambda url, name, browser=None, remember=True:
                        opened.append((url, browser)) or f"Opened {name}.")
    tools._PENDING_RULE_SLOT.clear()
    yield tools, opened, tmp_path
    tools._PENDING_RULE_SLOT.clear()


def test_spoken_command_with_name_opens_the_rule_site(spoken):
    tools, opened, tmp_path = spoken
    out = tools.try_rule_action("search movies Dune in brave browser, Jarvis.")
    assert opened == [("https://hollymoviehd.cc/?s=Dune", "brave")]
    assert out == "Searching hollymoviehd.cc for Dune in Brave."
    logged = (tmp_path / "audit.jsonl").read_text(encoding="utf-8")
    assert '"rules.action"' in logged


def test_missing_name_is_asked_then_answered(spoken):
    tools, opened, _ = spoken
    assert tools.try_rule_action("Search a movie in Brave.") == "Which movie, sir?"
    assert opened == []
    assert tools.try_rule_action("Dune.") == "Searching hollymoviehd.cc for Dune in Brave."
    assert opened == [("https://hollymoviehd.cc/?s=Dune", "brave")]
    # the question is closed once answered
    assert tools.try_rule_action("Dune") is None


def test_new_command_or_cancel_closes_the_question(spoken):
    tools, opened, _ = spoken
    tools.try_rule_action("search a movie in brave")
    assert tools.try_rule_action("what time is it") is None
    tools.try_rule_action("search a movie in brave")
    assert tools.try_rule_action("never mind") == "Okay, cancelled."
    assert opened == []


def test_search_query_drops_trailing_jarvis():
    import tools
    assert tools.parse_search_command("search movies in brave browser, Jarvis.")["query"] \
        == "movies"
