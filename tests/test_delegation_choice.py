"""Delegation choices: naming a helper picks it, a named CLI agent (Claude
Code) runs in the background after a confirm when it may change files, and
a word that is not a task never becomes a job."""

import time

import pytest

import jobs
import plain_reply
import tools


@pytest.fixture
def isolated(monkeypatch, tmp_path):
    """A job registry and audit trail of the test's own, and no confirm waiting."""
    monkeypatch.setattr(jobs, "_LOG_PATH", tmp_path / "jobs.jsonl")
    monkeypatch.setattr(jobs, "_jobs", {})
    monkeypatch.setattr(jobs, "_loaded", True)
    monkeypatch.setattr(jobs, "_listeners", [])
    monkeypatch.setattr(tools, "_audit_log", lambda *a, **k: None)
    monkeypatch.setattr(tools, "_PENDING_HERMES_CALL", None)
    monkeypatch.setitem(tools._PENDING_DESTRUCTIVE, "task", None)
    monkeypatch.setitem(tools._PENDING_DESTRUCTIVE, "autonomous", None)


@pytest.mark.parametrize("text, agent", [
    ("Create the page. Delegate this to Claude and tell me when it's done.", "claude"),
    ("have Claude Code fix the chat box", "claude"),
    ("summarize the notes using gemini", "gemini"),
    ("hand it off to claude: tidy the README", "claude"),
    ("delegate claude to fix your terminal chat send and mic buttons", "claude"),
    ("delegate to claude code: tidy the README", "claude"),
    ("fix your send, mic, and listening progress icon in the terminal chat since it is "
     "not quite aligned, use claude", "claude"),
    ("can you use claude to fix the chat box", "claude"),
    ("ask claude code to fix the chat box", "claude"),
    ("tell claude to tidy the README", "claude"),
    ("Claude, fix the mic button", "claude"),
])
def test_naming_a_helper_picks_it(text, agent):
    forced, name, cleaned = tools._explicit_cli_agent(text)
    assert forced and name == agent
    assert "claude" not in cleaned.lower() and "gemini" not in cleaned.lower()
    assert not cleaned.lower().startswith("to ")


@pytest.mark.parametrize("text", [
    "delegate claude to fix your terminal chat especially the placement layout of buttons "
    "send, mic, and listening icon animation. use playwright to confirm and also when in "
    "widget or panel mode you are unclickable. double check it",
    "check the chat box and fix it",
    "can you tweak the mic button?",
])
def test_an_instruction_to_fix_is_not_look_only(text):
    assert not tools._is_safe_task(text)


@pytest.mark.parametrize("text", ["how do I fix my wifi?", "check the current state", "what time is it"])
def test_questions_and_looks_stay_safe(text):
    assert tools._is_safe_task(text)


def test_delegate_claude_to_fix_asks_then_goes_to_claude(isolated, monkeypatch):
    _fake_claude(monkeypatch)
    out = tools.delegate("delegate claude to fix the send button layout in the chat. double check it")
    assert out.startswith("[NEEDS_CONFIRM:") and "Claude Code" in out


@pytest.mark.parametrize("text", ["I have claude installed", "ask claude what time it is", "open github",
                                  "I use claude every day", "do you use claude?",
                                  "ask claude code what time is it", "hey claude code, what's up"])
def test_mentioning_one_does_not(text):
    assert tools._explicit_cli_agent(text)[0] is False


@pytest.mark.parametrize("word", ["test", "Testing.", "hi", "ok", "thanks"])
def test_a_word_is_not_a_task(isolated, word):
    for out in (tools.delegate(word), tools.run_autonomous(word)):
        assert "isn't something I can run" in out
    assert jobs.recent(5) == []


@pytest.mark.parametrize("word", ["Cygnus?", "jarvis?", "Hey Cygnus.", "cygnus"])
def test_a_check_in_with_the_name_is_not_a_task(word):
    assert tools._is_not_a_task(word)


