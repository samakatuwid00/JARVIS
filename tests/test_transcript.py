"""transcript_pick: Whisper's transcript vs the browser's live one."""

import transcript_pick as tp

HEARD = "Search, and that's all shit left for me."
LIVE = "search The Social Network for me"


def _never(hint):
    raise AssertionError("re-decoded although the transcripts agree")


def test_agreeing_transcripts_keep_whisper_without_a_second_decode():
    assert tp.pick("Search movie Dune.", -0.3, "search movie dune", _never) == \
        ("Search movie Dune.", "whisper")


def test_whisper_agrees_once_given_the_live_words():
    assert tp.pick(HEARD, -0.4, LIVE, lambda h: "Search The Social Network for me.") == \
        (LIVE, "browser+whisper")


def test_unsure_whisper_loses_to_the_live_text():
    assert tp.pick(HEARD, -0.9, LIVE, lambda h: HEARD) == (LIVE, "browser")


def test_a_very_unsure_whisper_loses_to_a_short_live_command():
    garbage = "And on the other hand, Jarvis, help them spot the place you are next."
    assert tp.pick(garbage, -0.99, "jarvis open spotify", lambda h: garbage) == \
        ("jarvis open spotify", "browser")
    assert tp.pick("Be a part of it.", -0.97, "jarvis", lambda h: "Be a part of it.") == \
        ("jarvis", "browser")


def test_the_transcript_naming_a_known_app_wins():
    known = {"claude", "spotify", "notepad"}
    assert tp.pick("Can you open the clock?", -0.67, "jarvis can you open claude",
                   lambda h: "Can you open the clock?", known=known) == \
        ("jarvis can you open claude", "browser")
    # Whisper naming the app keeps its text.
    assert tp.pick("Open Spotify.", -0.3, "open spot if I", lambda h: "Open Spotify.",
                   known=known) == ("Open Spotify.", "whisper")


def test_garbage_with_nothing_to_fall_back_on_is_unsure():
    garbage = "Be a part of it."
    assert tp.pick(garbage, -0.97, "", lambda h: garbage) == (garbage, "unsure")
    assert tp.pick(garbage, -0.97, "um the", lambda h: garbage) == (garbage, "unsure")
    assert tp.pick(garbage, -0.5, "", lambda h: garbage) == (garbage, "whisper")


def test_confident_whisper_keeps_its_text():
    assert tp.pick(HEARD, -0.2, LIVE, lambda h: HEARD) == (HEARD, "whisper")


def test_a_cut_off_live_fragment_never_wins():
    long_heard = "search movie name the social network in brave browser please"
    assert tp.pick(long_heard, -0.9, "search", lambda h: long_heard) == (long_heard, "whisper")


def test_no_live_text_or_no_whisper_text():
    assert tp.pick(HEARD, -0.9, "", _never) == (HEARD, "whisper")
    assert tp.pick("", -9.0, LIVE, _never) == (LIVE, "browser")
