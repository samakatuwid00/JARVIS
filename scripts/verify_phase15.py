"""verify_phase15.py — Phase 15 gate: autonomous job runner.

Re-runnable. Covers (stub Popen, no real Hermes spawn):
  1. start_goal creates job + touches status file
  2. contract text contains STEP protocol + evidence mandate + RESULT rule
  3. supervisor tails status lines into jobs notes (live thread, stub proc)
  4. RESULT:done WITH steps -> state done; WITHOUT steps -> unverified
  5. RESULT:failed -> state error; missing RESULT -> unverified
  6. cancel kills the tracked process
  7. destructive goal gated through tools.run_autonomous (NEEDS_CONFIRM)
  8. py_compile clean on touched files

LIVE end-to-end gate is separate (real Hermes round-trip) and must be run
before marking Phase 15 DONE.
"""

import os
import sys
import time
import types
import tempfile
import shutil
import py_compile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"[{'PASS' if cond else 'FAIL'}] {name}"
          + (f"  {detail}" if detail and not cond else ""))


# --- static: compile ---------------------------------------------------------
for f in ("autonomous.py", "tools.py", "brain_gemini.py"):
    try:
        py_compile.compile(f, doraise=True)
        check(f"py_compile {f}", True)
    except py_compile.PyCompileError as e:
        check(f"py_compile {f}", False, str(e)[:120])

import autonomous
import jobs as jobreg

# --- contract text -----------------------------------------------------------
c = autonomous._contract("test goal", "C:/x/y.status")
check("contract has STEP protocol", "[STEP" in c and "status file" in c)
check("contract mandates evidence", "evidence" in c.lower()
      and "never" in c.lower())
check("contract has RESULT rule", "RESULT: done" in c)
check("contract embeds goal", "test goal" in c)

# --- start_goal creates job + file ------------------------------------------
jid = None
try:
    ack, jid = autonomous.start_goal("__gate_never_runs__", timeout=9999)
    ok_create = jid and os.path.exists(autonomous.status_path(jid))
    check("start_goal creates job + status file", bool(ok_create), f"jid={jid}")
    check("ack is background sentinel", "AUTONOMOUS_BACKGROUND" in ack)
    # kill immediately - we don't want a real hermes spawn for this test
    autonomous.cancel(jid)
except Exception as e:
    check("start_goal creates job + status file", False, repr(e))

# --- supervisor behaviour against a STUB process -----------------------------
class FakeProc:
    """Mimics Popen: alive until killed; communicate() returns canned reply."""
    def __init__(self, spath, reply, hold=6.0):
        self.spath, self.reply, self.hold = spath, reply, hold
        self.killed = False
        self.returncode = None
    def poll(self):
        return None if not self.killed else 0
    def kill(self):
        self.killed = True
    def communicate(self):
        return self.reply, ""

results = {}


def run_supervise(spath, reply, expect_state, label, steps_expected=None,
                  fail_note=None):
    global results
    # fresh job record without spawning a real process
    j = jobreg.create(f"gate:{label}", tier="autonomous-gate")
    proc = FakeProc(spath, reply)
    # register exactly like start_goal does so _supervise finds status_path
    with autonomous._LOCK:
        autonomous._RUNNING[j] = {"proc": proc, "status_path": spath}
    t0 = time.time()

    # write step lines progressively so the tail loop sees them appear
    import threading
    def writer():
        time.sleep(1.0)
        if steps_expected:
            with open(spath, "a", encoding="utf-8") as f:
                for s in steps_expected:
                    f.write(s + "\n")
        if fail_note:
            time.sleep(0.5)
            with open(spath, "a", encoding="utf-8") as f:
                f.write(fail_note + "\n")
    threading.Thread(target=writer, daemon=True).start()

    # shrink poll interval for speed
    old_poll = autonomous.POLL_INTERVAL
    autonomous.POLL_INTERVAL = 0.3
    # patch deadline via tiny timeout override: monkey-patch time.time? simpler:
    # call _supervise in-thread with generous timeout but proc dies after 'hold'
    def die_later():
        time.sleep(proc.hold)
        proc.kill()
    threading.Thread(target=die_later, daemon=True).start()

    autonomous._supervise(j, "gate goal", proc, timeout=60)
    autonomous.POLL_INTERVAL = old_poll

    rec = jobreg.get(j)
    results[label] = rec
    check(f"supervisor -> {label}: state={expect_state}",
          rec and rec.get("state") == expect_state,
          f"got {rec and rec.get('state')}")
    return j


# case A: honest done with verified steps (use REAL temp files so the
# independent verifier actually confirms them)
_tdirA = tempfile.mkdtemp(prefix="jarvis-A-")
_p1 = os.path.join(_tdirA, "probe_ok.txt")
_p2 = os.path.join(_tdirA, "out.txt")
open(_p1, "w").write("ok")
open(_p2, "w").write("verified")
spathA = os.path.join(autonomous._JOBS_DIR, "gateA.status")
open(spathA, "w").close()
run_supervise(
    spathA,
    "worked\nRESULT: done\n",
    "done", "done-with-steps",
    steps_expected=[f"[STEP 1] done - created probe | {_p1}",
                    f"[STEP 2] done - verified output | {_p2}"])
