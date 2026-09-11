"""Fixes from the 2026-09-11 session test: web searches stay web-wide, a
named browser is honoured, an unverified job names the failed step, closing
never force-kills, web_browse rejects sentences, old confirms expire, commands
aren't saved as preferences, rule slots drop filler, and the honesty guard
leaves questions alone."""

import time

import pytest


# ---------------------------------------------------------- web search --

def test_search_the_web_is_never_limited_to_the_last_site(monkeypatch):
    import conversation_window as cw
    import tools
    opened = []
    monkeypatch.setattr(cw, "last_site", lambda: {"key": "github", "url": "https://github.com",
                                                   "browser": "chrome"})
    monkeypatch.setattr(tools, "search_web", lambda q: opened.append(q) or f"Opened search: {q}")
    parsed = tools.parse_search_command("search the web for top lo-fi channels")
    assert parsed["query"] == "top lo-fi channels" and parsed["web"] is True
    assert tools.execute_search(parsed) == "Opened search: top lo-fi channels"
    assert opened == ["top lo-fi channels"]


def test_research_phrasing_is_not_a_search_tab():
    import tools
    assert tools.parse_search_command(
        "search the web for the top 3 lo-fi YouTube channels and summarize them") is None
    assert tools.parse_search_command("search the web for cats")["query"] == "cats"


# --------------------------------------------------------- named browser --

def test_open_site_tool_passes_the_browser(monkeypatch):
    import tools
    seen = {}
    monkeypatch.setattr(tools, "open_site",
                        lambda name, url=None, browser=None, **k: seen.update(name=name, browser=browser)
                        or "Opened.")
    tools.TOOL_MAP["open_site"](name="github", browser="brave")
    assert seen == {"name": "github", "browser": "brave"}


def test_split_browser_reads_in_brave():
    import tools
    text, word = tools._split_browser("open github in brave")
    assert text == "open github" and word == "brave"


# ------------------------------------------------------- unverified job --

def test_unverified_job_names_the_step_it_could_not_confirm(tmp_path, monkeypatch):
    import autonomous
    import jobs
    monkeypatch.setattr(jobs, "update", lambda *a, **k: None)
    monkeypatch.setattr(autonomous, "status_path", lambda jid: str(tmp_path / "s.status"))
    monkeypatch.setattr(autonomous, "_verify_step",
                        lambda rest: (False, "no window") if "VS Code" in rest else (True, "ok"))
    monkeypatch.setattr(autonomous, "POLL_INTERVAL", 0.01)
    (tmp_path / "s.status").write_text(
        "[STEP 1] done - Opened VS Code | launched code.exe\n"
        "[STEP 2] done - Opened GitHub in Brave | tab open\n", encoding="utf-8")
    finished = {}
    monkeypatch.setattr(autonomous, "_finish",
                        lambda jid, proc, **k: finished.update(k))

    class Proc:
        returncode = 0

        def poll(self):
            return 0

        def communicate(self):
            return "RESULT: done\n", ""
    autonomous._supervise("j1", "workspace", Proc(), timeout=30)
    assert finished["state"] == "unverified"
    assert finished["summary"] == "1 of 2 steps verified. I could not confirm: Opened VS Code."


# ------------------------------------------------------------- closing --

def test_close_never_force_kills_unless_asked(monkeypatch):
    import machine_capabilities as mc
    import tools
    ran = []
    monkeypatch.setattr(mc, "load_registry",
                        lambda: {"apps": {"notepad": {"name": "notepad", "bin": "C:/notepad.exe"}}})
    monkeypatch.setattr(tools.subprocess, "run",
                        lambda cmd, **k: ran.append(cmd) or type("R", (), {"stdout": "notepad.exe"})())
    monkeypatch.setattr(tools.time, "sleep", lambda s: None)
    out = tools.close_application("notepad")
    assert "still open" in out and "force close" in out
    assert not any("/F" in c for c in ran if isinstance(c, list))
    tools.close_application("notepad", force=True)
    assert any(isinstance(c, list) and "/F" in c for c in ran)


# ------------------------------------------------------------ web_browse --

def test_web_browse_refuses_a_sentence_as_a_url():
    import tools
    out = tools.web_browse("go back to the lo-fi channels which one is the most popular?")
    assert out.startswith("[Error]") and "isn't a web address" in out


# ---------------------------------------------------------- stale jobs --

def test_weeks_old_confirms_are_not_active(monkeypatch):
    import jobs
    monkeypatch.setattr(jobs, "_load_history", lambda: None)
    monkeypatch.setattr(jobs, "_append_log", lambda rec: None)
    monkeypatch.setattr(jobs, "_emit", lambda rec: None)
    monkeypatch.setattr(jobs, "_jobs", {
        "old": {"id": "old", "state": "waiting-on-confirm", "started": time.time() - 30 * 86400,
                "task": "x", "progress": []},
        "new": {"id": "new", "state": "waiting-on-confirm", "started": time.time() - 60,
                "task": "y", "progress": []}})
    assert [j["id"] for j in jobs.active()] == ["new"]
    assert jobs._jobs["old"]["state"] == "timeout"


# --------------------------------------------------------- preferences --

@pytest.mark.parametrize("text, pref", [
    ("I prefer lo-fi while coding", True), ("my favorite editor is VS Code", True),
    ("play some of my favorite on spotify", False), ("open my favorite site", False)])
def test_commands_are_not_saved_as_preferences(text, pref):
    import brain_gemini
    assert brain_gemini._looks_like_preference(text) is pref


