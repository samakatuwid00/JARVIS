"""rules_ai.py - AI rule compiler for the JARVIS Apps panel.

Turns what the user typed - a command phrase plus what JARVIS should do, or a
single draft line holding both - into a structured rule JARVIS can execute:

    {"rule_id", "source_phrase", "intent", "summary", "enforcement",
     "adapter_check", "scope", "triggers": [...], "action": {...},
     "action_text", "compiled_by", "needs_clarification",
     "clarification_question"}

    action = {"type": "search_site" | "open_site" | "block" | "reminder",
              "site": "hollymoviehd.cc", "url": "https://hollymoviehd.cc",
              "search_url": "https://hollymoviehd.cc/?s={query}",
              "search_url_guessed": bool, "slot": "movie name",
              "browser": "brave", "applies_to": "launch"}

Setup-time only: runs when the user authors rules, never per spoken command
(rules_engine.match_action does the runtime side, with no model call).

Model chain: router -> local Ollama -> None. A dead router never blocks rule
setup, and callers fall back to rules_compiler's keyword parser when both
models are down. Guard: a site the user never typed is dropped - the model
may structure the user's words, not invent destinations.
"""

import json
import re

import rules_engine

CONFIG_TIME_ONLY = True

ACTION_TYPES = ("search_site", "open_site", "block", "reminder")
RUNNABLE_TYPES = ("search_site", "open_site")

_BROWSER_KEYS = {"brave": "brave", "chrome": "chrome", "google chrome": "chrome",
                 "edge": "msedge", "msedge": "msedge", "microsoft edge": "msedge",
                 "firefox": "firefox"}
_BROWSER_LABELS = {"brave": "Brave", "chrome": "Chrome", "msedge": "Edge",
                   "firefox": "Firefox"}
_APPLIES_TO = {"launch": "pre_launch: ", "play": "pre_play: ",
               "close": "pre_close: ", "all": ""}

_URL_RE = re.compile(
    r"https?://[^\s'\"<>]+|(?:www\.)?[\w-]+(?:\.[\w-]+)*\.[a-z]{2,24}(?:/[^\s'\"<>]*)?",
    re.I)
_SEARCH_RE = re.compile(r"\b(search|find|look\s*up|look\s+for|query)\b", re.I)
_BLOCK_RE = re.compile(r"\b(never|block|don'?t|do\s+not|must\s+not|no\s+more)\b", re.I)
COMMAND_START_RE = re.compile(
    r"^(?:please\s+|jarvis[,\s]+)*(search|find|look\s*up|look\s+for|open|launch|"
    r"visit|go\s+to|take\s+me\s+to|play|watch|browse|show)\b", re.I)
_WHEN_I_SAY_RE = re.compile(
    r"^\s*(?:when|if)\s+i\s+say\s+[\"']?(.+?)[\"']?\s*[,;]\s*(?:then\s+)?(.+)$", re.I)
_ARROW_RE = re.compile(r"\s*(?:->|=>|→)\s*")
_MUSIC_CHECK_RE = re.compile(
    r"^pre_play:\s*(skip if track\.explicit|cap \w[\w .-]* volume at \d{1,3}%)$", re.I)
_SPECIFIC_STOP = {"a", "an", "the", "some", "in", "on", "at", "for", "me", "my",
                  "please", "jarvis", "sir", "browser", "to", "up"}
_VERBS = {"search", "find", "look", "lookup", "open", "launch", "visit", "go",
          "take", "play", "watch", "browse", "show"}

