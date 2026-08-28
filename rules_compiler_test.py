"""rules_compiler_test.py — tests for rules_compiler (uses tempfile, never the real registry)."""

import json
import tempfile
from pathlib import Path

import rules_compiler as rc


def test_clear_clause():
    parsed = rc.parse_scaffold("spotify", "no explicit songs")
    assert parsed["needs_clarification"] is False, "clear clause must not need clarification"
    assert len(parsed["candidate_rules"]) == 1
    r = parsed["candidate_rules"][0]
    assert r["needs_clarification"] is False
    assert r["clarification_question"] is None
    print("test_clear_clause: PASS")


def test_ambiguous_clause():
    parsed = rc.parse_scaffold("spotify", "keep it quiet")
    assert parsed["needs_clarification"] is True, "ambiguous clause must need clarification"
    assert len(parsed["questions"]) > 0, "ambiguous clause must produce a question"
    print("test_ambiguous_clause: PASS")


def test_hard_escalation():
    rule = rc.parse_scaffold("spotify", "never play explicit even if I ask")["candidate_rules"][0]
    assert rc.detect_hard_escalation(rule) is True, "never + even if I ask must be a hard block"
    print("test_hard_escalation: PASS")


def _run():
    test_clear_clause()
    test_ambiguous_clause()
    test_hard_escalation()
    print("\nALL TESTS PASSED")


if __name__ == "__main__":
    try:
        import pytest  # noqa: F401

        raise SystemExit(0 if __import__("pytest").main([__file__]) == 0 else 1)
    except ImportError:
        _run()
