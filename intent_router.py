"""intent_router.py - the understand-first router, running in shadow mode.

Phase 4 of the JARVIS Apps Intelligence design (decision: shadow first, then
switch on evidence). After every turn, in the background, it works out what it
WOULD have done:
  understand - the command becomes a structured intent, with the conversation
               state (open question, last result, app in front) as context
  resolve    - one ability of one app, one of the user's rules, a web search,
               plain conversation, or a question back to the user
Nothing is executed. The decision is logged next to what JARVIS actually did
(logs/shadow.jsonl). The Apps panel's Shadow review lists disagreements; the
user's verdicts (logs/shadow_labels.jsonl) are the evidence for the switch.
"""

import difflib
import json
import os
import re
import threading
import time
import uuid

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SHADOW_LOG = os.path.join(BASE_DIR, "logs", "shadow.jsonl")
LABELS_LOG = os.path.join(BASE_DIR, "logs", "shadow_labels.jsonl")
AUDIT_LOG = os.path.join(BASE_DIR, "logs", "audit.jsonl")
ENABLED = os.getenv("SHADOW_ROUTER", "on").lower() not in ("0", "off", "false")

MAX_CANDIDATES = 8
# Background turns finishing together must not interleave their lines.
_WRITE_LOCK = threading.Lock()
# Switch-over bar (design doc): enough real commands, the new router agrees
# where the old one was right, and never picks something unsafe.
READY_MIN_TURNS = 200
READY_AGREEMENT = 0.95

DECISION_TYPES = ("ability", "rule", "web_search", "chat", "ask")
# Words that point at a kind of app when no app is named.
_KIND_HINTS = {"browser": r"\b(movie|film|site|website|web|search|google|youtube|facebook|video)s?\b",
               "media": r"\b(song|music|playlist|track|album|spotify|volume|pause|skip)s?\b",
               "files": r"\b(folder|file|downloads|documents|desktop)s?\b",
               "office": r"\b(document|spreadsheet|slides|word|excel|powerpoint)s?\b"}

_PROMPT = """You are the command router of JARVIS, a voice assistant that controls this PC.
Decide what JARVIS should do for the user's command. Do not do it; only decide.

COMMAND: "{text}"
CONVERSATION NOW: {state}
APPS JARVIS MAY USE (abilities and the user's own rules):
{catalog}

Return ONLY one JSON object:
{
  "intent": {"action": "what the user wants done, one verb", "target": "what or who it is about",
             "refers_to_last_result": true or false},
  "decision": {"type": "ability" | "rule" | "web_search" | "chat" | "ask",
               "app": "app key from the list, or null",
               "id": "ability id or rule id from the list, or null",
               "args": {"detail name": "value"},
               "question": "only for ask: the one question to ask"},
  "confidence": 0.0 to 1.0,
  "reason": "one short sentence"
}
Rules: only use apps, ability ids and rule ids from the list. Prefer the user's rule when one fits.
Use "chat" for questions and small talk, "ask" when two options are equally likely.
JSON only. /no_think"""


# ----------------------------------------------------------------- catalog --

def _words(text):
    return re.findall(r"[a-z0-9]+", (text or "").lower())


def _names(key, entry):
    import app_abilities
    return {n.lower() for n in (key, entry.get("name"), entry.get("display_name"),
                                app_abilities.DISPLAY_NAMES.get(key)) if n}


def shortlist(text, snapshot, apps):
    """The few apps worth showing the model: named in the command, in front,
    behind the last result, or of the kind the command is about."""
    words = set(_words(text))
    low = (text or "").lower()
    picked = []

    def add(key):
        if key in apps and key not in picked and isinstance(apps[key], dict) \
                and not apps[key].get("hidden") and len(picked) < MAX_CANDIDATES:
            picked.append(key)

    for key, entry in apps.items():
        if not isinstance(entry, dict):
            continue
        for name in _names(key, entry):
            if name in low or difflib.get_close_matches(name, words, n=1, cutoff=0.85):
                add(key)
    add((snapshot or {}).get("active_app"))
    add(((snapshot or {}).get("last_result") or {}).get("app"))
    for key, entry in apps.items():
        if isinstance(entry, dict) and any(set(_words(r.get("source_phrase"))) <= words
                                           for r in entry.get("compiled_rules") or []
                                           if r.get("source_phrase")):
            add(key)
    for kind, pattern in _KIND_HINTS.items():
        if re.search(pattern, low):
            for key, entry in apps.items():
                if isinstance(entry, dict) and entry.get("app_kind") == kind:
                    add(key)
    return picked


