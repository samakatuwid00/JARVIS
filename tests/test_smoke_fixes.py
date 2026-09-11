"""Fixes from the 2026-09-11 end-to-end smoke test: the registry is read once
per change, "no" declines the waiting confirm, a model can't claim an action
that never ran, "search X there" uses the site's own search, and a clear
"never close X" rule needs no question and blocks in plain words."""

import json

import pytest


# ------------------------------------------------------- registry reads --

def test_registry_is_read_once_per_change(tmp_path, monkeypatch):
    import curate
    import machine_capabilities as mc
    path = tmp_path / "app_registry.json"
    apps = {f"tool{i}": {"name": f"tool{i}", "registered": False} for i in range(300)}
    apps["spotify"] = {"name": "spotify", "registered": True}
    path.write_text(json.dumps({"apps": apps}), encoding="utf-8")
    monkeypatch.setattr(mc, "REGISTRY_PATH", str(path))
    loads = []
    real = mc.load_registry
    monkeypatch.setattr(curate, "load_registry", lambda: loads.append(1) or real())
    assert curate.registered_match("open spotify") == "spotify"
    assert len(loads) == 1                     # was 600+ reads of the same file
    apps["vlc"] = {"name": "vlc", "registered": True}
    path.write_text(json.dumps({"apps": apps}), encoding="utf-8")
    assert curate.registered_match("vlc") == "vlc"     # a change is picked up
    assert len(loads) == 2


# -------------------------------------------------------------- decline --

@pytest.mark.parametrize("text, declines", [
    ("no", True), ("no, cancel that", True), ("nope, don't do it", True),
    ("cancel", True), ("no thanks", True), ("never mind", True),
    ("no, I meant open spotify", False), ("stop the music", False),
    ("No Time to Die", False), ("stop", False)])
def test_what_counts_as_a_no(text, declines):
    import brain_gemini
    assert bool(brain_gemini._BARE_DECLINE_RE.match(text)) is declines


def test_no_cancels_the_waiting_hermes_task(monkeypatch):
    import jobs
    import tools
    updates = []
    monkeypatch.setattr(jobs, "update", lambda jid, **k: updates.append((jid, k)))
    monkeypatch.setattr(tools, "_audit_log", lambda *a, **k: None)
    monkeypatch.setattr(tools, "_PENDING_HERMES_CALL",
                        {"raw_task": "delete x.txt", "task": "CONTEXT delete x.txt",
                         "jid": "j1", "ts": 5.0})
    monkeypatch.setitem(tools._PENDING_DESTRUCTIVE, "task", "CONTEXT delete x.txt")
    out = tools.decline_pending()
    assert out == "Cancelled, sir. I won't run “delete x.txt”."
    assert tools.pending_confirm_task() is None and tools._PENDING_DESTRUCTIVE["task"] is None
    assert updates == [("j1", {"state": "cancelled", "note": "declined by the user"})]
    assert tools.decline_pending() is None     # nothing left: route normally


def test_no_cancels_the_newest_of_two_latches(monkeypatch):
    import tools
    monkeypatch.setattr(tools, "_audit_log", lambda *a, **k: None)
    monkeypatch.setattr(tools, "_PENDING_HERMES_CALL", {"raw_task": "old task", "ts": 1.0})
    monkeypatch.setitem(tools._PENDING_DESTRUCTIVE, "autonomous", "clean my downloads")
    monkeypatch.setitem(tools._PENDING_DESTRUCTIVE, "autonomous_ts", 9.0)
    assert "clean my downloads" in tools.decline_pending()
    assert tools.pending_autonomous_goal() is None
    assert tools.pending_confirm_task() == "old task"


# ---------------------------------------------------------- honest reply --

def test_a_model_claiming_an_action_that_never_ran_is_corrected():
    from brain_gemini import honest_reply
    claim = "Opened notes app. Let me know if that's the one you meant."
    assert honest_reply(claim, [], "router").startswith("I didn't actually do that, sir")
    assert honest_reply(claim, [{"tool": "open_application"}], "router") == claim
    assert honest_reply("Opened notepad", [], "instant") == "Opened notepad"
    answer = "That is three hundred and ninety-one, sir."
    assert honest_reply(answer, [], "router") == answer


