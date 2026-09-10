"""rules_voice.py — Phase 4: voice-driven rule authoring on top of rules_compiler.

The voice flow is a small state machine, one pending setup per app key:

    begin_rule_setup(app_key, phrase)
        -> {"status": "clarify",  "rule_id": ..., "question": ...}   ambiguous phrase
        -> {"status": "proposal", "proposal": <markdown>, ...}       explicit phrase
    handle_clarification(app_key, rule_id, answer)
        -> {"status": "clarify", ...}    another clause still needs an answer
        -> {"status": "committed", ...}  finalized set written to the registry
        -> {"status": "cancelled"}       user backed out
    list_rules(app_key)
        -> {"status": "ok", "rules": [...], "summary": <human readable text>}
    forget_rule(app_key, rule_id=None)
        -> {"status": "removed" | "ok", "rules": [...remaining]}   one rule, or all

`rule_id` is optional on handle_clarification: an empty/None id (or "confirm")
means "accept what you proposed", which is how the explicit-phrase path commits.

Like rules_compiler, this module runs only when the USER SETS UP rules. There is
no per-command / runtime hook.
"""

import json
import os

import rules_compiler as rc

CONFIG_TIME_ONLY = True  # setup-time only, never per spoken command.

# app_key -> {"phrase": str, "candidate_rules": [...], "answers": {rule_id: str}}
_PENDING = {}

_CONFIRM_IDS = {"", "confirm", "*", "all"}
_NEGATIVE = {"no", "nope", "cancel", "stop", "nevermind", "never mind", "forget it", "abort"}


# ------------------------------------------------------------------ helpers --

def _registry_path(registry_path=None):
    """Resolve the registry lazily so tests can repoint machine_capabilities."""
    if registry_path:
        return registry_path
    import machine_capabilities
    return machine_capabilities.REGISTRY_PATH


def _load_registry(registry_path=None):
    path = _registry_path(registry_path)
    if not path or not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f) or {}
    except (OSError, ValueError):
        return {}


def _next_open_question(candidates, answers):
    """First candidate rule that still needs an answer, else None."""
    for rule in candidates:
        if rule.get("needs_clarification") and not answers.get(rule["rule_id"]):
            return rule
    return None


def _escalate(rules):
    """Promote 'never / even if I ask' rules to hard enforcement (Phase 3.5 guard)."""
    for r in rules:
        if rc.detect_hard_escalation(r) and r.get("enforcement") != "hard":
            r["enforcement"] = "hard"
            r["intent"] = (r.get("intent") or "") + \
                " (hard-escalated: user said never/even if I ask)"
    return rules


def _finalize(app_key, pending):
    finalized = rc.apply_clarifications(
        app_key, pending["candidate_rules"], pending["answers"])
    return _escalate(finalized)


def _clarify_step(app_key, pending, rule):
    """The wire shape for 'one question is still open'."""
    open_count = sum(
        1 for r in pending["candidate_rules"]
        if r.get("needs_clarification") and not pending["answers"].get(r["rule_id"])
    )
    return {
        "status": "clarify",
        "key": app_key,
        "rule_id": rule["rule_id"],
        "question": rule.get("clarification_question") or
                    f"What exactly should happen for: \"{rule['source_phrase']}\"?",
        "remaining": open_count,
    }


def _rule_line(idx, r):
    bits = [f"{idx}. {r.get('rule_id', 'rule')} — {r.get('intent', '')}",
            f"[{r.get('enforcement', 'soft')} · {r.get('scope', 'all_sessions')}]"]
    if r.get("adapter_check"):
        bits.append("· check: " + r["adapter_check"])
    return " ".join(bits)


def pending_setup(app_key):
    """The in-flight setup for an app, or None. Exposed for tests / the HUD."""
    return _PENDING.get(app_key)


def cancel_setup(app_key):
    """Drop any in-flight setup. Returns True if there was one."""
    return _PENDING.pop(app_key, None) is not None


# -------------------------------------------------------------------- flow --

def begin_rule_setup(app_key, phrase, registry_path=None):
    """Start voice rule authoring for `app_key` from a spoken `phrase`.

    Returns a `clarify` step when any clause is ambiguous, otherwise a
    `proposal` the user still has to accept (nothing is written here — the
    user owns the commit, which happens in handle_clarification).
    """
    app_key = (app_key or "").strip()
    phrase = (phrase or "").strip()
    if not app_key:
        return {"status": "error", "error": "missing app_key"}
    if not phrase:
        return {"status": "error", "error": "missing phrase"}

    parsed = rc.parse_scaffold(app_key, phrase)
    pending = {
        "phrase": phrase,
        "candidate_rules": parsed["candidate_rules"],
        "answers": {},
    }
    _PENDING[app_key] = pending

    open_rule = _next_open_question(pending["candidate_rules"], pending["answers"])
    if open_rule:
        return {
            "status": "clarify",
            "key": app_key,
            "rule_id": open_rule["rule_id"],
            "question": open_rule.get("clarification_question") or
                        f"What exactly should happen for: \"{open_rule['source_phrase']}\"?",
            "remaining": sum(
                1 for r in pending["candidate_rules"]
                if r.get("needs_clarification") and not pending["answers"].get(r["rule_id"])
            ),
            "candidate_rules": pending["candidate_rules"],
        }

    proposed = _finalize(app_key, pending)
    pending["proposed"] = proposed
    return {
        "status": "proposal",
        "key": app_key,
        "proposed": proposed,
        "proposal": rc.propose_ruleset(app_key, proposed),
        "question": "Accept these rules? Say yes to commit.",
        "rule_id": "",
    }