def catalog(keys, apps):
    """Plain-text catalog of the shortlisted apps' usable abilities and rules."""
    lines = []
    for key in keys:
        entry = apps[key]
        lines.append(f"- app {key} ({entry.get('app_kind') or 'app'}):")
        for a in entry.get("abilities") or []:
            if a.get("enabled") is False or a.get("level") == "never":
                continue
            needs = ", ".join(a.get("needs") or {})
            lines.append(f"    ability {a['id']}: {a['name']}" + (f" (needs {needs})" if needs else ""))
        for r in entry.get("compiled_rules") or []:
            lines.append(f"    rule {r.get('rule_id')}: when the user says \"{r.get('source_phrase')}\""
                         f" - {r.get('summary') or r.get('intent') or ''}")
    return "\n".join(lines) or "(no apps registered)"


# ------------------------------------------------------------------ decide --

def validate(decision, keys, apps):
    """Keep only what exists: an unknown app, ability or rule makes the
    decision "invalid" (counted against the router, never executed)."""
    d = dict(decision or {})
    kind = d.get("type") if d.get("type") in DECISION_TYPES else "invalid"
    app = d.get("app")
    if kind in ("ability", "rule"):
        entry = apps.get(app) if app in keys else None
        pool = [] if entry is None else (entry.get("abilities") or []) if kind == "ability" \
            else (entry.get("compiled_rules") or [])
        item = next((x for x in pool if (x.get("id") if kind == "ability" else x.get("rule_id"))
                     == d.get("id")), None)
        if item is None:
            kind = "invalid"
        elif kind == "ability":
            d["unsafe"] = item.get("level") == "never" or item.get("enabled") is False
            d["args"] = {k: v for k, v in (d.get("args") or {}).items() if k in (item.get("needs") or {})}
    d["type"] = kind
    return d


def decide(text, snapshot, apps, llm=None):
    """(decision, intent, confidence, reason, model) for one command."""
    keys = shortlist(text, snapshot, apps)
    prompt = (_PROMPT.replace("{text}", text.replace('"', "'"))
              .replace("{state}", json.dumps(snapshot or {}, ensure_ascii=False)[:1200])
              .replace("{catalog}", catalog(keys, apps)))
    if llm is None:
        import rules_ai
        llm = rules_ai._ask_llm
    raw, model = llm(prompt)
    raw = raw or {}
    decision = validate(raw.get("decision"), keys, apps)
    try:
        confidence = float(raw.get("confidence") or 0)
    except (TypeError, ValueError):
        confidence = 0.0
    return decision, raw.get("intent") or {}, confidence, str(raw.get("reason") or "")[:200], model


# ------------------------------------------------------------- what ran --

def _audit_since(pos):
    """Audit entries written after byte offset `pos` (this turn's actions)."""
    try:
        with open(AUDIT_LOG, "rb") as f:
            f.seek(pos)
            chunk = f.read().decode("utf-8", errors="replace")
    except OSError:
        return []
    out = []
    for line in chunk.splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def _args(entry):
    raw = entry.get("args") if "args" in entry else entry.get("task")
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except ValueError:
            return {}
    return raw if isinstance(raw, dict) else {}


def actual_route(entries, backend=None, stats=None):
    """What JARVIS actually did this turn, in the router's terms."""
    for e in entries:
        tool = e.get("tool") or (e.get("caller") or "").replace("execute_tool:", "")
        args = _args(e)
        if tool == "rules.action":
            return {"type": "rule", "app": args.get("owner"), "id": args.get("rule")}
        if tool == "rules.step":
            return {"type": "rule", "app": None, "id": args.get("rule")}
        if tool == "abilities.run":
            return {"type": "ability", "app": args.get("app"), "id": args.get("ability")}
        if tool in ("open_application", "close_application"):
            verb = "open" if tool == "open_application" else "close"
            return {"type": "ability", "app": str(args.get("app") or "").lower(), "id": verb}
        if tool in ("search_web", "execute_search"):
            return {"type": "web_search", "app": None, "id": None}
        if tool == "open_site":
            return {"type": "ability", "app": None, "id": "open_site"}
        if tool in ("play_music", "stop_music", "stop_spotify"):
            return {"type": "ability", "app": "media", "id": tool}
    if backend in ("hermes", "autonomous"):
        return {"type": "delegate", "app": None, "id": backend}
    return {"type": "chat", "app": None, "id": (stats or {}).get("intent")}


