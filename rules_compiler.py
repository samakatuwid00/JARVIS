"""rules_compiler.py — compile natural-language user rules into structured, reviewable rule sets.

Module constant: rules_compiler runs when the USER SETS UP rules, never per spoken command.
No runtime/per-command hook.
"""

import json
import os
import re
import tempfile
from datetime import datetime
from pathlib import Path

CONFIG_TIME_ONLY = True  # rules_compiler runs when the USER SETS UP rules, never per spoken command. No runtime/per-command hook.

# compiled_rule dict schema:
#   rule_id (str, snake_case)
#   intent (str)
#   enforcement ("soft" | "hard")
#   adapter_check (str, e.g. "pre_play: skip if track.explicit")
#   scope ("all_sessions" | "current_session" | "app_only")
#   source_phrase (str)
#   needs_clarification (bool)
#   clarification_question (str | None)

_HARD_RE = re.compile(r"\b(never|even if i ask|always block|hard block|must not|no exceptions)\b", re.IGNORECASE)


def _slug(text: str) -> str:
    words = re.findall(r"[a-z0-9]+", text.lower())
    return "_".join(words) or "rule"


def _split_clauses(phrase: str):
    parts = re.split(r"\b\s*(?:and|then|also|,|;)\s*\b", phrase.strip(), flags=re.IGNORECASE)
    return [p.strip() for p in parts if p.strip()]


def _is_ambiguous(clause: str) -> bool:
    low = clause.lower()
    ambiguous_markers = ["quiet", "keep it", "chill", "loud", "soft", "low", "calm", "appropriate"]
    return any(m in low for m in ambiguous_markers) and not any(
        x in low for x in ["explicit", "skip", "never", "block", "volume at", "under"]
    )


# "never close notepad", "don't let me open steam": a hard block on one action.
_NEVER_ACTION_RE = re.compile(
    r"^\s*(?:never|don'?t\s+ever|do\s+not\s+ever|don'?t|do\s+not|block)\s+"
    r"(?:let\s+(?:me|jarvis|anyone)\s+)?(open|launch|start|run|close|quit|exit|play)\b", re.I)
_NEVER_SCOPE = {"open": "pre_launch", "launch": "pre_launch", "start": "pre_launch",
                "run": "pre_launch", "close": "pre_close", "quit": "pre_close",
                "exit": "pre_close", "play": "pre_play"}


def parse_scaffold(app_id: str, phrase: str) -> dict:
    clauses = _split_clauses(phrase)
    candidate_rules = []
    for clause in clauses:
        if "explicit" in clause.lower():
            rule = {
                "rule_id": _slug(clause),
                "intent": "avoid explicit content",
                "enforcement": "soft",
                "adapter_check": "pre_play: skip if track.explicit",
                "scope": "all_sessions",
                "source_phrase": clause,
                "needs_clarification": False,
                "clarification_question": None,
            }
        elif not _is_ambiguous(clause) and _NEVER_ACTION_RE.match(clause):
            # "never close notepad" says exactly what to block: no question.
            verb = _NEVER_ACTION_RE.match(clause).group(1).lower()
            rule = {
                "rule_id": _slug(clause),
                "intent": clause,
                "enforcement": "hard",
                "adapter_check": f"{_NEVER_SCOPE[verb]}: block",
                "scope": "all_sessions",
                "source_phrase": clause,
                "needs_clarification": False,
                "clarification_question": None,
            }
        elif _is_ambiguous(clause):
            rule = {
                "rule_id": _slug(clause),
                "intent": "unclear — needs clarification",
                "enforcement": "soft",
                "adapter_check": "",
                "scope": "current_session",
                "source_phrase": clause,
                "needs_clarification": True,
                "clarification_question": (
                    "Quiet = under what % volume, and for all sessions or just now?"
                ),
            }
        else:
            rule = {
                "rule_id": _slug(clause),
                "intent": clause,
                "enforcement": "soft",
                "adapter_check": "",
                "scope": "all_sessions",
                "source_phrase": clause,
                "needs_clarification": True,
                "clarification_question": f"What exactly should happen for: \"{clause}\"?",
            }
        candidate_rules.append(rule)

    needs_clarification = any(r["needs_clarification"] for r in candidate_rules)
    questions = [r["clarification_question"] for r in candidate_rules if r["clarification_question"]]
    return {"candidate_rules": candidate_rules, "needs_clarification": needs_clarification, "questions": questions}


