# Rules Engine - Runtime enforcement of compiled app rules.
# Stdlib only. Consumed by tools.open_application (Phase 3.5).
import sys


def _rule_action_scope(adapter_check):
    """Infer which action a rule guards from its adapter_check prefix.

    "pre_play" / "pre_launch" -> launch/play actions.
    "pre_close"               -> close actions.
    None/empty                -> all actions (conservative: a hard rule with no
                                 scope still blocks, matching prior behavior).
    """
    ac = (adapter_check or "").lower()
    if ac.startswith("pre_close"):
        return "close"
    if ac.startswith("pre_play") or ac.startswith("pre_launch"):
        return "launch"
    return "all"


def evaluate_app_rules(entry, action):
    """Evaluate an app's compiled_rules before a launch/close action.

    Action-aware: a hard rule scoped to "play" blocks launch/play but NOT
    close, and a hard "pre_close" rule blocks close but not launch. This keeps
    e.g. the demo Spotify "never play explicit" rule from accidentally
    preventing "close Spotify".

    Returns {"allowed": bool, "notices": [str], "blocks": [str]}.
    """
    if not isinstance(entry, dict):
        return {"allowed": True, "notices": [], "blocks": []}

    rules = entry.get("compiled_rules") or []
    if not isinstance(rules, list):
        rules = []

    allowed = True
    notices = []
    blocks = []

    for rule in rules:
        if not isinstance(rule, dict):
            continue
        rule_id = rule.get("rule_id", "?")
        intent = rule.get("intent", "")
        enforcement = rule.get("enforcement", "soft")
        adapter_check = rule.get("adapter_check", "")

        if enforcement == "hard":
            scope = _rule_action_scope(adapter_check)
            # Normalize the incoming action to the scope vocabulary
            # {all, launch, close}: "play"/"open" are launch actions,
            # "close"/"quit" are close actions.
            a = (action or "").lower()
            if a in ("play", "open", "launch", "start"):
                a = "launch"
            elif a in ("close", "quit", "exit", "kill"):
                a = "close"
            # A scoped hard rule only blocks its own action; otherwise it is
            # irrelevant to THIS action and must not block it.
            if scope != "all" and scope != a:
                continue
            allowed = False
            blocks.append(
                f"[BLOCKED by rule {rule_id}] {intent}. Hard rule — cannot be "
                f"overridden by a voice command. Edit the app rules in settings "
                f"to allow it."
            )
        elif enforcement == "soft":
            notices.append(
                f"Reminder ({rule_id}): {adapter_check or intent}"
            )

    return {"allowed": allowed, "notices": notices, "blocks": blocks}


def format_rule_notices(notices):
    """Join a list of notice strings into a newline-separated string."""
    if not notices:
        return ""
    return "\n".join(notices)


if __name__ == "__main__":
    # (a) hard + soft rules
    hard_entry = {
        "compiled_rules": [
            {
                "rule_id": "r1",
                "intent": "never play explicit even if I ask",
                "enforcement": "hard",
                "adapter_check": "pre_play:explicit",
            },
            {
                "rule_id": "r2",
                "intent": "keep it quiet",
                "enforcement": "soft",
                "adapter_check": "cap volume at 60%",
            },
        ]
    }
    res_a = evaluate_app_rules(hard_entry, "play")
    print("a)", res_a)
    assert res_a["allowed"] is False
    assert res_a["blocks"]

    # (b) only soft rule
    soft_entry = {
        "compiled_rules": [
            {
                "rule_id": "r3",
                "intent": "keep it quiet",
                "enforcement": "soft",
                "adapter_check": "cap volume at 60%",
            }
        ]
    }
    res_b = evaluate_app_rules(soft_entry, "play")
    print("b)", res_b)
    assert res_b["allowed"] is True
    assert res_b["notices"]

    # (c) no entry
    res_c = evaluate_app_rules(None, "open")
    print("c)", res_c)
    assert res_c["allowed"] is True
    assert not res_c["notices"]
    assert not res_c["blocks"]

    print("\nAll runtime-enforcement demos passed.")
