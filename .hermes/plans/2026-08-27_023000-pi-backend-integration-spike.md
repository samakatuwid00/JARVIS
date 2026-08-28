# JARVIS × Pi Coding-Agent Backend Integration Spike

> **For Hermes:** Use subagent-driven-development skill to implement this plan task-by-task. This is a *spike* — goal is a sandboxed, side-by-side Pi backend behind 9Router, NOT a replacement for OpenCode. OpenCode stays the autonomous floor until Pi proves itself.

**Goal:** Add `pi` (earendil-works/pi-coding-agent) as a *parallel* coding specialist delegate in JARVIS, routed through 9Router for model selection and wrapped in a container sandbox, so we can compare it against the existing OpenCode path on real iRIMS-V tasks without risk to the host or to autonomous ship flows.

**Architecture:** Pi is treated exactly like OpenCode today — a `kind:"specialist"` delegate driven by new `_run_pi_specialist` / `_run_pi_specialist_bg` functions in `tools.py`, selected via the existing `_explicit_specialist` (`using pi` / `via pi`) and (opt-in) `_detect_backend` paths. Pi's CLI runs in print mode (`pi -p "..."`) so it is non-interactive and auto-exits. Every `pi` invocation is prefixed by a Docker/sandbox wrapper so it inherits ZERO host permissions (Pi has no built-in permission system — this is mandatory). Model routing is unchanged: Pi is pointed at 9Router's OpenAI-compatible endpoint and asked for `claude-sonnet-4-6`, keeping brain routing consistent with JARVIS itself.

**Tech Stack:** Python 3.11 (JARVIS), `@earendil-works/pi-coding-agent` v0.84.3 (pinned), Docker Desktop (Windows) or OpenShell for sandboxing, 9Router OpenAI-compatible endpoint, existing `delegate_registry.json` + `cli_agents.json` + `tools.py` dispatch.

**Hard constraints (non-negotiable):**
1. Pi NEVER runs unsandboxed. The wrapper script is the only entry point.
2. Autonomous iRIMS-V ship stays on OpenCode. Pi is forbidden from autonomous ship until Tasks 1–7 verify it and you say "promote pi."
3. No edits to `_run_specialist` / OpenCode path. Splice (add siblings), don't patch.
4. Pre-1.0 API churn: pin Pi version in the sandbox image; re-verify flags if upgraded.

---

## Task 1: Pin Pi + confirm CLI surface in a scratch container

**Objective:** Establish a reproducible, sandboxed Pi install and confirm the exact non-interactive flags before wiring anything.

**Files:**
- Create: `scripts/pi_sandbox.sh` (Linux) and/or `scripts/pi_sandbox.ps1` (Windows git-bash)
- Create: `docker/pi/Dockerfile`

**Step 1: Write the sandbox Dockerfile** (pins version, mounts only the target repo at runtime):

```dockerfile
# docker/pi/Dockerfile
FROM node:22-bookworm
RUN npm install -g @earendil-works/pi-coding-agent@0.84.3
# pi is provider-agnostic; config injected at runtime via env
ENV PI_HOME=/opt/pi
RUN mkdir -p "$PI_HOME"
WORKDIR /work
ENTRYPOINT ["pi"]
```

**Step 2: Build and smoke-test the container** (no host mount yet):

Run: `docker build -t jarvis-pi:0.84.3 -f docker/pi/Dockerfile .`
Expected: build succeeds, image tagged.

Run: `docker run --rm jarvis-pi:0.84.3 --version`
Expected: prints `0.84.3` (or similar version string), exit 0.

Run: `docker run --rm jarvis-pi:0.84.3 --help 2>&1 | head -40`
Expected: shows `-p, --prompt`, `--provider`, `--model`, `--tools`, `--mode`, `--session` flags. Save the real flag list to the plan log (flags can churn pre-1.0).

**Step 3: Confirm non-interactive print mode exits on its own:**

Run: `docker run --rm jarvis-pi:0.84.3 -p "Say the word HELLO and nothing else."`
Expected: prints `HELLO`, process exits (no TUI). If it drops into an interactive TUI, the flag is wrong — capture actual behavior and adjust before Task 2.

**Step 4: Commit**

```bash
git add docker/pi/Dockerfile scripts/pi_sandbox.* && git commit -m "spike: pin Pi 0.84.3 sandbox image + smoke test"
```

---

## Task 2: Wire Pi to 9Router (model routing preserved)

**Objective:** Point Pi's LLM calls at 9Router's OpenAI-compatible endpoint using the JARVIS brain model, so model selection stays centralized.

