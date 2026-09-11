"""Multi-step rules: choosing from top picks, pick extraction, the consult
flow (search -> picks -> choose -> open -> ask -> click), desktop steps, the
AI compile's step clean-up, validation, and simulation. The browser, desktop
driver and page loads are always faked."""

import json

import pytest

import browser_cdp
import rules_ai
import rules_engine
import rules_sim
import rules_steps

RESULTS = """<html><head><title>You searched for Dune</title></head><body>
<nav><a href="https://hollymoviehd.cc/">Home</a><a href="https://hollymoviehd.cc/genre/">Genre</a></nav>
<a href="https://hollymoviehd.cc/dune-part-two-2024/" title="Dune: Part Two (2024)"><img alt="x"></a>
<a href="https://hollymoviehd.cc/dune-2021/" title="Dune (2021)">Dune (2021)</a>
<a href="https://hollymoviehd.cc/dune-2021/">Dune (2021)</a>
<a href="https://hollymoviehd.cc/dune-1984/" title="Dune (1984)">Dune</a>
<a href="https://ads.example/dune" title="Dune deal">ad</a>
<a href="https://hollymoviehd.cc/?s=Dune&page=2">Dune page 2</a>
<a href="https://hollymoviehd.cc/arrival-2016/" title="Arrival (2016)">Arrival</a>
</body></html>"""
MOVIE_PAGE = ('<html><head><title>Dune (1984)</title></head><body>'
              '<iframe src="https://www.youtube.com/embed/x" id="iframe-trailer"></iframe>'
              '<iframe allowfullscreen src="https://goodstream.cc/embed/abc"></iframe></body></html>')

STEPS_ACTION = {"type": "steps", "site": "hollymoviehd.cc", "url": "https://hollymoviehd.cc",
                "search_url": "https://hollymoviehd.cc/?s={query}", "browser": "brave",
                "slot": "movie name", "sample": "Dune",
                "steps": [{"op": "search"}, {"op": "pick", "count": 5}, {"op": "open"},
                          {"op": "click", "target": "play", "confirm": True}]}
STEPS_RULE = {"rule_id": "search_movies_in_brave", "source_phrase": "search movies in brave",
              "enforcement": "action", "triggers": ["search movies in brave"],
              "action": STEPS_ACTION}
PICKS = [{"title": "Dune: Part Two (2024)", "url": "u1"}, {"title": "Dune (2021)", "url": "u2"},
         {"title": "Dune (1984)", "url": "u3"}]


# ------------------------------------------------------------- choosing --

@pytest.mark.parametrize("answer, index", [
    ("2", 1), ("number 3", 2), ("the second one", 1), ("the last one", 2),
    ("the 1984 one", 2), ("part two", 0), ("Dune 2021 please", 1), ("first", 0),
])
def test_choose_understands_the_reply(answer, index):
    assert rules_steps.choose(answer, PICKS) == index


@pytest.mark.parametrize("answer", ["7", "something else", "dune"])
def test_choose_refuses_to_guess(answer):
    assert rules_steps.choose(answer, PICKS) is None


def test_extract_picks_keeps_results_and_drops_nav_ads_and_pages():
    picks = rules_steps.extract_picks("https://hollymoviehd.cc/?s=Dune", "Dune", dom=RESULTS)
    # the exact titles first, then titles that start with the name
    assert picks == [
        {"title": "Dune (2021)", "url": "https://hollymoviehd.cc/dune-2021/"},
        {"title": "Dune (1984)", "url": "https://hollymoviehd.cc/dune-1984/"},
        {"title": "Dune: Part Two (2024)", "url": "https://hollymoviehd.cc/dune-part-two-2024/"},
    ]


