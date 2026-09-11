"""rules_sim.py - simulate a compiled rule before the user saves it.

After rules_ai compiles a rule, JARVIS runs it the way it would really run:
  1. phrase the command as the user would say it, with a sample name, and
     push it through the real matcher (rules_engine.match_action);
  2. build the address and load the real page in a hidden Chrome
     (site_probe): search results must mention the sample, an opened site
     must not be a 404;
  3. if the page fails, try the site's own search form, then common search
     patterns, then ask the model to repair the address from the evidence;
  4. ask the model whether the simulated outcome matches what the user asked.
Block and reminder rules are simulated through rules_engine.check.

Progress is published per app key (set_progress / get_progress) so the
panel can show a real progress bar while this runs.
"""

import copy
import threading
import time

import rules_engine

_PROGRESS = {}
_PLOCK = threading.Lock()
# Page checker used when simulate() gets none: None means site_probe (a real
# hidden Chrome). Tests set a fake here so nothing opens a browser.
PROBE = None

# Tried after the site's own search form and the model's proposal.
_COMMON_PATTERNS = ("{base}/?s={query}", "{base}/search/{query}",
                    "{base}/search?q={query}", "{base}/search?query={query}")
_MAX_PROBES = 5
_MAX_REPAIRS = 2
# Realistic names to search with when the model gave no sample.
_DEFAULT_SAMPLES = (("movie", "Inception"), ("film", "Inception"),
                    ("song", "Bohemian Rhapsody"), ("music", "Bohemian Rhapsody"),
                    ("video", "lofi hip hop"), ("show", "Breaking Bad"),
                    ("series", "Breaking Bad"), ("anime", "Naruto"), ("book", "Dune"),
                    ("game", "Minecraft"), ("product", "wireless mouse"),
                    ("recipe", "chicken adobo"))
_PREPOSITIONS = {"in", "on", "using", "with", "via", "at"}


# ---------------------------------------------------------------- progress --

def set_progress(key, stage, pct):
    with _PLOCK:
        _PROGRESS[key] = {"stage": stage, "pct": max(0, min(100, int(pct))),
                          "ts": time.time(), "done": False}


def finish_progress(key):
    with _PLOCK:
        p = _PROGRESS.setdefault(key, {"stage": "", "pct": 100})
        p.update(pct=100, done=True, ts=time.time())


def get_progress(key):
    with _PLOCK:
        return dict(_PROGRESS.get(key) or {"stage": "", "pct": 0, "done": True})


def reporter(key):
    """progress(stage, pct) callable bound to one app key."""
    return lambda stage, pct: set_progress(key, stage, pct)


# ------------------------------------------------------------------ helpers --

def sample_for(rule):
    a = rule.get("action") or {}
    s = str(a.get("sample") or "").strip()
    if s:
        return s
    slot = str(a.get("slot") or "").lower()
    return next((v for k, v in _DEFAULT_SAMPLES if k in slot), "test")


def spoken_example(trigger, sample):
    """'search movies in brave browser' + 'Inception' ->
    'search movies Inception in brave browser' (how the user would say it)."""
    words = (trigger or "").split()
    for i, w in enumerate(words):
        if i > 0 and w.lower() in _PREPOSITIONS:
            return " ".join(words[:i] + [sample] + words[i:])
    return f"{trigger} {sample}".strip()


def _default_llm():
    import rules_ai
    return rules_ai._ask_llm


def _repair_search_url(site, tried, form_note, llm):
    """Ask the model for a better search address, given what failed."""
    lines = "\n".join(f"- {r['url']}: {r['evidence']}" for _, _, r in tried)
    prompt = (f"JARVIS must search the website {site} for a name. These search "
              f"addresses were tried in a real browser and failed:\n{lines}\n"
              f"{form_note}\nReply with ONLY a JSON object "
              '{"search_url": "https://..."} giving the most likely working search '
              "address, with {query} where the search words go. /no_think")
    raw, _ = llm(prompt)
    url = str((raw or {}).get("search_url") or "").strip()
    if "{query}" in url and rules_engine.is_web_url(url.replace("{query}", "q")) \
            and rules_engine.same_site(url, site):
        return url
    return None


