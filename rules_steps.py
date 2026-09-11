"""rules_steps.py - multi-step rules: run a rule's steps in order and consult
the user before each consequential one.

A steps rule (compiled by rules_ai) carries:
    action = {"type": "steps", "site", "url", "search_url", "browser", "slot",
              "steps": [{"op": "search"}, {"op": "pick", "count": 5},
                        {"op": "open"}, {"op": "click", "target": "play", "confirm": true}]}
Desktop apps (any non-browser app in the registry) use
    {"op": "launch"}, {"op": "click", "target": "Liked Songs"},
    {"op": "type", "target": "Search", "text": "{query}"}, {"op": "key", "keys": "{ENTER}"}.

Consulting (the user's rule, 2026-09-11): "pick" lists the top picks from the
real results page and waits for the choice; a step with "confirm": true asks
before acting. One run waits between voice turns; tools.try_rule_action hands
the next reply to resume().
"""

import html
import re
import time
import urllib.parse

import rules_engine

WEB_OPS = {"search", "pick", "open", "click", "wait", "ask"}
DESKTOP_OPS = {"launch", "click", "type", "key", "wait", "ask"}
PICK_COUNT = 5
RUN_TTL = 180          # seconds a question may stay unanswered
MAX_WAIT = 10          # cap for a "wait" step, seconds

_RUN = {}

_ORDINALS = {"first": 1, "one": 1, "second": 2, "two": 2, "third": 3, "three": 3,
             "fourth": 4, "four": 4, "fifth": 5, "five": 5, "sixth": 6, "six": 6,
             "seventh": 7, "seven": 7, "eighth": 8, "eight": 8, "ninth": 9, "nine": 9,
             "tenth": 10, "ten": 10}
_CHOICE_FILLER = {"the", "a", "an", "one", "movie", "film", "show", "please", "i", "want",
                  "watch", "open", "pick", "choose", "number", "no", "option", "that",
                  "play", "give", "me", "lets", "let's", "go", "with", "sir", "jarvis",
                  "it", "this", "start", "stream", "now"}
_YES_RE = re.compile(r"^(yes|yeah|yep|yup|sure|ok(ay)?|go( ahead)?|do it|play( it)?|"
                     r"please|confirm|continue|restart( it)?)\b", re.I)
_NO_RE = re.compile(r"^(no|nope|nah|don'?t|stop|cancel|never ?mind|skip|leave it)\b", re.I)
# A reply that is only a stop word; "No Time to Die" is a title, not a no.
_STOP_RE = re.compile(r"^(no|nope|nah|stop|cancel|never ?mind|forget it|leave it)[\s.!,]*$", re.I)
_ANCHOR_RE = re.compile(r"<a\b([^>]*)>(.*?)</a>", re.I | re.S)
_ARTICLES = {"the", "a", "an"}
# Keys a desktop step may press: navigation and common edit shortcuts only —
# never the Windows key, Alt combos or anything that reaches outside the app.
SAFE_KEYS_RE = re.compile(
    r"^(?:\{(?:ENTER|TAB|ESC|SPACE|UP|DOWN|LEFT|RIGHT|HOME|END|PGUP|PGDN|BACKSPACE|DELETE|F5)\}"
    r"|\^[acfklvx])+$", re.I)
# pywinauto reads these as modifiers/groups; a typed name must send them literally.
_TYPE_SPECIALS_RE = re.compile(r"([+^%~(){}\[\]])")
_NAV_LABELS = {"home", "login", "log in", "sign in", "register", "next", "previous", "prev",
               "more", "menu", "search", "contact", "dmca", "faq", "genre", "genres",
               "country", "movies", "tv shows", "tv series", "top imdb", "privacy policy"}


# ------------------------------------------------------------------- picks --

def _attr(attrs, name):
    m = re.search(name + r'\s*=\s*"([^"]*)"', attrs, re.I) or \
        re.search(name + r"\s*=\s*'([^']*)'", attrs, re.I)
    return html.unescape(m.group(1)).strip() if m else ""


