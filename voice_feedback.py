"""voice_feedback.py — JARVIS spoken state-cue policy (Phase A, 2026-08-28 plan).

Single choke point every spoken state update must pass through, so the user
hears friendly feedback ("Thinking, sir.") without JARVIS becoming chatty and
without duplicate announcements from the progress and job-event channels.

Policy:
  - MODE off        -> nothing speaks.
  - MODE essential  -> thinking/escalation/terminal cues + throttled progress.
  - MODE full       -> same today; Phase B adds milestones here.
  - dedup: identical (normalized) line inside DEDUP_WINDOW_S is dropped.
  - cooldown: non-high cues within VOICE_CUE_COOLDOWN_S of the last spoken cue
    are dropped (HUD still gets the text silently via the caller).
  - high priority (terminal job states, gate reminders) always passes dedup
    cooldowns and preempts nothing (it IS the news).
  - cue audio for standard lines is pre-synthesized at startup (cache);
    dynamic lines synthesize live through the registered synthesizer.
"""
from __future__ import annotations

import asyncio
import os
import re
import threading
import time

from config import (VOICE_FEEDBACK_MODE, VOICE_THINKING_CUE_S,
                    VOICE_THINKING_ESCALATE_S, VOICE_CUE_COOLDOWN_S)

MODE = VOICE_FEEDBACK_MODE
THINKING_CUE_S = VOICE_THINKING_CUE_S
THINKING_ESCALATE_S = VOICE_THINKING_ESCALATE_S
COOLDOWN_S = VOICE_CUE_COOLDOWN_S
DEDUP_WINDOW_S = 20.0

# Live mode lives in a mutable holder so set_mode() reaches already-constructed
# Policies (per-connection instances) without re-registering anything.
_MODE_HOLDER = {"value": MODE}


def set_mode(mode: str) -> str:
    """Switch the feedback mode at runtime (HUD toggle / POST /voice_feedback)."""
    m = (mode or "").strip().lower()
    if m not in ("off", "essential", "full"):
        raise ValueError(f"unknown feedback mode: {mode!r}")
    _MODE_HOLDER["value"] = m
    return m


def current_mode() -> str:
    return _MODE_HOLDER["value"]

# Shared audio cache: line -> base64 audio. Static cues are pre-synthesized at
# startup; dynamic lines are cached on first use. Safe to share across all
# connected clients (audio is immutable per line).
_cache: dict[str, str] = {}

# Standard cue lines. Dynamic lines (task summaries) pass announce(text=...).
CUES = {
    "thinking": "Thinking, sir.",
    "thinking_escalate": "Still working on it.",
    "confirm_pending": "Still waiting on your confirm, sir.",
}

_PRIORITY = {
    "thinking": "low",
    "thinking_escalate": "low",
    "progress": "normal",
    "milestone": "normal",
    "confirm_pending": "normal",
    "job_done": "high",
    "job_error": "high",
    "job_timeout": "high",
}

class Policy:
    """Per-connection policy state (dedup/cooldown). The synthesizer and the
    audio cache are shared module-wide so cues are synthesized once globally,
    but each connected client decides (and hears) its own cues."""

    def __init__(self, mode: str | None = None):
        # mode=None -> follow the live global mode (set_mode() affects this
        # policy immediately); an explicit mode pins the instance (tests).
        self._pinned = (mode or "").strip().lower() or None
        self._last_seen: dict[str, float] = {}
        self._last_emit = 0.0
        self._synth = None
        self._lock = threading.Lock()

    @property
    def mode(self) -> str:
        return self._pinned or _MODE_HOLDER["value"]

    @mode.setter
    def mode(self, v: str) -> None:
        self._pinned = (v or "").strip().lower() or None

    def set_synthesizer(self, fn) -> None:
        self._synth = fn

    def warm_cache(self) -> None:
        """Pre-synthesize every static cue into the shared cache."""

        async def _warm():
            for text in list(CUES.values()):
                if text and text not in _cache:
                    try:
                        _cache[text] = await self._synth(text)
                    except Exception:
                        pass

        try:
            loop = asyncio.get_running_loop()
            loop.create_task(_warm())
        except RuntimeError:
            asyncio.run(_warm())

    def announce(self, kind: str, text: str | None = None) -> str | None:
        """Decide whether a state change should be spoken. Returns line or None.

        None is NOT an error: the caller should still update the HUD silently.
        """
        if self.mode == "off":
            return None
        line = (text or CUES.get(kind) or "").strip()
        if not line:
            return None
        priority = _PRIORITY.get(kind, "normal")
        now = time.monotonic()
        key = " ".join(line.lower().split())[:120]
        with self._lock:
            if now - self._last_seen.get(key, -1e9) < DEDUP_WINDOW_S:
                return None
            if priority != "high" and now - self._last_emit < COOLDOWN_S:
                return None
            self._last_seen[key] = now
            self._last_emit = now
            return line

    async def cue_audio(self, line: str) -> str | None:
        """Base64 audio for a cue line: shared cache first, live synth on miss."""
        audio = _cache.get(line)
        if audio is not None:
            return audio
        if self._synth is None:
            return None
        try:
            audio = await self._synth(line)
            _cache.setdefault(line, audio)
            return audio
        except Exception:
            return None


# Module-level default instance (used by tests and as the shared synthesizer
# registration point); live WS connections create their own Policy.
_default = Policy()


def set_synthesizer(fn) -> None:
    _default.set_synthesizer(fn)


def warm_cache() -> None:
    _default.warm_cache()


def announce(kind: str, text: str | None = None) -> str | None:
    return _default.announce(kind, text)


async def cue_audio(line: str) -> str | None:
    return await _default.cue_audio(line)


_STEP_WORDS = {1: "one", 2: "two", 3: "three", 4: "four", 5: "five",
               6: "six", 7: "seven", 8: "eight", 9: "nine", 10: "ten"}
_STEP_RE = re.compile(r"\[STEP\s+(\d+)\]\s*([✓✗✔❌])")


def step_line(note: str) -> str | None:
    """Map an autonomous step note ('[STEP 1] ✓ …') to a friendly line, or None."""
    m = _STEP_RE.search(note or "")
    if not m:
        return None
    n = int(m.group(1))
    word = _STEP_WORDS.get(n, str(n))
    if m.group(2) == "✓":
        return f"Step {word} done." if n > 1 else "Step one done, getting started."
    return f"Step {word} didn't verify, sir."
