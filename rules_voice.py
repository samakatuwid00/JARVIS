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
import re

import rules_ai
import rules_compiler as rc
import rules_sim

CONFIG_TIME_ONLY = True  # setup-time only, never per spoken command.

# app_key -> {"phrase": str, "candidate_rules": [...], "answers": {rule_id: str}}
# Command rules ("search movies in brave") add "mode": "action", "stage",
# "action_text", "feedback" and "proposed_rule".
_PENDING = {}

_CONFIRM_IDS = {"", "confirm", "*", "all"}
_NEGATIVE = {"no", "nope", "cancel", "stop", "nevermind", "never mind", "forget it", "abort"}
_YES = {"yes", "y", "yep", "yeah", "yup", "sure", "ok", "okay", "confirm", "save",
        "accept", "do it", "go ahead", "looks good", "correct", "right"}
ACTION_Q_ID = "__action__"


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


def _app_entry(app_key, registry_path=None):
    return (_load_registry(registry_path).get("apps") or {}).get(app_key) or {}


def _test_hint(rule):
    """What the panel's Test needs from the user, per rule: a name only when
    the rule searches for one (the box then suggests the rule's own example);
    an open-a-site rule just opens."""
    action = rule.get("action") or {}
    needs = action.get("type") == "search_site" or any(
        s.get("op") == "search" for s in action.get("steps") or [])
    if not needs:
        return {"needs_sample": False, "test_label": "Test: open it now"}
    slot = re.sub(r"\s+name$", "", action.get("slot") or "") or "name"
    return {"needs_sample": True, "test_label": "Test",
            "sample_placeholder": f"Try it with a {slot}, e.g. {rules_sim.sample_for(rule)}"}


def _verdict_line(report):
    """One plain sentence on what the simulation found."""
    status = report.get("status")
    line = {
        "verified": f"I tested it for real: {report.get('url')} works.",
        "failed": "My test failed (details below).",
        "unverified": "I couldn't test it in the hidden browser. Press Test to try it in yours.",
        "simulated": "I simulated it (details below).",
    }.get(status, "")
    judge = report.get("judge") or {}
    if judge and not judge.get("match"):
        line += f" Heads up, it may not match what you asked: {judge.get('reason', '')}"
    return f"{line} Save it? Or tell me what to change.".strip()


def _ai_step(app_key, pending, registry_path=None):
    """Compile the pending command rule with AI, simulate it, and return the
    next step. Publishes progress for the panel's progress bar."""
    try:
        return _ai_step_inner(app_key, pending, registry_path)
    finally:
        rules_sim.finish_progress(app_key)


def _ai_step_inner(app_key, pending, registry_path=None):
    say = rules_sim.reporter(app_key)
    say("Understanding your rule...", 12)
    rule, notes = rules_ai.compile_rule(
        app_key, pending["phrase"], pending.get("action_text", ""),
        feedback=pending.get("feedback", ""), entry=_app_entry(app_key, registry_path))
    if rule is None:
        # No model answered and nothing names a site: keep the plain rule.
        rule = _escalate([{
            "rule_id": rc._slug(pending["phrase"]),
            "intent": pending.get("action_text") or pending["phrase"],
            "enforcement": "soft", "adapter_check": "", "scope": "all_sessions",
            "source_phrase": pending["phrase"], "needs_clarification": False,
            "clarification_question": None}])[0]
    pending["proposed_rule"] = rule
    if rule.get("needs_clarification"):
        pending["stage"] = "need_detail"
        return {"status": "clarify", "key": app_key, "rule_id": ACTION_Q_ID,
                "question": rule["clarification_question"], "remaining": 1,
                "notes": notes}
    say("Simulating your rule...", 50)
    user_text = (f'When I say "{pending["phrase"]}": {pending.get("action_text", "")} '
                 f'{pending.get("feedback", "")}').strip()
    rule, report = rules_sim.simulate(app_key, rule, _app_entry(app_key, registry_path),
                                      progress=say, user_text=user_text)
    if report["status"] == "verified":
        notes = [n for n in notes if "guessed" not in n]
    pending["proposed_rule"] = rule
    pending["stage"] = "proposal"
    summary = rule.get("summary") or rule.get("intent", "")
    return {
        "status": "proposal",
        "key": app_key,
        "rule_id": "",
        "proposed": [rule],
        "summary": summary,
        "proposal": rules_ai.propose_markdown(app_key, [rule], notes),
        "question": f"{summary} {_verdict_line(report)}",
        "verification": report,
        "notes": notes,
        "testable": (rule.get("action") or {}).get("type") in rules_ai.RUNNABLE_TYPES,
        **_test_hint(rule),
    }


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

    # A command ("search movies in brave") is one rule, never split on
    # "and"/commas: ask what it should do, then compile it with AI.
    # Preferences ("no explicit stuff and keep it quiet") keep the clause flow.
    trig, act = rules_ai.split_draft(phrase)
    if act or rules_ai.is_command_phrase(trig):
        pending = {"mode": "action", "phrase": trig, "action_text": act,
                   "feedback": "", "stage": None if act else "need_action",
                   "candidate_rules": [], "answers": {}}
        _PENDING[app_key] = pending
        if act:
            return _ai_step(app_key, pending, registry_path)
        return {"status": "clarify", "key": app_key, "rule_id": ACTION_Q_ID,
                "question": f'What should JARVIS do when you say "{trig}"?',
                "remaining": 1, "candidate_rules": []}

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

    if pending.get("mode") == "action":
        if pending.get("stage") == "need_action":
            pending["action_text"] = answer
            return _ai_step(app_key, pending, registry_path)
        if pending.get("stage") == "proposal" and answer.lower().strip(" .!") in _YES:
            rule = pending["proposed_rule"]
            rc.commit_rules(app_key, [rule], _registry_path(registry_path),
                            accept=True, merge=True)
            _PENDING.pop(app_key, None)
            total = list_rules(app_key, registry_path).get("count", 1)
            return {"status": "committed", "key": app_key, "count": 1, "total": total,
                    "rules": [rule],
                    "proposal": rules_ai.propose_markdown(app_key, [rule])}
        # Anything else is a correction ("use the site's /search/ page") or the
        # missing detail (a website address): recompile with it.
        pending["feedback"] = f"{pending.get('feedback', '')} {answer}".strip()
        return _ai_step(app_key, pending, registry_path)

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
    # merge: adding rules by voice keeps the app's other rules.
    rc.commit_rules(app_key, finalized, _registry_path(registry_path),
                    accept=True, merge=True)
    _PENDING.pop(app_key, None)
    return {
        "status": "committed",
        "key": app_key,
        "count": len(finalized),
        "total": list_rules(app_key, registry_path).get("count", len(finalized)),
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
