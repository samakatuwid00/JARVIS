# Rules Engine - Runtime enforcement of compiled app rules.
# Stdlib only. Consumed by tools.open_application (Phase 3.5).
import json
import os
import re
import sys
import urllib.parse

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


# --------------------------------------------------------------------------
# Action rules: find the user's runnable rule in a spoken command.
# rules_ai compiles them at setup time; this side is deterministic (no model
# call per command) so matching stays instant.
# --------------------------------------------------------------------------

_WORD_RE = re.compile(r"[A-Za-z0-9]+(?:['.-][A-Za-z0-9]+)*")
# Words that carry no meaning for matching: articles, politeness, the
# assistant's name, and "browser" ("in brave browser" == "in brave").
# Prepositions too: "on brave" / "using brave" == "in brave".
_MATCH_STOP = {"a", "an", "the", "some", "please", "jarvis", "sir", "hey", "ok",
               "okay", "now", "browser", "me", "for", "my", "in", "on", "at",
               "using", "with", "via", "into"}
# Trimmed off the ends of the name the user filled in.
_SLOT_EDGE = _MATCH_STOP | {"named", "called", "titled", "about", "in", "on", "at",
                            "to", "and", "of"}
_VERB_WORDS = {"search", "find", "look", "lookup", "open", "launch", "visit", "go",
               "take", "play", "watch", "browse", "show"}
_BROWSER_WORDS = {"brave", "chrome", "edge", "msedge", "firefox"}
_ACTION_SEARCH_RE = re.compile(r"\b(search|find|look\s*up|look\s+for)\b", re.I)
_ACTION_NEGATIVE_RE = re.compile(r"\b(never|block|don'?t|do\s+not|must\s+not)\b", re.I)
_ACTION_DOMAIN_RE = re.compile(
    r"https?://[^\s'\"<>]+|(?:www\.)?[\w-]+(?:\.[\w-]+)*\.[a-z]{2,24}(?:/[^\s'\"<>]*)?",
    re.I)


def bare_host(url_or_domain):
    """'https://www.example.com/x' -> 'example.com'."""
    h = re.sub(r"^https?://", "", (url_or_domain or "").strip().lower())
    h = h.split("/", 1)[0].split("?", 1)[0].rstrip(".")
    return h[4:] if h.startswith("www.") else h


_WEB_URL_RE = re.compile(r"^https?://[^\s\"'<>]+$", re.I)


def is_web_url(url):
    """http(s) addresses only: a rule's url ends up on browser command lines,
    where anything starting with '-' would be read as a switch."""
    return bool(_WEB_URL_RE.match(url or ""))


def same_site(url, site):
    """url is on site or one of its subdomains — never a lookalike
    ('evilexample.com' is not 'example.com')."""
    host, site = bare_host(url), bare_host(site)
    return bool(site) and (host == site or host.endswith("." + site))


def _stem(word):
    """Plural and singular share one key: movies/movie -> movie,
    stories/story -> storie. Only ever compared with other stems."""
    if len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
        word = word[:-1]
    if len(word) > 3 and word.endswith("y"):
        word = word[:-1] + "ie"
    return word


def _tokens(text):
    """[(spoken word, match key or None)] — stop words get no key."""
    out = []
    for w in _WORD_RE.findall(text or ""):
        low = w.lower()
        out.append((w, None if low in _MATCH_STOP else _stem(low)))
    return out


def _match_trigger(trigger, toks):
    """Match one trigger against the spoken tokens.

    Every trigger word must appear, in order, and the command must open with
    the trigger's verb. Words left over are the name the user filled in:
    'search movies Dune in brave browser' vs 'search a movie in brave' ->
    'Dune'. Later trigger words match from the right so a name containing
    'in' ('coming in hot') stays whole.
    """
    t_keys = [k for _, k in _tokens(re.sub(r"\{[^}]*\}", " ", trigger or "")) if k]
    if len(t_keys) < 2:
        return None
    first = next((i for i, (_, k) in enumerate(toks) if k), None)
    if first is None or toks[first][1] != t_keys[0]:
        return None
    matched = {first}
    j = len(toks) - 1
    for key in reversed(t_keys[1:]):
        while j > first and toks[j][1] != key:
            j -= 1
        if j <= first:
            return None
        matched.add(j)
        j -= 1
    words = [w for i, (w, _) in enumerate(toks) if i not in matched]
    while words and words[0].lower() in _SLOT_EDGE:
        words.pop(0)
    while words and words[-1].lower() in _SLOT_EDGE:
        words.pop()
    return {"slot": " ".join(words), "score": len(t_keys)}


