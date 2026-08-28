#!/usr/bin/env python3
"""Unit tests for voice_feedback policy engine (dedup / cooldown / modes)."""
import os
import sys
import time

# Env must be set before importing the module under test.
os.environ["JARVIS_VOICE_FEEDBACK"] = "essential"
os.environ["JARVIS_VOICE_CUE_COOLDOWN_S"] = "8"
os.environ["JARVIS_VOICE_THINKING_CUE_S"] = "4"

import voice_feedback as vf  # noqa: E402

P = vf._default

fails = []


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        fails.append(name)


def fresh():
    P._last_seen.clear()
    P._last_emit = 0.0


# 1. standard thinking cue passes when quiet
fresh()
check("thinking cue speaks", P.announce("thinking") == "Thinking, sir.")

# 2. dedup: same line within window dropped
fresh()
P.announce("thinking")
check("dedup drops repeat", P.announce("thinking") is None)

# 3. cooldown: different line right after a spoken cue is dropped (non-high)
fresh()
P.announce("thinking")
check("cooldown drops quick progress", P.announce("progress", "Delegating to Hermes…") is None)

# 4. high priority bypasses cooldown
fresh()
P.announce("thinking")
line = P.announce("job_done", "Finished, sir — create folder: done")
check("high priority passes cooldown", line is not None and "Finished" in line)

# 5. high still deduped
fresh()
P.announce("job_done", "Finished, sir — X")
check("high still deduped", P.announce("job_done", "Finished, sir — X") is None)

# 6. after cooldown window, progress passes
fresh()
P.announce("progress", "Delegating to Hermes…")
P._last_emit = time.monotonic() - vf.COOLDOWN_S - 1
check("progress passes after cooldown", P.announce("progress", "Hermes finished.") is not None)

# 7. off mode silences everything
fresh()
P.mode = "off"
try:
    check("off mode silent", P.announce("job_done", "Finished, sir — X") is None)
finally:
    P.mode = "essential"

# 8. unknown kind with no text -> None (no crash)
fresh()
check("unknown kind safe", P.announce("nonexistent_kind") is None)

# 9. cue_audio cache hit without synthesizer
fresh()
vf._cache["Thinking, sir."] = "QUVUSUNfQVVESU8="
try:
    import asyncio
    audio = asyncio.run(P.cue_audio("Thinking, sir."))
    check("cache hit returns audio", audio == "QUVUSUNfQVVESU8=")
finally:
    pass

# 10. per-connection isolation: two policies don't dedupe each other
fresh()
p_a, p_b = vf.Policy(), vf.Policy()
p_a.announce("thinking")
check("policies isolated (b not deduped by a)", p_b.announce("thinking") == "Thinking, sir.")

# 11. step_line mapping
check("step_line ✓ first", vf.step_line("[STEP 1] ✓ created folder") == "Step one done, getting started.")
check("step_line ✓ nth", vf.step_line("[STEP 3] ✓ verified") == "Step three done.")
check("step_line ✗", vf.step_line("[STEP 2] ✗ CLAIM UNVERIFIED — missing") == "Step two didn't verify, sir.")
check("step_line none", vf.step_line("no step here") is None)

print("\n" + ("ALL PASS" if not fails else f"{len(fails)} FAILURES: {fails}"))
sys.exit(1 if fails else 0)
