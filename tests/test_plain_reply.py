"""plain_reply: markers, job ids and Markdown never reach the user; job
announcements and "status?" are plain; a job's closing question is spoken."""

import time

import jobs
import plain_reply as pr

MOBILE = ("here in mobile when I click your chat terminal the chat terminal does not expand, "
          "and also the orb disappears")


def test_a_hermes_confirm_is_a_plain_question_that_still_says_confirm():
    raw = ("[NEEDS_CONFIRM:6118166e] That task changes state or isn't on the pre-approved safe "
           f"list. If you want me to proceed, say or type 'confirm' and I'll run it through "
           f"Hermes: \"{MOBILE}\" (job 6118166e)")
    out = pr.for_user(raw)
    assert out.startswith("Before I start, please confirm: here in mobile when I click")
    assert "confirm" in out.split(":", 1)[1]
    for gone in ("NEEDS_CONFIRM", "6118166e", "allowlist", "safe list", "Hermes"):
        assert gone not in out


def test_an_autonomous_confirm_too():
    raw = ("[NEEDS_CONFIRM] That goal could change state on this machine and isn't on the safe "
           "allowlist. Say 'confirm' to let me run it autonomously: \"Create the test page.\"")
    assert pr.for_user(raw) == ("Before I start, please confirm: Create the test page. "
                                "Say confirm to go ahead, or no to cancel.")


def test_background_acks_lose_their_marker_and_job_id():
    assert pr.for_user("⟳ AUTONOMOUS_BACKGROUND:On it, sir — working autonomously now.") == \
        "On it, sir — working autonomously now."
    assert pr.for_user("⟳ HERMES_BACKGROUND: On it, sir, working on that now… (job c6112ad0)") == \
        "On it, sir, working on that now…"


def test_markdown_and_agent_chatter_are_removed():
    raw = ("### Analysis of the Issue (`jarvis_hud_v3.html`)\n1. **Chat terminal**: it does not "
           "grow.\n- see [docs](https://x.y)\n[tool] ( ˘⌣˘)♡ computing...\n"
           "↻ Resumed session 2026 \"```json #5\"")
    out = pr.for_user(raw)
    assert out == "Analysis of the Issue (jarvis_hud_v3.html)\n1. Chat terminal: it does not grow.\nsee docs"


def test_errors_are_worded_for_people():
    raw = ("[Error] Could not open github for me: [WinError 2] The system cannot find the file "
           "specified: 'github for me'. Say 'scan installed software' to refresh the manifest.")
    assert pr.for_user(raw) == ("That didn't work. I couldn't find an app called “github for me”. "
                                "Say 'scan installed software' so I pick up newly installed apps.")


def test_a_long_title_ends_once():
    raw = f'[NEEDS_CONFIRM:1] x: "{MOBILE}, please fix it quickly and tell me" (job 1)'
    assert "…. " not in pr.for_user(raw) and "… Say confirm" in pr.for_user(raw)


def test_thanks_keeps_its_answer():
    assert pr.tidy("You're welcome, sir.") == "You're welcome, sir."
    assert pr.tidy("Absolutely, sir.") == "Absolutely, sir."


def test_tidy_still_drops_machine_tics_and_dashes():
    assert pr.tidy("Great question! The answer is 4 — easy.") == "The answer is 4, easy."
    assert pr.tidy("⟳ HERMES_BACKGROUND: On it, sir (job c6112ad0)") == "On it, sir"


def test_terminal_colour_codes_are_never_read_out():
    raw = "\x1b[0m > build · cx/gpt-5.4-mini \x1b[0m \x1b[91m\x1b[1mError: \x1b[0mMissing API key."
    assert "\x1b" not in pr.for_user(raw) and "Missing API key" in pr.for_user(raw)


def test_ordinary_replies_are_untouched():
    assert pr.for_user("Your name is Roger, sir.") == "Your name is Roger, sir."


def _job(monkeypatch, **rec):
    monkeypatch.setattr(jobs, "get", lambda jid: rec)
    return {"type": "job", "id": "x", "task": rec.get("task"), "state": rec.get("state"),
            "note": rec.get("note"), "summary": (rec.get("result") or "")[:200], "error": rec.get("error")}


