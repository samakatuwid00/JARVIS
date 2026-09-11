"""transcript_pick.py - choose between Whisper's transcript and the browser's
live one for a voice turn.

The HUD shows the browser's own speech recognition while the user talks, and
used to replace it with Whisper's text unconditionally. Whisper (small.en,
beam 1) misheard commands the live text had right ("Search, and that's all
shit left for me.", 2026-09-11), so the two are compared:
  - they agree          -> Whisper's text (it has punctuation and casing)
  - they disagree       -> Whisper decodes again with the live words as a
                           hint; if that now agrees, the live text was right
  - still no agreement  -> the live text when it names a known app or site
                           that Whisper's text doesn't ("claude" vs "clock");
                           the live text when Whisper was very unsure and the
                           live text is a command or the bare wake word;
                           the live text when Whisper was unsure and the live
                           text is not a cut-off fragment; otherwise Whisper
  - Whisper very unsure and nothing to fall back on -> "unsure": the caller
    asks the user to say it again instead of answering a sentence nobody said
    ("And on the other hand, Jarvis, help them spot the place you are next."
    was "jarvis open spotify", 2026-09-11, and JARVIS chatted back to it)
Stdlib only, so it is testable without loading Whisper.
"""

import difflib
import re

AGREE = 0.8             # word-level similarity that counts as the same sentence
LOW_CONFIDENCE = -0.7   # Whisper mean segment log-prob below this = unsure
VERY_LOW = -0.95        # at or below this Whisper is guessing (the 2026-09-11 garbage scored -0.97/-0.99)
MIN_HINT_SHARE = 0.6    # live text needs 60% of Whisper's word count (not a fragment)
_WAKE = {"jarvis", "jarviss", "jarvus", "jervis", "javis"}
_COMMAND_RE = re.compile(
    r"^(?:(?:jarvis|hey|ok(?:ay)?|please)\s+)*(?:open|close|play|pause|stop|search|find|"
    r"go|visit|launch|start|turn|set|show|next|skip|mute|volume|remember|what|who|how|"
    r"can|could|tell|read|write|create|make|send)\b", re.I)


def _words(text):
    return re.findall(r"[a-z0-9']+", (text or "").lower())


def _names_in(text, known):
    """Known app/site names spoken in `text` (whole words or phrases)."""
    low = " " + " ".join(_words(text)) + " "
    return {n for n in known if n and f" {n} " in low}


def similarity(a, b):
    wa, wb = _words(a), _words(b)
    if not wa or not wb:
        return 0.0
    return difflib.SequenceMatcher(None, wa, wb).ratio()


def pick(whisper_text, whisper_conf, hint, redecode, known=()):
    """(text, source). redecode(hint) returns Whisper's text decoded with the
    hint as prompt; it runs only when the two transcripts disagree. `known`
    are lowercase names of the user's apps and sites. Source "unsure" means
    neither transcript can be trusted: ask again."""
    hint = (hint or "").strip()
    hint_words = _words(hint)
    if not hint_words:
        return (whisper_text, "unsure") if whisper_conf <= VERY_LOW else (whisper_text, "whisper")
    if not _words(whisper_text):
        return hint, "browser"
    if similarity(whisper_text, hint) >= AGREE:
        return whisper_text, "whisper"
    if similarity(redecode(hint), hint) >= AGREE:
        return hint, "browser+whisper"
    if _names_in(hint, known) and not _names_in(whisper_text, known):
        return hint, "browser"
    bare_wake = set(hint_words) <= _WAKE
    # A verb with something after it ("open spotify"), never a cut-off verb.
    command = _COMMAND_RE.match(hint) and len([w for w in hint_words if w not in _WAKE]) >= 2
    if whisper_conf <= VERY_LOW and (bare_wake or command):
        return hint, "browser"
    long_enough = len(hint_words) >= MIN_HINT_SHARE * len(_words(whisper_text))
    if whisper_conf < LOW_CONFIDENCE and long_enough:
        return hint, "browser"
    if whisper_conf <= VERY_LOW:
        return whisper_text, "unsure"
    return whisper_text, "whisper"