def _label(attrs, inner):
    text = " ".join(html.unescape(re.sub(r"<[^>]+>", " ", inner)).split())
    alt = _attr(inner, "alt")
    return _attr(attrs, "title") or text or alt


def _closeness(name, words):
    """Rank key, lower is closer: the exact title ('Dune (1984)' for 'Dune'),
    then titles that start with it ('Dune: Part Two'), then every word as a
    whole word, then partial matches ('The Dunes')."""
    base = re.sub(r"\s*[(\[]?(19|20)\d{2}[)\]]?\s*$", "", name.lower())
    toks = re.findall(r"[a-z0-9]+", base)
    # A leading article never decides closeness: the matcher trims "The" off
    # spoken names, so "The Social Network (2010)" must still be exact for
    # "Social Network".
    while toks and toks[0] in _ARTICLES:
        toks = toks[1:]
    words = list(words)
    while words and words[0] in _ARTICLES:
        words = words[1:]
    query = " ".join(words)
    whole = sum(1 for w in words if w in toks)
    joined = " ".join(toks)
    tier = (0 if joined == query else 1 if joined.startswith(query + " ")
            else 2 if whole == len(words) else 3)
    return tier, -whole


def extract_picks(url, query, count=PICK_COUNT, dom=None):
    """Top results on a search-results page: [{"title", "url"}], closest first.

    Links must stay on the site, name the query, and not be navigation or
    pagination. The site's own order breaks ties (it is already by relevance).
    count=None returns every result, for choosing a title that wasn't listed.
    """
    if dom is None:
        import site_probe
        dom = site_probe.load(url)["dom"]
    words = [w for w in re.findall(r"[a-z0-9]+", (query or "").lower()) if len(w) > 1]
    here = url.split("#", 1)[0].rstrip("/")
    seen, scored = set(), []
    for attrs, inner in _ANCHOR_RE.findall(dom or ""):
        href = _attr(attrs, "href")
        if not href or href.startswith(("#", "javascript:")):
            continue
        href = urllib.parse.urljoin(url, href).split("#", 1)[0]
        name = _label(attrs, inner)
        low = name.lower()
        if (href in seen or href.rstrip("/") == here or not rules_engine.same_site(href, url)
                or not rules_engine.is_web_url(href) or re.search(r"[?&](s|q|page)=|/page/\d", href)
                or len(name) < 2 or low in _NAV_LABELS):
            continue
        hits = sum(1 for w in words if w in low)
        if words and hits * 2 < len(words):
            continue
        seen.add(href)
        scored.append((_closeness(name, words), len(scored), {"title": name[:90], "url": href}))
    scored.sort(key=lambda s: s[:2])
    return [p for _, _, p in scored[:count]]


def format_picks(picks):
    return " ".join(f"{i}. {p['title']}." for i, p in enumerate(picks, 1))


def choose(answer, picks):
    """Index of the pick the user meant, or None.

    '2' / 'number 2', 'the 1984 one', 'part two', 'the second one', 'the last one'.
    """
    a = (answer or "").lower().strip(" .!?")
    m = re.search(r"(?:^|number |#|no\.? )(\d{1,2})\b", a) or re.fullmatch(r"(\d{1,2})", a)
    if m and 1 <= int(m.group(1)) <= len(picks):
        return int(m.group(1)) - 1
    words = [w for w in re.findall(r"[a-z0-9']+", a) if w not in _CHOICE_FILLER]
    year = re.search(r"\b(19|20)\d{2}\b", a)
    if year:
        hits = [i for i, p in enumerate(picks) if year.group(0) in p["title"]]
        if len(hits) == 1:
            return hits[0]
    if words and all(w in _ORDINALS or w == "last" for w in words):
        n = len(picks) if words[-1] == "last" else _ORDINALS[words[-1]]
        return n - 1 if n <= len(picks) else None
    if "last" in a.split() and not words:
        return len(picks) - 1
    scores = [sum(1 for w in words if w in p["title"].lower()) for p in picks]
    best = max(scores, default=0)
    if best and scores.count(best) == 1:
        return scores.index(best)
    ordinal = next((_ORDINALS[w] for w in words if w in _ORDINALS), None)
    return ordinal - 1 if ordinal and ordinal <= len(picks) else None


