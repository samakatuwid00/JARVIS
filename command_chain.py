"""command_chain.py - one utterance, several commands.

"close notepad and Spotify" closed Notepad only; "open Spotify and play Two
Ghosts" opened Spotify with the whole sentence as the app name. A chain of
quick local actions is split into commands that each route on their own.
A request with a real goal in it (create, write, delete, fix ...) stays
whole for the autonomous runner, and so does anything that is not clearly a
chain: a question, a sentence not led by an action, "play rock and roll".
"""

import re

# The quick local actions a chain is made of, as the words that start a command.
_ACTION = (r"(?:open|launch|start|(?:force\s+)?close|quit|exit|kill|play|pause|resume|stop|"
           r"skip|search|google|set|turn|lower|raise|mute|unmute|visit|browse|go\s+to|prompt)")
_ACTION_RE = re.compile(rf"^{_ACTION}\b", re.I)
# Verbs whose objects form a list of targets: "close spotify, notepad and calculator".
_LIST_VERBS = {"close", "quit", "exit", "kill"}
# A goal or a destructive step: the whole request belongs to the autonomous runner.
_GOAL_RE = re.compile(r"\b(?:create|make|build|write|delete|remove|erase|install|uninstall|"
                      r"summari[sz]e|compare|research|fix|verify|double\s+check|set\s*up|"
                      r"tell\s+me|read\s+(?:it|out|aloud))\b", re.I)
_QUESTION_RE = re.compile(r"^(?:what|which|who|whose|why|how|when|where)\b", re.I)
_SEP_RE = re.compile(r"(\s*,\s*(?:and\s+|then\s+)?|\s+(?:and\s+then|and\s+also|after\s+that|and|then)\s+)",
                     re.I)
_LEAD_RE = re.compile(r"^(?:(?:jarvis|cygnus|please|now|so|okay|ok|and|i\s+mean|"
                      r"(?:can|could|would)\s+you)[,\s]+)+", re.I)
_TAIL_RE = re.compile(r"[,\s]*(?:jarvis|cygnus|please|for\s+me|instead)?[\s.!?]*$", re.I)
# "open spotify play two ghosts": no "and", but still two commands.
_IMPLICIT_RE = re.compile(r"^((?:open|launch)\s+\S+(?:\s+\S+)?)\s+((?:play|search)\b.+)$", re.I)
# Where a chain opens something, and a search that names no place of its own.
_OPEN_OBJ_RE = re.compile(r"^(?:open|launch|start|go\s+to|visit)\s+(?:the\s+|my\s+)?(.+)$", re.I)
_SEARCH_LEAD_RE = re.compile(r"^(?:search|google|prompt)\b", re.I)
_HAS_PLACE_RE = re.compile(r"\s(?:in|on)\s+\S+\s*$", re.I)


def _clean(clause):
    return _TAIL_RE.sub("", _LEAD_RE.sub("", (clause or "").strip())).strip()


def _clauses(parts):
    """Commands from the pieces between separators, or None when a piece
    before the first command is not one."""
    clauses, verb = [], None
    for i in range(0, len(parts), 2):
        chunk = _clean(parts[i])
        if not chunk:
            continue
        m = _ACTION_RE.match(chunk)
        if m and chunk.lower() == m.group(0).lower():
            continue                                    # a bare "Open," is a stutter
        if m:
            verb = m.group(0)
            clauses.append(chunk)
        elif not clauses:
            return None
        elif verb.split()[-1].lower() in _LIST_VERBS and len(chunk.split()) <= 4:
            clauses.append(f"{verb} {chunk}")
        else:
            clauses[-1] += parts[i - 1] + chunk       # not a command: part of the last one
    return clauses


def _carry_place(clauses):
    """'open brave and search manus ai' searches in Brave: a search that names
    no place of its own runs where the chain just opened."""
    place, out = None, []
    for clause in clauses:
        m = _OPEN_OBJ_RE.match(clause)
        if m:
            place = m.group(1)
        elif place and _SEARCH_LEAD_RE.match(clause) and not _HAS_PLACE_RE.search(clause):
            clause = f"{clause} in {place}"
        out.append(clause)
    return out


def split_commands(text):
    """The commands in `text`, in order; [text] when it is not a chain."""
    t = _clean(text)
    if not t or _GOAL_RE.search(t) or _QUESTION_RE.match(t):
        return [text]
    clauses = _clauses(_SEP_RE.split(t)) or []
    # "play some music, play some music": once is what was meant.
    clauses = [c for i, c in enumerate(clauses) if i == 0 or c.lower() != clauses[i - 1].lower()]
    if len(clauses) < 2:
        m = _IMPLICIT_RE.match(t)
        clauses = [m.group(1), m.group(2)] if m else []
    return _carry_place(clauses) if len(clauses) > 1 else [text]