def test_a_finished_job_that_asks_something_says_so(monkeypatch):
    result = ("Here is the summary of the analysis.\n### Recommended Fix Plan\n- add a focus class\n"
              + "detail line\n" * 30 + "### Next Step\nSay **proceed** to launch the fix.\n"
              "[tool] ( ˘⌣˘)♡ computing...")
    line = pr.announce_job(_job(monkeypatch, task=MOBILE, state="done", result=result))
    assert line == "“here in mobile when I click your chat terminal…” needs you: Say proceed to launch the fix."


def test_a_plain_finished_job(monkeypatch):
    line = pr.announce_job(_job(monkeypatch, task="Create the page.", state="done",
                                result="The page is ready at Documents. It has a growing box."))
    assert line == "Done: Create the page. The page is ready at Documents."


def test_end_states_in_plain_words(monkeypatch):
    err = _job(monkeypatch, task="read the HUD file", state="error",
               error="[Error] Hermes did not finish within 300s. The task may be too large.")
    assert pr.announce_job(err) == "“read the HUD file” didn't finish: it ran out of time after 5 minutes."
    re_run = _job(monkeypatch, task="fix it", state="timeout", note="superseded — re-confirmed, running as a new job")
    assert pr.announce_job(re_run) is None
    assert "couldn't double-check" in pr.announce_job(_job(monkeypatch, task="fix it", state="unverified"))
    assert pr.announce_job(_job(monkeypatch, task="fix it", state="running")) is None


def test_cards_have_one_plain_state_each(monkeypatch):
    def c(**rec):
        return pr.card(_job(monkeypatch, **rec))
    run = c(task="Create the smoke page. Delegate it to Claude.", state="running",
            note="[STEP 2] … Dispatching creation task to Claude Code | claude -p")
    assert (run["title"], run["label"], run["tone"], run["detail"]) == \
        ("Create the smoke page", "Working", "run", "Step 2, dispatching creation task to claude code")
    wait = c(task="test", state="waiting-on-confirm")
    assert (wait["label"], wait["tone"], wait["confirm"]) == ("Needs you", "ask", True)
    ask = c(task=MOBILE, state="done", result="Analysis done.\nSay **proceed** to launch the fix.")
    assert (ask["label"], ask["tone"], ask["detail"], ask["confirm"]) == \
        ("Needs you", "ask", "Say proceed to launch the fix.", False)
    assert c(task="x", state="done", result="The page is ready.")["tone"] == "ok"
    assert c(task="x", state="unverified")["label"] == "Done, not checked"
    bad = c(task="x", state="error", error="[Error] Hermes did not finish within 300s.")
    assert (bad["label"], bad["detail"]) == ("Didn't finish", "It ran out of time after 5 minutes.")
    assert c(task="x", state="timeout", note="superseded — re-confirmed, running as a new job")["tone"] == "gone"
    assert c(task="x", state="timeout", note="expired — no confirm within 10 min")["tone"] == "muted"


def test_status_while_a_job_runs(monkeypatch):
    now = time.time()
    monkeypatch.setattr(jobs, "active", lambda: [
        {"task": "Create the smoke page, delegate to Claude.", "state": "running", "started": now - 130,
         "progress": ["planning…", "[STEP 2] … Dispatching creation task to Claude Code | claude -p"]},
        {"task": "test", "state": "waiting-on-confirm", "started": now - 5, "progress": []}])
    out = pr.job_status(now)
    assert out == ("Still working on “Create the smoke page, delegate to Claude”, 2 minutes so far. "
                   "Now on step 2, dispatching creation task to claude code. "
                   "“test” is waiting for you to say confirm.")
    assert "status file" not in out


def test_status_when_nothing_runs(monkeypatch):
    now = time.time()
    monkeypatch.setattr(jobs, "active", lambda: [])
    monkeypatch.setattr(jobs, "recent", lambda n: [{"task": "read the HUD file", "state": "error",
                                                    "started": now - 600, "elapsed": 300}])
    assert pr.job_status(now) == "Nothing is running. The last task, “read the HUD file”, didn't finish 5 minutes ago."
