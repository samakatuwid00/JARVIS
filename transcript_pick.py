"""transcript_pick.py - choose between Whisper's transcript and the browser's
live one for a voice turn.

The HUD shows the browser's own speech recognition while the user talks, and
used to replace it with Whisper's text unconditionally. Whisper (small.en,
beam 1) misheard commands the live text had right ("Search, and that's all
shit left for me.", 2026-09-11), so the two are compared:
  - they agree          -> Whisper's text (it has punctuation and casing)
  - they disagree       -> Whisper decodes again with the live words as a
                           hint; if that now agrees, the live text was right
  - still no agreement  -> the live text when Whisper was unsure and the live
                           text is not a cut-off fragment; otherwise Whisper
Stdlib only, so it is testable without loading Whisper.
"""

import difflib
import re

AGREE = 0.8             # word-level similarity that counts as the same sentence
LOW_CONFIDENCE = -0.7   # Whisper mean segment log-prob below this = unsure
MIN_HINT_SHARE = 0.6    # live text needs 60% of Whisper's word count (not a fragment)


def _words(text):
    return re.findall(r"[a-z0-9']+", (text or "").lower())


def similarity(a, b):
    wa, wb = _words(a), _words(b)
    if not wa or not wb:
        return 0.0
    return difflib.SequenceMatcher(None, wa, wb).ratio()


def pick(whisper_text, whisper_conf, hint, redecode):
    """(text, source). redecode(hint) returns Whisper's text decoded with the
    hint as prompt; it runs only when the two transcripts disagree."""
    hint = (hint or "").strip()
    if not _words(hint):
        return whisper_text, "whisper"
    if not _words(whisper_text):
        return hint, "browser"
    if similarity(whisper_text, hint) >= AGREE:
        return whisper_text, "whisper"
    if similarity(redecode(hint), hint) >= AGREE:
        return hint, "browser+whisper"
    long_enough = len(_words(hint)) >= MIN_HINT_SHARE * len(_words(whisper_text))
    if whisper_conf < LOW_CONFIDENCE and long_enough:
        return hint, "browser"
    return whisper_text, "whisper"
