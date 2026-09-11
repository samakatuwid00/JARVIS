"""Rule simulation: site_probe page parsing/judging (fixture HTML) and
rules_sim's search-address verification, repair and intent check (fake
probe + fake model, never a real browser)."""

import json

import rules_ai
import rules_engine
import rules_sim
import rules_voice
import site_probe

WP_HOME = """<html><head><title>HollyMovieHD - Watch Movies</title></head><body>
<form method="post" action="/login"><input type="text" name="user"></form>
<form method="get" id="searchform" action="https://hollymoviehd.cc">
<input class="search-input" type="text" name="s" id="s" value=""></form></body></html>"""

YT_HOME = """<html><head><title>YouTube</title></head><body>
<form action="/results" class="ytSearchboxComponentSearchForm">
<input class="ytSearchboxComponentInput" name="search_query"></form></body></html>"""


def _page(title, body):
    return f"<html><head><title>{title}</title></head><body>{body}</body></html>"


# ------------------------------------------------------------- site_probe --

def test_search_template_from_the_sites_own_form():
    assert site_probe.search_template_from_dom(WP_HOME, "https://hollymoviehd.cc/") == \
        {"search_url": "https://hollymoviehd.cc?s={query}", "field": "s"}
    assert site_probe.search_template_from_dom(YT_HOME, "https://www.youtube.com/") == \
        {"search_url": "https://www.youtube.com/results?search_query={query}",
         "field": "search_query"}


def test_hidden_form_fields_are_kept_and_post_forms_ignored():
    dom = ('<form action="/find" method="GET"><input type="hidden" name="lang" value="en">'
           '<input type="search" name="q"></form>')
    assert site_probe.search_template_from_dom(dom, "https://ex.com/")["search_url"] == \
        "https://ex.com/find?lang=en&q={query}"
    assert site_probe.search_template_from_dom(
        '<form method="post"><input name="s"></form>', "https://ex.com/") is None


def test_judge_search_pages():
    ok = _page("You searched for Dune - HollyMovieHD",
               '<input name="s" value="Dune"><a title="Dune (1984)">Dune (1984)</a>')
    assert site_probe.judge_search(ok, site_probe.title_of(ok), "Dune")[0] == "ok"
    missing = _page("Page not found - HollyMovieHD", "Oops")
    assert site_probe.judge_search(missing, site_probe.title_of(missing), "Dune")[0] == "not_found"
    empty = _page("Search", '<input name="s" value="Dune"><p>Sorry, but nothing matched</p>')
    assert site_probe.judge_search(empty, "Search", "Dune")[0] == "no_results"
    wall = _page("Just a moment...", "checking")
    assert site_probe.judge_search(wall, "Just a moment...", "Dune")[0] == "blocked"


def test_verified_address_is_remembered_per_site(tmp_path, monkeypatch):
    monkeypatch.setattr(site_probe, "CACHE_PATH", str(tmp_path / "site_search.json"))
    monkeypatch.setattr(site_probe, "load",
                        lambda url: (_ for _ in ()).throw(AssertionError("page loaded")))
    site_probe.remember_search("https://www.hollymoviehd.cc", "https://hollymoviehd.cc?s={query}")
    found = site_probe.discover_search("https://hollymoviehd.cc")
    assert found["search_url"] == "https://hollymoviehd.cc?s={query}" and found["cached"]


def test_search_box_echo_is_not_a_result():
    echo = _page("Search", '<input name="s" value="Dune"><p>Latest movies</p>')
    assert site_probe.judge_search(echo, "Search", "Dune")[0] == "unclear"


# ---------------------------------------------------------------- rules_sim --

class FakeProbe:
    """Pages keyed by search template: status for each."""

    def __init__(self, form=None, results=None, blocked=False):
        self.form, self.results, self.blocked = form, results or {}, blocked
        self.searched = []

    def discover_search(self, site_url):
        if self.blocked:
            return {"blocked": True}
        return {"search_url": self.form, "field": "s"} if self.form else None

    def probe_search(self, tpl, sample):
        self.searched.append(tpl)
        status = "blocked" if self.blocked else self.results.get(tpl, "not_found")
        return {"status": status, "evidence": f"fake {status}", "title": "",
                "url": tpl.replace("{query}", sample)}

    def probe_open(self, url):
        return {"status": "ok", "evidence": "page title \"Home\"", "url": url, "title": "Home"}