# ------------------------------------------------------------ rule slot --

def test_rule_slot_drops_that_site_for_and_instead():
    import rules_engine
    rule = {"rule_id": "search_movie", "source_phrase": "search movie",
            "triggers": ["search movie"], "enforcement": "action",
            "action": {"type": "search_site", "site": "hollymoviehd.cc",
                       "url": "https://hollymoviehd.cc",
                       "search_url": "https://hollymoviehd.cc?s={query}", "browser": "brave"}}
    apps = {"brave": {"category": "browser", "compiled_rules": [rule]}}
    m = rules_engine.match_action("search that movie site for Blade Runner instead", apps)
    assert m and m["slot"] == "Blade Runner"
    # Titles that start with a pointer word keep it.
    assert rules_engine.match_action("search movie This Is Us", apps)["slot"] == "This Is Us"
    assert rules_engine.match_action("search movie That Thing You Do", apps)["slot"] == "That Thing You Do"


def test_pinning_takes_the_scan_lock(tmp_path, monkeypatch):
    import json
    import app_abilities as aa
    import curate
    import machine_capabilities as mc
    path = tmp_path / "app_registry.json"
    path.write_text(json.dumps({"apps": {"vlc": {"name": "vlc"}}}), encoding="utf-8")
    monkeypatch.setattr(mc, "REGISTRY_PATH", str(path))
    monkeypatch.setattr(curate, "REGISTRY_PATH", str(path))
    held = []

    class Lock:
        def __enter__(self):
            held.append(True)

        def __exit__(self, *a):
            pass
    monkeypatch.setattr(aa, "_LOCK", Lock())
    assert curate.mark_registered("vlc") is True and held == [True]
    assert json.loads(path.read_text(encoding="utf-8"))["apps"]["vlc"]["pinned_by"] == "user"


# ------------------------------------------------------ voice session 2 --

def test_my_favorites_on_spotify_plays_liked_songs(monkeypatch):
    import app_abilities as aa
    import brain_gemini
    ran = []
    monkeypatch.setattr(aa, "run_ability",
                        lambda key, aid, args=None: ran.append((key, aid)) or "Done: Play liked songs.")
    assert brain_gemini.play_favorites("can you play a song on spotify my favorites") == \
        "Playing your liked songs on Spotify, sir."
    assert ran == [("spotify", "spotify.play_liked")]
    assert brain_gemini.play_favorites("play my favorites") is None           # no platform named
    assert brain_gemini.play_favorites("play lo-fi on spotify") is None       # not favorites


def test_an_unconfigured_manus_is_never_picked(monkeypatch):
    import manus_agent
    import tools
    monkeypatch.setattr(tools, "_detect_backend_by_task", lambda task: "manus")
    monkeypatch.setattr(manus_agent, "_cookies_present", lambda: False)
    assert tools._detect_backend("make it 100% volume") == "hermes"
    monkeypatch.setattr(manus_agent, "_cookies_present", lambda: True)
    assert tools._detect_backend("make an image of a cat") == "manus"


def test_set_the_system_volume(monkeypatch):
    import app_abilities as aa
    presses = []
    monkeypatch.setattr(aa, "_press_media", lambda name: presses.append(name))
    op = {"op": "volume_set", "percent": "{percent}"}
    assert aa._run_op("spotify", {}, op, {"percent": "100%"}) == ""
    assert presses.count("volume_down") == aa.VOLUME_STEPS and presses.count("volume_up") == 50
    presses.clear()
    assert aa._run_op("spotify", {}, op, {"percent": "30"}) == "" and presses.count("volume_up") == 15
    assert aa._run_op("spotify", {}, op, {"percent": "150"}).startswith("[Error]")
    ids = [a["id"] for a in aa.build_abilities("spotify", {}, "media")]
    assert "spotify.set_volume" in ids


def test_a_question_that_names_an_app_does_not_open_it(monkeypatch):
    import tools
    ran = []
    monkeypatch.setattr(tools, "execute_tool", lambda name, args, *a: ran.append((name, args)) or "ok")
    assert tools._desktop_dispatch("from what is the status of the sticky brain on open source repo") is None
    assert tools._desktop_dispatch("what are the ai agents installed in the computer right now") is None
    assert ran == []
    tools._desktop_dispatch("can you open hermes")
    tools._desktop_dispatch("close notepad.")
    assert ran == [("open_application", {"app": "hermes"}), ("close_application", {"app": "notepad"})]


@pytest.mark.parametrize("text, job", [
    ("what's the status?", True), ("what is the status of the task", True),
    ("any update on it", True), ("how is the job going", True),
    ("what is the status of the sticky brain open source repo", False),
    ("what's the progress on my thesis", False)])
def test_only_job_questions_get_the_job_status(text, job):
    import brain_gemini
    assert (brain_gemini.classify_intent(text) == "job_status") is job


def test_jarvis_does_not_try_to_close_itself():
    import tools
    assert tools.close_application("yourself").startswith("I can't close myself")
    assert tools.close_application("can you close yourself").startswith("I can't close myself")


# ------------------------------------------------------- honesty guard --

def test_honesty_guard_leaves_questions_alone():
    from brain_gemini import honest_reply
    answer = "Opened three things for you so far, sir."
    assert honest_reply(answer, [], "router", "how many things have you opened for me so far?") == answer
    assert honest_reply("Opened notes app.", [], "router", "open my notes app") != "Opened notes app."