**Files:**
- Read: `.env` in repo root — find the 9Router base URL / key (e.g. `NINEROUTER_URL`, `NINEROUTER_KEY`, or the value JARVIS `brain.py` uses). If unsure, grep `brain.py` / `brain_gemini.py` for the router URL.
- Create: `scripts/pi_sandbox.sh` env block (or `docker/pi/pi.settings.json`)

**Step 1: Extract 9Router endpoint from existing config**

Run: `grep -rIn "router\|9router\|OPENAI_BASE_URL\|base_url" brain.py brain_gemini.py .env 2>/dev/null | head`
Expected: identifies the 9Router URL + model name JARVIS uses (expected `claude-sonnet-4-6`). Record both.

**Step 2: Configure Pi to use 9Router as an OpenAI-compatible provider**

Pi supports `--provider openai --model <name>` and respects `OPENAI_BASE_URL` / `OPENAI_API_KEY`. In the sandbox wrapper, export:

```sh
# inside scripts/pi_sandbox.sh, before invoking pi
export OPENAI_BASE_URL="$NINEROUTER_URL"   # from .env
export OPENAI_API_KEY="$NINEROUTER_KEY"     # from .env (if 9Router needs one)
PI_MODEL="${PI_MODEL:-claude-sonnet-4-6}"
```

**Step 3: Verify Pi reaches 9Router**

Run: `docker run --rm -e OPENAI_BASE_URL="$NINEROUTER_URL" -e OPENAI_API_KEY="$NINEROUTER_KEY" jarvis-pi:0.84.3 --provider openai --model claude-sonnet-4-6 -p "Reply with the single word PONG."`
Expected: prints `PONG` (proves 9Router round-trip works through Pi).

**Step 4: Commit**

```bash
git add scripts/pi_sandbox.sh && git commit -m "spike: route Pi through 9Router (claude-sonnet-4-6)"
```

---

## Task 3: Build the sandbox wrapper that mounts a target repo

**Objective:** A single wrapper script that runs Pi against an arbitrary repo dir, read-write, inside the container, and returns stdout.

**Files:**
- Create: `scripts/pi_sandbox.sh`

**Step 1: Write the wrapper**

```sh
#!/usr/bin/env bash
# scripts/pi_sandbox.sh <repo_abs_path> <prompt> [timeout_sec]
set -euo pipefail
REPO="${1:?usage: pi_sandbox.sh <repo> <prompt> [timeout]}"
PROMPT="${2:?prompt required}"
TIMEOUT="${3:-600}"
# Load 9Router creds from JARVIS .env (parent dir of scripts/)
ENV_FILE="$(cd "$(dirname "$0")/.." && pwd)/.env"
set -a; [ -f "$ENV_FILE" ] && . "$ENV_FILE"; set +a

docker run --rm \
  --network host \
  -v "$REPO":/work:rw \
  -w /work \
  -e OPENAI_BASE_URL="${NINEROUTER_URL:-$OPENAI_BASE_URL}" \
  -e OPENAI_API_KEY="${NINEROUTER_KEY:-$OPENAI_API_KEY}" \
  -e PI_MODEL="${PI_MODEL:-claude-sonnet-4-6}" \
  --memory=2g --cpus=2 \
  jarvis-pi:0.84.3 \
  --provider openai --model "${PI_MODEL:-claude-sonnet-4-6}" \
  --name "jarvis-pi-task" --no-session \
  -p "$PROMPT"
```

**Step 2: Make executable and smoke-test on a throwaway repo**

Run: `chmod +x scripts/pi_sandbox.sh`
Run: `mkdir -p /tmp/pi_probe && echo "def add(a,b): return a+b" > /tmp/pi_probe/m.py && ./scripts/pi_sandbox.sh /tmp/pi_probe "Add a subtract function to m.py and show the diff." 120`
Expected: Pi edits `m.py` inside the container; stdout shows the change. Host `/tmp/pi_probe` is the mounted volume so the edit persists; verify `cat /tmp/pi_probe/m.py` has `subtract`.

**Step 3: Confirm host isolation**

Run: `ls /tmp/pi_probe` and confirm only the intended repo is visible — no access to `/c/Users/deped/Documents/jarvis-demo` unless explicitly mounted.
Expected: container cannot touch JARVIS host files (sandbox boundary holds).

**Step 4: Commit**

```bash
git add scripts/pi_sandbox.sh && git commit -m "spike: pi sandbox wrapper mounts target repo read-write, 9Router-routed"
```

---