def _llm(replies=None, seen=None):
    replies = list(replies or [])

    def llm(prompt):
        if seen is not None:
            seen.append(prompt)
        if "Does the simulated result" in prompt:
            return {"match": True, "reason": "It searches the movie site."}, "fake"
        return (replies.pop(0), "fake") if replies else (None, "none")
    return llm


def _search_rule(search_url="https://hollymoviehd.cc/{query}"):
    return {"rule_id": "search_a_movie", "source_phrase": "search a movie in brave",
            "summary": "search hollymoviehd.cc", "enforcement": "action",
            "triggers": ["search a movie in brave"],
            "action": {"type": "search_site", "site": "hollymoviehd.cc",
                       "url": "https://hollymoviehd.cc", "browser": "brave",
                       "slot": "movie name", "sample": "Dune",
                       "search_url": search_url, "search_url_guessed": True}}


def test_site_search_box_replaces_the_ai_guess():
    probe = FakeProbe(form="https://hollymoviehd.cc?s={query}",
                      results={"https://hollymoviehd.cc?s={query}": "ok"})
    rule, report = rules_sim.simulate("brave", _search_rule(), {"category": "browser"},
                                      llm=_llm(), probe=probe)
    assert report["status"] == "verified"
    assert report["url"] == "https://hollymoviehd.cc?s=Dune"
    assert rule["action"]["search_url"] == "https://hollymoviehd.cc?s={query}"
    assert rule["action"]["search_url_verified"] is True
    assert probe.searched == ["https://hollymoviehd.cc?s={query}"]
    assert report["steps"][0] == 'OK "search a movie Dune in brave" matches the rule, name = "Dune"'
    assert report["judge"]["match"] is True


def test_broken_ai_guess_falls_back_to_a_common_pattern():
    probe = FakeProbe(results={"https://hollymoviehd.cc/?s={query}": "ok"})
    rule, report = rules_sim.simulate("brave", _search_rule(), llm=_llm(), probe=probe)
    assert report["status"] == "verified"
    assert probe.searched[0] == "https://hollymoviehd.cc/{query}"  # the AI's guess, 404
    assert any(s.startswith("FAILED https://hollymoviehd.cc/Dune") for s in report["steps"])
    assert rule["action"]["search_url"] == "https://hollymoviehd.cc/?s={query}"


def test_model_repairs_the_address_from_the_evidence():
    fixed = "https://hollymoviehd.cc/find?title={query}"
    seen = []
    probe = FakeProbe(results={fixed: "ok"})
    rule, report = rules_sim.simulate(
        "brave", _search_rule(), llm=_llm([{"search_url": fixed}], seen), probe=probe)
    assert report["status"] == "verified"
    assert rule["action"]["search_url"] == fixed
    assert any("failed" in p and "hollymoviehd.cc/Dune" in p for p in seen)


def test_repair_cannot_move_to_another_site():
    probe = FakeProbe()
    rule, report = rules_sim.simulate(
        "brave", _search_rule(), llm=_llm([{"search_url": "https://evil.example/?q={query}"}]),
        probe=probe)
    assert report["status"] == "failed"
    assert "evil.example" not in json.dumps(rule)


def test_repair_rejects_a_lookalike_domain():
    probe = FakeProbe(results={"https://evilhollymoviehd.cc/?s={query}": "ok"})
    rule, report = rules_sim.simulate(
        "brave", _search_rule(),
        llm=_llm([{"search_url": "https://evilhollymoviehd.cc/?s={query}"}]), probe=probe)
    assert report["status"] == "failed"
    assert "evilhollymoviehd.cc" not in probe.searched[-1]


def test_rule_with_a_switch_for_a_url_never_reaches_a_browser():
    rule = _search_rule()
    rule["action"]["url"] = "--remote-debugging-port=9555"
    probe = FakeProbe()
    _, report = rules_sim.simulate("brave", rule, llm=_llm(), probe=probe)
    assert report["status"] == "failed" and probe.searched == []
    assert rules_engine.action_of(rule, "brave") is None


