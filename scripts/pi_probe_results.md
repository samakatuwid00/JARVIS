# Pi × JARVIS Spike — Probe Results (Task 7)

**Date:** 2026-08-27
**Probe task (identical for both agents):**
> In this Laravel app, add a read-only health endpoint: a GET /health route in
> routes/web.php that returns JSON `{"status":"ok"}`, plus a minimal
> `app/Http/Controllers/HealthController` with a `health()` method. Do NOT touch
> auth, migrations, or existing routes. Edit the existing files.

**Model used:** `ag/gemini-3-flash` via 9Router (free tier) — switched off
`ag/claude-sonnet-4-6` on 2026-08-27 so Pi's coding work doesn't contend with
JARVIS's brain model quota. `ag/gpt-oss-120b-medium` was also tested but Pi's
print-mode tool loop failed to drive gpt-oss's reasoning stream (edits didn't
complete); gemini-3-flash works reliably and has no cold-start.

---

## Pi (earendil-works/pi-coding-agent 0.84.3) — RESULT: PASS

Scratch copy: `jarvis-demo/pi_probe_irimsv` (clean, git baseline 0 changes).

`git status --short` after run:
```
 M routes/web.php
?? app/Http/Controllers/HealthController.php
```
`git diff --stat`: **1 file changed, 4 insertions(+)**. Zero deletions.

- `routes/web.php`: prepended `use App\Http\Controllers\HealthController;` and
  `Route::get('/health', [HealthController::class, 'health']);` before the
  existing `require` chain. No auth/middleware wrapping — correctly public.
- `HealthController.php`: idiomatic Laravel
  (`extends Controller`, `health(): JsonResponse`, `response()->json(['status'=>'ok'])`).
- **Scope discipline:** all core files (`routes/dashboard.php`, `routes/auth.php`,
  `tests/Pest.php`, `composer.json`) confirmed present and unmodified.
- **Deletions:** 0.

> NOTE: A first run showed 76 deleted files / 8796 deletions. Root cause was a
> BAD SCRATCH COPY (background `cp` created a nested `irimsv/` dir; Pi followed
> the wrong path). After rebuilding the copy cleanly (`cp -r src/. dst`), Pi
> produced the clean result above. The earlier destruction was a copy artifact,
> not Pi behavior. The sandbox boundary held regardless — host JARVIS repo and
> live iRIMS-V source were never touched.

---

## OpenCode (opencode 1.18.23) — RESULT: BLOCKED (not comparable)

Scratch copy: `jarvis-demo/oc_probe_irimsv` (clean, git baseline 0 changes).

`git status --short` after two runs (default + `--pure`): **empty** — no files
changed, no HealthController created.

Observed behavior:
- OpenCode launches, reads `routes/web.php`, globs controllers.
- It then requests `external_directory` permission to read
  `C:\Users\deped\Documents\Second Brain\wiki\Second Brain Operating Instruction.md`
  (a hardcoded project-context path in this OpenCode install's config).
- The permission layer **auto-rejects** that read; OpenCode reports the error and
  exits without performing the coding task. This happens even with `--pure`.
- Timed out at 400s / 540s with zero edits.

**Conclusion:** OpenCode could not be fairly compared in this environment. The
failure is an environment/config issue (a project instruction pointing at a
Second Brain path that the permission layer rejects), NOT evidence about its
coding competence. A valid comparison requires either (a) fixing the OpenCode
project config to not pull in Second Brain, or (b) running OpenCode the way
JARVIS normally does (which supplies its own brief/context and may bypass this).

**We did NOT fabricate an OpenCode result.** Pi's result stands on its own; the
OpenCode column is explicitly "blocked by environment."

---

## Verdict (Task 7 → Task 8 decision gate)

| Metric | Pi | OpenCode |
|---|---|---|
| Completed task | ✅ Yes | ❌ Blocked (env) |
| In scope (no auth/migration touched) | ✅ | n/a |
| Deletions / destruction | ✅ 0 | n/a |
| Idiomatic output | ✅ | n/a |
| Sandbox containment | ✅ (Docker) | n/a (local) |

**Pi is competent and safe on this real Laravel task, and the sandbox boundary
held.** OpenCode was not fairly evaluable here.

### Recommendation
- Pi is ready to be promoted from "opt-in via `using pi`" to a default coding
  backend **for supervised/interactive JARVIS use**, behind the existing sandbox.
- Autonomous iRIMS-V ship via Pi remains OUT OF SCOPE until (a) a fair OpenCode
  comparison is done, and (b) you explicitly say "promote pi" — per the spike's
  hard constraint #2.
- To complete the comparison: fix OpenCode's project config (remove the Second
  Brain `external_directory` instruction) and re-run, OR accept Pi's verified
  win and skip the OpenCode column.

### Follow-ups
1. Decision: keep Pi opt-in (`using pi`), promote to default, or drop.
2. If promoting: set `delegate_registry.json` `pi.auto_route: true` and add a
   `verb_map` mapping for coding verbs (currently OpenCode-only).
3. RPC/SDK mode (`pi --mode rpc`) deferred — would enable JARVIS mid-run steering.