def _slot_name(phrase):
    """'search a movie in brave' -> 'movie name' (spoken form, not the stem)."""
    for word, k in _tokens(phrase):
        low = word.lower()
        if k and low not in _VERB_WORDS and low not in _BROWSER_WORDS \
                and low not in ("in", "on", "at"):
            if len(low) > 3 and low.endswith("s") and not low.endswith("ss"):
                low = low[:-1]
            return f"{low} name"
    return "name"


def action_of(rule, owner, entry=None):
    """The runnable action of a rule, or None.

    Structured rules (rules_ai) carry an `action`. An older plain rule whose
    words name a site ('it will search a movie on hollymoviehd.cc') still
    runs: its action is derived here, with a guessed search address.
    """
    if not isinstance(rule, dict):
        return None
    is_browser = (entry or {}).get("category") == "browser"
    act = rule.get("action")
    if isinstance(act, dict):
        if act.get("type") == "steps":
            return _steps_action(dict(act), owner, is_browser)
        if act.get("type") not in ("search_site", "open_site"):
            return None
        out = dict(act)
        if not out.get("url") and out.get("site"):
            out["url"] = "https://" + out["site"]
        if not is_web_url(out.get("url")):
            return None
        if out.get("search_url") and not is_web_url(out["search_url"].replace("{query}", "q")):
            out["search_url"] = None
        if not out.get("browser") and is_browser:
            out["browser"] = owner
        return out
    text = f"{rule.get('intent') or ''} {rule.get('source_phrase') or ''}"
    if (rule.get("enforcement") or "soft").lower() not in ("soft", "action") \
            or _ACTION_NEGATIVE_RE.search(text):
        return None
    hosts = [bare_host(u.rstrip(".,;:!?)")) for u in _ACTION_DOMAIN_RE.findall(text)]
    if not hosts:
        return None
    site = hosts[0]
    out = {"type": "search_site" if _ACTION_SEARCH_RE.search(text) else "open_site",
           "site": site, "url": "https://" + site,
           "browser": owner if is_browser else None}
    if out["type"] == "search_site":
        out.update(search_url=f"https://{site}/?s={{query}}", search_url_guessed=True,
                   slot=_slot_name(rule.get("source_phrase") or ""))
    return out


_STEP_OPS = {"search", "pick", "open", "click", "type", "key", "launch", "wait", "ask"}
_WEB_STEP_OPS = {"search", "pick", "open"}


def _steps_action(out, owner, is_browser):
    """Validated multi-step action (rules_steps runs it), or None.

    Web steps need a valid http(s) site; desktop-only steps (launch, click,
    type, key) run in the owner app's window and need no address.
    """
    steps = [s for s in out.get("steps") or []
             if isinstance(s, dict) and s.get("op") in _STEP_OPS]
    if not steps:
        return None
    out["steps"] = steps
    if any(s["op"] in _WEB_STEP_OPS for s in steps):
        if not out.get("url") and out.get("site"):
            out["url"] = "https://" + out["site"]
        if not is_web_url(out.get("url")):
            return None
        search_url = out.get("search_url") or ""
        if search_url and not is_web_url(search_url.replace("{query}", "q")):
            out["search_url"] = None
        if not out.get("browser") and is_browser:
            out["browser"] = owner
    return out


def match_action(utterance, apps=None):
    """Best runnable rule for a spoken command, or None.

    Returns {"rule", "owner", "action", "slot", "trigger", "score"}; the most
    specific trigger (most words) wins. Disabled apps are skipped.
    """
    if apps is None:
        try:
            apps = _load_json(APP_REGISTRY_PATH).get("apps") or {}
        except Exception:
            return None
    toks = _tokens(utterance)
    if not toks:
        return None
    best = None
    for owner, entry in apps.items():
        if not isinstance(entry, dict) or entry.get("enabled") is False:
            continue
        for rule in entry.get("compiled_rules") or []:
            action = action_of(rule, owner, entry)
            if not action:
                continue
            for trig in [rule.get("source_phrase")] + list(rule.get("triggers") or []):
                if not isinstance(trig, str) or not trig.strip():
                    continue
                m = _match_trigger(trig, toks)
                if m and (best is None or m["score"] > best["score"]):
                    best = dict(m, rule=rule, owner=owner, action=action, trigger=trig,
                                entry=entry)
    return best


def build_action_url(action, slot=""):
    """The address a runnable rule opens: its search page for `slot`, or the site."""
    search_url = action.get("search_url") or ""
    if action.get("type") == "search_site" and slot and "{query}" in search_url:
        return search_url.replace("{query}", urllib.parse.quote_plus(slot))
    return action.get("url") or ""


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