_PROMPT = """You compile ONE user rule for the JARVIS voice assistant.
The rule belongs to the app "{app}".{browser_note}

The user wrote:
COMMAND PHRASE: "{trigger}"
WHAT JARVIS SHOULD DO: "{action}"{feedback}

Work out what the user really means and return ONLY one JSON object:
{
  "type": "search_site" | "open_site" | "block" | "reminder",
  "trigger": the command phrase, cleaned up (lowercase, no filler words),
  "triggers": 3 to 6 other natural ways to say the same command, keeping its key words (the browser name and the kind of thing, e.g. "movies"),
  "site": the website domain the user wrote (e.g. "example.com"), or null,
  "search_url": for search_site, the site's search address with {query} where the search words go, or null if unsure,
  "slot": for search_site, a short name for what the user fills in (e.g. "movie name"), or null,
  "sample": for search_site, one realistic example of what the user would fill in (e.g. "Inception" for a movie), or null,
  "browser": "brave" | "chrome" | "msedge" | "firefox" | null,
  "applies_to": for block only: "launch" | "play" | "close" | "all",
  "adapter_check": for music limits only, exactly "pre_play: skip if track.explicit" or "pre_play: cap {app} volume at N%", otherwise ""
}
Meaning of type:
- search_site: the command searches a website for something the user names.
- open_site: the command just opens a website.
- block: the user wants something prevented (never / don't / no).
- reminder: a preference JARVIS should keep in mind.
Only use a site the user actually wrote. Never invent one.
If WHAT JARVIS SHOULD DO is empty, the command phrase may hold both parts - split it yourself.
JSON only. /no_think"""


# ------------------------------------------------------------------ helpers --

def _slug(text):
    words = re.findall(r"[a-z0-9]+", (text or "").lower())
    return "_".join(words) or "rule"


def _clean_phrase(text):
    t = re.sub(r"\{[^}]*\}", " ", text or "")
    t = re.sub(r"[\"'.!?,;:]+", " ", t.lower())
    return re.sub(r"\s+", " ", t).strip()


def _bare_host(url_or_domain):
    h = re.sub(r"^https?://", "", (url_or_domain or "").strip().lower())
    h = h.split("/", 1)[0].split("?", 1)[0].rstrip(".")
    return h[4:] if h.startswith("www.") else h


def _domains_in(text):
    return [_bare_host(u.rstrip(".,;:!?)")) for u in _URL_RE.findall(text or "")]


def _specific_enough(phrase):
    """A trigger needs a word beyond the verb and browser ('search movies in
    brave', not 'search brave') or it would capture every search."""
    words = [w for w in re.findall(r"[a-z0-9]+", phrase) if w not in _SPECIFIC_STOP]
    rest = [w for w in words if w not in _VERBS and w not in _BROWSER_KEYS]
    return len(words) >= 2 and bool(rest)


def _slot_guess(trigger):
    """'search a movie in brave' -> 'movie name'."""
    for w in re.findall(r"[a-z0-9]+", (trigger or "").lower()):
        if w in _SPECIFIC_STOP or w in _VERBS or w in _BROWSER_KEYS:
            continue
        if len(w) > 3 and w.endswith("s") and not w.endswith("ss"):
            w = w[:-1]
        return f"{w} name"
    return "name"


def _browser_in(text):
    low = (text or "").lower()
    for word, key in _BROWSER_KEYS.items():
        if re.search(r"\b" + re.escape(word) + r"\b", low):
            return key
    return None


def browser_label(key):
    return _BROWSER_LABELS.get(key or "", key or "your browser")


def split_draft(line):
    """(command phrase, action text) from one draft line.

    'search movies in brave -> search it on hollymoviehd.cc' and
    'when I say search movies, search hollymoviehd.cc' split here; anything
    else is returned whole with an empty action (the model may split it)."""
    line = (line or "").strip()
    m = _WHEN_I_SAY_RE.match(line)
    if m:
        return m.group(1).strip(), m.group(2).strip()
    parts = _ARROW_RE.split(line, maxsplit=1)
    if len(parts) == 2 and parts[0].strip() and parts[1].strip():
        return parts[0].strip(), parts[1].strip()
    return line, ""


def is_command_phrase(phrase):
    return bool(COMMAND_START_RE.match((phrase or "").strip()))