def test_fetch_dom_refuses_anything_but_a_web_address(monkeypatch):
    monkeypatch.setattr(site_probe, "chrome_path", lambda: "chrome.exe")
    monkeypatch.setattr(site_probe.subprocess, "Popen",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("Chrome started")))
    for bad in ("--remote-debugging-port=9555", "file:///C:/Windows/win.ini", "", "javascript:x"):
        assert site_probe.fetch_dom(bad) == ""


def test_web_url_and_same_site_helpers():
    assert rules_engine.is_web_url("https://hollymoviehd.cc/?s=q")
    assert not rules_engine.is_web_url("--gpu-launcher=calc")
    assert rules_engine.same_site("https://www.hollymoviehd.cc/x", "hollymoviehd.cc")
    assert rules_engine.same_site("https://m.hollymoviehd.cc/x", "hollymoviehd.cc")
    assert not rules_engine.same_site("https://evilhollymoviehd.cc/x", "hollymoviehd.cc")


def test_bot_check_means_unverified_not_failed():
    probe = FakeProbe(blocked=True)
    rule, report = rules_sim.simulate("brave", _search_rule(), llm=_llm(), probe=probe)
    assert report["status"] == "unverified"
    assert "Press Test" in report["steps"][-1]
    assert rule["action"]["search_url_verified"] is False


def test_command_that_cannot_match_fails_before_any_page_load():
    rule = _search_rule()
    rule["source_phrase"], rule["triggers"] = "movies", ["movies"]
    probe = FakeProbe()
    _, report = rules_sim.simulate("brave", rule, llm=_llm(), probe=probe)
    assert report["status"] == "failed" and probe.searched == []


def test_block_rule_shows_what_it_stops():
    rule = {"rule_id": "no_explicit", "source_phrase": "never play explicit",
            "enforcement": "hard", "adapter_check": "pre_play: skip if track.explicit",
            "action": {"type": "block", "applies_to": "play"}}
    _, report = rules_sim.simulate("spotify", rule, llm=_llm(), probe=FakeProbe())
    assert report["status"] == "simulated"
    assert report["steps"] == ["Simulated: open allowed, play BLOCKED, close allowed"]


def test_simulation_shows_in_the_proposal():
    probe = FakeProbe(results={"https://hollymoviehd.cc/?s={query}": "ok"})
    rule, _ = rules_sim.simulate("brave", _search_rule(), llm=_llm(), probe=probe)
    text = rules_ai.propose_markdown("brave", [rule])
    assert "simulation: VERIFIED" in text and "intent check: matches" in text


def test_progress_lifecycle():
    rules_sim.set_progress("brave", "Checking...", 70)
    assert rules_sim.get_progress("brave")["pct"] == 70
    rules_sim.finish_progress("brave")
    assert rules_sim.get_progress("brave")["done"] is True


def test_voice_setup_proposal_carries_the_verdict(tmp_path, monkeypatch):
    path = tmp_path / "app_registry.json"
    path.write_text(json.dumps({"apps": {"brave": {"category": "browser"}}}), encoding="utf-8")
    reply = {"type": "search_site", "site": "hollymoviehd.cc", "slot": "movie name",
             "sample": "Dune", "search_url": "https://hollymoviehd.cc/{query}"}
    monkeypatch.setattr(rules_ai, "_ask_llm", _llm([reply]))
    monkeypatch.setattr(rules_sim, "PROBE",
                        FakeProbe(form="https://hollymoviehd.cc?s={query}",
                                  results={"https://hollymoviehd.cc?s={query}": "ok"}))
    rules_voice.begin_rule_setup("brave", "search a movie in brave", registry_path=str(path))
    step = rules_voice.handle_clarification(
        "brave", rules_voice.ACTION_Q_ID, "search the movie on hollymoviehd.cc",
        registry_path=str(path))
    assert step["status"] == "proposal"
    assert "I tested it for real: https://hollymoviehd.cc?s=Dune works." in step["question"]
    assert step["proposed"][0]["action"]["search_url"] == "https://hollymoviehd.cc?s={query}"
    assert not any("guessed" in n for n in step["notes"])
    assert rules_sim.get_progress("brave")["done"] is True
    rules_voice.cancel_setup("brave")