# ------------------------------------------------------- follow-ups ("it") --
# What the last rule left on screen, so "play it" / "open number 2" can pick
# up from there: a search-results page ("results") or an opened title ("page").

LAST_TTL = 900         # seconds "it" keeps pointing at the last result
_LAST = {}
_FOLLOW_VERB_RE = re.compile(r"^(play|watch|stream|open|pick|choose|start)\b", re.I)
_FOLLOW_REF_RE = re.compile(
    r"\b(it|that|this|one|number|first|second|third|fourth|fifth|last|\d{1,2})\b", re.I)
_PLAY_RE = re.compile(r"\b(play|watch|stream|start)\b", re.I)


def remember(kind, run, url, title=None):
    """Record what a rule just showed; the next "play it" continues from it."""
    _LAST.clear()
    _LAST.update(kind=kind, url=url, title=title, ts=time.time(),
                 rule=run["rule"], owner=run["owner"], action=run["action"],
                 slot=run.get("slot", ""), entry=run.get("entry") or {})


def is_follow_up(text):
    """'play it', 'play the first one', 'open number 2' — short, a follow-up
    verb, and a word pointing back at what is on screen."""
    words = (text or "").split()
    return (bool(_LAST) and time.time() - _LAST.get("ts", 0) <= LAST_TTL and len(words) <= 7
            and bool(_FOLLOW_VERB_RE.match(text or "")) and bool(_FOLLOW_REF_RE.search(text or "")))


def follow_up(text):
    """Continue from the last rule's result. On a results page: top picks,
    the user's choice (taken straight from the reply when it names one, e.g.
    "play the first one"), open, and press play when asked to play. On an
    opened title: press play. The explicit "play" is the consent — no second
    'Should I press play?'."""
    ctx = dict(_LAST)
    wants_play = bool(_PLAY_RE.search(text))
    play = [{"op": "click", "target": "play"}] if wants_play else []
    steps = ([{"op": "pick", "count": PICK_COUNT}, {"op": "open"}] + play
             if ctx["kind"] == "results" else play)
    if not steps:
        return f"It's already open: {ctx.get('title') or ctx['url']}."
    run = {"rule": ctx["rule"], "owner": ctx["owner"], "slot": ctx["slot"], "i": 0,
           "entry": ctx["entry"], "url": ctx["url"], "title": ctx.get("title"),
           "action": dict(ctx["action"], type="steps", steps=steps)}
    cancel()
    out = _advance(run)
    if active() and _RUN.get("awaiting") == "pick" \
            and choose(text, _RUN.get("picks") or []) is not None:
        return resume(text)
    return out


# --------------------------------------------------------------------- run --

def active():
    return bool(_RUN) and time.time() - _RUN.get("ts", 0) <= RUN_TTL


def cancel():
    _RUN.clear()


def _is_web(run):
    return bool(run["action"].get("url") or run["action"].get("site"))


def _window(run):
    """Title pattern for a desktop app's window: its registry name."""
    name = (run.get("entry") or {}).get("name") or run["owner"]
    return re.escape(name)


def _needs_clicks(run):
    return any(s.get("op") == "click" for s in run["action"]["steps"][run["i"]:])


def _audit(run, step, result):
    try:
        import audit
        audit.log_call("rules.step", {"rule": run["rule"].get("rule_id"), "op": step.get("op"),
                                      "target": step.get("target"), "url": run.get("url")},
                       0.0, result)
    except Exception:
        pass