def _parse_json(raw):
    raw = re.sub(r"<think>.*?</think>", "", raw or "", flags=re.S | re.I).strip()
    raw = re.sub(r"^```[a-z]*\s*|\s*```$", "", raw, flags=re.I)
    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(raw[start:end + 1])
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _ask_llm(prompt):
    """(parsed JSON dict, model) from the first model that answers, else
    (None, reason). Router first, then the local Ollama model."""
    try:
        from openai import OpenAI
        import config
    except Exception as e:
        return None, f"no LLM client ({type(e).__name__})"
    # Cloud models through 9router first (the brain's model, then two of its
    # fallbacks), then the local Ollama model.
    router_models = [config.ROUTER_MODEL] + list(
        getattr(config, "ROUTER_FALLBACK_MODELS", []) or [])[:2]
    tries = [(config.ROUTER_BASE_URL, config.ROUTER_API_KEY or "dummy", m, 20)
             for m in dict.fromkeys(router_models)]
    tries.append((config.OLLAMA_BASE_URL, config.OLLAMA_API_KEY or "ollama",
                  config.OLLAMA_MODEL, 90))
    errors = []
    router_down = False
    for base, key, model, timeout in tries:
        if router_down and base == config.ROUTER_BASE_URL:
            continue
        try:
            client = OpenAI(base_url=base, api_key=key, timeout=timeout, max_retries=0)
            r = client.chat.completions.create(
                model=model, temperature=0, max_tokens=600,
                messages=[{"role": "user", "content": prompt}])
            data = _parse_json(r.choices[0].message.content or "")
            if data:
                return data, model
            errors.append(f"{model}: no JSON")
        except Exception as e:
            errors.append(f"{model}: {type(e).__name__}")
            # 9router not running: its other models would fail the same way.
            if base == config.ROUTER_BASE_URL and "Connection" in type(e).__name__:
                router_down = True
    return None, "; ".join(errors)


def _fallback_raw(trigger, action_text):
    """Keyword-only structure for when no model answers. Only used when the
    user's words name a site - otherwise the caller keeps the legacy parser."""
    text = f"{trigger} {action_text}"
    domains = _domains_in(text)
    if not domains:
        return None
    return {
        "type": "search_site" if _SEARCH_RE.search(text) else "open_site",
        "trigger": trigger, "triggers": [], "site": domains[0],
        "search_url": None, "slot": _slot_guess(trigger),
        "browser": _browser_in(text),
    }


# ------------------------------------------------------------------ compile --

def _summary(rule_type, trigger, site, slot, browser, app_key, action_text, applies_to):
    where = browser_label(browser)
    if rule_type == "search_site":
        article = "an" if slot[:1].lower() in "aeiou" else "a"
        return (f'When you say "{trigger}" with {article} {slot}, I\'ll search {site} '
                f"for it in {where}.")
    if rule_type == "open_site":
        return f'When you say "{trigger}", I\'ll open {site} in {where}.'
    if rule_type == "block":
        act = {"launch": "open", "play": "play", "close": "close"}.get(applies_to, "use")
        return f"I won't {act} {app_key} when this applies: {action_text or trigger}."
    return f"I'll keep this in mind for {app_key}: {action_text or trigger}."


