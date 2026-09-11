"""The spoken side of learned defaults and the conversation state: three
identical searches lead to one question, and the answer is honoured."""

import json

import pytest

import dialogue_state
import preferences

RULE = {"rule_id": "search_movie", "source_phrase": "search movie", "enforcement": "action",
        "triggers": ["search movie"],
        "action": {"type": "search_site", "site": "hollymoviehd.cc", "url": "https://hollymoviehd.cc",
                   "search_url": "https://hollymoviehd.cc?s={query}", "browser": "brave",
                   "slot": "movie name"}}


@pytest.fixture
def spoken(tmp_path, monkeypatch):
    import audit
    import rules_engine
    import tools
    path = tmp_path / "app_registry.json"
    path.write_text(json.dumps({"apps": {"brave": {"category": "browser",
                                                   "compiled_rules": [RULE]}}}))
    monkeypatch.setattr(rules_engine, "APP_REGISTRY_PATH", str(path))
    monkeypatch.setattr(audit, "log_call", lambda *a, **k: None)
    monkeypatch.setattr(tools, "_open_url_in_browser",
                        lambda url, name, browser=None, remember=True: f"Opened {name}.")
    return tools


def test_third_identical_search_asks_and_yes_saves(spoken):
    tools = spoken
    for name in ("Dune", "Arrival"):
        assert tools.try_rule_action(f"search movie {name}") == \
            f"Searching hollymoviehd.cc for {name} in Brave."
    out = tools.try_rule_action("search movie Heat")
    assert out.endswith("You always use hollymoviehd.cc in Brave for movie search. "
                        "Make that the default?")
    assert dialogue_state.pending("preference")["payload"] == {"topic": "movie search"}
    assert tools.try_rule_action("yes") == \
        "Saved. Movie search now defaults to hollymoviehd.cc in Brave."
    assert preferences.default("movie search") == {"site": "hollymoviehd.cc", "browser": "brave"}
    assert dialogue_state.pending() is None


def test_no_stops_asking(spoken):
    tools = spoken
    for name in ("Dune", "Arrival", "Heat"):
        tools.try_rule_action(f"search movie {name}")
    assert tools.try_rule_action("no") == "Okay, I won't ask about that again."
    assert preferences.default("movie search") is None
    assert "default" not in tools.try_rule_action("search movie Up").lower()


def test_a_command_with_yes_in_it_is_still_a_command(spoken):
    tools = spoken
    for name in ("Dune", "Arrival", "Heat"):
        tools.try_rule_action(f"search movie {name}")
    out = tools.try_rule_action("yes, search movie Godzilla")
    assert out.startswith("Searching hollymoviehd.cc for Godzilla in Brave.")
    assert preferences.default("movie search") is None           # not taken as a yes
    assert dialogue_state.pending("preference") is not None       # still asking
    out = tools.try_rule_action("no, search movie Up")
    assert out.startswith("Searching hollymoviehd.cc for Up in Brave.")
    assert tools.try_rule_action("yes please") == \
        "Saved. Movie search now defaults to hollymoviehd.cc in Brave."


def test_the_name_question_is_in_the_conversation_state(spoken):
    tools = spoken
    assert tools.try_rule_action("search a movie") == "Which movie, sir?"
    assert dialogue_state.pending("slot")["question"] == "Which movie, sir?"
    tools.try_rule_action("Dune")
    assert dialogue_state.last_result("results")["slot"] == "Dune"
