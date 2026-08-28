import sys

try:
    import pytest
except ImportError:
    pytest = None

from rules_engine import evaluate_app_rules


def _hard_entry():
    return {"compiled_rules": [
        {"rule_id": "x", "intent": "never explicit",
         "enforcement": "hard", "adapter_check": "pre_play: skip if explicit",
         "scope": "all_sessions", "source_phrase": "never",
         "needs_clarification": False, "clarification_question": None}
    ]}


def test_hard_blocks():
    v = evaluate_app_rules(_hard_entry(), "play")
    assert v["allowed"] is False
    assert v["blocks"]
    print("test_hard_blocks OK:", v)


def test_soft_notice():
    e = _hard_entry()
    e["compiled_rules"][0]["enforcement"] = "soft"
    v = evaluate_app_rules(e, "play")
    assert v["allowed"] is True
    assert v["notices"]
    assert not v["blocks"]
    print("test_soft_notice OK:", v)


def test_no_rules():
    v1 = evaluate_app_rules(None, "play")
    v2 = evaluate_app_rules({}, "play")
    for v in (v1, v2):
        assert v["allowed"] is True
        assert not v["notices"]
        assert not v["blocks"]
    print("test_no_rules OK:", v1, v2)


def test_action_scope_play_rule_does_not_block_close():
    # Demo Spotify rule is pre_play + hard. It must block "play" but NOT "close".
    e = _hard_entry()  # pre_play: skip if explicit, hard
    v_play = evaluate_app_rules(e, "play")
    assert v_play["allowed"] is False, v_play
    v_close = evaluate_app_rules(e, "close")
    assert v_close["allowed"] is True, v_close
    assert not v_close["blocks"]
    print("test_action_scope_play_rule_does_not_block_close OK:", v_play, v_close)


def test_action_scope_close_guard_blocks_close():
    e = {"compiled_rules": [
        {"rule_id": "c1", "intent": "never close this app",
         "enforcement": "hard", "adapter_check": "pre_close: refuse voice close",
         "scope": "all_sessions", "source_phrase": "never close",
         "needs_clarification": False, "clarification_question": None}
    ]}
    v_close = evaluate_app_rules(e, "close")
    assert v_close["allowed"] is False, v_close
    assert v_close["blocks"]
    # Same guard must NOT block launch/play.
    v_play = evaluate_app_rules(e, "play")
    assert v_play["allowed"] is True, v_play
    print("test_action_scope_close_guard_blocks_close OK:", v_close, v_play)


if __name__ == "__main__":
    test_hard_blocks()
    test_soft_notice()
    test_no_rules()
    test_action_scope_play_rule_does_not_block_close()
    test_action_scope_close_guard_blocks_close()
    print("ALL TESTS PASSED")