def _open(run, url, title):
    """Open url in the rule's browser; attach the debugging port when a later
    step must click. Returns (message, wait_kind)."""
    import browser_cdp
    import tools
    browser = run["action"].get("browser") or "chrome"
    if _needs_clicks(run) and browser_cdp.supported(browser) and not run.get("no_clicks"):
        if browser_cdp.is_attached(browser):
            ok = browser_cdp.open_tab(browser, url) is not None
        elif not browser_cdp.is_running(browser):
            ok = browser_cdp.launch(browser, url)
        else:
            run["pending_open"] = (url, title)
            return (f"{browser_cdp.label(browser)} is open without JARVIS control. Restart "
                    f"{browser_cdp.label(browser)} so I can click in the page? Your tabs come "
                    f"back if it restores sessions. Say yes, or no to open it without clicks.",
                    "restart")
        if ok:
            remember("page", run, url=url, title=title)
            return f"Opened {title} in {browser_cdp.label(browser)}.", None
        return f"[Error] {browser_cdp.label(browser)} didn't open its control port.", None
    out = tools._open_url_in_browser(url, title, browser, remember=False)
    if out.startswith("[Error]"):
        return out, None
    remember("page", run, url=url, title=title)
    return f"Opened {title}.", None


def _do_web(run, step):
    op = step.get("op")
    action = run["action"]
    if op == "search":
        run["url"] = rules_engine.build_action_url(dict(action, type="search_site"), run["slot"])
        return "", None
    if op == "pick":
        everything = extract_picks(run.get("url") or action.get("url", ""), run["slot"],
                                   count=None)
        picks = everything[:int(step.get("count") or PICK_COUNT)]
        if not picks:
            return (f"[Error] I couldn't read any results for {run['slot'] or 'that'} on "
                    f"{action.get('site') or 'the site'}."), None
        run["picks"], run["all_picks"] = picks, everything
        return (f"Top picks for {run['slot']}: {format_picks(picks)} Which one?"), "pick"
    if op == "open":
        url = run.get("url") or action.get("url")
        return _open(run, url, run.get("title") or action.get("site") or url)
    if op == "click":
        if run.get("no_clicks"):
            return f"You can {step.get('target') or 'click'} it yourself.", None
        import browser_cdp
        return browser_cdp.click(action.get("browser") or "chrome", step.get("target") or "play",
                                 url_hint=run.get("url")), None
    return _do_common(run, step)


def _do_desktop(run, step):
    import desktop_driver
    import tools
    op = step.get("op")
    if op == "launch":
        return tools.open_application(run["owner"]), None
    # "50% off" must type a percent sign, not press Alt: escape the name first.
    slot = _TYPE_SPECIALS_RE.sub(r"{\1}", run["slot"])
    text = str(step.get("text") or "").replace("{query}", slot)
    if op == "click":
        return desktop_driver.click_control(_window(run), step.get("target")), None
    if op == "type":
        return desktop_driver.type_into_control(_window(run), step.get("target") or "Search",
                                                text), None
    if op == "key":
        keys = str(step.get("keys") or "{ENTER}")
        if not SAFE_KEYS_RE.match(keys):
            return f"[Error] I won't press \"{keys}\": only Enter, Tab, arrows and edit shortcuts.", None
        win = desktop_driver.find_window(_window(run))
        if not win:
            return f"[Error] {run['owner']} isn't open.", None
        win.set_focus()
        win.type_keys(keys)
        return "", None
    return _do_common(run, step)


def _do_common(run, step):
    op = step.get("op")
    if op == "wait":
        time.sleep(min(float(step.get("seconds") or 1), MAX_WAIT))
        return "", None
    if op == "ask":
        return str(step.get("question") or "Should I continue?"), "confirm_ask"
    return f"[Error] I don't know how to do the step \"{op}\".", None


def _confirm_question(run, step):
    target = step.get("target") or step.get("op")
    what = run.get("title") or run["slot"] or ""
    if step.get("op") == "click" and re.search(r"play|watch|stream", str(target), re.I):
        return f"Should I press play{' on ' + what if what else ''}?"
    return f"Should I {step.get('op')} {target}?"