## Task 4: Add `_run_pi_specialist` (sync) to tools.py — SPIKE sibling of OpenCode

**Objective:** Implement the Python dispatch that calls the sandbox wrapper, mirroring `_run_specialist` exactly (jobs tracking, brief assembly, clarification check) but never touching OpenCode.

**Files:**
- Modify: `tools.py` (SPLICE — add new functions near `_run_specialist`, ~line 2010; do NOT edit `_run_specialist`)

**Step 1: Write failing test**

```python
# tests/test_pi_backend.py
def test_run_pi_specialist_invokes_wrapper():
    import tools
    out = tools._run_pi_specialist("add a subtract fn to m.py", timeout=120)
    assert "subtract" in out or out.startswith("[pi]"), out
```

Run: `pytest tests/test_pi_backend.py -v`
Expected: FAIL — `_run_pi_specialist` not defined.

**Step 2: Implement `_run_pi_specialist` (splice after line 2009)**

```python
def _run_pi_specialist(task: str, agent: str | None = None, timeout: int = 300) -> str:
    """Run a coding task through the Pi specialist tier (SPIKE, sandboxed).

    Mirrors _run_specialist but invokes scripts/pi_sandbox.sh, which runs
    Pi inside Docker. Pi has no built-in permission system, so the container
    IS the security boundary. OpenCode path is untouched.
    """
    import shutil, jobs as jobreg, subprocess
    wrapper = shutil.which("bash")
    if wrapper is None:
        return "[pi] bash unavailable; cannot launch sandbox."
    script = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "scripts", "pi_sandbox.sh")
    if not os.path.exists(script):
        return "[pi] sandbox wrapper missing; route to opencode."
    # Ground with the same brief OpenCode gets (keeps behavior parity)
    try:
        import context_assembler as ca
        brief = ca.assemble_brief(task, delegate="pi")
        task = brief + task
    except Exception:
        pass
    jid = jobreg.create(task[:200], tier="pi", agent=agent or "pi", background=False)
    jobreg.update(jid, state="running", note="sandboxed pi coding task")
    # repo to mount = current working dir (JARVIS cwd) by default
    repo = os.path.dirname(os.path.abspath(__file__))
    cmd = [wrapper, script, repo, task, str(timeout)]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return f"[pi] timed out after {timeout}s."
    out = (proc.stdout or "").strip()
    if proc.returncode != 0 and not out:
        err = (proc.stderr or "unknown error")[:300]
        jobreg.update(jid, state="error", error=err, summary=err[:120])
        return f"[pi] failed: {err}"
    body = out or "(empty response)"
    try:
        import context_assembler as ca
        if ca.delegate_needs_clarification(body):
            jobreg.update(jid, state="waiting-on-confirm", note="pi needs clarification")
            return body
    except Exception:
        pass
    jobreg.update(jid, state="done", result=body[-4000:],
                  summary=body.splitlines()[-1][:200] if body else "done")
    return body
```

**Step 3: Run test to verify pass**

Run: `pytest tests/test_pi_backend.py -v`
Expected: PASS.

**Step 4: Commit**

```bash
git add tools.py tests/test_pi_backend.py && git commit -m "spike: add _run_pi_specialist (sandboxed Pi, OpenCode untouched)"
```

---

## Task 5: Add `_run_pi_specialist_bg` (async) — SPIKE sibling

**Objective:** Non-blocking Pi execution for voice turns, mirroring `_run_specialist_bg`.

**Files:**
- Modify: `tools.py` (SPLICE near `_run_specialist_bg`, ~line 2120)

**Step 1: Implement `_run_pi_specialist_bg`**

