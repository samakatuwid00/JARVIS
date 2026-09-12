"""routing_eval.py - score JARVIS's routing against a labeled set.

Step 2 of the semantic-routing plan: before anything replaces the keyword
router, measure it. eval/routing_gold.jsonl holds one utterance per line with
the route JARVIS should take (a coarse class: chat, task, open_app, music...)
and its dialogue act. Two routers are scored:

  keyword - brain_gemini's local decision order (bare confirm/decline,
            classify_intent, the own-work / conversational / multi-step
            checks, then delegate()'s search and open fast lanes), replayed
            offline. An approximation: the user's rules, pending questions,
            conversation fragments and the model router are not replayed.
  live    - what JARVIS actually did, for turns in logs/shadow.jsonl.

  python routing_eval.py              keyword router, every label
  python routing_eval.py --reviewed   only labels a person has checked
  python routing_eval.py --live       also score the live turns
  python routing_eval.py --semantic   also cross-validate the semantic router
  python routing_eval.py --misses 40  list up to 40 misses per router
"""

import argparse
import datetime
import json
import os
import re
from collections import Counter

import numpy as np

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
GOLD = os.path.join(BASE_DIR, "eval", "routing_gold.jsonl")
SHADOW = os.path.join(BASE_DIR, "logs", "shadow.jsonl")
AUDIT = os.path.join(BASE_DIR, "logs", "audit.jsonl")

# classify_intent labels that name their route outright.
INTENT_ROUTE = {
    "math": "math", "greeting": "greeting", "thanks": "thanks", "time": "time", "help": "help",
    "clarify_play": "clarify", "clarify_search": "clarify", "clarify_open": "clarify",
    "list_sites": "list_sites", "list_installed": "list_apps", "report": "report",
    "launch": "launch_project", "rescan": "rescan_apps", "app_install": "app_install",
    "app_uninstall": "app_uninstall", "job_status": "job_status", "job_stop": "job_stop",
    "recall_memory": "recall_memory", "remember": "remember", "close_app": "close_app",
    "music": "music", "stop": "media_control", "open_site": "open_site", "add_site": "add_site",
    "web_browse": "read_page",
}
# Instant-path turn labels (last_stats["intent"]) that are not classify_intent's.
_LIVE_INTENT_ROUTE = {"confirm": "confirm", "decline": "decline", "ability_consent": "confirm",
                      "music_favorites": "music", "rule_action": "rule"}
# Routes that answer in words; a question landing anywhere else was acted on.
ANSWERS = {"chat", "time", "greeting", "thanks", "math", "help", "list_sites", "list_apps",
           "job_status", "recall_memory", "clarify"}
# One of the user's own rules is right wherever the label is one of these.
RULE_OK = {"web_search", "open_site", "app_action", "music"}
# Answered in words by the brain instead of the instant path: slower, same outcome.
ANSWER_OK = {"math", "greeting", "thanks", "help"}
# Semantic router cross-validation: folds, what counts as a near-duplicate
# (kept in one fold so no utterance is tested against its twin), and the
# minimum scores swept.
FOLDS = 5
TWIN_SIMILARITY = 0.9
THRESHOLDS = (0.0, 0.5, 0.6, 0.7, 0.8)
# The best plain-hybrid minimum from the sweep on 2026-09-12.
TRUST_MIN_SCORE = 0.7

_OPEN_RE = re.compile(r"^\W*(?:(?:please|jarvis|cygnus|hey|ok(?:ay)?|now)\b\W*)*"
                      r"(?:open|launch|go\s+to|goto|visit|browse|take\s+me\s+to)\s+(.+)$", re.I | re.S)


def load_jsonl(path):
    rows = []
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    continue
    except OSError:
        pass
    return rows


def load_gold(path=GOLD, reviewed_only=False):
    return [r for r in load_jsonl(path)
            if r.get("route") and (r.get("reviewed") or not reviewed_only)]