def compile_rule(app_key, trigger, action_text="", feedback="", entry=None, llm=None):
    """Compile one rule. Returns (rule, notes), or (None, notes) when no model
    answered and the text names no site - the caller keeps its legacy path.

    feedback: the user's correction to a previous proposal ('use the site's
    /search/ page'), folded into the prompt so the model revises its answer.
    """
    trigger = (trigger or "").strip()
    action_text = (action_text or "").strip()
    feedback = (feedback or "").strip()
    entry = entry if isinstance(entry, dict) else {}
    notes = []

    is_browser_app = entry.get("category") == "browser"
    prompt = (_PROMPT
              .replace("{app}", app_key)
              .replace("{browser_note}", " It is a web browser." if is_browser_app else "")
              .replace("{trigger}", trigger)
              .replace("{action}", action_text)
              .replace("{feedback}",
                       f'\nCORRECTION FROM THE USER: "{feedback}"' if feedback else ""))
    raw, model = (llm or _ask_llm)(prompt)
    compiled_by = f"ai:{model}" if raw else "keywords"
    if not raw:
        notes.append(f"AI compile unavailable ({model}) - compiled from keywords.")
        raw = _fallback_raw(trigger, f"{action_text} {feedback}")
        if raw is None:
            return None, notes

    user_text = f"{trigger} {action_text} {feedback}"
    user_domains = _domains_in(user_text)

    rule_type = raw.get("type") if raw.get("type") in ACTION_TYPES else None
    site = _bare_host(raw.get("site") or "")
    if site and site not in user_domains:
        # The model named a site the user never wrote: keep the user's own.
        site = ""
    if not site and user_domains:
        site = user_domains[0]
    if rule_type is None:
        rule_type = ("search_site" if site and _SEARCH_RE.search(user_text)
                     else "open_site" if site
                     else "block" if _BLOCK_RE.search(user_text) else "reminder")

    clean_trigger = _clean_phrase(raw.get("trigger") or "") or _clean_phrase(trigger)
    # The phrase the user typed stays the primary trigger when they gave one
    # separately; the model's cleanup only replaces a combined draft line.
    if action_text:
        clean_trigger = _clean_phrase(trigger) or clean_trigger

    browser = _BROWSER_KEYS.get(str(raw.get("browser") or "").lower())
    typed_browser = _browser_in(user_text)
    if browser and browser != typed_browser and browser != app_key:
        browser = None
    browser = browser or typed_browser or (app_key if is_browser_app else None)

    action = {"type": rule_type}
    needs_q = None
    if rule_type in RUNNABLE_TYPES:
        if site:
            action.update(site=site, url=f"https://{site}")
        else:
            needs_q = "Which website should I use? Type its address, like example.com."
        action["browser"] = browser
    if rule_type == "search_site":
        slot = str(raw.get("slot") or "").strip().lower()[:30] or _slot_guess(clean_trigger)
        search_url = str(raw.get("search_url") or "").strip()
        if not ("{query}" in search_url
                and rules_engine.is_web_url(search_url.replace("{query}", "q"))
                and rules_engine.same_site(search_url, site)):
            search_url = f"https://{site}/?s={{query}}" if site else ""
        # Only an address the user typed is known good; the model's is a guess.
        typed = search_url.split("{query}", 1)[0].lower()
        typed = re.sub(r"^https?://(?:www\.)?", "", typed)
        guessed = bool(search_url) and typed not in user_text.lower()
        if guessed:
            notes.append(f"Search address guessed as {search_url} - press Test to check it.")
        action.update(slot=slot, search_url=search_url, search_url_guessed=guessed)
        sample = str(raw.get("sample") or "").strip()[:60]
        if sample:
            action["sample"] = sample

    applies_to = str(raw.get("applies_to") or "launch").lower()
    if applies_to not in _APPLIES_TO:
        applies_to = "launch"
    adapter_check = str(raw.get("adapter_check") or "").strip()
    if not _MUSIC_CHECK_RE.match(adapter_check):
        adapter_check = ""
    if rule_type == "block":
        action["applies_to"] = applies_to
        enforcement = "hard"
        adapter_check = adapter_check or (_APPLIES_TO[applies_to] + (action_text or trigger)
                                          if _APPLIES_TO[applies_to] else "")
    elif rule_type == "reminder":
        enforcement = "soft"
    else:
        # Runnable rules are neither blocks nor reminders: rules_engine's gate
        # ignores this enforcement, so opening the browser stays silent.
        enforcement = "action"

    triggers = []
    for t in [clean_trigger] + [_clean_phrase(str(x)) for x in (raw.get("triggers") or [])
                                if isinstance(x, str)]:
        if t and len(t) <= 80 and t not in triggers and (
                t == clean_trigger or _specific_enough(t)):
            triggers.append(t)
    triggers = triggers[:8]

    summary = _summary(rule_type, clean_trigger, site or "the site",
                       action.get("slot", ""), browser, app_key,
                       action_text, applies_to)
    rule = {
        "rule_id": _slug(clean_trigger),
        "source_phrase": clean_trigger,
        "intent": summary,
        "summary": summary,
        "action_text": action_text or trigger,
        "enforcement": enforcement,
        "adapter_check": adapter_check,
        "scope": "all_sessions",
        "triggers": triggers,
        "action": action,
        "compiled_by": compiled_by,
        "needs_clarification": bool(needs_q),
        "clarification_question": needs_q,
    }
    return rule, notes