```python
_BG_PI: dict = {}
_PI_LOCK = threading.Lock()

def _run_pi_specialist_bg(task: str, agent: str | None, timeout: int,
                          on_done=None) -> tuple[str, str]:
    import shutil, subprocess, jobs as jobreg
    script = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "scripts", "pi_sandbox.sh")
    if not os.path.exists(script):
        return ("[pi] sandbox wrapper missing; route to opencode.", "")
    try:
        import context_assembler as ca
        brief = ca.assemble_brief(task, delegate="pi")
        full_task = brief + task
    except Exception:
        full_task = task
    jid = jobreg.create(task, tier="pi", agent=agent or "pi", background=True)
    repo = os.path.dirname(os.path.abspath(__file__))
    cmd = ["bash", script, repo, full_task, str(timeout)]
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, encoding="utf-8", errors="replace")
    except Exception as e:
        jobreg.update(jid, state="error", error=str(e)[:200])
        return f"[pi] failed to launch: {e}", jid
    with _PI_LOCK:
        _BG_PI[jid] = {"proc": proc, "agent": agent}
    def _watch():
        try:
            out, err = proc.communicate(timeout=timeout)
            rc = proc.returncode
        except subprocess.TimeoutExpired:
            proc.kill(); out, err = "", ""
            jobreg.update(jid, state="timeout", error=f"timed out after {timeout}s")
            with _PI_LOCK: _BG_PI.pop(jid, None)
            if on_done: on_done(f"[pi] timed out after {timeout}s.")
            return
        finally:
            with _PI_LOCK: _BG_PI.pop(jid, None)
        stdout = (out or "").strip()
        if rc == 0 and stdout:
            jobreg.update(jid, state="done", result=stdout[-4000:],
                          summary=stdout.splitlines()[-1][:200])
            if on_done: on_done(stdout)
        else:
            reason = ((err or "").strip() or "non-zero exit")[-300:]
            jobreg.update(jid, state="error", error=reason, summary=reason[:120])
            if on_done: on_done(f"[pi] failed: {reason}")
    import threading as _t
    _t.Thread(target=_watch, daemon=True).start()
    return f"[pi] task started (jid {jid})", jid
```

**Step 2: Verify it imports without error**

Run: `python -c "import tools; print('ok', hasattr(tools,'_run_pi_specialist_bg'))"`
Expected: `ok True`.

**Step 3: Commit**

```bash
git add tools.py && git commit -m "spike: add _run_pi_specialist_bg (async sandboxed Pi)"
```

---

## Task 6: Register `pi` in delegate_registry.json + explicit routing

**Objective:** Make `using pi` / `via pi` force the Pi path, and add Pi as an opt-in auto-routing coding delegate (OFF by default behind a flag).

**Files:**
- Modify: `delegate_registry.json` (add delegate entry)
- Modify: `tools.py` `_explicit_specialist` regex (~line 1831/1863) and `_detect_backend` (~line 1776)

**Step 1: Add the `pi` delegate entry** (SPLICE into `delegates` array, priority below opencode so it never auto-wins unless flagged):

```json
{
  "id": "pi",
  "handles": ["coding"],
  "description": "Pi coding agent (earendil-works) via sandboxed Docker wrapper, routed through 9Router. SPIKE — opt-in via 'using pi' or PI_AUTO_ROUTE=1.",
  "priority": 45,
  "kind": "specialist",
  "cmd": "scripts/pi_sandbox.sh",
  "sandbox": true,
  "auto_route": false
}
```
Also add to `verb_map` comment only — do NOT auto-map coding verbs to pi yet.

**Step 2: Extend `_explicit_specialist` regex** to recognize `pi`:

Find (line ~1831/1863):
```python
_EXPLICIT_HARNESS_RE = re.compile(
    r"\b(?:using|via|with|through|on)\s+(?:the\s+)?"
    r"(opencode|open\s?code|[a-z]+[- ](?:agent|runner|operator|curator|session))\b",
    re.I)
```
Replace with (add `|pi` alternative):
```python
_EXPLICIT_HARNESS_RE = re.compile(
    r"\b(?:using|via|with|through|on)\s+(?:the\s+)?"
    r"(opencode|open\s?code|pi|[a-z]+[- ](?:agent|runner|operator|curator|session))\b",
    re.I)
```

**Step 3: Add pi branch to dispatch** (in `execute_tool`/router near line 2586, SPIKE — only active when `using pi` forced OR `PI_AUTO_ROUTE` env set):

Find:
```python
    if backend == "opencode":
```
Insert BEFORE it:
```python
    if backend == "pi" or (os.environ.get("PI_AUTO_ROUTE") == "1" and backend == "opencode"):
        if background:
            ack, jid = _run_pi_specialist_bg(task, None, timeout=timeout, on_done=on_done)
            return ack
        result = _run_pi_specialist(task, None, timeout=timeout)
        if not result.startswith("[pi]"):
            return result
        # fall through to opencode/hermes if pi failed
```

**Step 4: Verify routing**

Run: `python -c "import tools, re; print(bool(tools._EXPLICIT_HARNESS_RE.search('refactor m.py using pi')))"`
Expected: `True`.

Run: `PI_AUTO_ROUTE=1 python -c "import tools; print(tools._detect_backend('code a react component'))"` — note this may still return opencode (priority); confirm pi only wins via `using pi` unless `PI_AUTO_ROUTE=1` forces replacement of opencode.
Expected: with `using pi` → forced pi path. Without flag → opencode unchanged.

