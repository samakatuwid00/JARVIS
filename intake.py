"""
JARVIS Intake Layer (Layer 1)
==============================

Sits between raw speech/text and the router. Its job: turn a messy transcript
("jarvus play taylor son", "can you please open the notepad for me jarvis",
"compose report for iRIMS-B") into a clean, normalized intent that downstream
tooling can trust -- surviving bad microphone audio AND broken grammar.

Design rules
------------
* Pure stdlib only (no rapidfuzz / no LLM call). This layer must run in
  sub-100ms so it never adds latency to the warm harness path.
* The phrase corpus is DATA (intake_corpus.json), seeded from the user's
  profile + Second Brain so it is portable and editable -- not code.
* Fuzzy matching is token-set based: word ORDER and grammar do not matter,
  only the set of meaningful tokens. That is what makes Taglish / broken
  grammar survive.
* Confidence triage: we combine (a) Whisper per-word confidence (optional,
  passed in) and (b) fuzzy match score, then decide:
      high   -> auto-resolve silently
      medium -> return top candidate as the interpretation (router may act)
      low    -> flag for confirmation / "did you mean?" chips in the HUD

Public API
----------
    load_corpus(path) -> dict
    normalize(text) -> str            # grammar-stripped, lowercase, tokens
    Intent                           # dataclass result
    resolve_intent(raw, corpus, word_conf=None) -> Intent
    autocomplete(normalized, corpus, top_n=3) -> list[str]
    format_echo(intent) -> str       # "You said: play Taylor Swift?"
"""

from __future__ import annotations

import difflib
import json
import os
import re
from dataclasses import dataclass, field
from typing import Optional

# ---------------------------------------------------------------------------
# Corpus
# ---------------------------------------------------------------------------

DEFAULT_CORPUS = {
    "commands": [
        "play", "open", "search", "write", "list", "read", "make", "build",
        "compose", "launch", "stop", "close", "what", "who", "how", "weather",
        "remember", "find", "show", "create", "generate", "email", "send",
        "copy", "paste", "note",
    ],
    "apps": [
        "notepad", "chrome", "brave", "spotify", "vlc", "excel", "word",
        "powerpoint", "calculator", "file explorer", "explorer", "slack",
        "discord", "telegram", "obsidian", "visual studio code", "code",
        "terminal", "command prompt", "outlook", "onenote",
    ],
    "artists": [
        "taylor swift", "the beatles", "ed sheeran", "linkin park",
        "coldplay", "adele", "bruno mars", "billie eilish", "bts",
        "moira dela torre", "ben&ben", "erald mandanas",
    ],
    "projects": [
        "iRIMS-V", "iRIMS", "schema-mapper", "jarvis-demo", "Musubae",
        "Second Brain", "my-app", "Portfolio", "LRMIS",
    ],
    "contacts": ["Roger", "nikoo", "abay"],
    "genres": [
        "pop", "rock", "jazz", "lofi", "classical", "hip hop", "rnb",
        "opm", "ballad",
    ],
}

CORPUS_PATH = os.path.join(os.path.dirname(__file__), "intake_corpus.json")


