"""wake_engine: the server-side wake fallback decodes at most once per step,
catches up on a backlog in one pass, stays quiet on silence, and holds off
while the browser records a command. No microphone: _tick() is fed directly."""

import time

import numpy as np

from wake_engine import WakeEngine, wake_targets


# --- who a server wake is delivered to -------------------------------------

def test_remote_clients_never_get_a_server_wake():
    assert set(wake_targets({"tab", "phone"}, remote={"phone"}, desktop=set())) == {"tab"}


def test_the_desktop_window_wins_over_a_local_tab():
    assert wake_targets({"tab", "desk"}, remote=set(), desktop={"desk"}) == ["desk"]


def test_without_the_desktop_app_every_local_tab_gets_it():
    assert set(wake_targets({"tab1", "tab2"}, remote=set(), desktop=set())) == {"tab1", "tab2"}


def test_a_remote_client_claiming_desktop_is_ignored():
    assert wake_targets({"tab", "phone"}, remote={"phone"}, desktop={"phone"}) == ["tab"]


BLOCK = 1600          # 0.1 s at 16 kHz, what the audio callback delivers


class _Voice:
    def __init__(self, text=""):
        self.calls, self.text = 0, text

    def transcribe_wake(self, window):
        self.calls += 1
        return self.text


def _loud(value=0.05):
    return np.full(BLOCK, value, dtype=np.float32)


def test_music_decodes_once_per_step_not_once_per_block():
    voice = _Voice()
    engine = WakeEngine(voice)
    start = 1000.0                       # far past the initial cooldown
    for i in range(50):                  # 5 s of steady music, block by block
        engine._tick([_loud()], start + i / 10)
    # The 2.5 s window is full at block 25; then one decode per second.
    assert voice.calls == 3


def test_a_backlog_is_folded_in_at_once_and_keeps_the_newest_audio():
    voice = _Voice()
    engine = WakeEngine(voice)
    backlog = [_loud(0.01)] * 39 + [_loud(0.09)]              # 4 s piled up
    assert engine._tick(backlog, 1000.0) is True
    assert voice.calls == 1                                   # one decode, not 40
    assert engine._buf.shape[0] == int(engine.sr * (engine.window_s + engine.step_s))
    assert engine._buf[-1] == np.float32(0.09)


def test_silence_never_reaches_whisper():
    voice = _Voice()
    engine = WakeEngine(voice)
    for i in range(50):
        engine._tick([np.zeros(BLOCK, dtype=np.float32)], 1000.0 + i / 10)
    assert voice.calls == 0


def test_hold_while_the_browser_records_then_release():
    voice = _Voice()
    engine = WakeEngine(voice)
    engine._tick([_loud()] * 25, 1000.0)
    engine.hold(30000)
    now = time.time()
    assert engine._tick([_loud()], now) is False and voice.calls == 1
    engine.release()
    assert engine._tick([_loud()], now + 0.1) is True and voice.calls == 2


def test_release_does_not_cut_the_post_speech_suspend_short():
    engine = WakeEngine(_Voice())
    engine.suspend(5000)
    engine.release()
    assert engine._tick([_loud()] * 25, time.time()) is False


def test_wake_word_fires_the_callback_once_per_cooldown():
    voice = _Voice("jarvis")
    engine = WakeEngine(voice)
    fired = []
    engine.set_callback(lambda: fired.append(1))
    for i in range(40):
        engine._tick([_loud()], 1000.0 + i / 10)
    assert fired == [1]                   # 4 s cooldown after the first wake
