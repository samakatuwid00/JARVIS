"""A Hermes task that needs a confirm asks at once and builds its brief only
after "confirm" (the brief took 37-60 s before the question, 2026-09-12)."""

import pytest

import jobs
import tools


@pytest.fixture
def hermes(monkeypatch, tmp_path):
    monkeypatch.setattr(jobs, "_LOG_PATH", tmp_path / "jobs.jsonl")
    monkeypatch.setattr(jobs, "_jobs", {})
    monkeypatch.setattr(jobs, "_loaded", True)
    monkeypatch.setattr(jobs, "_listeners", [])
    monkeypatch.setattr(tools, "_audit_log", lambda *a, **k: None)
    monkeypatch.setattr(tools, "_PENDING_HERMES_CALL", None)
    monkeypatch.setitem(tools._PENDING_DESTRUCTIVE, "task", None)
    monkeypatch.setitem(tools._PENDING_DESTRUCTIVE, "autonomous", None)
    import context_assembler
    calls = {"brief": [], "ran": []}
    monkeypatch.setattr(context_assembler, "assemble_brief",
                        lambda task, delegate=None: calls["brief"].append(task) or "BRIEF\n")
    monkeypatch.setattr(tools, "_run_hermes_sync",
                        lambda task, *a, **k: calls["ran"].append(task) or "Deleted it.")
    return calls


def test_a_gated_task_asks_without_building_the_brief(hermes):
    out = tools.delegate_to_hermes_grounded("delete the file C:/tmp/old.txt")
    assert out.startswith("[NEEDS_CONFIRM:")
    assert hermes["brief"] == [] and hermes["ran"] == []
    assert tools.pending_confirm_task() == "delete the file C:/tmp/old.txt"


def test_confirm_builds_the_brief_then_runs_the_same_task(hermes):
    tools.delegate_to_hermes_grounded("delete the file C:/tmp/old.txt")
    out = tools.confirm_pending()
    assert out == "Deleted it."
    assert hermes["brief"] == ["delete the file C:/tmp/old.txt"]
    assert hermes["ran"] == ["BRIEF\ndelete the file C:/tmp/old.txt"]
    assert tools._PENDING_HERMES_CALL is None


def test_no_releases_it_without_building_anything(hermes):
    tools.delegate_to_hermes_grounded("delete the file C:/tmp/old.txt")
    assert tools.decline_pending() is not None
    assert hermes["brief"] == [] and hermes["ran"] == [] and tools.confirm_pending() is None


def test_a_safe_task_still_runs_with_its_brief(hermes, monkeypatch):
    monkeypatch.setattr(tools, "_is_safe_task", lambda t: True)
    monkeypatch.setattr(tools, "_repo_changes", lambda: {})
    tools.delegate_to_hermes_grounded("what is on my calendar today?")
    assert hermes["brief"] == ["what is on my calendar today?"]
    (ran,) = hermes["ran"]                      # look-only brief first, then the task's own
    assert ran.startswith("LOOK-ONLY TASK") and ran.endswith("BRIEF\nwhat is on my calendar today?")


def test_a_confirm_from_the_model_still_needs_the_latched_task(hermes):
    out = tools.delegate_to_hermes_grounded("format the D: drive", confirm=True)
    assert out.startswith("[NEEDS_CONFIRM]") and hermes["ran"] == []
