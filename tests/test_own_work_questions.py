"""2026-09-12 session: "What coding agent did you use to create this website?"
came back as the site registry. The list_sites fast path fired on any
what-question that said "website", and with that fixed alone the same words
(create + website) would have queued a second build. A question about what
JARVIS already did is answered from the job log and audit trail, never
re-run and never read as a registry listing."""

import datetime
import json

import pytest


@pytest.mark.parametrize("text", [
    "What coding agent did you use to create this website?",
    "what is this website about?",
    "what did you put on the website?",
    "which agent built the site?",
    "tell me what the website does"])
def test_questions_about_a_website_are_not_the_site_list(text):
    import brain_gemini
    assert brain_gemini.classify_intent(text) != "list_sites"


@pytest.mark.parametrize("text", [
    "what sites do you know?", "what websites do you know", "which sites have i visited the most",
    "list my sites", "show my bookmarks", "tell me the sites you know", "do you know my websites"])
def test_registry_questions_still_list_the_sites(text):
    import brain_gemini
    assert brain_gemini.classify_intent(text) == "list_sites"


@pytest.mark.parametrize("text, own", [
    ("What coding agent did you use to create this website?", True),
    ("which tool built the site?", True),
    ("how did you build it", True),
    ("did you use opencode?", True),
    ("Jarvis, what did you do?", True),
    ("who made this website?", True),
    ("Can you create a Hello World website with open code?", False),
    ("create a website then open it", False),
    ("how do I build a website?", False),
    ("what time is it", False)])
def test_questions_about_jarvis_own_work(text, own):
    import brain_gemini
    assert brain_gemini._asks_about_own_work(text) is own


def test_job_history_keeps_every_step_across_a_restart(tmp_path, monkeypatch):
    import jobs
    log = tmp_path / "jobs.jsonl"
    rows = [{"id": "j1", "ts": 1, "task": "build a site", "tier": "autonomous", "agent": None,
             "state": "queued"},
            {"id": "j1", "ts": 2, "state": "running", "note": "[STEP 1] ✓ wrote spec.md"},
            {"id": "j1", "ts": 3, "state": "running", "note": "[STEP 2] ✓ Dispatched OpenCode CLI"},
            {"id": "j1", "ts": 4, "state": "unverified", "summary": "no RESULT line"}]
    log.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    monkeypatch.setattr(jobs, "_LOG_PATH", log)
    monkeypatch.setattr(jobs, "_jobs", {})
    monkeypatch.setattr(jobs, "_loaded", False)
    facts = jobs.recent_work()
    assert "[STEP 1] ✓ wrote spec.md" in facts and "Dispatched OpenCode CLI" in facts
    assert "unverified" in facts and "no RESULT line" in facts


def test_recent_file_writes_reads_only_executed_writes(tmp_path, monkeypatch):
    import tools
    now = datetime.datetime.now()
    old = now - datetime.timedelta(hours=5)
    rows = [{"ts": now.isoformat(), "caller": "write_file", "task": "C:\\site\\index.html",
             "decision": "executed"},
            {"ts": now.isoformat(), "caller": "write_file", "task": "C:\\site\\blocked.html",
             "decision": "blocked_exists"},
            {"ts": old.isoformat(), "caller": "write_file", "task": "C:\\old.txt",
             "decision": "executed"}]
    audit = tmp_path / "audit.jsonl"
    audit.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    monkeypatch.setattr(tools, "_AUDIT_PATH", audit)
    writes = tools.recent_file_writes(hours=2)
    assert len(writes) == 1 and writes[0].endswith("C:\\site\\index.html")


def test_the_answer_is_grounded_then_the_turn_text_restored(monkeypatch):
    import brain_gemini
    import jobs
    import tools
    monkeypatch.setattr(jobs, "recent_work", lambda *a, **k: "- 14:06 job j1 (autonomous): "
                                                               "[STEP 2] ✓ Dispatched OpenCode CLI")
    monkeypatch.setattr(tools, "recent_file_writes", lambda *a, **k: ["14:16 C:\\site\\index.html"])
    facts = brain_gemini._own_work_facts()
    assert "Dispatched OpenCode CLI" in facts and "C:\\site\\index.html" in facts

    brain = object.__new__(brain_gemini.JarvisBrain)
    brain.conversation = [{"role": "user", "content": "which agent built it?"}]
    seen = []
    brain._lead_record = None
    brain.last_stats, brain.last_backend = {}, "cloud"

    def turn(user_input, **kw):
        brain._ground_own_work()
        seen.append(brain.conversation[-1]["content"])
        brain.conversation.append({"role": "assistant", "content": "OpenCode, sir."})
        return "OpenCode, sir."
    monkeypatch.setattr(brain, "_think_turn", turn)
    assert brain.think("which agent built it?") == "OpenCode, sir."
    assert "Dispatched OpenCode CLI" in seen[0]
    assert brain.conversation[0]["content"] == "which agent built it?"