def handle_clarification(app_key, rule_id, answer, registry_path=None):
    """Answer one clarification question — or confirm a finished proposal.

    Commits the finalized rule set to the registry once nothing is open.
    """
    app_key = (app_key or "").strip()
    answer = (answer or "").strip()
    pending = _PENDING.get(app_key)
    if not pending:
        return {"status": "error", "error": "no rule setup in progress",
                "key": app_key}
    if not answer:
        return {"status": "error", "error": "missing answer", "key": app_key}

    if answer.lower() in _NEGATIVE:
        _PENDING.pop(app_key, None)
        return {"status": "cancelled", "key": app_key}

    rid = (rule_id or "").strip()
    if rid.lower() not in _CONFIRM_IDS:
        known = {r["rule_id"] for r in pending["candidate_rules"]}
        if rid not in known:
            return {"status": "error", "error": "unknown rule_id",
                    "key": app_key, "rule_id": rid}
        pending["answers"][rid] = answer

    open_rule = _next_open_question(pending["candidate_rules"], pending["answers"])
    if open_rule:
        return {
            "status": "clarify",
            "key": app_key,
            "rule_id": open_rule["rule_id"],
            "question": open_rule.get("clarification_question") or
                        f"What exactly should happen for: \"{open_rule['source_phrase']}\"?",
            "remaining": sum(
                1 for r in pending["candidate_rules"]
                if r.get("needs_clarification") and not pending["answers"].get(r["rule_id"])
            ),
        }

    finalized = _finalize(app_key, pending)
    rc.commit_rules(app_key, finalized, _registry_path(registry_path), accept=True)
    _PENDING.pop(app_key, None)
    return {
        "status": "committed",
        "key": app_key,
        "count": len(finalized),
        "rules": finalized,
        "proposal": rc.propose_ruleset(app_key, finalized),
    }


def list_rules(app_key, registry_path=None):
    """Human-readable summary of the rules currently committed for an app."""
    app_key = (app_key or "").strip()
    if not app_key:
        return {"status": "error", "error": "missing app_key"}

    data = _load_registry(registry_path)
    entry = (data.get("apps") or {}).get(app_key) or {}
    rules = entry.get("compiled_rules") or []
    if not rules:
        return {"status": "ok", "key": app_key, "count": 0, "rules": [],
                "summary": f"No rules set for {app_key} yet."}

    lines = [f"Rules for {app_key} ({len(rules)}):"]
    lines += [_rule_line(i, r) for i, r in enumerate(rules, 1)]
    return {"status": "ok", "key": app_key, "count": len(rules),
            "rules": rules, "summary": "\n".join(lines)}


def forget_rule(app_key, rule_id=None, registry_path=None):
    """Remove one committed rule, or every rule for the app when `rule_id` is empty."""
    app_key = (app_key or "").strip()
    rid = (rule_id or "").strip()
    if not app_key:
        return {"status": "error", "error": "missing app_key"}
    try:
        removed, remaining = rc.remove_rule(app_key, rid, _registry_path(registry_path))
    except KeyError:
        return {"status": "error", "error": "unknown app", "key": app_key}
    if rid and not removed:
        return {"status": "error", "error": "unknown rule_id", "key": app_key, "rule_id": rid}
    return {"status": "removed" if removed else "ok", "key": app_key,
            "count": len(remaining), "rules": remaining}


if __name__ == "__main__":
    import tempfile

    tmp = os.path.join(tempfile.mkdtemp(prefix="rules_voice_"), "app_registry.json")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"apps": {"spotify": {"name": "Spotify"}}}, f)

    step = begin_rule_setup("spotify", "no explicit stuff and keep it quiet",
                            registry_path=tmp)
    print("STEP 1:", step["status"], step.get("question"))
    step = handle_clarification("spotify", step["rule_id"],
                                "under 40% volume all sessions", registry_path=tmp)
    print("STEP 2:", step["status"], step.get("count"))
    print(list_rules("spotify", registry_path=tmp)["summary"])