def _advance(run):
    """Run steps until one needs the user or all are done. Returns what to say."""
    said = []
    steps = run["action"]["steps"]
    do = _do_web if _is_web(run) else _do_desktop
    while run["i"] < len(steps):
        step = steps[run["i"]]
        if step.get("confirm") and run.get("confirmed") != run["i"] and not run.get("no_clicks"):
            run["awaiting"] = "confirm"
            said.append(_confirm_question(run, step))
            return _pause(run, said)
        try:
            msg, wait = do(run, step)
        except Exception as e:
            # Say what failed; an exception escaping here would drop the turn
            # into the generic search path with this question still open.
            msg, wait = f"[Error] The \"{step.get('op')}\" step failed ({type(e).__name__}).", None
        _audit(run, step, msg or "ok")
        if msg:
            said.append(msg)
        if msg.startswith("[Error]"):
            cancel()
            return " ".join(said)
        if wait:
            run["awaiting"] = wait
            return _pause(run, said)
        run["i"] += 1
    cancel()
    return " ".join(s for s in said if s) or "Done."


def _pause(run, said):
    run["ts"] = time.time()
    _RUN.clear()
    _RUN.update(run)
    return " ".join(s for s in said if s)


def start(rule, owner, action, slot="", entry=None):
    """Begin a steps rule. Returns what JARVIS says (a result or a question)."""
    cancel()
    if action.get("steps") and action["steps"][0].get("op") == "search" and not slot:
        label = re.sub(r"\s+name$", "", action.get("slot") or "") or "one"
        run = {"rule": rule, "owner": owner, "action": action, "slot": "", "i": 0,
               "entry": entry or {}, "awaiting": "slot"}
        return _pause(run, [f"Which {label}, sir?"])
    run = {"rule": rule, "owner": owner, "action": action, "slot": slot or "", "i": 0,
           "entry": entry or {}}
    return _advance(run)


def resume(text, looks_new=False):
    """Feed the user's reply into the waiting run. None when no run is waiting
    or the reply is a new request rather than an answer (looks_new: the caller
    saw a command verb or question) — the caller then routes it normally."""
    if not active():
        cancel()
        return None
    run = dict(_RUN)
    reply = (text or "").strip()
    kind = run.get("awaiting")
    # For the restart question a plain "no" means "open it without clicks";
    # only an explicit cancel ends the run there.
    stop = (re.match(r"^(cancel|stop|never ?mind|forget it)\b", reply, re.I) if kind == "restart"
            else _STOP_RE.match(reply) or (kind in ("confirm", "confirm_ask")
                                           and _NO_RE.match(reply)))
    if stop:
        cancel()
        return "Okay, I'll leave it there."
    if kind == "slot":
        answer = rules_engine.answer_slot(reply, run["rule"])
        if looks_new and not answer:
            cancel()
            return None
        run["slot"] = answer or reply.strip(" .!?,")
        return _advance(run)
    if kind == "pick":
        shown, everything = run.get("picks") or [], run.get("all_picks") or []
        i = choose(reply, shown)
        # A title the user names that was on the page but not in the top list
        # ("the 1984 one" when only five were read out) still counts.
        j = None if i is not None or re.fullmatch(r"\D*\d{1,2}\D*", reply) \
            else choose(reply, everything)
        if i is None and j is None and looks_new:
            cancel()
            return None
        if i is None and j is None:
            return _pause(run, [f"Say a number from 1 to {len(shown)}, or the title."])
        pick = shown[i] if i is not None else everything[j]
        run.update(url=pick["url"], title=pick["title"], i=run["i"] + 1)
        return _advance(run)
    if kind == "restart":
        pending = run.pop("pending_open", None)
        if not pending:
            cancel()
            return "[Error] I lost track of which page to open. Say the command again."
        url, title = pending
        if _YES_RE.match(reply):
            import browser_cdp
            browser = run["action"].get("browser") or "chrome"
            if not browser_cdp.restart(browser, url):
                cancel()
                return f"[Error] {browser_cdp.label(browser)} didn't come back with its control port."
            run["i"] += 1
            return _advance(dict(run, url=url))
        run["no_clicks"] = True
        return _advance(run)
    if kind in ("confirm", "confirm_ask"):
        if not _YES_RE.match(reply):
            if looks_new:
                cancel()
                return None
            return _pause(run, ["Say yes to continue, or no to stop."])
        if kind == "confirm":
            run["confirmed"] = run["i"]
        else:
            run["i"] += 1
        return _advance(run)
    cancel()
    return None