@pytest.mark.parametrize("text", ["?", "", "cygnus open notepad", "delete the file old.txt"])
def test_real_requests_and_blanks_are_not_check_ins(text):
    assert not tools._is_not_a_task(text)


@pytest.mark.parametrize("text, rest", [
    ("hey cygnus, play some music", "play some music"),
    ("Hi Jarvis, open notepad", "open notepad"),
    ("cygnus open notepad", "open notepad"),
    ("hey", "hey"),                              # nothing behind it: left as said
])
def test_the_name_and_a_greeting_are_lead_ins(text, rest):
    assert tools.strip_fillers(text) == rest


@pytest.mark.parametrize("text", ["open VS Code", "launch Visual Studio Code", "open claude code"])
def test_an_editor_named_code_is_not_coding_work(text):
    assert tools._detect_backend(text) != "opencode"


@pytest.mark.parametrize("text", ["code a small website", "debug the login page",
                                  "create a react app for my notes"])
def test_real_coding_work_still_goes_to_opencode(text):
    assert tools._detect_backend(text) == "opencode"


class _Proc:
    returncode = 0
    cmd = None

    def __init__(self, cmd, **kw):
        _Proc.cmd = cmd

    def communicate(self, timeout=None):
        return "Built the page. It grows as you type.", ""


def _fake_claude(monkeypatch):
    monkeypatch.setattr(tools.shutil, "which", lambda b: f"C:/bin/{b}.exe")
    monkeypatch.setattr(tools.subprocess, "Popen", _Proc)


def _wait_done(jid, got):
    """The job is done and its answer delivered (the watcher does both, in that order)."""
    for _ in range(200):
        job = jobs.get(jid) or {}
        if job.get("state") == "done" and got:
            return job
        time.sleep(.02)
    raise AssertionError((jobs.get(jid), got))


def test_a_changing_task_asks_first_then_runs_in_the_background(isolated, monkeypatch):
    _fake_claude(monkeypatch)
    ask = tools.delegate("Create C:/Users/x/Documents/page.html with a growing chat box. Delegate this to Claude.")
    assert ask.startswith("[NEEDS_CONFIRM:") and "Claude Code" in ask
    assert plain_reply.for_user(ask).startswith("Before I hand this to Claude Code, please confirm: Create")
    (job,) = jobs.recent(1)
    assert job["state"] == "waiting-on-confirm"
    got = []
    ack = tools.confirm_pending(on_done=got.append)
    assert "Claude Code is working on it" in ack and job["id"] in ack
    done = _wait_done(job["id"], got)           # the same card carries on
    assert len(jobs.recent(5)) == 1 and done["agent"] == "Claude Code"
    assert got == ["Built the page. It grows as you type."]
    assert _Proc.cmd[1:3] == ["--permission-mode", "acceptEdits"] and _Proc.cmd[-2] == "-p"
    assert tools._PENDING_HERMES_CALL is None


def test_the_model_can_name_the_backend(isolated, monkeypatch):
    _fake_claude(monkeypatch)
    out = tools.delegate("fix the chat box in jarvis_hud_v3.html", backend="Claude Code")
    assert out.startswith("[NEEDS_CONFIRM:") and "Claude Code" in out


def test_a_goal_that_names_claude_skips_hermes(isolated, monkeypatch):
    _fake_claude(monkeypatch)
    monkeypatch.setattr(tools, "_is_safe_task", lambda t: False)
    out = tools.run_autonomous("Build a test page in Documents. Delegate this to Claude.")
    assert out.startswith("[NEEDS_CONFIRM:") and "Claude Code" in out


def test_a_cancelled_confirm_is_not_counted_as_working():
    card = plain_reply.card({"task": "x", "state": "cancelled", "note": "declined by the user"})
    assert card["tone"] == "muted" and card["label"] == "Cancelled"