_NUM_UNITS = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
    "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16,
    "seventeen": 17, "eighteen": 18, "nineteen": 19,
}
_NUM_TENS = {
    "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50,
    "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90,
}
_PCT_SUFFIX = r"\s*(?:%|(?:percent|pct)\b)"
_DIGIT_PCT_RE = re.compile(r"(\d+)" + _PCT_SUFFIX, re.IGNORECASE)
# "one hundred" | "forty five" / "forty-five" | "forty" | "seventeen" — longest first.
_WORD_PCT_RE = re.compile(
    r"\b(one[\s-]+hundred"
    r"|(?:" + "|".join(_NUM_TENS) + r")(?:[\s-]+(?:one|two|three|four|five|six|seven|eight|nine)\b)?"
    r"|" + "|".join(sorted(_NUM_UNITS, key=len, reverse=True)) + r")" + _PCT_SUFFIX,
    re.IGNORECASE,
)


def _parse_percent(text):
    """Pull a percentage out of a spoken answer as a digit string, else None.

    "40%" keeps the original digit+% match; then "40 percent" / "40 pct"; then
    number-words zero..one hundred ("forty percent", "sixty-five pct")."""
    m = re.search(r"(\d+)\s*%", text)
    if m:
        return m.group(1)
    m = _DIGIT_PCT_RE.search(text)
    if m:
        return m.group(1)
    m = _WORD_PCT_RE.search(text)
    if m:
        parts = re.split(r"[\s-]+", m.group(1).lower())
        if parts[-1] == "hundred":
            return "100"
        return str(sum(_NUM_TENS.get(p, _NUM_UNITS.get(p, 0)) for p in parts))
    return None


def apply_clarifications(app_id, candidate_rules, answers: dict) -> list:
    finalized = []
    for rule in candidate_rules:
        rid = rule["rule_id"]
        answer = answers.get(rid)
        new_rule = dict(rule)
        if rule["needs_clarification"] and answer:
            low = answer.lower()
            pct = _parse_percent(low)
            if "all session" in low:
                scope = "all_sessions"
            elif "this session" in low or "just now" in low or "current" in low:
                scope = "current_session"
            else:
                scope = "all_sessions"
            if "volume" in low or "quiet" in low:
                cap = pct or "60"
                new_rule["intent"] = "cap playback volume"
                new_rule["adapter_check"] = f"pre_play: cap {app_id} volume at {cap}%"
                new_rule["scope"] = scope
                new_rule["enforcement"] = "soft"
            else:
                new_rule["intent"] = answer
                new_rule["scope"] = scope
            new_rule["needs_clarification"] = False
            new_rule["clarification_question"] = None
        finalized.append(new_rule)
    return finalized


def propose_ruleset(app_id, compiled_rules) -> str:
    lines = [f"# Proposed rules for `{app_id}`", "",
             "JARVIS proposes, you own. Review/edit before accepting.", ""]
    for r in compiled_rules:
        lines.append(f"- **{r['rule_id']}** (`{r['enforcement']}` / `{r['scope']}`)")
        lines.append(f"  - intent: {r['intent']}")
        lines.append(f"  - check: `{r['adapter_check'] or '(resolve)'}`")
        lines.append(f"  - from: \"{r['source_phrase']}\"")
        lines.append("")
    return "\n".join(lines)


def detect_hard_escalation(rule) -> bool:
    hay = f"{rule.get('intent','')} {rule.get('source_phrase','')} {rule.get('adapter_check','')}"
    return bool(_HARD_RE.search(hay))


