"""A rescan refreshes what apps are installed and never deletes what the user
set on them (rules, drafts, switches, registration, hidden flags)."""

import json

import machine_capabilities as mc

RULE = {"rule_id": "search_movie", "source_phrase": "search movie", "enforcement": "action"}


def _setup(tmp_path, monkeypatch, old_apps, scanned):
    reg = tmp_path / "app_registry.json"
    reg.write_text(json.dumps({"apps": old_apps}), encoding="utf-8")
    monkeypatch.setattr(mc, "REGISTRY_PATH", str(reg))
    monkeypatch.setattr(mc, "MANIFEST_PATH", str(tmp_path / "capabilities.json"))
    monkeypatch.setattr(mc, "scan", lambda: {k: dict(v) for k, v in scanned.items()})
    monkeypatch.setattr(mc, "_is_launchable", lambda path: True)
    return reg


def test_rescan_keeps_rules_switches_and_flags(tmp_path, monkeypatch):
    old = {"brave": {"name": "brave", "bin": "C:/old/brave.exe", "version": "1",
                     "compiled_rules": [RULE], "rule_drafts": ["search movie"],
                     "enabled": False, "registered": True},
           "asusupdater": {"name": "asusupdater", "bin": "C:/a.exe", "hidden": True}}
    scanned = {"brave": {"name": "brave", "bin": "C:/new/brave.exe", "version": "2"},
               "asusupdater": {"name": "asusupdater", "bin": "C:/a.exe"},
               "spotify": {"name": "spotify", "bin": "C:/spotify.exe"}}
    reg = _setup(tmp_path, monkeypatch, old, scanned)
    mc.write_registry()
    apps = json.loads(reg.read_text(encoding="utf-8"))["apps"]
    assert apps["brave"]["bin"] == "C:/new/brave.exe" and apps["brave"]["version"] == "2"
    assert apps["brave"]["compiled_rules"] == [RULE]
    assert apps["brave"]["rule_drafts"] == ["search movie"]
    assert apps["brave"]["enabled"] is False and apps["brave"]["registered"] is True
    assert apps["asusupdater"]["hidden"] is True
    assert "spotify" in apps


def test_rescan_keeps_what_jarvis_learned(tmp_path, monkeypatch):
    learned = {"app_kind": "media", "deep_scan": {"at": "2026-09-11 10:00", "found": 3},
               "pinned_by": "scan", "hidden_by": "user"}
    old = {"vlc": dict({"name": "vlc", "bin": "C:/vlc.exe"}, **learned)}
    reg = _setup(tmp_path, monkeypatch, old, {"vlc": {"name": "vlc", "bin": "C:/vlc.exe"}})
    mc.write_registry()
    apps = json.loads(reg.read_text(encoding="utf-8"))["apps"]
    assert {k: apps["vlc"].get(k) for k in learned} == learned


def test_an_app_with_rules_survives_a_scan_that_misses_it(tmp_path, monkeypatch):
    old = {"portable_tool": {"name": "portable_tool", "bin": "D:/tool.exe",
                             "compiled_rules": [RULE]},
           "gone_app": {"name": "gone_app", "bin": "C:/gone.exe"}}
    reg = _setup(tmp_path, monkeypatch, old, {})
    mc.write_registry()
    apps = json.loads(reg.read_text(encoding="utf-8"))["apps"]
    assert apps["portable_tool"]["compiled_rules"] == [RULE]
    assert "gone_app" not in apps  # uninstalled, nothing of the user's on it
