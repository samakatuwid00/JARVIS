# Apply JARVIS HUD v3 Mockup to Live `/` — Implementation Plan

> **For Hermes:** Use subagent-driven-development skill to implement this plan task-by-task.

**Goal:** Make `http://localhost:8080/` render the exact layout/design of the v3 amber mockup (`C:\Users\deped\.claude\skills\ui-mockup\out\jarvis-hud-v3.html`) while preserving the live voice engine (WebSocket, VAD, telemetry, apps modal).

**Architecture:** Port the mockup's DOM/CSS (3-column `280px | 1fr | 320px`, amber plates, pills, conversation stack, confirm gate) into a new shell `jarvis_hud_v3.html` served at `/` by `jarvis_web.py`. The existing `jarvis_visual.html` script block (the full voice pipeline) is reused verbatim; only the body markup + styles are swapped to match the mockup. Live data keeps flowing into the same element IDs; demo-seed `?demo=1` continues to work.

**Tech Stack:** Static HTML + vanilla JS (no build), FastAPI (`jarvis_web.py`) serving the file, existing WebSocket `/ws` backend.

---

## Current Context (verified)

- Serving process: pid 26284 → `C:\Users\deped\Documents\jarvis-demo\jarvis_web.py` (uv python).
- `/` currently serves `jarvis_visual.html` (title "JARVIS HUD — Voice", `#jv-shell` grid `288px 1fr 324px`).
- `jarvis_visual.html` = 2108 lines: v3-like amber shell + full voice JS (VAD, wake words, WS `/ws`, telemetry, apps modal, demo-seed `?demo=1`).
- The mockup `jarvis-hud-v3.html` = 200-line static Tailwind HTML: exact desired look.
- **Gap:** mockup layout differs from live shell in section rows, center conversation stack (caption plate present in mockup, absent live), and right-column LAST TURN labels.

## Proposed Approach (port, not rewrite)

1. **Create `jarvis_hud_v3.html`** — copy `jarvis_visual.html`, then replace its `<style>` + `<body>` markup with the mockup's exact structure:
   - Columns: `grid-template-columns: 280px minmax(0,1fr) 320px` (mockup 280/1fr/320, not live 288/1fr/324).
   - **Top bar:** mockup's 4 pills + ONLINE badge; keep live `id`s (`jv-status`, `jv-dot`, `jv-level`, `jv-pill-*`, `jv-actions`).
   - **Left column:** mockup's 3 sections with mockup labels:
     - SYSTEM STATUS: Brain / STT / Wake / Vault index / Voice in (keep `st-brain`, `st-mic`, `st-wake` ids; add `st-stt`, `st-vault`).
     - VOICE FEEDBACK: OFF/ESSNTL/FULL toggle (keep `.vf-opt`) + 3 cues (`.vf-cue`).
     - NOW PLAYING: Source / Track + STOP MUSIC / RESUME buttons (keep `np-source`, `np-title`, `jv-music-stop`, `jv-music-spotify`).
   - **Center:** add the **caption plate** (`J.A.R.V.I.S // GOOD EVENING, SIR — SHALL I BEGIN?`) above the conversation stack; keep `#jv-feed` with `#jv-gate`, `#jv-side-head`(Transcript), `#jv-side-log`, `#jv-cues`, `#jv-input-wrap`. The iframe orb (`#stage`/`#hud`) stays as background in the center `1fr`.
   - **Right column:** mockup's 3 sections: ACTIVE TASKS (keep `#job-tray`, `#jv-taskcount`), QUICK ACTIONS (`.jv-qa-grid`), LAST TURN — match mockup labels (Backend / Tools / Grounded / Spoken), keep `st-backend`, `st-latency`, `st-ctx`, `st-tool` ids; add `st-grounded`, `st-spoken`.
2. **Preserve ALL voice JS** — the entire `<script>` from `jarvis_visual.html` is copied unchanged; only adjust element-ID references that the new markup renames (add the new row ids `st-stt`, `st-vault`, `st-grounded`, `st-spoken` as inert placeholders the JS may touch).
3. **Point `/` at the new file** — change `jarvis_web.py` route `/` to serve `jarvis_hud_v3.html` instead of `jarvis_visual.html`.
4. **Keep `?demo=1`** — the demo-seed block works unchanged because it references the same ids; add the new placeholder rows to the seed values.

