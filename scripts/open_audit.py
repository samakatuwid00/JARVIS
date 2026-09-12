"""open_audit.py - does "open X" reach X, for every registered app and site?

A dry run: each phrasing takes the brain's local order (open-site intent, the
model router's lead, a multi-step goal, then delegate()'s fast lanes) with
every launcher replaced by a recorder, so nothing opens and no model is
called. The recorded name is then resolved the way open_site() and
open_application() resolve it.

  python scripts/open_audit.py            summary and failures
  python scripts/open_audit.py --all      also list every passing phrasing

Not replayed: the user's rules (try_rule_action), conversation-window
follow-ups, and what the model router would pick - a phrasing it would take
is reported as "router", neither pass nor fail.
"""

import argparse
import json
import os
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

APP_PHRASES = ("open {}", "can you open {} for me", "open the {} app", "please open {} now")
SITE_PHRASES = ("open {}", "can you open {} for me", "open {} site", "go to the {} website")


class _Stop(Exception):
    """Raised by a recorder: the first launch decides the outcome."""


def _recorders(calls):
    def record(kind, arg):
        calls.append((kind, arg))
        raise _Stop

    def execute_tool(name, args, *a, **k):
        record(name, (args or {}).get("app") or (args or {}).get("name") or args)
    return {
        "execute_tool": execute_tool,
        "open_site": lambda name, url=None, *a, **k: record("open_site", url or name),
        "try_rule_action": lambda *a, **k: None,
        "delegate_to_hermes": lambda *a, **k: record("hermes", None),
        "_run_specialist": lambda *a, **k: record("specialist", None),
        "_run_specialist_bg": lambda *a, **k: record("specialist", None),
        "_run_cli_agent": lambda *a, **k: record("cli", None),
    }


def route(text, apps_abilities):
    """(kind, arg) of what the brain would launch for `text`."""
    import brain_gemini as b
    import intent_router as ir
    import tools
    t = tools.strip_fillers(tools.strip_correction_prefix(tools.strip_fillers(text)))
    intent = b.classify_intent(t)
    if intent == "open_site":
        return ("open_site", b.parse_open_site(t))
    if intent in ("general", "app_reference") and not tools.is_conversational(t):
        if ir.MODE == "lead" and not b._fast_lane_opens(t) and b._router_may_lead(t) \
                and ir.worth_asking(t, {}, apps_abilities):
            return ("router", None)
        if b._is_multistep_goal(t):
            return ("autonomous", None)
    if intent not in ("general", "app_reference", "open_site"):
        return ("intent:" + intent, None)
    calls = []
    saved = {name: getattr(tools, name) for name in _recorders(calls) if hasattr(tools, name)}
    try:
        for name, fn in _recorders(calls).items():
            if name in saved:
                setattr(tools, name, fn)
        try:
            out = tools.delegate(t)
        except _Stop:
            out = None
    finally:
        for name, fn in saved.items():
            setattr(tools, name, fn)
    if calls:
        return calls[0]
    return ("reply", (out or "")[:80])


def _app_key(arg, apps):
    """The registry key open_application() would land on for `arg`, or None."""
    import tools
    from machine_capabilities import resolve_normalized
    name = tools._clean_app_name(str(arg or "")).lower().strip()
    if name in apps:
        return name
    key = resolve_normalized(apps, name)
    if key:
        return key
    try:
        from curate import registered_match
        return registered_match(name)
    except Exception:
        return None


def verdict(kind, arg, want_kind, want_key, apps):
    import web_registry as wr
    if kind == "router":
        return "router"
    if kind == "clarify" or (isinstance(arg, str) and arg.startswith("Found '")):
        return "pass" if want_key in apps and wr.get_site(want_key) else "fail"
    if kind == "open_site":
        import tools
        key = wr.resolve_site(tools._clean_site_name(str(arg or "")))
        return "pass" if want_kind == "site" and key == want_key else "fail"
    if kind == "open_application":
        got = _app_key(arg, apps)
        same_program = got and (apps.get(got) or {}).get("bin") == (apps.get(want_key) or {}).get("bin")
        return "pass" if want_kind == "app" and (got == want_key or same_program) else "fail"
    if kind == "reply" and "which one, the app or the site" in str(arg):
        return "pass" if want_key in apps and wr.get_site(want_key) else "fail"
    return "fail"


def audit():
    import app_abilities
    import web_registry as wr
    apps = json.load(open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                       "app_registry.json"), encoding="utf-8"))["apps"]
    abilities = app_abilities.load_apps()["apps"]
    targets = [("app", k, APP_PHRASES) for k, v in apps.items()
               if isinstance(v, dict) and not v.get("hidden")]
    targets += [("site", k, SITE_PHRASES) for k in wr.load_registry().get("sites", {})]
    rows = []
    for want_kind, key, phrases in targets:
        for p in phrases:
            text = p.format(key)
            kind, arg = route(text, abilities)
            rows.append({"target": key, "want": want_kind, "text": text, "kind": kind,
                         "arg": arg, "verdict": verdict(kind, arg, want_kind, key, apps)})
    return rows


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--all", action="store_true")
    args = ap.parse_args(argv)
    rows = audit()
    tally = Counter(r["verdict"] for r in rows)
    print(f"{len(rows)} phrasings: {tally['pass']} pass, {tally['fail']} fail, "
          f"{tally['router']} left to the model router")
    by_phrase = Counter()
    for r in rows:
        shape = r["text"].replace(r["target"], "{}", 1)
        by_phrase[(r["want"], shape, r["verdict"])] += 1
    for (want, shape, v), n in sorted(by_phrase.items()):
        print(f"  {want:<5} {shape:<28} {v:<7} {n}")
    for r in rows:
        if r["verdict"] == "fail" or args.all:
            print(f"  [{r['verdict']}] {r['text']!r} -> {r['kind']}({str(r['arg'])[:60]!r})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
