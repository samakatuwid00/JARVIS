"""memory_hygiene.py — Phase 8: Periodic summarization for bounded memory.

jarvis-profile.md and sessions/*.jsonl grow forever; this keeps them bounded
via a summarization pass that:
  - for jarvis-profile.md: trims ## Remembered to _REMEMBER_CAP and archives older bullets to sessions/archive/
  - for sessions/: rolls up sessions older than 7 days into a summary note in vault (or local archive) and marks original JSONL as summarized

This is the "finished thing" feeling: JARVIS retains what matters without growing unbounded.

Run via: python -m memory_hygiene  (or as background thread from jarvis_web)
Stdlib only.
"""

import os
import re
import json
import time
import datetime
from pathlib import Path

HERE = Path(__file__).parent
PROFILE_PATH = HERE / "jarvis-profile.md"
SESSIONS_DIR = HERE / "sessions"
ARCHIVE_DIR = SESSIONS_DIR / "archive"
REMEMBER_HEADING = "## Remembered"
REMEMBER_CAP = 40
SESSION_SUMMARY_AGE_DAYS = 7

def _audit(msg: str, extra: dict = None):
    try:
        from tools import _audit_log
        _audit_log("memory_hygiene", msg[:200], "executed", extra=extra or {})
    except Exception:
        pass

def summarize_profile() -> str:
    """Keep ## Remembered at cap; archive older bullets. Returns report."""
    try:
        if not PROFILE_PATH.exists():
            return "No profile to summarize"
        content = PROFILE_PATH.read_text(encoding="utf-8")
        if REMEMBER_HEADING not in content:
            return "Profile has no Remembered section"
        lines = content.splitlines()
        idx = lines.index(REMEMBER_HEADING)
        remembered = [l for l in lines[idx+1:] if l.startswith("- ")]
        if len(remembered) <= REMEMBER_CAP:
            return f"Profile Remembered at {len(remembered)}/{REMEMBER_CAP} — no action"
        # keep newest cap, archive older
        keep = remembered[-REMEMBER_CAP:]
        archive = remembered[:-REMEMBER_CAP]
        new_content = "\n".join(lines[:idx+1] + keep) + "\n"
        PROFILE_PATH.write_text(new_content, encoding="utf-8")
        # archive
        ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M")
        arch_path = ARCHIVE_DIR / f"remembered_{stamp}.md"
        arch_path.write_text("# Archived Remembered bullets\n\n" + "\n".join(archive) + "\n", encoding="utf-8")
        _audit(f"summarized profile: archived {len(archive)} bullets to {arch_path.name}")
        return f"Archived {len(archive)} old Remembered bullets to {arch_path.name}, kept {len(keep)}"
    except Exception as e:
        return f"[Error] summarize_profile: {e}"

def summarize_sessions(older_than_days: int = SESSION_SUMMARY_AGE_DAYS) -> str:
    """Roll up sessions/*.jsonl older than N days into a one-line summary and mark."""
    try:
        if not SESSIONS_DIR.exists():
            return "No sessions dir"
        cutoff = time.time() - older_than_days * 86400
        rolled = 0
        for p in SESSIONS_DIR.glob("jarvis-*.jsonl"):
            if p.stat().st_mtime > cutoff:
                continue
            if ".summarized" in p.name:
                continue
            # count turns
            try:
                turns = [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]
            except Exception:
                continue
            if not turns:
                continue
            # simple extract: last 5 turns as summary
            summary = "; ".join(f"{t.get('role')}:{(t.get('text') or '')[:80]}" for t in turns[-5:])
            # write summary sidecar
            side = p.with_suffix(".summary.txt")
            side.write_text(f"Summary of {p.name} ({len(turns)} turns):\n{summary}\n", encoding="utf-8")
            # mark original as summarized by renaming? Keep file but create marker
            marker = p.with_name(p.stem + ".summarized")
            marker.write_text(f"summarized {datetime.datetime.now().isoformat()} {len(turns)} turns\n", encoding="utf-8")
            rolled += 1
            _audit(f"summarized session {p.name}", extra={"turns": len(turns)})
        return f"Summarized {rolled} session(s) older than {older_than_days}d"
    except Exception as e:
        return f"[Error] summarize_sessions: {e}"

def run_hygiene() -> str:
    """Run both passes."""
    a = summarize_profile()
    b = summarize_sessions()
    # Semantic fact store: decay unreinforced facts below the recall floor.
    c = ""
    try:
        import semantic_memory
        c = semantic_memory.consolidate()
    except Exception as e:
        c = f"[semantic decay skipped: {e}]"
    parts = [a, b, c]
    # Sleep-time compute: distill NEW episode lines into durable facts (the
    # extraction LLM runs here, off the user's turn — Letta's pattern). The
    # state marker only advances for chunks whose extraction succeeded, so a
    # router blip re-processes the same chunk next cycle instead of losing it.
    d = ""
    try:
        d = distill_episodes()
    except Exception as e:
        d = f"[distill skipped: {e}]"
    parts.append(d)
    return "; ".join(parts)


