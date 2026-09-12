"""2026-09-12: "open X" reaches X. GitHub failed three ways - "can you ... for
me" skipped the registry fast lane and became the app "github for me", a
trailing "site" made "GitHub site" an unknown site, and the desktop path
never looked at the site registry. open_application's registry lookup was
also dead: it imported a resolve_normalized that did not exist."""

import builtins
import io
import json

import pytest

import machine_capabilities as mc
import tools


@pytest.mark.parametrize("spoken, name", [
    ("the github for me.", "github"), ("my notes app please", "notes app"),
    ("spotify now", "spotify"), ("github", "github")])
def test_target_noise_is_dropped(spoken, name):
    assert tools.strip_target_noise(spoken) == name


@pytest.mark.parametrize("spoken, name", [
    ("GitHub site", "GitHub"), ("the facebook website", "facebook"),
    ("github for me", "github"), ("youtube music", "youtube music")])
def test_site_names_lose_the_word_site(spoken, name):
    assert tools._clean_site_name(spoken) == name


def test_app_names_lose_for_me_but_keep_their_dots():
    assert tools._clean_app_name("Open the notepad for me.") == "notepad"
    assert tools._clean_app_name("open python 3.14.") == "python 3.14"     # was "python 314"
    assert tools._clean_app_name("node.js, please") == "node.js"


def test_an_app_named_with_app_keeps_the_word(registries, monkeypatch):
    monkeypatch.setattr(tools, "_load_app_registry", lambda: {"ollama app": {}, "ollama": {}})
    import web_registry as wr
    monkeypatch.setattr(wr, "resolve_site", lambda t: None)
    real_open = builtins.open

    def fake_open(path, *a, **k):
        if str(path).endswith("app_registry.json"):
            return io.StringIO(json.dumps({"apps": {"ollama app": {}, "ollama": {}}}))
        return real_open(path, *a, **k)
    monkeypatch.setattr(builtins, "open", fake_open)
    assert tools.resolve_open_target("ollama app") == ("app", "ollama app")


def test_normalized_app_keys_match():
    assert mc.normalize_key("Snipping Tool") == "snippingtool"
    apps = {"snippingtool": {}, "git bash": {}}
    assert mc.resolve_normalized(apps, "snipping tool") == "snippingtool"
    assert mc.resolve_normalized(apps, "Git-Bash") == "git bash"
    assert mc.resolve_normalized(apps, "") is None


@pytest.fixture
def registries(monkeypatch):
    """ngrok is an app, grok and github are sites, cursor is both."""
    import web_registry as wr
    sites = {"grok": {"aliases": []}, "github": {"aliases": []}, "cursor": {"aliases": []}}
    apps = json.dumps({"apps": {"ngrok": {}, "cursor": {}, "notepad": {}}})
    real_open = builtins.open

    def fake_open(path, *a, **k):
        if str(path).endswith("app_registry.json"):
            return io.StringIO(apps)
        return real_open(path, *a, **k)
    monkeypatch.setattr(builtins, "open", fake_open)
    monkeypatch.setattr(tools, "_rule_site_url", lambda name: None)
    monkeypatch.setattr(wr, "get_site", lambda key: sites.get(key))
    monkeypatch.setattr(wr, "resolve_site",
                        lambda t: t if t in sites else ("grok" if t == "ngrok" else None))


def test_an_exact_name_beats_a_fuzzy_one(registries):
    assert tools.resolve_open_target("grok") == ("site", "grok")       # app ngrok is only close
    assert tools.resolve_open_target("ngrok") == ("app", "ngrok")      # site grok is only close
    assert tools.resolve_open_target("cursor")[0] == "clarify"         # exact on both sides
    assert tools.resolve_open_target("github for me") == ("site", "github")


def test_the_desktop_path_opens_registered_sites(registries, monkeypatch):
    ran = []
    monkeypatch.setattr(tools, "execute_tool", lambda name, args, *a: ran.append((name, args)) or "ok")
    tools._desktop_dispatch("can you open github for me")
    tools._desktop_dispatch("open notepad")
    assert ran == [("open_site", {"name": "github"}), ("open_application", {"app": "notepad"})]


def test_can_you_open_takes_the_fast_lane(monkeypatch):
    import brain_gemini
    monkeypatch.setattr(tools, "resolve_open_target",
                        lambda text, force=None, browser=None:
                        ("site", "github") if tools.strip_target_noise(text) == "github" else (None, text))
    assert brain_gemini._fast_lane_opens("Can you open github for me?") is True
    assert brain_gemini._fast_lane_opens("can you visit websites?") is False


@pytest.mark.parametrize("text, launch", [
    ("open the spotify app", False), ("open ollama app", False),
    ("launch the irimsv project", True), ("start the dev server", True)])
def test_only_projects_and_servers_are_launches(text, launch):
    import brain_gemini
    assert (brain_gemini.classify_intent(text) == "launch") is launch