def load_corpus(path: str = CORPUS_PATH) -> dict:
    """Load the phrase corpus. Falls back to defaults if the file is absent
    or corrupt, so the layer never hard-fails."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        merged = dict(DEFAULT_CORPUS)
        for k, v in data.items():
            merged[k] = v
        return merged
    except (FileNotFoundError, json.JSONDecodeError):
        return dict(DEFAULT_CORPUS)


# ---------------------------------------------------------------------------
# Text normalization (grammar / Taglish resilient)
# ---------------------------------------------------------------------------

# Words that carry no routing meaning. Stripping them is what lets
# "can you please open the notepad for me jarvis" collapse to "open notepad".
_FILLERS = {
    "a", "an", "the", "please", "can", "you", "could", "would", "kindly",
    "me", "for", "my", "to", "of", "on", "in", "is", "are", "do", "did",
    "does", "i", "want", "need", "just", "like", "uh", "um", "so", "then",
    "okay", "ok", "hey", "hi", "hello", "jarvis", "javis", "jarvus",
    "makita", "kita", "naman", "lang", "nga", "po", "pero", "yung", "yong",
    "yung", "itong", "eto", "nito", "din", "rin", "na", "pa", "ba",
}

_PUNCT = re.compile(r"[^\w\s]", re.UNICODE)
_WS = re.compile(r"\s+")


def normalize(text: str) -> str:
    """Lowercase, strip punctuation, drop fillers, collapse whitespace.
    Returns a clean token string suitable for fuzzy matching."""
    if not text:
        return ""
    t = text.lower()
    t = _PUNCT.sub(" ", t)
    toks = [w for w in _WS.split(t) if w and w not in _FILLERS]
    return " ".join(toks)


def _token_set_ratio(a: str, b: str) -> float:
    """Order-independent similarity in [0, 1]. Combines a set-Jaccard score
    with difflib's sequence ratio so partial / reordered phrases still match."""
    if not a or not b:
        return 0.0
    sa, sb = set(a.split()), set(b.split())
    if sa == sb:
        return 1.0
    # Jaccard over tokens
    inter = len(sa & sb)
    union = len(sa | sb) or 1
    jaccard = inter / union
    # Sequence ratio (rewards contiguous overlap)
    seq = difflib.SequenceMatcher(None, a, b).ratio()
    # Weight: set overlap matters most for intent, sequence for spelling.
    return round(0.7 * jaccard + 0.3 * seq, 3)


# ---------------------------------------------------------------------------
# Intent result
# ---------------------------------------------------------------------------


@dataclass
class Intent:
    raw: str
    normalized: str
    verb: Optional[str] = None
    entity: Optional[str] = None
    entity_kind: Optional[str] = None  # app|artist|project|contact|genre|none
    score: float = 0.0          # best fuzzy score for the entity
    confidence: str = "low"     # low|medium|high
    candidates: list = field(default_factory=list)  # top autocomplete strings
    echo: str = ""              # human-readable confirmation line

    def needs_confirmation(self) -> bool:
        return self.confidence == "low"


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------

# Map a detected verb to the backend it should route to (used by the router
# in later phases; harmless here).
_VERB_BACKEND = {
    "play": "music",
    "open": "desktop",
    "search": "web",
    "compose": "native",
    "report": "native",
    "write": "native",
    "make": "manus",
    "build": "manus",
    "create": "manus",
    "generate": "manus",
    "copy": "desktop",
    "read": "native",
    "list": "native",
    "launch": "desktop",
    "stop": "music",
    "close": "desktop",
}


# Weak interrogatives that should yield to a real content command when both
# are present ("what is the weather" -> weather, not what).
_INTERROGATIVES = {"what", "who", "how", "when", "where", "why"}


def _detect_verb(tokens: list[str], corpus: dict) -> Optional[str]:
    """Pick the routing verb. Prefers a CONTENT command (play/open/weather/...)
    over a weak interrogative (what/who/how) when both are present, so
    'what is the weather in manila' resolves to 'weather', not 'what'.
    Falls back to fuzzy-matching a token against the command list."""
    cmds = corpus["commands"]
    content_hits = [t for t in tokens if t in cmds and t not in _INTERROGATIVES]
    if content_hits:
        return content_hits[0]
    interr = [t for t in tokens if t in cmds]
    if interr:
        return interr[0]
    # fuzzy: any token close to a command?
    best, score = None, 0.0
    for tok in tokens:
        m = difflib.get_close_matches(tok, cmds, n=1, cutoff=0.8)
        if m and score < 0.8:
            best, score = m[0], 0.82
    return best



