"""Case table for the song-title matcher. Run: python scripts/probe_title_matcher.py"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from music_agent import _strip_ytm_suffix, _title_matches, _title_similarity  # noqa: E402

CASES = [
    ('bohemian rhapsody', 'Bohemian Rhapsody - Queen - YouTube Music', True),
    ('bohemian rhapsody', 'The most INSANE Bohemian Rhapsody Flashmob you will ever see!! - YouTube Music', False),
    ('bohemian rhapsody', 'Someone Like You - Adele - YouTube Music', False),
    ('calm instrumental', 'Deep Focus - Calm Instrumental Mix - YouTube Music', True),
    ('two ghosts', 'Two Ghosts - Harry Styles - YouTube Music', True),
    ('two ghosts', 'Two Goals - Haristay - YouTube Music', False),
    ('lofi study beats', 'lofi hip hop radio - beats to relax/study to - YouTube Music', True),
    ('coldplay', 'Yellow - Coldplay - YouTube Music', True),
    ('jazz', 'Blue in Green - Miles Davis - YouTube Music', False),
    ('nothing else matters', 'Nothing Else Matters - Remastered - Metallica - YouTube Music', True),
]

# Titles a real search returns that no case above covers - these must not
# regress when the video gate rejects the flashmob.
EXTRA = [
    ('bohemian rhapsody', 'Bohemian Rhapsody (Official Video Remastered 2018) - Queen - YouTube Music', True),
    ('metallica', 'Nothing Else Matters - Remastered - Metallica - YouTube Music', True),
    ('queen', 'Bohemian Rhapsody - Queen - YouTube Music', True),
]


def run(cases, label):
    failed = 0
    print(f"--- {label} ---")
    for query, raw, want in cases:
        match_title = _strip_ytm_suffix(raw)
        got = _title_matches(match_title, query)
        ok = got == want
        failed += not ok
        ratio = _title_similarity(match_title, query)
        print(f"{'PASS' if ok else 'FAIL'}  {got!s:5} (want {want!s:5}) sim={ratio:.3f}  "
              f"{query!r} vs {match_title!r}")
    return failed


bad = run(CASES, "required 10")
bad += run(EXTRA, "extra sanity")
print(f"\n{'ALL GREEN' if not bad else str(bad) + ' FAILED'}")
sys.exit(1 if bad else 0)