def test_closest_titles_beat_partial_matches():
    dom = "".join(f'<a href="https://s.cc/{i}/" title="{t}">{t}</a>' for i, t in enumerate(
        ["Dune: Prophecy Season 1", "The Dunes (2021)", "Planet Dune (2021)", "Dune (2021)",
         "Dune (1984)"]))
    titles = [p["title"] for p in rules_steps.extract_picks("https://s.cc/?s=Dune", "Dune", dom=dom)]
    assert titles == ["Dune (2021)", "Dune (1984)", "Dune: Prophecy Season 1",
                      "Planet Dune (2021)", "The Dunes (2021)"]


# -------------------------------------------------------------- the run --

@pytest.fixture
def web(monkeypatch):
    import audit
    import site_probe
    import tools
    calls = []
    monkeypatch.setattr(audit, "log_call", lambda *a, **k: None)
    monkeypatch.setattr(site_probe, "load", lambda url: {"dom": RESULTS, "blocked": False})
    monkeypatch.setattr(browser_cdp, "is_attached", lambda b: True)
    monkeypatch.setattr(browser_cdp, "open_tab",
                        lambda b, url: calls.append(("open", b, url)) or {"id": "t"})
    monkeypatch.setattr(browser_cdp, "click",
                        lambda b, target, url_hint=None: calls.append(("click", b, target, url_hint))
                        or "Clicked the player in Brave.")
    monkeypatch.setattr(tools, "_open_url_in_browser",
                        lambda url, name, browser=None, remember=True:
                        calls.append(("plain", browser, url)) or f"Opened {name}.")
    rules_steps.cancel()
    yield calls
    rules_steps.cancel()


def test_full_consult_flow(web):
    out = rules_steps.start(STEPS_RULE, "brave", STEPS_ACTION, "Dune")
    assert out.startswith("Top picks for Dune: 1. Dune (2021). 2. Dune (1984). 3. Dune: Part Two (2024).")
    assert out.endswith("Which one?") and web == []
    out = rules_steps.resume("the 1984 one")
    assert out == "Opened Dune (1984) in Brave. Should I press play on Dune (1984)?"
    assert web == [("open", "brave", "https://hollymoviehd.cc/dune-1984/")]
    assert rules_steps.resume("yes") == "Clicked the player in Brave."
    assert web[-1] == ("click", "brave", "play", "https://hollymoviehd.cc/dune-1984/")
    assert not rules_steps.active()


def test_missing_name_is_asked_first(web):
    assert rules_steps.start(STEPS_RULE, "brave", STEPS_ACTION, "") == "Which movie, sir?"
    assert rules_steps.resume("Dune").startswith("Top picks for Dune:")


def test_unclear_choice_asks_again_and_no_stops(web):
    rules_steps.start(STEPS_RULE, "brave", STEPS_ACTION, "Dune")
    assert rules_steps.resume("hmm") == "Say a number from 1 to 3, or the title."
    rules_steps.resume("2")
    assert rules_steps.resume("no") == "Okay, I'll leave it there."
    assert not any(c[0] == "click" for c in web)


def test_a_failing_step_says_so_instead_of_falling_through(web, monkeypatch):
    import site_probe
    monkeypatch.setattr(site_probe, "load", lambda url: (_ for _ in ()).throw(OSError("down")))
    out = rules_steps.start(STEPS_RULE, "brave", STEPS_ACTION, "Dune")
    assert out == '[Error] The "pick" step failed (OSError).'
    assert not rules_steps.active()


def test_a_new_command_releases_the_run(web):
    rules_steps.start(STEPS_RULE, "brave", STEPS_ACTION, "Dune")
    assert rules_steps.resume("what time is it", looks_new=True) is None
    assert not rules_steps.active()


def test_title_starting_with_no_is_a_name_not_a_stop(web):
    rules_steps.start(STEPS_RULE, "brave", STEPS_ACTION, "")
    out = rules_steps.resume("No Time to Die")
    assert "No Time to Die" in out and out != "Okay, I'll leave it there."