def _fuzzy_token(a: str, b: str, cutoff: float = 0.75) -> float:
    """Single-token similarity in [0, 1]: 1.0 exact, 0.6 a genuine fuzzy
    miss (typo / vowel swap), 0 no relation. Uses length-aware edit distance
    so short words like 'son'->'swift' are still caught (difflib is poor
    for very short strings)."""
    if a == b:
        return 1.0
    la, lb = len(a), len(b)
    if la == 0 or lb == 0:
        return 0.0
    dist = _edit_distance(a, b)
    score = 1.0 - dist / max(la, lb)
    # short words need a looser match threshold
    thr = 0.5 if max(la, lb) <= 5 else cutoff
    if score >= thr:
        return 0.6          # fuzzy but real, never as strong as an exact hit
    return 0.0


def _edit_distance(a: str, b: str) -> int:
    """Standard Wagner-Fischer Levenshtein distance (tiny, stdlib-only)."""
    la, lb = len(a), len(b)
    if la == 0:
        return lb
    if lb == 0:
        return la
    prev = list(range(lb + 1))
    for i in range(1, la + 1):
        cur = [i] + [0] * lb
        ca = a[i - 1]
        for j in range(1, lb + 1):
            cost = 0 if ca == b[j - 1] else 1
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost)
        prev = cur
    return prev[lb]


def _match_entity(tokens: list[str], values: list[str]) -> tuple[Optional[str], float]:
    """Return (best_entity, score) by combining three signals:
      (1) token-set ratio of the full joined phrase vs candidate,
      (2) same ratio with the leading verb dropped (so 'copy ... excel'
          still matches 'excel'),
      (3) token-membership: fraction of the candidate's tokens that appear
          (exact or fuzzy) in the input -- this recovers a single token
          buried inside a long, grammatical sentence ("copy the budget
          table from excel" -> 'excel' at ~0.95).
    The max of the three is the score."""
    joined = " ".join(tokens)
    best, best_score = None, 0.0
    for cand in values:
        ctoks = cand.lower().split()
        s1 = _token_set_ratio(joined, cand.lower())
        s2 = _token_set_ratio(" ".join(tokens[1:]), cand.lower()) if len(tokens) > 1 else 0.0
        if ctoks:
            # token-level membership: each candidate token matches SOME input
            # token (exact=1.0 / fuzzy=0.6). A fuzzy-only match is weaker so
            # 'son'->'swift' still recovers 'taylor swift' but never beats a
            # clean exact match.
            best_per = [max(_fuzzy_token(ct, it, 0.5) for it in tokens)
                        for ct in ctoks]
            contain = sum(best_per) / len(ctoks)
            # A candidate with NO exact token in common is far less certain
            # than one that shares a real word; penalize it so a 1-word
            # fuzzy match can't beat a 2-word candidate with an exact hit.
            if not any(b == 1.0 for b in best_per):
                contain *= 0.6
        else:
            contain = 0.0
        s = max(s1, s2, contain * 0.95)
        if s > best_score:
            best, best_score = cand, s
    return best, round(best_score, 3)


