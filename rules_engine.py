# Rules Engine - Runtime enforcement of compiled app rules.
# Stdlib only. Consumed by tools.open_application (Phase 3.5).
import json
import os
import sys

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
APP_REGISTRY_PATH = os.path.join(BASE_DIR, "app_registry.json")
WEB_REGISTRY_PATH = os.path.join(BASE_DIR, "web_registry.json")

# enforcement -> gate verdict. "hard" is the legacy spelling of "deny".
_DENY_ENFORCEMENTS = {"deny", "hard", "block"}
_REDIRECT_ENFORCEMENTS = {"redirect"}
_WARN_ENFORCEMENTS = {"soft", "warn", "warning"}


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


# --------------------------------------------------------------------------
# Phase 3 gate: check() — the single entry point a launcher calls.
# --------------------------------------------------------------------------

def _load_json(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def registry_entry(app_key):
    """Look app_key up in app_registry.json (apps), then web_registry.json (sites).

    Returns the entry dict or None. Never raises: an unreadable registry is
    treated as "no rules" so a launch is never blocked by a broken file.
    """
    key = (app_key or "").strip()
    if not key:
        return None
    for path, bucket in ((APP_REGISTRY_PATH, "apps"), (WEB_REGISTRY_PATH, "sites")):
        try:
            items = _load_json(path).get(bucket) or {}
        except Exception:
            continue
        entry = items.get(key) or items.get(key.lower())
        if isinstance(entry, dict):
            return entry
    return None


def _gate_scope(adapter_check):
    """Which action a rule guards, for the check() gate.

    Unlike _rule_action_scope this keeps "play" distinct from "launch": a
    "never play explicit" rule must not stop the app from being opened.
    """
    ac = (adapter_check or "").strip().lower()
    if ac.startswith("pre_close"):
        return "close"
    if ac.startswith("pre_play"):
        return "play"
    if ac.startswith("pre_launch") or ac.startswith("pre_open"):
        return "launch"
    return "all"


def _gate_action(action):
    a = (action or "").strip().lower()
    if a in ("play", "resume"):
        return "play"
    if a in ("close", "quit", "exit", "kill"):
        return "close"
    # check() is a pre-launch gate: an unspecified action is a launch.
    return "launch"


def _rule_phrase(rule):
    return (rule.get("source_phrase") or rule.get("intent")
            or rule.get("adapter_check") or rule.get("rule_id") or "rule")


def _redirect_target(rule):
    for field in ("redirect_key", "redirect_to", "redirect", "target"):
        val = rule.get(field)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return None


def check(app_key, context=None):
    """Runtime gate consulted before opening an app or a site.

    `context` may carry:
      - "action": the verb being attempted ("open"/"play"/"close"). Defaults
        to a launch, since that is what this gate guards.
      - "entry": an already-resolved registry entry, so a caller that just read
        the registry does not pay for a second read.

    Returns one of:
      {"action": "deny", "reason": "Rule <id> blocked launch: <phrase>"}
      {"action": "redirect", "target": "<key>", "reason": "Redirected by rule <id>"}
      {"action": "allow", "warning": "<phrase>"}
      {"action": "allow"}

    Fails open: any unexpected error yields {"action": "allow"}.
    """
    try:
        context = context or {}
        entry = context.get("entry")
        if not isinstance(entry, dict):
            entry = registry_entry(app_key)
        if not isinstance(entry, dict):
            return {"action": "allow"}

        rules = entry.get("compiled_rules") or []
        if not isinstance(rules, list):
            return {"action": "allow"}

        acting = _gate_action(context.get("action"))
        redirect = None
        warnings = []

        for rule in rules:
            if not isinstance(rule, dict):
                continue
            scope = _gate_scope(rule.get("adapter_check"))
            if scope != "all" and scope != acting:
                continue
            rule_id = rule.get("rule_id", "?")
            enforcement = (rule.get("enforcement") or "soft").strip().lower()

            if enforcement in _DENY_ENFORCEMENTS:
                return {
                    "action": "deny",
                    "reason": f"Rule {rule_id} blocked launch: {_rule_phrase(rule)}",
                }
            if enforcement in _REDIRECT_ENFORCEMENTS:
                target = _redirect_target(rule)
                if target:
                    if redirect is None:
                        redirect = {
                            "action": "redirect",
                            "target": target,
                            "reason": f"Redirected by rule {rule_id}",
                        }
                    continue
                # A redirect with nowhere to go degrades to a warning rather
                # than silently doing nothing.
                warnings.append(_rule_phrase(rule))
            elif enforcement in _WARN_ENFORCEMENTS:
                warnings.append(_rule_phrase(rule))

        if redirect:
            return redirect
        if warnings:
            return {"action": "allow", "warning": "; ".join(warnings)}
        return {"action": "allow"}
    except Exception:
        return {"action": "allow"}


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