def compile_drafts(app_key, drafts, existing_rules=None, entry=None, llm=None):
    """Compile every draft line of the Apps panel. Returns (rules, notes).

    A line that already has a structured rule is kept as is. A line matching an
    older plain rule is recompiled with that rule's saved action, so the site
    it named is never lost. Lines no model can read keep the legacy keyword
    parser, as before.
    """
    import rules_compiler as rc
    by_phrase = {}
    for r in existing_rules or []:
        if isinstance(r, dict) and r.get("source_phrase"):
            by_phrase[_clean_phrase(r["source_phrase"])] = r
    rules, notes = [], []
    for line in drafts or []:
        line = (line or "").strip()
        if not line:
            continue
        trig, act = split_draft(line)
        old = by_phrase.get(_clean_phrase(line)) or by_phrase.get(_clean_phrase(trig))
        if old and isinstance(old.get("action"), dict) and not act:
            rules.append(old)
            continue
        if old and not act:
            saved = old.get("action_text") or old.get("intent") or ""
            if saved and not saved.lower().startswith("unclear"):
                act = saved
        rule, n = compile_rule(app_key, trig, act, entry=entry, llm=llm)
        notes.extend(f"{trig}: {x}" for x in n)
        if rule is None:
            legacy = rc.parse_scaffold(app_key, line)["candidate_rules"]
            answers = {}
            for c in legacy:
                if c.get("needs_clarification"):
                    low = (c.get("source_phrase") or "").lower()
                    quiet = any(m in low for m in ("quiet", "loud", "soft", "low", "calm", "chill"))
                    answers[c["rule_id"]] = (f"under 60% {app_key} volume all sessions" if quiet
                                             else f"{c['source_phrase']}, all sessions")
            rules.extend(rc.apply_clarifications(app_key, legacy, answers))
            continue
        if rule.get("needs_clarification"):
            notes.append(f"{trig}: {rule['clarification_question']}")
        rules.append(rule)
    for r in rules:
        if rc.detect_hard_escalation(r) and r.get("enforcement") not in ("hard", "action"):
            r["enforcement"] = "hard"
    return rules, notes


def propose_markdown(app_key, rules, notes=None):
    """Human-readable proposal for the panel."""
    lines = [f"Proposed rules for {app_key}", ""]
    for r in rules:
        a = r.get("action") or {}
        lines.append(f"- {r.get('summary') or r.get('intent', '')}")
        if a.get("type") in RUNNABLE_TYPES:
            lines.append(f"    type: {a['type']} | browser: {browser_label(a.get('browser'))}")
            if a.get("search_url"):
                lines.append(f"    search address: {a['search_url']}")
            others = [t for t in r.get("triggers") or [] if t != r.get("source_phrase")]
            if others:
                lines.append("    also works for: " + "; ".join(others))
        else:
            lines.append(f"    {r.get('enforcement', 'soft')} | check: "
                         f"{r.get('adapter_check') or '(none)'}")
        if r.get("compiled_by"):
            lines.append(f"    compiled by: {r['compiled_by']}")
        v = r.get("verification") or {}
        if v.get("steps"):
            label = {"verified": "VERIFIED", "failed": "FAILED CHECK",
                     "unverified": "NOT VERIFIED", "simulated": "SIMULATED"}.get(
                         v.get("status"), str(v.get("status", "")).upper())
            lines.append(f"    simulation: {label}")
            lines.extend(f"      {s}" for s in v["steps"])
            j = v.get("judge") or {}
            if j:
                lines.append(f"      intent check: {'matches' if j.get('match') else 'MISMATCH'}"
                             f" - {j.get('reason', '')}")
    for n in notes or []:
        lines.append(f"Note: {n}")
    return "\n".join(lines)