## Files to change

- **Create:** `C:\Users\deped\Documents\jarvis-demo\jarvis_hud_v3.html` (copy of `jarvis_visual.html` + restyled body/CSS to mockup).
- **Modify:** `C:\Users\deped\Documents\jarvis-demo\jarvis_web.py:567-572` (route `/` → `jarvis_hud_v3.html`).
- Backup: `jarvis_visual.html` stays untouched (rollback = revert route).

## Step-by-step

### Task 1: Fork the shell
- Copy `jarvis_visual.html` → `jarvis_hud_v3.html`.
- Verify: file exists, same line count.

### Task 2: Restyle `<style>` + `<body>` to mockup
- Replace the CSS `#jv-shell` grid to `280px 1fr 320px`; add caption-plate class; add mockup row/label styles (`.k`, `.v`, `.lbl`, `.pill` reused from mockup's inline styles or mapped to existing `.jv-st-k/.jv-st-v`).
- Replace `<body>` markup sections to mockup's exact structure, keeping live ids.
- Verify: served HTML contains mockup labels + ids.

### Task 3: Wire new placeholder ids
- Add inert refs: `st-stt`, `st-vault`, `st-grounded`, `st-spoken` (set by JS to `—` or mockup values; demo seed fills them).
- `jarvis_visual.html`'s `logConv`, telemetry, demo block already target existing ids; no JS change needed except adding 4 new id writes in the demo seed.

### Task 4: Point `/` at new file
- Edit `jarvis_web.py` route `/` to read `jarvis_hud_v3.html`.
- Verify: `curl http://localhost:8080/ | grep jarvis_hud_v3` (or title shows v3).

### Task 5: Render + verify (ad-hoc, leave probe on disk)
- Playwright load `/` and `/?demo=1`, assert: title, shell grid, mockup labels present (SYSTEM STATUS / STT / Vault index / caption / GOOD EVENING / STOP MUSIC / ACTIVE TASKS / Grounded), 0 console errors.
- Screenshot to `C:\Users\deped\AppData\Local\Temp\jarvis-v3-live.png`; leave probe `hermes-verify-hud-v3.py` on disk.
- Report as ad-hoc render/DOM verification, not suite green.

## Tests / validation

- No formal test suite (static HTML + vanilla JS; creative-UI hold rule keeps linters off until user approves look).
- Validation = live Playwright render of `/` + `/?demo=1`:
  - `/` shows mockup layout with live data (or `—` placeholders) and working orb iframe.
  - `/?demo=1` shows the full seeded conversation + panels, ONLINE status, 0 console errors.
- Rollback: `git checkout jarvis_web.py` (or revert route to `jarvis_visual.html`).

## Risks / tradeoffs / open questions

- **Risk:** The live voice JS references specific ids (`jv-stats`, `jv-left`, `jv-side-log`, etc.). Renaming body markup to mockup could break a JS binding → mitigate by keeping all existing ids in the new markup, only adding new ones. Position functions (`positionStats`, `positionLeft`, `positionSidePanel`) may become no-ops in the new static layout — guard them (`if (!el) return`) which they already do.
- **Risk:** The mockup is Tailwind-CDN; the live shell is custom CSS. We are NOT pulling Tailwind into the live app — we port the *visual result* with existing custom CSS classes (`.jv-sec`, `.jv-pill`, `.vf-*`), matching colors/layout.
- **Tradeoff:** Two HUD files (`jarvis_visual.html` + `jarvis_hud_v3.html`) coexist; `jarvis_visual.html` remains as the rollback/the old shell. Consider deleting it after the swap is approved.
- **Open question:** Should `/` serve the *static mockup look* (with live data filling rows) — confirmed by this plan — or should the user park the old `jarvis_visual.html` at `/hud-old.html` for A/B? Default: keep as rollback only.

## Execution handoff

Plan complete. Ready to execute (Task 1-5) — I can implement directly (bounded, 1 file fork + 1 route tweak) or via OpenCode on `hy3-free` per the standing rule, then verify on disk myself. Which?