# -------------------------------------------------------- search there --

def _last_site(monkeypatch, url):
    import conversation_window as cw
    import tools
    opened = []
    monkeypatch.setattr(cw, "last_site",
                        lambda: {"key": "youtube", "url": url, "browser": "chrome"})
    monkeypatch.setattr(tools, "_open_url_in_browser",
                        lambda url, name, browser=None, remember=True:
                        opened.append(url) or f"Opened {name}.")
    return opened


def test_search_there_uses_the_sites_own_search(monkeypatch):
    import tools
    opened = _last_site(monkeypatch, "https://www.youtube.com/")
    out = tools.execute_search({"query": "lo-fi study music"})
    assert opened == ["https://www.youtube.com/results?search_query=lo-fi+study+music"]
    assert out == "Searching youtube for lo-fi study music in Chrome."


def test_search_there_uses_a_verified_rule_search_address(monkeypatch):
    import site_probe
    import tools
    monkeypatch.setattr(site_probe, "_read_cache",
                        lambda: {"hollymoviehd.cc": "https://hollymoviehd.cc?s={query}"})
    opened = _last_site(monkeypatch, "https://hollymoviehd.cc/")
    tools.execute_search({"query": "Dune"})
    assert opened == ["https://hollymoviehd.cc?s=Dune"]


def test_search_there_on_an_unknown_site_keeps_the_google_site_search(monkeypatch):
    import site_probe
    import tools
    monkeypatch.setattr(site_probe, "_read_cache", lambda: {})
    opened = _last_site(monkeypatch, "https://example.org/")
    out = tools.execute_search({"query": "cats"})
    assert opened == ["https://www.google.com/search?q=site%3Aexample.org+cats"]
    assert "limited to example.org" in out


def test_there_is_not_part_of_the_search_words():
    import tools
    assert tools.parse_search_command("now search for lo-fi study music there")["query"] == \
        "lo-fi study music"
    assert tools.parse_search_command("search cat videos on this site")["query"] == "cat videos"
    assert tools.parse_search_command("search for there will be blood")["query"] == \
        "there will be blood"


# ------------------------------------------------------------ confirm gate --

@pytest.mark.parametrize("task", [
    "empty my recycle bin", "can you empty my recycle bin?", "clear my browser history",
    "reset my network settings", "flush the dns cache", "uninstall spotify",
    "nuke my downloads folder", "please get rid of old screenshots",
    "could you wipe the temp folder?", "rename my projects folder", "restart the computer",
    "move my photos to the trash", "I need you to shred these files"])
def test_state_changing_tasks_always_need_a_confirm(task):
    import tools
    assert tools._is_safe_task(task) is False


@pytest.mark.parametrize("task", [
    "what is the weather today?", "who am I?", "my codename is Falcon, acknowledge it",
    "show me my downloads folder", "list my projects", "search the web for lo-fi music",
    "open spotify", "please tell me a joke", "good morning", "what is a factory pattern?"])
def test_chat_and_reads_stay_unconfirmed(task):
    import tools
    assert tools._is_safe_task(task) is True


# ---------------------------------------------------------- site names --

@pytest.mark.parametrize("host, name", [
    ("www.youtube.com", "youtube"), ("music.youtube.com", "youtube music"),
    ("accounts.google.com.ph", "google accounts"), ("m.facebook.com", "facebook"),
    ("irimsv-library.net", "irimsv-library"), ("chat.deepseek.com", "deepseek chat")])
def test_site_names_keep_their_subdomain(host, name):
    import web_registry
    assert web_registry._domain_name(host) == name


