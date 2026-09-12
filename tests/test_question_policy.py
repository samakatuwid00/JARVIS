"""A question about what happened is answered, never acted on; a look-only
Hermes job is told it is look-only and reports files that changed anyway."""

import pytest

import brain_gemini
import tools


@pytest.mark.parametrize("text", [
    "What happened with that? Explain it simply.",
    "summarize it to me what happened?",
    "status?",
    "Cygnus, what did you do?",
    "explain what went wrong",
])
def test_a_report_question_holds_a_tool_that_changes_things(text):
    held = brain_gemini._held_for_question("write_file", {"path": "x.html"}, text)
    assert held and held.startswith("[Held]")
    assert brain_gemini._held_for_question("ask_ai", {"site": "claude"}, text)
    assert brain_gemini._held_for_question("job_control", {"action": "stop"}, text)


@pytest.mark.parametrize("text", [
    "open github",
    "can you open github for me?",
    "write a note about the meeting",
    "what is the weather in Manila?",
])
def test_requests_and_other_questions_still_run(text):
    assert brain_gemini._held_for_question("open_site", {"name": "github"}, text) is None


def test_looking_is_allowed_while_answering():
    for name, args in (("read_file", {}), ("job_control", {"action": "status"})):
        assert brain_gemini._held_for_question(name, args, "what happened?") is None


def test_a_held_tool_never_reaches_tools(monkeypatch):
    ran = []
    monkeypatch.setattr(tools, "execute_tool", lambda name, args: ran.append(name) or "ran")
    out = brain_gemini.execute_tool("write_file", {"path": "x"}, "what happened?")
    assert out.startswith("[Held]") and ran == []


def _fake_hermes(monkeypatch, changes, tmp_path):
    import jobs
    # The job registry and audit trail are the real ones otherwise: every run
    # of these tests put a "read jarvis_hud_v3.html…" job in the owner's
    # Active Tasks.
    monkeypatch.setattr(jobs, "_LOG_PATH", tmp_path / "jobs.jsonl")
    monkeypatch.setattr(jobs, "_jobs", {})
    monkeypatch.setattr(jobs, "_loaded", True)
    monkeypatch.setattr(jobs, "_listeners", [])
    monkeypatch.setattr(tools, "_audit_log", lambda *a, **k: None)
    sent = []
    snapshots = iter(changes)
    monkeypatch.setattr(tools, "_is_safe_task", lambda t: True)
    monkeypatch.setattr(tools, "_run_hermes_sync", lambda task, *a, **k: sent.append(task) or "Here is what I found.")
    monkeypatch.setattr(tools, "_repo_changes", lambda: next(snapshots))
    return sent


def test_a_look_only_job_is_told_so(monkeypatch, tmp_path):
    sent = _fake_hermes(monkeypatch, [{}, {}], tmp_path)
    out = tools.delegate_to_hermes("read jarvis_hud_v3.html and find the chat CSS")
    assert sent[0].startswith("LOOK-ONLY TASK")
    assert out == "Here is what I found."


def test_a_look_only_job_that_changed_a_file_says_so(monkeypatch, tmp_path):
    _fake_hermes(monkeypatch, [{"tools.py": 1.0}, {"tools.py": 1.0, "jarvis_hud_v3.html": 2.0}], tmp_path)
    out = tools.delegate_to_hermes("read jarvis_hud_v3.html and find the chat CSS")
    assert "changed while this look-only job ran: jarvis_hud_v3.html" in out
    assert "tools.py" not in out.split("ran:")[1]