def test_running_browser_without_port_asks_before_restart(web, monkeypatch):
    monkeypatch.setattr(browser_cdp, "is_attached", lambda b: False)
    monkeypatch.setattr(browser_cdp, "is_running", lambda b: True)
    rules_steps.start(STEPS_RULE, "brave", STEPS_ACTION, "Dune")
    out = rules_steps.resume("1")
    assert "Restart Brave so I can click in the page?" in out
    # declining opens it normally and leaves the click to the user
    out = rules_steps.resume("no")
    assert web == [("plain", "brave", "https://hollymoviehd.cc/dune-2021/")]
    assert out.endswith("You can play it yourself.")


def test_typed_names_are_literal_and_unsafe_keys_are_refused(monkeypatch):
    import audit
    import desktop_driver
    typed = []
    monkeypatch.setattr(audit, "log_call", lambda *a, **k: None)
    monkeypatch.setattr(desktop_driver, "type_into_control",
                        lambda win, target, text: typed.append(text) or "")
    action = {"type": "steps", "steps": [{"op": "type", "target": "Search", "text": "{query}"},
                                         {"op": "key", "keys": "{LWIN}r"}]}
    rule = {"rule_id": "x", "source_phrase": "find deals", "action": action}
    out = rules_steps.start(rule, "shop", action, "50% off (new)", {"name": "Shop"})
    assert typed == ["50{%} off {(}new{)}"]
    assert out.startswith('[Error] I won\'t press "{LWIN}r"')
    assert rules_steps.SAFE_KEYS_RE.match("{ENTER}") and rules_steps.SAFE_KEYS_RE.match("^f")


def test_compile_drops_unsafe_key_steps():
    reply = {"type": "steps", "steps": [{"op": "launch"}, {"op": "key", "keys": "%{F4}"},
                                        {"op": "key", "keys": "{ENTER}"}]}
    rule, _ = rules_ai.compile_rule("notepad", "save and close", "press enter",
                                    entry={"bin": "C:/notepad.exe"}, llm=_llm(reply))
    assert rule["action"]["steps"] == [{"op": "launch"}, {"op": "key", "keys": "{ENTER}"}]


def test_desktop_steps_use_the_app_window(monkeypatch):
    import audit
    import desktop_driver
    import tools
    calls = []
    monkeypatch.setattr(audit, "log_call", lambda *a, **k: None)
    monkeypatch.setattr(tools, "open_application", lambda app: calls.append(("launch", app)) or "")
    monkeypatch.setattr(desktop_driver, "type_into_control",
                        lambda win, target, text: calls.append(("type", win, target, text)) or "")
    monkeypatch.setattr(desktop_driver, "click_control",
                        lambda win, target: calls.append(("click", win, target)) or "")
    action = {"type": "steps", "steps": [{"op": "launch"},
                                         {"op": "type", "target": "Search", "text": "{query}"},
                                         {"op": "click", "target": "Play"}]}
    rule = {"rule_id": "play_on_spotify", "source_phrase": "play on spotify", "action": action}
    out = rules_steps.start(rule, "spotify", action, "lofi", {"name": "Spotify"})
    assert out == "Done."
    assert calls == [("launch", "spotify"), ("type", "Spotify", "Search", "lofi"),
                     ("click", "Spotify", "Play")]


# --------------------------------------------------------------- compile --

def _llm(reply):
    return lambda prompt: (dict(reply), "fake")


def test_compile_adds_pick_open_and_confirm():
    reply = {"type": "steps", "site": "hollymoviehd.cc", "slot": "movie name", "sample": "Dune",
             "steps": [{"op": "search"}, {"op": "click", "target": "play"},
                       {"op": "rm -rf", "target": "x"}]}
    rule, _ = rules_ai.compile_rule(
        "brave", "search movies in brave",
        "search hollymoviehd.cc, let me choose, then play it", entry={"category": "browser"},
        llm=_llm(reply))
    assert [s["op"] for s in rule["action"]["steps"]] == ["search", "pick", "open", "click"]
    assert rule["action"]["steps"][-1]["confirm"] is True
    assert rule["enforcement"] == "action"
    assert "show you the top picks to choose from" in rule["summary"]
    assert rules_engine.action_of(rule, "brave", {"category": "browser"})["browser"] == "brave"