def test_normalize_renames_subdomains_and_keeps_bare_names_as_aliases():
    import web_registry
    h = lambda host, visits: {"url": f"https://{host}", "host": host, "aliases": [],
                              "source": "history", "visits": visits, "top_urls": []}
    sites = {"youtube": h("music.youtube.com", 251), "google": h("gemini.google.com", 16),
             "com": h("accounts.google.com.ph", 10), "deepseek": h("chat.deepseek.com", 56),
             "facebook": h("www.facebook.com", 430),
             "email": {"url": "https://mail.google.com", "host": "mail.google.com",
                       "aliases": [], "source": "manual"}}
    web_registry.normalize_sites(sites)
    assert set(sites) == {"youtube music", "google gemini", "google accounts",
                          "deepseek chat", "facebook", "email"}
    assert sites["youtube music"]["aliases"] == ["youtube"]
    assert sites["google gemini"]["aliases"] == ["google"]      # the most-visited google
    assert sites["deepseek chat"]["aliases"] == ["deepseek"]
    assert sites["email"]["host"] == "mail.google.com"           # manual untouched


def test_a_manual_name_beats_another_sites_alias():
    import web_registry
    reg = {"generated": "t", "sites": {
        "youtube music": {"url": "https://music.youtube.com", "host": "music.youtube.com",
                          "aliases": ["youtube"], "source": "history"},
        "youtube": {"url": "https://www.youtube.com", "host": "www.youtube.com",
                    "aliases": [], "source": "manual"}}}
    web_registry.build_index(reg)
    assert web_registry._index["exact"]["youtube"] == "youtube"
    assert web_registry._index["exact"]["youtube music"] == "youtube music"


# --------------------------------------------------------- ollama preload --

def _brain_with(monkeypatch, **flags):
    import brain_gemini as bg
    for k, v in flags.items():
        monkeypatch.setattr(bg, k, v)
    monkeypatch.setattr(bg, "GEMINI_API_KEY", "")
    monkeypatch.setattr(bg, "JARVIS_USE_9ROUTER", True)
    loads = []
    monkeypatch.setattr(bg.JarvisBrain, "_preload_ollama", lambda self: loads.append(1))
    monkeypatch.setattr(bg.JarvisBrain, "_maybe_reset_idle", lambda self: None, raising=False)
    bg.JarvisBrain()
    return loads


def test_fallback_ollama_is_not_loaded_at_startup(monkeypatch):
    assert _brain_with(monkeypatch, JARVIS_USE_OLLAMA=True, JARVIS_PREFER_LOCAL=False,
                       JARVIS_LOCAL_ONLY=False, OLLAMA_KEEP_WARM=False) == []


def test_ollama_is_preloaded_when_local_is_the_brain_or_kept_warm(monkeypatch):
    assert _brain_with(monkeypatch, JARVIS_USE_OLLAMA=True, JARVIS_PREFER_LOCAL=True,
                       JARVIS_LOCAL_ONLY=False, OLLAMA_KEEP_WARM=False) == [1]
    assert _brain_with(monkeypatch, JARVIS_USE_OLLAMA=True, JARVIS_PREFER_LOCAL=False,
                       JARVIS_LOCAL_ONLY=False, OLLAMA_KEEP_WARM=True) == [1]


# ------------------------------------------------------------ never rule --

def test_never_close_is_a_clear_hard_close_block():
    import rules_compiler as rc
    import rules_engine
    rule = rc.parse_scaffold("notepad", "never close notepad")["candidate_rules"][0]
    assert rule["needs_clarification"] is False and rule["enforcement"] == "hard"
    assert rule["adapter_check"] == "pre_close: block"
    entry = {"compiled_rules": [rule]}
    assert rules_engine.evaluate_app_rules(entry, "open")["allowed"] is True
    out = rules_engine.evaluate_app_rules(entry, "close")
    assert out["allowed"] is False
    assert out["blocks"] == ["I can't do that, sir: your rule “never close notepad” "
                             "blocks it. Change it in the Apps panel if you want to allow it."]


def test_vague_rules_still_ask():
    import rules_compiler as rc
    rule = rc.parse_scaffold("spotify", "keep it quiet")["candidate_rules"][0]
    assert rule["needs_clarification"] is True