# ---- sleep-time distillation (upgrade #2) ---------------------------------
DISTILL_STATE = HERE / "memory" / "distilled.json"
DISTILL_CHUNK_LINES = 80     # episode lines per extraction call
DISTILL_MAX_FACTS = 8        # per chunk
DISTILL_RECENT_DAYS = 14     # only distill sessions touched in the last 2 weeks
_DISTILL_PROMPT = """You are the memory curator of an AI assistant. Below is a slice of
today's conversation between the user and the assistant (JARVIS).

Extract ONLY durable facts worth remembering long-term: the user's preferences,
decisions, projects, business details, recurring people/places, and standing
instructions. Ignore ephemeral commands, questions, small talk, and anything
already obvious from the text. Write each fact as one short standalone sentence.
Return a JSON array of strings, at most {max} items. Return [] when nothing
durable appears.

CONVERSATION SLICE:
{turns}"""


def _llm_extract(turns_text: str) -> list:
    """Ask the router model for durable facts. Raises on failure (caller must
    treat failure as 'chunk not processed' — the state marker must not move)."""
    from openai import OpenAI
    from config import ROUTER_BASE_URL, ROUTER_API_KEY, ROUTER_MODEL
    client = OpenAI(base_url=ROUTER_BASE_URL, api_key=ROUTER_API_KEY or "dummy",
                    timeout=60, max_retries=1)
    prompt = _DISTILL_PROMPT.replace("{max}", str(DISTILL_MAX_FACTS)).replace(
        "{turns}", turns_text[:12000])
    r = client.chat.completions.create(
        model=ROUTER_MODEL,
        messages=[{"role": "user", "content": prompt}],
        max_tokens=600, temperature=0.2)
    raw = (r.choices[0].message.content or "").strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```[a-z]*\n?|\n?```$", "", raw.strip(), flags=re.I)
    arr = json.loads(raw)
    if not isinstance(arr, list):
        raise ValueError("extractor returned non-array")
    return [str(x).strip() for x in arr if str(x).strip()][:DISTILL_MAX_FACTS]


def distill_episodes() -> str:
    """Extract durable facts from new episode lines into semantic memory."""
    import semantic_memory

    state = {}
    if DISTILL_STATE.exists():
        try:
            state = json.loads(DISTILL_STATE.read_text(encoding="utf-8"))
        except Exception:
            state = {}
    cutoff = time.time() - DISTILL_RECENT_DAYS * 86400
    total_facts, touched, failures = 0, 0, 0

    for p in sorted(SESSIONS_DIR.glob("jarvis-*.jsonl")):
        if p.stat().st_mtime < cutoff:
            continue
        if p.stem + ".summarized" in {q.name for q in SESSIONS_DIR.glob(p.stem + ".*")}:
            continue
        done = int(state.get(p.name, 0))
        try:
            lines = p.read_text(encoding="utf-8").splitlines()
        except Exception:
            continue
        new = lines[done:]
        if not new:
            continue
        touched += 1
        for i in range(0, len(new), DISTILL_CHUNK_LINES):
            chunk = new[i:i + DISTILL_CHUNK_LINES]
            turns = []
            for ln in chunk:
                try:
                    rec = json.loads(ln)
                except Exception:
                    continue
                role = "USER" if rec.get("role") == "user" else "JARVIS"
                turns.append(f"{role}: {(rec.get('text') or '')[:220]}")
            if not turns:
                state[p.name] = done + len(chunk)
                continue
            try:
                facts = _llm_extract("\n".join(turns))
            except Exception as e:
                failures += 1
                print(f"[DISTILL] extraction failed on {p.name}: {e}", flush=True)
                continue        # state does NOT advance — retried next cycle
            for f in facts:
                try:
                    semantic_memory.add(f, source="hygiene",
                                        source_ref=f"{p.name}:L{done + i + 1}-{done + i + len(chunk)}")
                    total_facts += 1
                except Exception:
                    pass
            state[p.name] = done + len(chunk)

    try:
        DISTILL_STATE.parent.mkdir(parents=True, exist_ok=True)
        DISTILL_STATE.write_text(json.dumps(state), encoding="utf-8")
    except Exception:
        pass
    return (f"distilled {total_facts} fact(s) from {touched} session file(s)"
            + (f" ({failures} extraction failure(s))" if failures else ""))

# Proactive reporting hook: called by jobs listener to voice completions
def proactive_job_announcement(event: dict) -> str | None:
    """Return a short spoken line for a terminal job event, or None if not terminal.
    Plain words (plain_reply.announce_job): a short title, no Markdown, the
    job's closing question if it has one, and nothing for a job that was only
    replaced by its re-confirmed run."""
    import plain_reply
    return plain_reply.announce_job(event)

# Background hygiene loop (for jarvis_web to start as daemon thread)
def start_hygiene_loop(interval_hours: int = 6):
    """Start daemon thread that runs hygiene every interval_hours. Returns thread."""
    import threading
    def loop():
        while True:
            time.sleep(interval_hours * 3600)
            try:
                run_hygiene()
            except Exception:
                pass
    t = threading.Thread(target=loop, daemon=True, name="memory-hygiene")
    t.start()
    return t

if __name__ == "__main__":
    print(run_hygiene())