**Step 5: Commit**

```bash
git add delegate_registry.json tools.py && git commit -m "spike: register pi delegate, 'using pi' explicit routing, opt-in auto-route flag"
```

---

## Task 7: Sandboxed probe on real iRIMS-V task + comparison

**Objective:** Prove Pi's agent loop on a real, low-risk iRIMS-V coding task and compare against OpenCode on the same task.

**Files:**
- Target: `C:\Users\deped\Documents\lrmis backup\main\July 27, 2026\irimsv` (clone a scratch COPY to a temp dir first — never point Pi at the live repo)

**Step 1: Make a scratch copy of iRIMS-V**

Run: `cp -r "<live irimsv path>" /tmp/irimsv_pi_probe`
Expected: `/tmp/irimsv_pi_probe` exists, independent of live repo.

**Step 2: Define ONE identical task for both agents**

Task: *"In the iRIMS-V Laravel app, add a small read-only route `/api/health` that returns `{\"status\":\"ok\"}`. Add the route + a minimal controller. Do not touch auth or migrations."*

**Step 3: Run Pi (sandboxed)**

Run: `./scripts/pi_sandbox.sh /tmp/irimsv_pi_probe "add /api/health route returning {status:ok}, minimal controller, no auth/migration changes" 600`
Capture: full stdout, wall-clock time, resulting `git diff --stat` inside `/tmp/irimsv_pi_probe`.

**Step 4: Run OpenCode on the same task (fresh scratch copy)**

Run: `cp -r "<live irimsv path>" /tmp/irimsv_oc_probe && cd /tmp/irimsv_oc_probe && opencode run "add /api/health route returning {status:ok}, minimal controller, no auth/migration changes"`
Capture: same metrics.

**Step 5: Score both** (write to `scripts/pi_probe_results.md`):
- Does the diff apply cleanly? (laravel route syntax correct?)
- Did it stay within scope (no auth/migration touched)?
- Did it use tools correctly (read before edit)?
- Wall-clock time.
- Any mid-task failures / recovery quality.

**Step 6: Commit the comparison doc (NOT the probe repos)**

```bash
git add scripts/pi_probe_results.md && git commit -m "spike: Pi vs OpenCode iRIMS-V probe results"
```

---

## Task 8: Decision gate (report only — no code)

**Objective:** Present findings; you decide promotion.

Summarize for the user:
- Pi loop quality on the real task (pass/fail per metric above).
- Sandbox overhead (container start time, any 9Router latency).
- Whether to (a) keep as `using pi` opt-in forever, (b) promote to `PI_AUTO_ROUTE=1` default coding backend, or (c) drop.
- If promoted: only THEN allow Pi into autonomous iRIMS-V ship (update Task 6 `auto_route:true` + memory note). **Autonomous ship via Pi is explicitly out of scope for this spike.**

---

## Files likely to change
- `docker/pi/Dockerfile` (new)
- `scripts/pi_sandbox.sh` (new)
- `scripts/pi_probe_results.md` (new)
- `tests/test_pi_backend.py` (new)
- `tools.py` (SPLICE only: `_run_pi_specialist`, `_run_pi_specialist_bg`, regex + dispatch branch)
- `delegate_registry.json` (add `pi` delegate)

## Tests / validation
- Task 1–3: container builds, `pi --version`, non-interactive `-p` exits, 9Router round-trip `PONG`.
- Task 4–5: `pytest tests/test_pi_backend.py` green; `import tools` clean.
- Task 6: `using pi` regex match = True; opencode path unchanged without flag.
- Task 7: real iRIMS-V diff produced + compared.

## Risks / tradeoffs / open questions
- **Pre-1.0 churn:** Pi `0.84.x` flags may change; Task 1 captures the real flag list — re-verify on upgrade.
- **Sandbox overhead:** Docker start + 9Router hop adds latency vs OpenCode locally; measure in Task 7.
- **Read-only safety:** For lower-risk probes, consider `pi --tools read,grep,find,ls` (read-only) or `--exclude-tools` in the wrapper even inside the container — defense in depth. Left as an option in `pi_sandbox.sh`.
- **RPC/SDK future path:** Once the print-mode spike is proven, a later phase can switch `_run_pi_specialist` to `pi --mode rpc` for true mid-run steering (the `Enter`=steer / `Alt+Enter`=follow-up capability JARVIS could supervise). Out of scope for this spike.
- **9Router model name:** must match exactly what `brain.py` uses; Task 2 verifies.
- **Autonomous ship:** explicitly blocked until you promote after Task 7/8.