def resolve_intent(raw: str, corpus: Optional[dict] = None,
                   word_conf: Optional[list[float]] = None) -> Intent:
    """Turn raw transcript into a normalized Intent.

    word_conf: optional list of per-word Whisper probabilities (0..1). When
    provided we fold the minimum word confidence into the triage so a
    low-confidence span forces confirmation even if the fuzzy score is high.
    """
    if corpus is None:
        corpus = load_corpus()
    raw = (raw or "").strip()
    norm = normalize(raw)
    tokens = norm.split()

    intent = Intent(raw=raw, normalized=norm)

    if not tokens:
        intent.confidence = "low"
        intent.echo = "I didn't catch that — say it again?"
        return intent

    verb = _detect_verb(tokens, corpus)
    intent.verb = verb

    # Entity search across all entity buckets.
    best_entity, best_kind, best_score = None, None, 0.0
    for kind in ("apps", "artists", "projects", "contacts", "genres"):
        ent, score = _match_entity(tokens, corpus.get(kind, []))
        if score > best_score:
            best_entity, best_kind, best_score = ent, kind, score

    # Only commit an entity when the match clears a confidence floor. Below
    # it we leave entity=None (the HUD can still show candidates) so a
    # garbage match never becomes the routed target.
    if best_entity and best_score >= 0.5:
        intent.entity = best_entity
        intent.entity_kind = best_kind
        intent.score = best_score
    else:
        intent.entity = None
        intent.entity_kind = None
        intent.score = best_score

    # ---- Confidence triage ----------------------------------------------
    # Start from fuzzy score bands, then demote if Whisper was unsure.
    if best_score >= 0.85:
        conf = "high"
    elif best_score >= 0.6:
        conf = "medium"
    else:
        conf = "low"

    if word_conf:
        min_wc = min(word_conf) if word_conf else 1.0
        # A clearly-misheard word demotes the whole intent: no matter how
        # confident the fuzzy match is, unreliable audio needs confirmation.
        if min_wc < 0.5:
            conf = "low"
        elif min_wc < 0.7 and conf == "high":
            conf = "medium"

    intent.confidence = conf

    # ---- Autocomplete candidates (for HUD "did you mean?") --------------
    intent.candidates = autocomplete(norm, corpus, top_n=3, verb=intent.verb)

    # ---- Human echo ------------------------------------------------------
    if intent.entity:
        subject = intent.entity
    elif intent.verb:
        subject = intent.verb
    else:
        subject = norm
    intent.echo = f"You said: {subject}?" if conf != "high" else f"Okay: {subject}."

    return intent


def autocomplete(normalized: str, corpus: Optional[dict] = None,
                 top_n: int = 3, verb: Optional[str] = None) -> list[str]:
    """Return the top-N corpus phrases most similar to the normalized input.
    Used to render tappable correction chips in the HUD / typed autocomplete.
    Always seeds the verb (if known) so there is at least one sensible chip."""
    if corpus is None:
        corpus = load_corpus()
    if not normalized:
        return []
    pool: list[str] = []
    for kind in ("commands", "apps", "artists", "projects", "contacts", "genres"):
        pool.extend(corpus.get(kind, []))

    scored = []
    commands = set(corpus.get("commands", []))
    for cand in pool:
        s = _token_set_ratio(normalized, cand.lower())
        # typo tolerance for ENTITIES only (apps/artists/projects/...), not
        # generic 1-token commands -- so 'opn notpad' still surfaces
        # 'notepad' but a stray fuzzy hit on 'stop' doesn't crowd it out.
        ctoks = cand.lower().split()
        if cand not in commands and ctoks and any(
            _fuzzy_token(ct, it, 0.5) > 0 for ct in ctoks for it in normalized.split()
        ):
            s = max(s, 0.45)
        if s >= 0.25:
            scored.append((s, cand))
    scored.sort(reverse=True, key=lambda x: x[0])
    out = [c for _, c in scored[:top_n]]
    if verb and verb not in out:
        out.insert(0, verb)
    return out[:top_n]


# Convenience: which backend a resolved verb maps to (used by later phases).
def verb_backend(verb: Optional[str]) -> Optional[str]:
    return _VERB_BACKEND.get(verb) if verb else None


if __name__ == "__main__":
    # Quick smoke demo when run directly.
    corpus = load_corpus()
    samples = [
        "jarvus play taylor son",
        "can you please open the notepad for me jarvis",
        "compose report for iRIMS-B",
        "make me a website like my portfolio",
        "what is the weather in manila",
        "copy the budget table from excel",
        "play lofi",
    ]
    for s in samples:
        it = resolve_intent(s, corpus)
        print(f"\nIN : {s!r}")
        print(f"OUT: verb={it.verb} entity={it.entity!r}({it.entity_kind}) "
              f"score={it.score} conf={it.confidence}")
        print(f"     echo={it.echo} candidates={it.candidates}")