def keyword_route(text):
    """The route the keyword router takes for `text`, with no model call."""
    import brain_gemini as b
    import tools
    t = (text or "").strip()
    if b._BARE_CONFIRM_RE.match(t):
        return "confirm"
    if b._BARE_DECLINE_RE.match(t):
        return "decline"
    intent = b.classify_intent(t)
    if intent in INTENT_ROUTE:
        return INTENT_ROUTE[intent]
    if b._asks_about_own_work(t) or tools.is_conversational(t):
        return "chat"
    if b._is_multistep_goal(t):
        return "task"
    rt = tools.strip_fillers(t)
    try:
        if tools.parse_search_command(rt):
            return "web_search"
    except Exception:
        pass
    m = _OPEN_RE.match(tools.strip_fillers(tools.strip_correction_prefix(rt)).rstrip(".!?"))
    if m:
        try:
            stripped, bword = tools._split_browser(m.group(1))
            kind, _ = tools.resolve_open_target(
                stripped, browser=tools._browser_key(bword) if bword else None)
        except Exception:
            kind = None
        route = {"site": "open_site", "app": "open_app", "clarify": "clarify"}.get(kind)
        if route:
            return route
    # delegate(): the backend it detects, then that backend's local handler.
    try:
        backend = tools._detect_backend(rt)
    except Exception:
        backend = "hermes"
    if backend == "desktop":
        m = tools._DESKTOP_VERB_RE.match(rt.rstrip(".!?"))
        if m:
            return "close_app" if m.group(1).lower() in ("close", "quit", "exit") else "open_app"
    if backend == "music":
        return "media_control" if "stop" in rt.lower() else "music"
    return "web_search" if backend == "web" else "task"


def live_route(record):
    """What JARVIS actually did on one shadow turn, as a route class."""
    a = record.get("actual") or {}
    kind, aid = a.get("type"), str(a.get("id") or "")
    if kind == "web_search":
        return "web_search"
    if kind == "delegate":
        return "task"
    if kind == "rule":
        return "rule"
    if kind == "ability":
        fixed = {"open": "open_app", "close": "close_app", "open_site": "open_site",
                 "play_music": "music", "play_spotify": "music", "stop_music": "media_control",
                 "stop_spotify": "media_control", "web_browse": "read_page"}
        if aid in fixed:
            return fixed[aid]
        if re.search(r"volume|mute|pause|resume|stop|skip|next|previous", aid):
            return "media_control"
        return "music" if "play" in aid else "app_action"
    # A chat turn's id is the intent the brain classified it as.
    return INTENT_ROUTE.get(aid) or _LIVE_INTENT_ROUTE.get(aid) or "chat"


def _ts(value):
    try:
        return datetime.datetime.fromisoformat(str(value).replace(" ", "T"))
    except ValueError:
        return None


# Tools actual_route learned on 2026-09-12 whose use the reply confirms
# ("Playing ... on Spotify"). Turns logged before then that used one say
# "chat". Only these are re-read: records carry no audit offset, so a time
# window also catches the neighbouring turns' actions (a math question picked
# up the previous turn's "open"). Hermes hand-offs stay as logged - too
# often another turn's.
_RELEARNED = ("play_spotify", "web_browse")


def rebuild_actual(records, audit):
    """Logged turns, with the plain-chat ones that used a relearned tool
    re-read from the audit lines between the previous record and their own."""
    import intent_router
    stamped = [(t, a) for a in audit
               if (a.get("tool") or (a.get("caller") or "").replace("execute_tool:", "")) in _RELEARNED
               and (t := _ts(a.get("ts")))]
    out, prev = [], None
    for r in records:
        t = _ts(r.get("ts"))
        if t is None:
            out.append(r)
            continue
        end = t + datetime.timedelta(seconds=1)       # record ts has no milliseconds
        start = max(prev, t - datetime.timedelta(minutes=2)) if prev else t - datetime.timedelta(minutes=2)
        old = r.get("actual") or {}
        entries = [a for ta, a in stamped if start < ta <= end]
        if old.get("type") == "chat" and not old.get("id") and entries:
            r = dict(r, actual=intent_router.actual_route(entries))
        out.append(r)
        prev = end
    return out


def false_disagreements(records, rebuilt):
    """Logged disagreements that agree once the turn's actions are re-read."""
    import intent_router
    return [r for r, n in zip(records, rebuilt)
            if not r.get("agree") and intent_router.agree(r.get("decision") or {}, n["actual"])]


def hit(pred, gold):
    return pred == gold or (pred == "rule" and gold in RULE_OK) or \
        (pred == "chat" and gold in ANSWER_OK)