recA = results["done-with-steps"]
check("done-with-steps: summary counts steps",
      recA and "2 step(s) verified" in (recA.get("summary") or ""),
      recA and recA.get("summary"))
shutil.rmtree(_tdirA, ignore_errors=True)

# case B: claimed done but NO steps -> unverified
spathB = os.path.join(autonomous._JOBS_DIR, "gateB.status")
open(spathB, "w").close()
run_supervise(spathB, "trust me\nRESULT: done\n",
              "unverified", "unverified-no-steps")

# case C: reported failure -> error
spathC = os.path.join(autonomous._JOBS_DIR, "gateC.status")
open(spathC, "w").close()
run_supervise(spathC, "RESULT: failed - disk full\n",
              "error", "reported-failure",
              steps_expected=["[STEP 1] started - clearing space"])

# case D: no RESULT line at all -> unverified
spathD = os.path.join(autonomous._JOBS_DIR, "gateD.status")
open(spathD, "w").close()
run_supervise(spathD, "half-finished work...\n",
              "unverified", "missing-result",
              steps_expected=["[STEP 1] done - something | ev"])

# case E: step notes landed in job progress (live tail proof)
recE = results["missing-result"]
notes = " | ".join(recE.get("progress") or [])
check("supervisor tails STEP lines into progress notes",
      "[STEP 1]" in notes, notes[:120])

# --- Phase 17 slice: independent verifier ----------------------------------
import autonomous as _au

# a claimed step whose path does NOT exist on disk must be UNVERIFIED
_nonexistent = _au._verify_step(
    "[STEP 1] done - Created C:/Users/deped/Documents/does-not-exist-xyz/hello.txt "
    "| folder exists per ls")
check("verifier rejects non-existent artifact",
      _nonexistent[0] is False and "✗" in _nonexistent[1])

# reasoning-only step (no path) passes
_reason = _au._verify_step("[STEP 1] done - analysed the request | reasoned about scope")
check("verifier accepts reasoning-only step", _reason[0] is True)

# content check: make a real temp file and verify
import tempfile as _tf
_tfdir = _tf.mkdtemp(prefix="jarvis-v17-")
_tfpath = os.path.join(_tfdir, "probe.txt")
open(_tfpath, "w").write("phase 15 works")
_real = _au._verify_step(
    f"[STEP 1] done - Created {_tfpath} | file says 'phase 15 works'")
check("verifier confirms real file + content",
      _real[0] is True and "✓" in _real[1])
import shutil as _sh
_sh.rmtree(_tfdir, ignore_errors=True)

# supervisor: claimed-done but artifact missing -> NOT done (unverified)
spathF = os.path.join(autonomous._JOBS_DIR, "gateF.status")
open(spathF, "w").close()
run_supervise(
    spathF,
    f"RESULT: done\n",
    "unverified", "claimed-done-missing-artifact",
    steps_expected=["[STEP 1] done - Created C:/nope/missing.txt | ls shows it"])

recF = results["claimed-done-missing-artifact"]
check("unverified because JARVIS could not verify",
      "could not verify" in (recF.get("summary") or ""),
      recF.get("summary"))
check("unverified note flags CLAIM UNVERIFIED",
      any("CLAIM UNVERIFIED" in (n or "") for n in (recF.get("progress") or [])))

# MSYS path normalization: /c/Users/... must resolve on Windows
_msys = _au._verify_step(
    "[STEP 1] done - Created /c/Users/deped/Documents/jarvis-phase15-test | folder exists")
check("verifier normalizes /c/... MSYS path",
      _msys[0] is True and "C:\\" in _msys[1])

# --- cancel -----------------------------------------------------------------
class KillableProc(FakeProc):
    pass

j = jobreg.create("gate:cancel", tier="autonomous-gate")
kp = KillableProc("", "")
with autonomous._LOCK:
    autonomous._RUNNING[j] = {"proc": kp, "status_path": ""}
check("cancel returns True when running", autonomous.cancel(j) is True)
check("cancel killed the process", kp.killed)
recJ = jobreg.get(j)
check("cancel marks job cancelled", recJ and recJ.get("state") == "cancelled")

# --- destructive gate through the tool surface -------------------------------
import importlib
tl = importlib.import_module("tools")
r1 = tl.run_autonomous("delete all my files in Documents")
check("destructive goal NEEDS_CONFIRM", "[NEEDS_CONFIRM]" in r1, r1[:80])
# latch consumed only by exact re-issue + confirm path; clear to not leak state
if tl._PENDING_DESTRUCTIVE.get("autonomous"):
    tl._PENDING_DESTRUCTIVE.pop("autonomous", None)

# empty goal
check("empty goal rejected", tl.run_autonomous("").startswith("[Error]"))

print(f"\n=== RESULT: {len(PASS)} passed, {len(FAIL)} failed ===")
sys.exit(1 if FAIL else 0)
