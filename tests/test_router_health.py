"""router_health: the health table the brain's 9router chain reads, and the
words for which model is answering. No network: probes are not run here."""

import time

import pytest

import router_health as rh


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    monkeypatch.setattr(rh, "_state", {})
    monkeypatch.setattr(rh, "_last_switch_notice", 0.0)
    monkeypatch.setattr(rh, "candidates", lambda: ["ag/gemini-3.8-flash-low", "ag/claude-sonnet-4-6",
                                                   "gc/gemini-2.5-flash", "cu/claude-4.5-sonnet"])


@pytest.mark.parametrize("model, name", [
    ("ag/gemini-3.8-flash-low", "Gemini 3.8 Flash"),
    ("ag/claude-sonnet-4-6", "Claude Sonnet 4.6"),
    ("cu/claude-4.6-sonnet-medium-thinking", "Claude 4.6 Sonnet"),
    ("gh/gpt-5.4-mini", "GPT-5.4 Mini"),
    ("cl/anthropic/claude-sonnet-4.6", "Claude Sonnet 4.6"),
    ("jarvis-qwen3", "the local Qwen model"),
    ("oc/big-pickle", "Big Pickle"),
    ("oc/mimo-v2.5-free", "MiMo v2.5"),
    ("oc/deepseek-v4-flash-free", "DeepSeek v4 Flash"),
])
def test_models_have_names_people_know(model, name):
    assert rh.friendly(model) == name


def test_nothing_is_healthy_before_the_first_probe():
    assert rh.healthy_models() == []


def test_healthy_models_keep_the_owner_order_and_skip_cooling_ones():
    rh.mark_healthy("cu/claude-4.5-sonnet", 0.6)
    rh.mark_healthy("gc/gemini-2.5-flash", 2.1)
    rh.mark_healthy("ag/claude-sonnet-4-6", 1.0)
    rh.mark_unhealthy("ag/claude-sonnet-4-6", "Unavailable (reset after 130h 29m 34s)")
    assert rh.healthy_models() == ["gc/gemini-2.5-flash", "cu/claude-4.5-sonnet"]


@pytest.mark.parametrize("reason, seconds", [
    ("[antigravity/gemini-3.8-flash-low] Unavailable (reset after 95h 47m 7s)", 95 * 3600 + 47 * 60 + 7),
    ("[gemini-cli/gemini-2.5-flash] [403]: HTTP 403 (reset after 2m)", 120),
    ("empty reply", rh.EMPTY_COOLDOWN_S),
    ("Error code: 429", rh.DEFAULT_COOLDOWN_S),
])
def test_a_failure_waits_until_the_provider_says(reason, seconds):
    before = time.time()
    rh.mark_unhealthy("gc/gemini-2.5-flash", reason)
    until = rh.snapshot()["gc/gemini-2.5-flash"]["until"]
    assert abs((until - before) - seconds) < 2


def test_a_restart_knows_what_answered(tmp_path, monkeypatch):
    path = str(tmp_path / "router_health.json")
    rh.mark_healthy("cu/claude-4.5-sonnet", 0.6)
    rh.mark_unhealthy("ag/claude-sonnet-4-6", "Unavailable (reset after 95h 47m 7s)")
    rh.save(path)
    monkeypatch.setattr(rh, "_state", {})                    # a fresh process
    assert rh.load(path) is True
    assert rh.healthy_models() == ["cu/claude-4.5-sonnet"]
    assert "ag/claude-sonnet-4-6" not in rh.healthy_models()


def test_an_old_saved_table_is_not_trusted_for_health(tmp_path, monkeypatch):
    path = str(tmp_path / "router_health.json")
    rh.mark_healthy("cu/claude-4.5-sonnet", 0.6)
    rh.mark_unhealthy("ag/claude-sonnet-4-6", "Unavailable (reset after 95h 47m 7s)")
    rh.save(path)
    monkeypatch.setattr(rh, "_state", {})
    rh.load(path, now=time.time() + rh.CACHE_FRESH_S + 5)    # saved too long ago
    assert rh.healthy_models() == []                         # health must be re-checked
    assert rh.snapshot()["ag/claude-sonnet-4-6"]["ok"] is False   # a named reset still holds


def test_a_switch_is_announced_once_per_turn():
    first = rh.switch_notice("ag/gemini-3.8-flash-low", "ag/claude-sonnet-4-6")
    assert first == "Gemini 3.8 Flash isn't available right now, so I'm switching to another model, sir."
    assert rh.switch_notice("ag/claude-sonnet-4-6", "gc/gemini-2.5-flash") is None


def test_the_answering_model_is_named_when_it_changes(monkeypatch):
    monkeypatch.setattr(rh, "primary", lambda: "Gemini 3.8 Flash")
    assert rh.answer_notice("oc/big-pickle", "router", "Gemini 3.8 Flash") == \
        "Big Pickle, OpenCode's free model, is answering while Gemini 3.8 Flash is unavailable."
    assert rh.answer_notice("cu/claude-4.5-sonnet", "router", "Gemini 3.8 Flash") == \
        "Claude 4.5 Sonnet through Cursor is answering while Gemini 3.8 Flash is unavailable."
    assert rh.answer_notice("ag/gemini-3.8-flash-low", "router", "Big Pickle") == \
        "Gemini 3.8 Flash is back and answering again."
    assert rh.answer_notice("ag/gemini-3.8-flash-low", "router", "Gemini 3.8 Flash") is None
    assert "local model is answering" in rh.answer_notice(None, "ollama", "Gemini 3.8 Flash")
    assert rh.answer_notice(None, "ollama", "the local model") is None
    assert rh.answer_notice(None, "tool", "Gemini 3.8 Flash") is None


def test_a_reconnecting_client_is_not_told_again(monkeypatch):
    monkeypatch.setattr(rh, "primary", lambda: "Gemini 3.8 Flash")
    monkeypatch.setattr(rh, "_heard", {})
    assert rh.notice_for("remote", "oc/big-pickle", "router")          # first time: said
    assert rh.notice_for("remote", "oc/big-pickle", "router") is None  # same phone, new socket
    assert rh.notice_for("desktop", "oc/big-pickle", "router")         # another device: said once
    assert rh.notice_for("remote", None, "instant") is None            # a tool turn changes nothing
    assert rh.notice_for("remote", "oc/big-pickle", "router") is None