def score(pairs):
    """pairs: (gold row, predicted route). Accuracy, recall per gold route,
    confusions, misses, and questions the router acted on instead of answering."""
    per = {}
    confusion = Counter()
    misses = []
    acted_on = []
    for row, pred in pairs:
        gold = row["route"]
        n, h = per.get(gold, (0, 0))
        ok = hit(pred, gold)
        per[gold] = (n + 1, h + ok)
        if not ok:
            confusion[(gold, pred)] += 1
            misses.append((row.get("text", ""), gold, pred))
        if row.get("act") in ("question", "about_own_work") and gold in ANSWERS \
                and pred not in ANSWERS:
            acted_on.append((row.get("text", ""), pred))
    total = sum(n for n, _ in per.values())
    hits = sum(h for _, h in per.values())
    return {"n": total, "hits": hits, "accuracy": round(hits / total, 3) if total else 0.0,
            "per_route": per, "confusion": confusion, "misses": misses, "acted_on": acted_on}


def score_keyword(gold):
    return score((row, keyword_route(row["text"])) for row in gold)


def score_live(gold, shadow=None):
    """Only turns that have a label: the gold rows list their shadow ids."""
    by_id = {sid: row for row in gold for sid in row.get("shadow_ids") or []}
    records = rebuild_actual(load_jsonl(SHADOW), load_jsonl(AUDIT)) if shadow is None else shadow
    return score((by_id[r["id"]], live_route(r)) for r in records if r.get("id") in by_id)


def _groups(vectors):
    """Group id per utterance; near-duplicates share one."""
    group = [-1] * len(vectors)
    for i in range(len(vectors)):
        if group[i] >= 0:
            continue
        group[i] = i
        for j in np.flatnonzero(vectors[i + 1:] @ vectors[i] >= TWIN_SIMILARITY) + i + 1:
            if group[j] < 0:
                group[j] = i
    return group


def fold_ids(gold):
    """Fold per row; near-duplicates share one."""
    import semantic_route as sr
    return [g % FOLDS for g in _groups(sr.embed([r["text"] for r in gold]))]


def cross_validate(gold):
    """(row, route, score) per row, each routed with no threshold by a router
    built from the folds it is not in."""
    import semantic_route as sr
    vectors = sr.embed([r["text"] for r in gold])
    fold = [g % FOLDS for g in _groups(vectors)]
    out = [None] * len(gold)
    for k in range(FOLDS):
        train = [i for i, f in enumerate(fold) if f != k]
        router = sr.Router([gold[i]["text"] for i in train], [gold[i]["route"] for i in train],
                           vectors=vectors[train])
        for i in (i for i, f in enumerate(fold) if f == k):
            out[i] = (gold[i], *router.route(vector=vectors[i], min_score=0.0))
    return out


def semantic_table(cv, keyword):
    """Per minimum score: how many turns the semantic router takes, how many of
    those it gets right, and the hybrid score (semantic at or above the
    minimum, the keyword route below it)."""
    rows = []
    for t in THRESHOLDS:
        taken = [(row, label) for row, label, s in cv if s >= t]
        right = sum(hit(label, row["route"]) for row, label in taken)
        hybrid = score((row, label if s >= t else keyword[row["id"]]) for row, label, s in cv)
        rows.append((t, len(taken), right, hybrid))
    return rows


def trusted_routes(triples):
    """Semantic labels worth trusting: those where, over these (row, semantic
    label, keyword route) triples, the semantic pick was right more often
    than the keyword route on the same rows."""
    tally = {}
    for row, label, kw in triples:
        sem, key = tally.get(label, (0, 0))
        tally[label] = (sem + hit(label, row["route"]), key + hit(kw, row["route"]))
    return {label for label, (sem, key) in tally.items() if sem > key}


def trust_eval(cv, keyword, folds, min_score=TRUST_MIN_SCORE):
    """Hybrid that takes the semantic pick only for labels it earned, with the
    labels chosen on the other folds - so the score is not graded on the
    same rows that chose it. Returns (score, labels trusted on all rows)."""
    def triples(rows):
        return [(row, label, keyword[row["id"]]) for row, label, s in rows if s >= min_score]
    preds = []
    for k in range(FOLDS):
        trusted = trusted_routes(triples(c for c, f in zip(cv, folds) if f != k))
        for (row, label, s), f in zip(cv, folds):
            if f == k:
                preds.append((row, label if s >= min_score and label in trusted else keyword[row["id"]]))
    return score(preds), trusted_routes(triples(cv))


