"""plain_reply.py - what Cygnus tells the user, in words the user knows.

Tools and jobs report in their own terms: "[NEEDS_CONFIRM:ab12cd34] ... isn't
on the pre-approved safe list", job ids, "⟳ AUTONOMOUS_BACKGROUND:", Markdown
headings, "[STEP 2] ✓ ... | claude -p". Those are right for the brain and the
logs and wrong for a person. On 2026-09-12 the owner was told "(status file
empty — still planning or stuck)" about a job that was going fine, and a
finished job's one question ("say proceed") was cut from its announcement.

Applied only where replies leave the server (jarvis_web: display and speech),
never to brain.think's return value: the command chain reads the markers.
Confirm prompts keep the word "confirm", which is what the user must say.
Stdlib only.
"""

from __future__ import annotations

import re
import time

_JOB_ID = re.compile(r"\s*\((?:job|jid)\s+[0-9a-f]{6,}\)|\bjob\s+[0-9a-f]{8}\b", re.I)
_BACKGROUND_MARK = re.compile(r"⟳\s*[A-Z_]+_BACKGROUND:\s*")
_CONFIRM = re.compile(r'^\[NEEDS_CONFIRM(?::[0-9a-f]+)?\][^"“]*["“](?P<task>.+?)["”]'
                      r'(?:\s*\(job\s+[0-9a-f]+\))?\s*$', re.S)
_REISSUE = "[NEEDS_CONFIRM] Please re-issue"
_SAY_CONFIRM = re.compile(r"^NEEDS_CONFIRM(?:\s*\[[^\]]*\])?:\s*I will(?:\s+run)?:?\s*")
# Lines that are the agent's own chatter, not an answer.
_NOISE_LINE = re.compile(r"^\s*(?:\[tool\]|↻ Resumed session|⚠️?\s*No reply:|-{3,}\s*$)", re.I)
_LEAD_IN = re.compile(r"^(?:(?:hey|ok(?:ay)?|please|so|cygnus|jarvis|sir)[,\s]+)*"
                      r"(?:(?:can|could|would)\s+you\s+(?:please\s+)?)?", re.I)
_STEP = re.compile(r"\[STEP\s*(\d+)\]\s*(✓|✗|…|\.\.\.)?\s*([^|]*)", re.I)
_ASKS_USER = re.compile(r"\?\s*$|\bsay\s+\W*(?:proceed|confirm|yes|continue|go ahead)\b", re.I)


def md_to_plain(text: str) -> str:
    """Markdown the HUD would show raw and the voice would read out, as text."""
    t = re.sub(r"^\s*```.*$", "", text, flags=re.M)             # code fences
    t = re.sub(r"^\s{0,3}#{1,6}\s*", "", t, flags=re.M)          # headings
    t = re.sub(r"\*\*(.+?)\*\*|__(.+?)__", lambda m: m.group(1) or m.group(2), t)
    t = re.sub(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])", r"\1", t)
    t = re.sub(r"`([^`]*)`", r"\1", t)                           # inline code
    t = re.sub(r"\[([^\]]+)\]\((?:[^)]+)\)", r"\1", t)           # links
    t = re.sub(r"^\s*[-*•]\s+", "", t, flags=re.M)               # bullets
    return re.sub(r"\n{3,}", "\n\n", t).strip()


def short_task(task, words: int = 9) -> str:
    """A task as a short title: 'set up the chat test page…'."""
    t = str(task or "")
    t = t.split("\n\n", 1)[1] if t.startswith("LOOK-ONLY TASK") and "\n\n" in t else t
    t = _LEAD_IN.sub("", " ".join(t.split()))
    t = re.split(r"(?<=[.!?])\s", t)[0].rstrip(".!?,; ")
    ws = t.split()
    return " ".join(ws[:words]) + ("…" if len(ws) > words else "") if ws else "that task"


def _sentence(s: str) -> str:
    """`s` ending in one full stop, whatever it already ended with."""
    return s if s.endswith(("…", ".", "!", "?")) else s + "."


# Launcher errors as the user would put them.
_NOT_FOUND = re.compile(r"Could not open (?P<app>.+?): The system cannot find the file specified: '[^']*'\.?")


def for_user(text):
    """A reply as the user should see and hear it."""
    if not isinstance(text, str) or not text.strip():
        return text
    t = text.strip()
    if t.startswith(_REISSUE):
        return "I lost track of which task you meant. Please say the task again, then say confirm."
    m = _CONFIRM.match(t)
    if m:
        return (f"Before I start, please confirm: {_sentence(short_task(m['task'], 16))} "
                "Say confirm to go ahead, or no to cancel.")
    t = _SAY_CONFIRM.sub("Before I do this, please confirm. I will ", t)
    t = re.sub(r"^\[NEEDS_PICK\]\s*", "", t)
    t = _BACKGROUND_MARK.sub("", t)
    t = _JOB_ID.sub("", t)
    t = re.sub(r"^\[Error\]\s*", "That didn't work. ", t)
    t = re.sub(r"\[WinError \d+\]\s*", "", t)
    t = _NOT_FOUND.sub(lambda m: f"I couldn't find an app called “{m['app']}”.", t)
    t = t.replace("to refresh the manifest", "so I pick up newly installed apps")
    t = re.sub(r"^\[Blocked\]\s*", "", t)
    t = md_to_plain(t)
    return "\n".join(l for l in t.splitlines() if not _NOISE_LINE.match(l)).strip()