def _load_registry(registry_path):
    data = {"apps": {}}
    if registry_path.exists():
        with open(registry_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    data.setdefault("apps", {})
    return data


def _write_registry(registry_path, data):
    tmp = registry_path.parent / (registry_path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, registry_path)


def commit_rules(app_id, compiled_rules, registry_path, accept=False, merge=False):
    """Write a finalized rule set. merge=True adds/updates by rule_id and keeps
    the app's other rules (voice setup adds one rule at a time); the default
    replaces the whole set (the drafts editor holds every rule)."""
    if accept != True:
        return propose_ruleset(app_id, compiled_rules)

    registry_path = Path(registry_path)
    data = _load_registry(registry_path)
    app_entry = data["apps"].setdefault(app_id, {})
    compiled_rules = list(compiled_rules)
    if merge:
        # An updated rule keeps its place in the list; new rules go last.
        incoming = {r.get("rule_id"): r for r in compiled_rules}
        merged = []
        for r in app_entry.get("compiled_rules") or []:
            if not isinstance(r, dict):
                continue
            merged.append(incoming.pop(r.get("rule_id"), r))
        compiled_rules = merged + [r for r in compiled_rules if r.get("rule_id") in incoming]
    app_entry["rule_drafts"] = [r["source_phrase"] for r in compiled_rules
                                if r.get("source_phrase")]
    app_entry["compiled_rules"] = compiled_rules
    _write_registry(registry_path, data)

    entry = {
        "ts": datetime.now().isoformat(timespec="milliseconds"),
        "event": "rule_set_finalized",
        "app": app_id,
        "rules": [r["rule_id"] for r in compiled_rules],
    }
    try:
        import audit
        audit.log_call(
            tool="rules.commit",
            args={"event": entry["event"], "app": app_id, "rules": entry["rules"]},
            duration_s=0.0,
            result="finalized",
            confirmed=True,
        )
    except Exception:
        print(json.dumps(entry, ensure_ascii=False))
    return registry_path


def remove_rule(app_id, rule_id, registry_path):
    """Drop one committed rule — or every rule when `rule_id` is empty — from an app.

    rule_drafts are resynced from the remaining rules' source phrases, same as
    commit_rules. Returns (removed, remaining). Raises KeyError for an unknown app.
    Nothing is written when nothing matched.
    """
    registry_path = Path(registry_path)
    data = _load_registry(registry_path)
    app_entry = data["apps"].get(app_id)
    if not isinstance(app_entry, dict):
        raise KeyError(app_id)

    current = app_entry.get("compiled_rules") or []
    if rule_id:
        remaining = [r for r in current if r.get("rule_id") != rule_id]
    else:
        remaining = []
    dropped = [r.get("rule_id") for r in current if r not in remaining]
    if not dropped:
        return False, current

    app_entry["compiled_rules"] = remaining
    app_entry["rule_drafts"] = [r["source_phrase"] for r in remaining if r.get("source_phrase")]
    _write_registry(registry_path, data)

    entry = {
        "ts": datetime.now().isoformat(timespec="milliseconds"),
        "event": "rule_removed",
        "app": app_id,
        "rules": dropped,
    }
    try:
        import audit
        audit.log_call(
            tool="rules.remove",
            args={"event": entry["event"], "app": app_id, "rules": entry["rules"]},
            duration_s=0.0,
            result="removed",
            confirmed=True,
        )
    except Exception:
        print(json.dumps(entry, ensure_ascii=False))
    return True, remaining


if __name__ == "__main__":
    app_id = "spotify"
    phrase = "no explicit stuff and keep it quiet"

    parsed = parse_scaffold(app_id, phrase)
    print("=== CANDIDATE RULES ===")
    for r in parsed["candidate_rules"]:
        print(json.dumps(r, ensure_ascii=False))
    print("\n=== NEEDS CLARIFICATION:", parsed["needs_clarification"])
    for q in parsed["questions"]:
        print("Q:", q)

    answers = {}
    for r in parsed["candidate_rules"]:
        if r["needs_clarification"]:
            answers[r["rule_id"]] = "under 60% spotify volume all sessions"

    finalized = apply_clarifications(app_id, parsed["candidate_rules"], answers)
    print("\n=== PROPOSED RULESET ===")
    print(propose_ruleset(app_id, finalized))

    tmp_reg = Path("C:/Temp/rc_test_registry.json")
    tmp_reg.parent.mkdir(parents=True, exist_ok=True)
    stub = {"apps": {"spotify": {"name": "spotify"}}}
    with open(tmp_reg, "w", encoding="utf-8") as f:
        json.dump(stub, f, ensure_ascii=False, indent=2)

    out = commit_rules(app_id, finalized, tmp_reg, accept=True)
    print("\n=== COMMITTED TO (temp copy):", out)