def agree(decision, actual):
    """Same kind of action on the same app (and the same rule, when both
    name one). Asking back counts as agreement only with a question reply."""
    dt, at = decision.get("type"), actual.get("type")
    if dt == "invalid":
        return False
    if dt == "chat" or at == "chat":
        return dt == at
    if dt == "web_search" or at == "web_search":
        return dt == at
    if dt == "rule" and at == "rule":
        return decision.get("id") == actual.get("id")
    if dt != at:
        # A rule and an ability are different decisions even on the same app.
        return False
    same_app = not actual.get("app") or not decision.get("app") or \
        str(decision.get("app")).lower() == str(actual.get("app")).lower()
    verb = str(actual.get("id") or "")
    return dt in ("ability", "rule") and same_app and (not verb or verb in str(decision.get("id")))


# --------------------------------------------------------------- the turn --

def turn_started(text):
    """Called before the old router runs: the state the new one should see."""
    import dialogue_state
    try:
        pos = os.path.getsize(AUDIT_LOG)
    except OSError:
        pos = 0
    return {"text": text, "ts": time.time(), "audit_pos": pos, "snapshot": dialogue_state.snapshot()}


def turn_finished(started, reply, backend=None, stats=None, llm=None, background=True):
    """After the reply: record the turn, then decide in shadow and log both."""
    import dialogue_state
    entries = _audit_since(started["audit_pos"])
    actual = actual_route(entries, backend, stats)
    dialogue_state.record_turn(started["text"], reply, actual)
    if not ENABLED or not (started["text"] or "").strip():
        return None

    def work():
        try:
            import app_abilities
            apps = app_abilities.load_apps()["apps"]
            t0 = time.time()
            decision, intent, confidence, reason, model = decide(
                started["text"], started["snapshot"], apps, llm=llm)
            record = {"id": uuid.uuid4().hex[:12], "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                      "user": started["text"], "reply": (reply or "")[:300],
                      "snapshot": started["snapshot"], "actual": actual,
                      "decision": decision, "intent": intent, "confidence": confidence,
                      "reason": reason, "model": model, "ms": int((time.time() - t0) * 1000),
                      "agree": agree(decision, actual)}
            os.makedirs(os.path.dirname(SHADOW_LOG), exist_ok=True)
            with _WRITE_LOCK, open(SHADOW_LOG, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
            return record
        except Exception as e:
            print(f"[shadow] decision failed: {type(e).__name__}: {e}", flush=True)
            return None

    if not background:
        return work()
    threading.Thread(target=work, daemon=True).start()
    return None


# ------------------------------------------------------------------ review --

def _read_jsonl(path):
    try:
        with open(path, encoding="utf-8") as f:
            return [json.loads(l) for l in f if l.strip()]
    except (OSError, ValueError):
        return []


def labels():
    """Latest verdict per shadow record: "old", "new" or "neither"."""
    return {l["id"]: l["verdict"] for l in _read_jsonl(LABELS_LOG) if l.get("id")}


def label(record_id, verdict):
    if verdict not in ("old", "new", "neither"):
        raise ValueError(verdict)
    os.makedirs(os.path.dirname(LABELS_LOG), exist_ok=True)
    with _WRITE_LOCK, open(LABELS_LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps({"id": record_id, "verdict": verdict,
                            "ts": time.strftime("%Y-%m-%d %H:%M:%S")}) + "\n")


def review(disagreements_only=True, limit=100):
    """Newest shadow records for the panel, with any verdict attached."""
    verdicts = labels()
    items = [dict(r, verdict=verdicts.get(r["id"])) for r in _read_jsonl(SHADOW_LOG)
             if not disagreements_only or not r.get("agree")]
    return items[::-1][:limit]


def stats():
    """Readiness for the switch-over, from the shadow log and the verdicts."""
    records = _read_jsonl(SHADOW_LOG)
    verdicts = labels()
    total = len(records)
    agreed = sum(1 for r in records if r.get("agree"))
    unsafe = sum(1 for r in records if (r.get("decision") or {}).get("unsafe"))
    judged = [verdicts[r["id"]] for r in records if r.get("id") in verdicts and not r.get("agree")]
    old_right = judged.count("old")
    # Where the old router was right: agreed turns, plus disagreements judged "old".
    old_correct = agreed + old_right
    agreement_where_old_right = agreed / old_correct if old_correct else 0.0
    return {"turns": total, "agreed": agreed, "disagreed": total - agreed,
            "judged": len(judged), "new_right": judged.count("new"), "old_right": old_right,
            "both_wrong": judged.count("neither"), "unsafe": unsafe,
            "agreement_where_old_right": round(agreement_where_old_right, 3),
            "ready": total >= READY_MIN_TURNS and unsafe == 0
            and agreement_where_old_right >= READY_AGREEMENT
            and judged.count("new") >= old_right}