# The worst machine tics a model might still emit (from jarvis_web, where
# they were stripped from speech only). "You're welcome" is not one: it is the
# answer to "thank you", and removing it left a bare "sir." (2026-09-12).
_AI_TICS = [
    re.compile(r"\b(great question[!.]?|i hope this helps[!.]?|absolutely[!.]?|"
               r"certainly[!.]?|feel free to (?:ask|reach out)|"
               r"let me know if you need anything)\b", re.I),
    re.compile(r"\b(let's explore|let's dive in|let's break this down|let me break this down)\b", re.I),
    re.compile(r"\b(a pivotal moment|a game-?changer|a watershed moment|the future looks bright|"
               r"only time will tell)\b", re.I),
]
_EM_DASH = re.compile(r"—|--")
_ONLY_ADDRESS = re.compile(r"\W*(?:sir)?\W*", re.I)


def _tidy_speech(text: str) -> str:
    for pat in _AI_TICS:
        text = pat.sub("", text)
    text = _EM_DASH.sub(",", text)                  # a dash makes speech stumble
    text = re.sub(r"\s{2,}", " ", text)
    text = re.sub(r"\s+,", ",", text).strip()
    return re.sub(r"^[\s,.!]+", "", text)             # what a removed opener left


def tidy(text):
    """for_user(), then the machine tics out. A reply that was nothing but a
    tic ("Absolutely, sir.") is kept as it is rather than cut to "sir."."""
    if not isinstance(text, str) or not text:
        return text
    plain = for_user(text)
    tidied = _tidy_speech(plain)
    return _EM_DASH.sub(",", plain) if _ONLY_ADDRESS.fullmatch(tidied) else tidied


def pending_ask(text: str) -> str | None:
    """The question a result ends on ('Say proceed to launch…'), if any."""
    lines = [l.strip() for l in for_user(text or "").splitlines() if l.strip()]
    if not lines or not _ASKS_USER.search(lines[-1]):
        return None
    ask = lines[-1]
    if ask.lower().startswith("next step"):
        ask = ask.split(":", 1)[-1].strip() or ask
    return ask[:200]


def _first_sentence(text: str, limit: int = 160) -> str:
    t = " ".join(for_user(text or "").split())
    return re.split(r"(?<=[.!?])\s", t)[0][:limit] if t else ""


def _reason(err: str) -> str:
    e = err or ""
    m = re.search(r"within (\d+)\s*s", e)
    if m:
        return f"it ran out of time after {round(int(m.group(1)) / 60)} minutes"
    if "interrupted by restart" in e:
        return "Cygnus restarted while it was running"
    return _first_sentence(e).rstrip(".") or "something went wrong"


def _full_result(event: dict) -> str:
    """The job's whole result (the event carries only a 200-character summary)."""
    try:
        import jobs
        j = jobs.get(event.get("id")) or {}
        return j.get("result") or event.get("summary") or ""
    except Exception:
        return event.get("summary") or ""


def announce_job(event: dict) -> str | None:
    """A short spoken line for a job that reached an end state, or None."""
    state = event.get("state")
    title = short_task(event.get("task"))
    note = event.get("note") or ""
    if state == "done":
        body = _full_result(event)
        ask = pending_ask(body)
        if ask:
            return f"“{title}” needs you: {ask}"
        first = _first_sentence(body)
        return f"Done: {_sentence(title)}" + (f" {first}" if first else "")
    if state == "error":
        return f"“{title}” didn't finish: {_reason(event.get('error') or event.get('summary'))}."
    if state == "timeout":
        if "superseded" in note:        # re-confirmed: the same task runs as a new job
            return None
        if note.startswith("expired"):
            return f"I dropped “{title}” because it wasn't confirmed."
        return f"“{title}” ran out of time."
    if state == "unverified":
        return (f"“{title}” finished, but I couldn't double-check the result. "
                "Ask me what happened and I'll look.")
    return None


def _step(note: str) -> str | None:
    m = _STEP.search(note or "")
    if not m:
        return "planning it" if (note or "").startswith("planning") else None
    return f"step {m.group(1)}, {m.group(3).strip().rstrip('.').lower()}"


def _ago(seconds: float) -> str:
    mins = int(seconds // 60)
    if mins < 1:
        return "just now"
    return f"{mins} minute{'s' if mins != 1 else ''} ago" if mins < 90 else f"{mins // 60} hours ago"


def job_status(now: float | None = None) -> str:
    """'status?' for every kind of background task, in plain words."""
    import jobs
    now = now or time.time()
    act = jobs.active()
    if act:
        parts = []
        for j in act[-3:]:
            title = short_task(j.get("task"))
            if j.get("state") == "waiting-on-confirm":
                parts.append(f"“{title}” is waiting for you to say confirm.")
                continue
            mins = int((now - (j.get("started") or now)) // 60)
            took = f"{mins} minute{'s' if mins != 1 else ''} so far" if mins else "just started"
            step = _step((j.get("progress") or [""])[-1])
            parts.append(f"Still working on “{title}”, {took}." + (f" Now on {step}." if step else ""))
        return " ".join(parts)
    recent = jobs.recent(1)
    if not recent:
        return "Nothing is running, and there are no recent tasks."
    j = recent[0]
    outcome = {"done": "finished", "error": "didn't finish", "timeout": "ran out of time",
               "unverified": "finished but wasn't double-checked"}.get(j.get("state"), j.get("state"))
    ended = (j.get("started") or now) + (j.get("elapsed") or 0)
    return f"Nothing is running. The last task, “{short_task(j.get('task'))}”, {outcome} {_ago(now - ended)}."