def test_desktop_app_steps_need_no_site():
    reply = {"type": "steps", "steps": [{"op": "launch"}, {"op": "click", "target": "Liked Songs"},
                                        {"op": "search"}]}
    rule, _ = rules_ai.compile_rule("spotify", "play my liked songs", "open liked songs",
                                    entry={"bin": "C:/Spotify.exe", "category": "media"},
                                    llm=_llm(reply))
    assert [s["op"] for s in rule["action"]["steps"]] == ["launch", "click"]
    assert not rule["needs_clarification"] and "url" not in rule["action"]


def test_steps_action_validation():
    assert rules_engine.action_of({"action": dict(STEPS_ACTION, url="--flag")}, "brave") is None
    assert rules_engine.action_of({"action": {"type": "steps", "steps": [{"op": "nope"}]}}, "x") is None


def test_rule_command_starts_the_run(web, tmp_path, monkeypatch):
    import tools
    path = tmp_path / "app_registry.json"
    path.write_text(json.dumps({"apps": {"brave": {"category": "browser",
                                                   "compiled_rules": [STEPS_RULE]}}}))
    monkeypatch.setattr(rules_engine, "APP_REGISTRY_PATH", str(path))
    out = tools.try_rule_action("search movies Dune in brave browser")
    assert out.startswith("Top picks for Dune:")
    assert tools.try_rule_action("number 2").startswith("Opened Dune (1984) in Brave.")


def test_a_title_beyond_the_top_list_can_still_be_chosen(web, monkeypatch):
    import site_probe
    many = "".join(f'<a href="https://hollymoviehd.cc/d{i}/" title="Dune Story {i}">x</a>'
                   for i in range(6)) + \
        '<a href="https://hollymoviehd.cc/woman-in-the-dunes-1964/" title="Woman in the Dunes (1964)">w</a>'
    monkeypatch.setattr(site_probe, "load", lambda url: {"dom": many, "blocked": False})
    out = rules_steps.start(STEPS_RULE, "brave", dict(STEPS_ACTION, steps=[
        {"op": "search"}, {"op": "pick", "count": 3}, {"op": "open"}]), "Dune")
    assert "Woman in the Dunes" not in out
    # no click step follows, so it opens as a plain link (no control port needed)
    assert rules_steps.resume("the 1964 one") == "Opened Woman in the Dunes (1964)."
    assert web[-1] == ("plain", "brave", "https://hollymoviehd.cc/woman-in-the-dunes-1964/")


# ------------------------------------------------------------- simulate --

class StepsProbe:
    def discover_search(self, site_url):
        return {"search_url": "https://hollymoviehd.cc?s={query}", "field": "s"}

    def probe_search(self, tpl, sample):
        return {"status": "ok", "evidence": "fake ok", "title": "", "url": tpl.replace("{query}", sample)}

    def load(self, url):
        return {"dom": RESULTS if "?s=" in url else MOVIE_PAGE, "blocked": False}

    def click_target_in_dom(self, dom, target):
        import site_probe
        return site_probe.click_target_in_dom(dom, target)


def test_simulate_steps_reports_picks_and_player():
    judge = lambda p: ({"match": True, "reason": "ok"}, "fake")
    rule, report = rules_sim.simulate("brave", STEPS_RULE, {"category": "browser"},
                                      llm=judge, probe=StepsProbe())
    assert report["status"] == "verified"
    assert any(s.startswith("OK top picks: Dune (2021); Dune (1984); Dune: Part Two (2024)")
               for s in report["steps"])
    assert 'OK "play" is on https://hollymoviehd.cc/dune-2021/' in report["steps"]


def test_trailer_alone_is_not_a_player():
    import site_probe
    only_trailer = '<iframe id="iframe-trailer" src="https://youtube.com/embed/x"></iframe>'
    assert not site_probe.click_target_in_dom(only_trailer, "play")
    assert site_probe.click_target_in_dom(MOVIE_PAGE, "play")
