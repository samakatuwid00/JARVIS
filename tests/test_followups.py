"""Follow-ups after a rule ("play it", "open number 2"), Chrome opens without
Playwright, and a Test box that fits the rule. Browser and pages are faked."""

import json
import time

import pytest

import browser_cdp
import rules_steps
import rules_voice

RESULTS = """<a href="https://hollymoviehd.cc/the-social-network-2010/" title="The Social Network (2010)">x</a>
<a href="https://hollymoviehd.cc/social-network-doc/" title="Social Network: The Documentary">y</a>"""
SEARCH_RULE = {"rule_id": "search_movie", "source_phrase": "search movie", "enforcement": "action",
               "triggers": ["search movie"],
               "action": {"type": "search_site", "site": "hollymoviehd.cc",
                          "url": "https://hollymoviehd.cc", "browser": "brave",
                          "search_url": "https://hollymoviehd.cc?s={query}", "slot": "movie name"}}
OPEN_RULE = {"rule_id": "open_my_facebook", "source_phrase": "open my facebook",
             "action": {"type": "open_site", "site": "facebook.com", "url": "https://facebook.com",
                        "browser": "chrome"}}


@pytest.fixture
def world(tmp_path, monkeypatch):
    import audit
    import rules_engine
    import site_probe
    import tools
    calls = []
    path = tmp_path / "app_registry.json"
    path.write_text(json.dumps({"apps": {"brave": {"category": "browser",
                                                   "compiled_rules": [SEARCH_RULE]}}}))
    monkeypatch.setattr(rules_engine, "APP_REGISTRY_PATH", str(path))
    monkeypatch.setattr(audit, "log_call", lambda *a, **k: None)
    monkeypatch.setattr(site_probe, "load", lambda url: {"dom": RESULTS, "blocked": False})
    monkeypatch.setattr(browser_cdp, "is_attached", lambda b: True)
    monkeypatch.setattr(browser_cdp, "open_tab",
                        lambda b, url: calls.append(("tab", b, url)) or {"id": "t"})
    monkeypatch.setattr(browser_cdp, "click",
                        lambda b, target, url_hint=None: calls.append(("click", b, url_hint))
                        or "Pressed play in Brave. It's playing.")
    monkeypatch.setattr(tools, "_open_url_in_browser",
                        lambda url, name, browser=None, remember=True:
                        calls.append(("open", browser, url)) or f"Opened {name}.")
    monkeypatch.setattr(rules_steps, "_LAST", {})
    tools._PENDING_RULE_SLOT.clear()
    return tools, calls


def test_play_it_after_a_search_lists_picks_then_plays_the_choice(world):
    tools, calls = world
    assert tools.try_rule_action("search movie The Social Network").startswith("Searching")
    out = tools.try_rule_action("Now play it.")
    # the spoken name loses its leading "The"; ranking ignores articles
    assert out.startswith("Top picks for Social Network: 1. The Social Network (2010).")
    out = tools.try_rule_action("the first one")
    assert out == "Opened The Social Network (2010) in Brave. Pressed play in Brave. It's playing."
    assert calls[-1] == ("click", "brave", "https://hollymoviehd.cc/the-social-network-2010/")


def test_play_the_first_one_needs_no_question(world):
    tools, calls = world
    tools.try_rule_action("search movie The Social Network")
    out = tools.try_rule_action("play the first one")
    assert out == "Opened The Social Network (2010) in Brave. Pressed play in Brave. It's playing."


def test_open_number_two_opens_without_playing(world):
    tools, calls = world
    tools.try_rule_action("search movie The Social Network")
    out = tools.try_rule_action("open number 2")
    assert out == "Opened Social Network: The Documentary."
    assert not any(c[0] == "click" for c in calls)


def test_play_it_on_an_open_title_just_presses_play(world):
    tools, calls = world
    rules_steps.remember("page", {"rule": SEARCH_RULE, "owner": "brave",
                                  "action": SEARCH_RULE["action"], "slot": "x"},
                         url="https://hollymoviehd.cc/dune-1984/", title="Dune (1984)")
    assert tools.try_rule_action("play it") == "Pressed play in Brave. It's playing."


def test_follow_ups_expire_and_need_a_reference(world, monkeypatch):
    tools, _ = world
    tools.try_rule_action("search movie The Social Network")
    assert not rules_steps.is_follow_up("play some jazz music for me tonight")
    monkeypatch.setitem(rules_steps._LAST, "ts", time.time() - rules_steps.LAST_TTL - 1)
    assert not rules_steps.is_follow_up("play it")


# ------------------------------------------------------------ Chrome opens --

def test_chrome_open_uses_the_control_port_first(monkeypatch):
    import tools
    monkeypatch.setattr(browser_cdp, "is_attached", lambda b: True)
    monkeypatch.setattr(browser_cdp, "open_tab", lambda b, url: {"id": "t"})
    monkeypatch.setattr("browser_agent.open_site",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("Playwright used")))
    assert tools._open_in_chrome("https://facebook.com", "facebook") == "Opened facebook in Chrome."


def test_chrome_open_uses_the_users_own_chrome_not_the_driver(monkeypatch, tmp_path):
    import tools
    launched = []
    exe = tmp_path / "chrome.exe"
    exe.write_text("")
    monkeypatch.setattr(browser_cdp, "is_attached", lambda b: False)
    # the driver could land in a signed-out JARVIS profile: never used when Chrome exists
    monkeypatch.setattr("browser_agent.open_site",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("driver used")))
    monkeypatch.setattr(tools, "_load_app_registry", lambda: {"chrome": {"bin": str(exe)}})
    monkeypatch.setattr(tools.subprocess, "Popen", lambda cmd, **k: launched.append(cmd))
    assert tools._open_in_chrome("https://facebook.com", "facebook") == "Opened facebook in Chrome."
    assert launched == [[str(exe), "https://facebook.com"]]
    launched.clear()
    assert tools._open_in_chrome("--gpu-launcher=calc", "x").startswith("[Error]")
    assert launched == []


# -------------------------------------------------------------- Test box --

def test_test_box_fits_the_rule():
    assert rules_voice._test_hint(OPEN_RULE) == {"needs_sample": False,
                                                 "test_label": "Test: open it now"}
    hint = rules_voice._test_hint(SEARCH_RULE)
    assert hint["needs_sample"] is True
    assert hint["sample_placeholder"] == "Try it with a movie, e.g. Inception"