def report_semantic(gold):
    pool = [r for r in gold if not r.get("unsure")]
    keyword = {r["id"]: keyword_route(r["text"]) for r in pool}
    base = score((r, keyword[r["id"]]) for r in pool)
    lines = [f"\n== semantic router, {FOLDS}-fold cross-validation on {len(pool)} confident labels "
             f"(keyword router on the same set: {base['accuracy']:.1%}, "
             f"questions acted on {len(base['acted_on'])})",
             f"  {'min score':>9}{'takes':>8}{'right there':>13}{'hybrid':>9}{'acted on':>10}"]
    cv = cross_validate(pool)
    for t, taken, right, hybrid in semantic_table(cv, keyword):
        lines.append(f"  {t:>9.2f}{taken:>8}{(right / taken if taken else 0.0):>13.1%}"
                     f"{hybrid['accuracy']:>9.1%}{len(hybrid['acted_on']):>10}")
    trust, trusted = trust_eval(cv, keyword, fold_ids(pool))
    lines.append(f"  per-route trust at min score {TRUST_MIN_SCORE:.2f} (labels chosen on the other folds): "
                 f"{trust['accuracy']:.1%}, questions acted on {len(trust['acted_on'])}")
    lines.append(f"  labels trusted on all rows: {', '.join(sorted(trusted)) or 'none'}")
    return "\n".join(lines)


def report(name, s, misses=20):
    lines = [f"\n== {name}: {s['hits']}/{s['n']} right ({s['accuracy']:.1%}); "
             f"questions acted on instead of answered: {len(s['acted_on'])}"]
    lines.append(f"  {'route':<15}{'n':>5}{'right':>7}{'recall':>8}")
    for route, (n, h) in sorted(s["per_route"].items(), key=lambda kv: -kv[1][0]):
        lines.append(f"  {route:<15}{n:>5}{h:>7}{h / n:>8.0%}")
    if s["confusion"]:
        lines.append("  most common mistakes (should be -> went to):")
        for (gold, pred), c in s["confusion"].most_common(8):
            lines.append(f"    {c:>3}  {gold} -> {pred}")
    if s["acted_on"]:
        lines.append("  questions acted on:")
        for text, pred in s["acted_on"][:misses]:
            lines.append(f"    [{pred}] {text[:90]}")
    if misses and s["misses"]:
        lines.append(f"  misses (first {min(misses, len(s['misses']))}):")
        for text, gold, pred in s["misses"][:misses]:
            lines.append(f"    {gold:>13} <- {pred:<13} {text[:80]}")
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--gold", default=GOLD)
    ap.add_argument("--reviewed", action="store_true", help="only person-checked labels")
    ap.add_argument("--live", action="store_true", help="also score logs/shadow.jsonl turns")
    ap.add_argument("--semantic", action="store_true", help="also cross-validate the semantic router")
    ap.add_argument("--misses", type=int, default=20)
    args = ap.parse_args(argv)
    gold = load_gold(args.gold, reviewed_only=args.reviewed)
    if not gold:
        print(f"No labels in {args.gold}" + (" marked reviewed" if args.reviewed else ""))
        return 1
    reviewed = sum(1 for r in gold if r.get("reviewed"))
    print(f"{len(gold)} labeled utterances ({reviewed} reviewed by a person"
          f"{'' if reviewed else ' - numbers are provisional'})")
    print(report("keyword router (offline replay)", score_keyword(gold), args.misses))
    if args.live:
        print(report("live JARVIS (shadow log, actions re-read from the audit trail)",
                     score_live(gold), args.misses))
        records = load_jsonl(SHADOW)
        false = false_disagreements(records, rebuild_actual(records, load_jsonl(AUDIT)))
        logged = sum(1 for r in records if not r.get("agree"))
        print(f"\n{len(false)} of {logged} logged router disagreements agree once the turn's "
              "actions are re-read - no verdict needed for those")
    if args.semantic:
        print(report_semantic(gold))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