def judge_intent(user_text, rule, steps, llm):
    """Does the simulated outcome do what the user asked? {"match", "reason"} or None."""
    prompt = ("A user set up a rule for the JARVIS voice assistant.\n"
              f'THE USER ASKED: "{user_text}"\n'
              f"JARVIS UNDERSTOOD: {rule.get('summary') or rule.get('intent', '')}\n"
              "SIMULATION (real browser):\n" + "\n".join(f"- {s}" for s in steps) +
              "\nDoes the simulated result do what the user asked? Reply with ONLY "
              '{"match": true or false, "reason": "one short sentence"}. /no_think')
    raw, model = llm(prompt)
    if not raw or "match" not in raw:
        return None
    return {"match": bool(raw.get("match")), "reason": str(raw.get("reason") or "")[:200],
            "model": model}


# --------------------------------------------------------------- simulation --

def _remember(probe, site, search_url):
    """Keep a verified address for the next rule on this site (real probe only)."""
    keep = getattr(probe, "remember_search", None)
    if callable(keep):
        keep(site, search_url)


def _simulate_search(action, sample, steps, say, probe, llm):
    """Find a working search address. Returns (status, url)."""
    site = action.get("site") or rules_engine.bare_host(action.get("url") or "")
    site_url = action.get("url") or f"https://{site}"
    candidates = []
    say(f"Reading {site}'s search box...", 62)
    found = probe.discover_search(site_url) or {}
    blocked = bool(found.get("blocked"))
    if found.get("search_url"):
        candidates.append((found["search_url"], "the site's own search box"))
    proposal = action.get("search_url") or ""
    if rules_engine.is_web_url(proposal.replace("{query}", "q")) \
            and rules_engine.same_site(proposal, site):
        candidates.append((proposal, "the AI's proposal"))
    base = site_url.rstrip("/")
    candidates += [(p.replace("{base}", base), "a common search pattern")
                   for p in _COMMON_PATTERNS]

    tried, seen = [], set()
    for tpl, source in candidates:
        if blocked or tpl in seen or len(tried) >= _MAX_PROBES:
            continue
        seen.add(tpl)
        say(f"Checking {tpl.replace('{query}', sample)} ...", 66 + 5 * len(tried))
        r = probe.probe_search(tpl, sample)
        tried.append((tpl, source, r))
        if r["status"] == "ok":
            action.update(search_url=tpl, search_url_guessed=False, search_url_verified=True)
            if found.get("cached") and tpl == found.get("search_url"):
                source = "an address verified before for this site"
            steps.append(f"OK {r['url']} - {r['evidence']} (address from {source})")
            _remember(probe, site, tpl)
            return "verified", r["url"]
        steps.append(f"FAILED {r['url']} - {r['evidence']}")
        if r["status"] == "blocked":
            blocked = True

    if not blocked and llm is not None:
        form_note = ("The homepage has no GET search form JARVIS could read."
                     if not found.get("search_url") else "")
        for attempt in range(_MAX_REPAIRS):
            say("Asking the AI to fix the search address...", 88)
            tpl = _repair_search_url(site, tried, form_note, llm)
            if not tpl or tpl in seen:
                break
            seen.add(tpl)
            r = probe.probe_search(tpl, sample)
            tried.append((tpl, "the AI's repair", r))
            if r["status"] == "ok":
                action.update(search_url=tpl, search_url_guessed=False,
                              search_url_verified=True)
                steps.append(f"OK {r['url']} - {r['evidence']} (address from the AI's repair)")
                _remember(probe, site, tpl)
                return "verified", r["url"]
            steps.append(f"FAILED {r['url']} - {r['evidence']}")

    action["search_url_verified"] = False
    if blocked:
        steps.append(f"Could not verify: {site}'s bot check blocked the hidden browser. "
                     "Press Test to try it in your browser.")
        return "unverified", None
    return "failed", None


