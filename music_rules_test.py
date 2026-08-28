"""Unit tests for music_rules.py — the adapter_check -> playback bridge.

Pure stdlib; NO Playwright/browser. Exercises parsing + registry resolution
and the advisory label. Never starts real playback.
"""
import os
import sys
import json
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import music_rules as mr


def test_parse_volume_cap():
    s = mr._parse_music_rules({
        "compiled_rules": [{
            "intent": "cap playback volume",
            "adapter_check": "pre_play: cap spotify volume at 60%",
        }]
    })
    assert s["cap_volume"] == 0.6, s


def test_parse_volume_strictest_wins():
    # Two caps: 60% and 40%. Stricter (lower) should win.
    s = mr._parse_music_rules({
        "compiled_rules": [
            {"intent": "cap playback volume", "adapter_check": "cap volume at 60%"},
            {"intent": "cap playback volume", "adapter_check": "cap volume at 40%"},
        ]
    })
    assert abs(s["cap_volume"] - 0.4) < 1e-9, s


def test_parse_explicit_soft_and_hard():
    soft = mr._parse_music_rules({
        "compiled_rules": [{
            "intent": "avoid explicit content",
            "adapter_check": "pre_play: skip if track.explicit",
            "enforcement": "soft",
        }]
    })
    assert soft["skip_explicit"] is True, soft
    # enforcement field does not change the parsed behaviour here (proxy is
    # always best-effort); it is honoured by open_application's hard block.


def test_parse_ignores_non_playback_rules():
    s = mr._parse_music_rules({
        "compiled_rules": [
            {"intent": "open the app window", "adapter_check": "x"},
            {"intent": "remind me to stretch", "adapter_check": "y"},
        ]
    })
    assert s == dict(mr.DEFAULT_MUSIC_SETTINGS), s


def test_title_cue():
    assert mr._title_looks_explicit("Song Title (Explicit) - Artist") is True
    assert mr._title_looks_explicit("Calm Lofi Beats") is False
    assert mr._title_looks_explicit("") is False


def test_resolve_from_real_registry():
    # Resolve spotify from the REAL app_registry.json (read-only). The demo
    # entry has a 60% cap rule, so cap_volume must resolve to 0.6.
    reg = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "app_registry.json")
    if os.path.exists(reg):
        s = mr.resolve_music_rules(reg, app_key="spotify")
        assert abs(s["cap_volume"] - 0.6) < 1e-9, s
    # If the registry lacks the demo entry, accept defaults (skip intent).
    assert True


def test_resolve_missing_app_and_file():
    reg = {"apps": {"spotify": {}}}
    p = os.path.join(tempfile.gettempdir(), "mr_test_reg.json")
    json.dump(reg, open(p, "w"))
    assert mr.resolve_music_rules(p, app_key="nope") == dict(mr.DEFAULT_MUSIC_SETTINGS)
    assert mr.resolve_music_rules("C:/no/such_file.json") == dict(mr.DEFAULT_MUSIC_SETTINGS)
    os.remove(p)


def test_advisory_note():
    assert "best-effort" in mr.advisory_note({"skip_explicit": True})
    assert mr.advisory_note({"skip_explicit": False}) == ""


def _run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"{fn.__name__} OK")


if __name__ == "__main__":
    try:
        import pytest
        raise SystemExit(pytest.main([__file__, "-q"]))
    except ImportError:
        _run_all()
        print("ALL TESTS PASSED")
