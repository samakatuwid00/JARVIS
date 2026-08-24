"""conversation_window.py — rolling context window for JARVIS's LOCAL fast path.

Phase 9 of jarvis_rearchitecture.md. Hermes-routed turns already carry
conversation memory via the warm session; this module gives the local router
(open app/site, music, notepad...) the same sense of "what were we just
talking about" without any model calls:

  - append(utterance, kind, payload) after every routed turn
  - classify(text) decides: COMMAND (route normally) | FRAGMENT (complete
    from previous turn) | CLARIFY_ANSWER (answers a pending question)
  - complete(fragment) resolves a fragment against the last turn

Stdlib only, in-process. Window is lost on restart — by design.
"""

import re
import time

MAX_TURNS = 6                 # rolling window size
TURN_TTL = 120                # seconds a turn stays referable

# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------

_turns = []   # [{t, text, kind, tool, result}]


def reset() -> None:
    _turns.clear()


def append(text: str, kind: str, tool: str = "", result: str = "",
           payload: dict = None) -> None:
    """Record a handled turn. kind: command|fragment|clarify_answer|hermes."""
    now = time.time()
    # drop expired turns first so the window never grows unbounded
    while _turns and now - _turns[0]["t"] > TURN_TTL:
        _turns.pop(0)
    _turns.append({"t": now, "text": (text or "").strip(),
                   "kind": kind, "tool": tool, "result": (result or "")[:200],
                   "payload": payload or {}})
    while len(_turns) > MAX_TURNS:
        _turns.pop(0)


def last():
    """Most recent still-fresh turn, or None."""
    if not _turns:
        return None
    t = _turns[-1]
    if time.time() - t["t"] > TURN_TTL:
        return None
    return t


def pending_clarify():
    """The last turn if it is an unanswered clarify question, else None."""
    t = last()
    if t and t["kind"] == "clarify":
        return t
    return None


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------

_SITE_WORDS = {"site", "website", "web", "browser", "web version", "webpage", "page",
               # homophones / common ASR renderings of "site"
               "sight", "cite", "sat"}
_APP_WORDS = {"app", "application", "program", "desktop", "desktop app", "installed",
              # common ASR renderings of "app"
              "ap", "abs", "op"}

# A fragment is SHORT and lacks a verb of its own. Anything with its own
# verb+entity ("play music", "open youtube") must route normally, never be
# hijacked by context.
_VERB_START = re.compile(
    r"^(open|launch|start|run|close|quit|exit|kill|play|stop|pause|resume|skip|"
    r"next|previous|search|find|write|type|read|check|make|create|build|add|"
    r"rescan|scan|list|show|tell|what|who|when|where|why|how|is|are|do|does|can|"
    r"could|would|will|set|turn|volume|mute|unmute)\b", re.I)

_TOO_LONG = 5      # words; real commands are rarely longer as fragments


def classify(text: str) -> str:
    """'command' | 'fragment' | 'clarify_answer' | 'contextual'."""
    t = (text or "").strip().lower().rstrip(".!?")
    words = t.split()
    if not words:
        return "command"

    pend = pending_clarify()

    # Bare disambiguator answering our own question?
    bare = set(words)
    if pend and (bare & (_SITE_WORDS | _APP_WORDS)) and len(words) <= 4 \
       and not _VERB_START.match(t):
        return "clarify_answer"

    # Explicit follow-up markers ("also open youtube", "and tiktok too")
    if re.match(r"^(and|also|too|then)\b", t) and len(words) <= _TOO_LONG + 2:
        return "contextual"

    if len(words) > _TOO_LONG or _VERB_START.match(t):
        return "command"

    # Short, verbless, no pending clarify -> contextual fragment ("that one",
    # "youtube", "again") — only meaningful when there IS a previous turn.
    if last() is not None:
        return "fragment"

    return "command"


# ---------------------------------------------------------------------------
# Completion
# ---------------------------------------------------------------------------

def answer_clarify(text: str):
    """Resolve a clarify answer to 'site' | 'app'. Returns None if unclear."""
    t = (text or "").strip().lower().rstrip(".!?")
    bare = set(t.split())
    if bare & _SITE_WORDS:
        return "site"
    if bare & _APP_WORDS:
        return "app"
    # fuzzy: whisper may render 'site' oddly
    for w in bare:
        import difflib
        if difflib.get_close_matches(w, list(_SITE_WORDS) + list(_APP_WORDS),
                                     n=1, cutoff=0.75):
            m = difflib.get_close_matches(w, list(_SITE_WORDS), n=1, cutoff=0.75)
            a = difflib.get_close_matches(w, list(_APP_WORDS), n=1, cutoff=0.75)
            if m and (not a or len(m[0]) >= len(a[0])):
                return "site"
            if a:
                return "app"
    return None


def complete_fragment(text: str):
    """Complete a fragment using the last turn.

    Returns ('site', key) to open a site, ('app', name) to launch,
    ('repeat', last_tool) to redo, or None when nothing sensible applies.
    """
    prev = last()
    if prev is None:
        return None
    t = (text or "").strip().lower().rstrip(".!?")

    if t in ("again", "once more", "repeat", "do it again"):
        return ("repeat", prev["tool"])

    # Entity carry-over: previous was an open/launch -> treat fragment as the
    # new target of the same verb. Works for site keys AND app names because
    # resolve_open_target checks both registries.
    if prev["tool"] in ("open_site", "open_application") or \
       (prev.get("result") or "").lower().startswith(("opened", "launched")):
        return ("open_target", t)
    return None