def simulate(app_key, rule, entry=None, progress=None, llm=None, probe=None,
             user_text=""):
    """Simulate `rule` for real. Returns (rule, report).

    The returned rule is a copy: a verified search address replaces the
    model's guess, and `verification` records what was checked. report =
    {"status": verified|failed|unverified|simulated|skipped, "steps", "judge",
    "url"}. llm/probe are injectable for tests.
    """
    rule = copy.deepcopy(rule)
    say = progress or (lambda stage, pct: None)
    if probe is None:
        probe = PROBE
    if probe is None:
        import site_probe as probe
    if llm is None:
        llm = _default_llm()
    steps = []
    report = {"status": "skipped", "steps": steps, "judge": None, "url": None}
    action = rule.get("action") or {}
    kind = action.get("type")

    if kind in ("search_site", "open_site") and not rules_engine.action_of(rule, app_key, entry):
        kind = "invalid"

    if kind == "invalid":
        steps.append("FAILED the rule has no valid http(s) web address")
        report["status"] = "failed"
    elif kind in ("search_site", "open_site"):
        sample = sample_for(rule) if kind == "search_site" else ""
        spoken = spoken_example(rule.get("source_phrase", ""), sample) if sample \
            else rule.get("source_phrase", "")
        say(f'Simulating "{spoken}" ...', 55)
        apps = {app_key: dict(entry or {}, enabled=True, compiled_rules=[rule])}
        m = rules_engine.match_action(spoken, apps=apps)
        if not m:
            steps.append(f'FAILED "{spoken}" does not match the rule')
            report["status"] = "failed"
        elif sample and m["slot"].lower() != sample.lower():
            steps.append(f'FAILED "{spoken}" matched, but the name came out as '
                         f'"{m["slot"]}" instead of "{sample}"')
            report["status"] = "failed"
        else:
            steps.append(f'OK "{spoken}" matches the rule'
                         + (f', name = "{m["slot"]}"' if sample else ""))
            if kind == "search_site":
                report["status"], report["url"] = _simulate_search(
                    action, sample, steps, say, probe, llm)
            else:
                url = action.get("url") or ""
                say(f"Checking {url} ...", 70)
                r = probe.probe_open(url)
                steps.append(("OK " if r["status"] == "ok" else "FAILED ")
                             + f"{url} - {r['evidence']}")
                report["status"] = {"ok": "verified", "blocked": "unverified"}.get(
                    r["status"], "failed")
                report["url"] = url
    elif kind == "block" or rule.get("enforcement") in ("hard", "deny", "block"):
        say("Simulating the block...", 70)
        table = {a: rules_engine.check(app_key, {"action": a,
                                                 "entry": {"compiled_rules": [rule]}})
                 for a in ("open", "play", "close")}
        steps.append("Simulated: " + ", ".join(
            f"{a} {'BLOCKED' if v['action'] == 'deny' else 'allowed'}"
            for a, v in table.items()))
        report["status"] = "simulated"
    else:
        steps.append("Reminder only: nothing is opened or blocked.")
        report["status"] = "simulated"

    if report["status"] != "skipped":
        say("Checking it matches what you asked...", 93)
        try:
            report["judge"] = judge_intent(
                user_text or rule.get("action_text") or rule.get("source_phrase", ""),
                rule, steps, llm)
        except Exception:
            report["judge"] = None

    rule["verification"] = {"status": report["status"], "steps": steps,
                            "judge": report["judge"],
                            "checked": time.strftime("%Y-%m-%d %H:%M")}
    return rule, report


def simulate_all(app_key, rules, entry=None, progress=None, llm=None, probe=None):
    """Simulate every rule not checked yet (Compile with JARVIS). Progress is
    spread over the 35-95% band, one slice per rule."""
    say = progress or (lambda stage, pct: None)
    todo = [i for i, r in enumerate(rules)
            if not r.get("verification") and (r.get("action") or {}).get("type")]
    out = list(rules)
    for n, i in enumerate(todo):
        lo, span = 35 + 60 * n / len(todo), 60 / len(todo)
        sub = (lambda stage, pct, lo=lo, span=span: say(stage, lo + span * pct / 100))
        out[i], _ = simulate(app_key, rules[i], entry, progress=sub, llm=llm, probe=probe,
                             user_text=rules[i].get("action_text") or "")
    return out
