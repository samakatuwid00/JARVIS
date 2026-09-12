"""Every helper that can change things waits for the user's own confirm.
The model's confirm=true alone, or asking for the same goal twice, is not
one: both started work unasked before 2026-09-13."""

import pytest

import jobs
import tools


@pytest.fixture
def ran(monkeypatch, tmp_path):
    monkeypatch.setattr(jobs, "_LOG_PATH", tmp_path / "jobs.jsonl")
    monkeypatch.setattr(jobs, "_jobs", {})
    monkeypatch.setattr(jobs, "_loaded", True)
    monkeypatch.setattr(jobs, "_listeners", [])
    monkeypatch.setattr(tools, "_audit_log", lambda *a, **k: None)
    monkeypatch.setattr(tools, "_PENDING_HERMES_CALL", None)
    monkeypatch.setitem(tools._PENDING_DESTRUCTIVE, "task", None)
    monkeypatch.setitem(tools._PENDING_DESTRUCTIVE, "autonomous", None)
    calls = []
    monkeypatch.setattr(tools, "_start_cli_job",
                        lambda task, name, **k: calls.append(("cli", name, task)) or "STARTED")
    monkeypatch.setattr(tools, "_run_specialist",
                        lambda task, agent=None, timeout=300: calls.append(("spec", agent, task)) or "SPEC DONE")
    monkeypatch.setattr(tools, "_run_pi_specialist",
                        lambda task, agent=None, timeout=300: calls.append(("pi", agent, task)) or "PI DONE")
    import autonomous
    monkeypatch.setattr(autonomous, "start_goal",
                        lambda goal, timeout=1800, progress_cb=None: (calls.append(("auto", goal)) or "AUTO ACK", "a1"))
    return calls


def test_the_models_confirm_alone_does_not_start_claude(ran):
    out = tools._cli_delegate("fix the chat box", "claude", confirm=True)
    assert out.startswith("[NEEDS_CONFIRM:") and ran == []


def test_a_confirm_for_the_task_the_user_heard_starts_it(ran):
    tools._cli_delegate("fix the chat box", "claude")
    assert tools._cli_delegate("fix the chat box", "claude", confirm=True) == "STARTED"
    assert ran == [("cli", "claude", "fix the chat box")] and tools._PENDING_HERMES_CALL is None


def test_a_confirm_for_another_task_does_not(ran):
    tools._cli_delegate("fix the chat box", "claude")
    out = tools._cli_delegate("delete my notes folder", "claude", confirm=True)
    assert out.startswith("[NEEDS_CONFIRM:") and ran == []


def test_asking_twice_is_not_a_confirm_for_a_goal(ran):
    goal = "organize my downloads folder"
    assert tools.run_autonomous(goal).startswith("[NEEDS_CONFIRM]")
    assert tools.run_autonomous(goal).startswith("[NEEDS_CONFIRM]")
    assert ran == []
    assert tools.confirm_autonomous() == "AUTO ACK"
    assert ran == [("auto", goal)] and tools.pending_autonomous_goal() is None
    assert tools.confirm_autonomous() is None


def test_a_goal_confirm_flag_needs_the_goal_the_user_heard(ran):
    goal = "organize my downloads folder"
    assert tools.run_autonomous(goal, confirm=True).startswith("[NEEDS_CONFIRM]") and ran == []
    assert tools.run_autonomous(goal, confirm=True) == "AUTO ACK"


def test_opencode_asks_before_changing_things(ran):
    out = tools.delegate("refactor the login page", backend="opencode")
    assert out.startswith("[NEEDS_CONFIRM:") and ran == []
    assert tools.confirm_pending() == "SPEC DONE"
    assert ran == [("spec", None, "refactor the login page")] and tools._PENDING_HERMES_CALL is None


def test_a_look_only_specialist_task_runs_at_once(ran):
    assert tools._specialist_delegate("list the files in my project", "project-runner") == "SPEC DONE"


def test_pi_asks_then_runs_in_its_sandbox(ran):
    out = tools._specialist_delegate("build a todo app", None, pi=True)
    assert out.startswith("[NEEDS_CONFIRM:") and "Pi" in out and ran == []
    assert tools.confirm_pending() == "PI DONE" and ran == [("pi", None, "build a todo app")]


def test_no_drops_a_waiting_specialist(ran):
    tools._specialist_delegate("build a todo app", None)
    assert tools.decline_pending() is not None
    assert tools.confirm_pending() is None and ran == []


def test_claude_gets_the_code_brief(monkeypatch):
    import context_assembler as ca
    monkeypatch.setattr(ca, "_load_profile", lambda: "")
    monkeypatch.setattr(ca, "_recent_summary", lambda n: "")
    monkeypatch.setattr(ca, "_app_context", lambda t: "")
    monkeypatch.setattr(ca, "_vault_context", lambda t, max_hits=3, delegate=None: "")
    assert "CODE delegate (claude-cli)" in ca.assemble_brief("fix the chat box", delegate="claude")
