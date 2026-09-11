"""Phase 2-3: the conversation state, learned preferences (asked before saved),
and the "Deep scan all" background job. Files live in tmp_path."""

import json
import time

import pytest

import app_abilities as aa
import dialogue_state as ds
import preferences as prefs


@pytest.fixture(autouse=True)
def fresh(tmp_path, monkeypatch):
    ds.reset()
    monkeypatch.setattr(prefs, "_PATH", str(tmp_path / "preferences.json"))
    yield
    ds.reset()


# ------------------------------------------------------- conversation state --

def test_open_question_expires_and_filters_by_kind(monkeypatch):
    ds.ask("slot", "Which movie, sir?")
    assert ds.pending()["question"] == "Which movie, sir?"
    assert ds.pending("preference") is None
    monkeypatch.setitem(ds._STATE["pending"], "ts", time.time() - ds.PENDING_TTL - 1)
    assert ds.pending() is None


def test_snapshot_carries_the_last_result_and_recent_turns(monkeypatch):
    monkeypatch.setattr(ds, "active_app", lambda: "brave")
    ds.remember_result("results", url="https://x.cc?s=Dune", app="brave", slot="Dune", secret="no")
    ds.record_turn("search movie Dune", "Searching x.cc for Dune in Brave.", {"type": "rule"})
    snap = ds.snapshot()
    assert snap["active_app"] == "brave"
    assert snap["last_result"] == {"kind": "results", "url": "https://x.cc?s=Dune",
                                   "app": "brave", "slot": "Dune"}
    assert snap["recent_turns"] == [{"user": "search movie Dune",
                                     "reply": "Searching x.cc for Dune in Brave."}]


# -------------------------------------------------------------- preferences --

CHOICE = {"site": "hollymoviehd.cc", "browser": "brave"}


def test_a_default_is_suggested_after_a_streak_and_saved_only_on_yes():
    assert prefs.observe("movie search", CHOICE) is None
    assert prefs.observe("movie search", CHOICE) is None
    q = prefs.observe("movie search", CHOICE)
    assert q == "You always use hollymoviehd.cc in Brave for movie search. Make that the default?"
    assert prefs.default("movie search") is None           # not saved until you agree
    assert prefs.observe("movie search", CHOICE) is None   # asked once only
    assert prefs.confirm("movie search") == CHOICE
    assert prefs.all_defaults() == {"movie search": CHOICE}


def test_a_different_choice_breaks_the_streak_and_decline_stops_asking():
    prefs.observe("movie search", CHOICE)
    prefs.observe("movie search", CHOICE)
    assert prefs.observe("movie search", {"site": "other.cc"}) is None
    for _ in range(2):
        prefs.observe("song search", {"app": "spotify"})
    assert prefs.observe("song search", {"app": "spotify"}) is not None
    prefs.decline("song search")
    assert prefs.default("song search") is None
    assert prefs.forget("song search") is True


# ------------------------------------------------------------ deep scan all --

@pytest.fixture
def registry(tmp_path, monkeypatch):
    import machine_capabilities as mc
    path = tmp_path / "app_registry.json"
    monkeypatch.setattr(mc, "REGISTRY_PATH", str(path))
    path.write_text(json.dumps({"apps": {
        "figma": {"name": "figma", "registered": True, "app_kind": "editor"},
        "vlc": {"name": "vlc", "registered": True, "app_kind": "media",
                "deep_scan": {"at": "2026-09-11 10:00"}},
        "asusx": {"name": "asusx", "registered": True, "app_kind": "system"},
        "slack": {"name": "slack", "registered": True, "app_kind": "chat"}}}), encoding="utf-8")
    return path


def test_deep_scan_all_skips_scanned_and_system_apps(registry, monkeypatch):
    scanned = []
    monkeypatch.setattr(aa, "scan_app", lambda key, launch=False, progress=None:
                        scanned.append((key, launch)) or {"found": 3})
    out = aa.deep_scan_all()
    assert sorted(scanned) == [("figma", True), ("slack", True)]
    assert out == {"total": 2, "scanned": 2, "failed": 0, "cancelled": False, "left": 0}
    assert aa.scan_all_status()["running"] is False


def test_only_one_deep_scan_all_at_a_time(registry, monkeypatch):
    assert aa.claim_scan_all() is True
    assert aa.claim_scan_all() is False                    # a second click
    assert "already running" in aa.deep_scan_all()["error"]
    monkeypatch.setattr(aa, "scan_app", lambda key, launch=False, progress=None: {"found": 1})
    assert aa.deep_scan_all(claimed=True)["scanned"] == 2  # the holder runs normally
    assert aa.claim_scan_all() is True                     # and frees the slot when done
    aa._SCAN_ALL["running"] = False


def test_deep_scan_all_can_be_cancelled_and_resumed(registry, monkeypatch):
    calls = []

    def scan(key, launch=False, progress=None):
        calls.append(key)
        aa.cancel_scan_all()               # the user presses Cancel during the first app
        return {"error": "window never opened"}
    monkeypatch.setattr(aa, "scan_app", scan)
    out = aa.deep_scan_all()
    assert out["cancelled"] is True and out["failed"] == 1 and out["left"] == 1
    assert len(calls) == 1
