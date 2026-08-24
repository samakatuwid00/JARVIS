# JARVIS Harness Plan

**Status:** Planning complete — build not yet started (phases pending approval).
**Owner:** Roger A. Abay Jr.
**Goal:** Turn JARVIS from a voice shell with 22 hand-rolled local tools into a **thin
voice I/O + router** whose general intelligence is **Hermes (this agent) as the executor
harness**, wrapped in three pillars: a **tiered router**, a **capability manifest**, and a
**persistent memory layer**. Target: an **open-source, portable** assistant that works on
different machines based on the software they have and the preferences they teach it.

---

## 1. Vision

- **JARVIS = the voice, the HUD, and a smart router.** It hears you, speaks back, and
  decides *who* should do the work.
- **Hermes = the harness.** Every general, multi-step, or machine-touching task is handed
  to Hermes, which runs as a separate process with the **full machine toolkit** (terminal,
  file, browser, code, subagents, cron, skills, memory, computer_use).
- **Local tools = reduced to the minimum.** Keep only what is (a) fast/deterministic or
  (b) an authenticated session Hermes cannot replicate. Everything else is delegated.

### What this buys us
- Stop maintaining 22 fragile local tools + a hand-rolled brain with history/reset hacks.
- Every task gets a mature agent loop (subagents, memory, skills, big context).
- Portable: capabilities and preferences become **data**, not code — so the same JARVIS
  runs on any machine.

### What it costs (explicit trade-offs)
- **General tasks are slower** (Hermes cold-start ≈ 2 min 16s measured). Solved by the
  async voice pattern (Phase 4) + warm manifest scan at boot.
- **Bigger blast radius per turn** (every general task = full-Hermes agent). Covered by
  the structural confirm gate (destructive needs explicit approval).
- **No full offline fallback** — if Hermes/9router is down, JARVIS degrades to music +
  ChatGPT-history + deterministic/tools kept.

---

## 2. Architecture

```
 voice (mic → Whisper small.en) ─┐
                                  ├─▶ JARVIS (THIN): I/O + router + HUD
 typed text ─────────────────────┘        │
                                          ▼
                              classify_intent(turn)
        ┌──────────────────────┬──────────┼───────────────┬──────────────────┐
        ▼                      ▼          │                ▼                  ▼
  DETERMINISTIC?          APP-REFERENCE?  │          GENERAL TASK?      AUTH SIDE-ROUTE?
  weather, time,          name/imply a    │          → delegate_to_     music,
  system, open app,       discovered app  │          hermes (full       ChatGPT history
  simple vault-read       → resolve from  │          harness,           (signed-in
  → tiny local tool       manifest →      │          confirm-gated)     Chrome :9223)
  (fast path)             delegate        │                │
        │                      │           │                │
        └──────────────────────┴───────────┴────────────────┘
                                          ▼
                          before answering, Hermes grounds via:
                          • Tier 1 RAG  → Second Brain vault (turbovec)
                          • Tier 2      → jarvis-profile note (injected)
                          • Tier 3      → JARVIS session store (rolling)
                                          ▼
                                   edge-tts → speaker
```

### Backend chains (current, from live `/status`)
- **Brain fallback:** `router (9router cx/gpt-5.4-mini @ :20128) → Cerebras → Groq → Ollama (jarvis-qwen3) → demo`. 9router is frequently down → live turns fall to Ollama.
- **Voice:** Whisper `small.en` (STT), edge-tts (TTS).
- **Harness bridge:** `delegate_to_hermes` spawns `hermes chat -q "<task>" -Q -t <full toolset> --max-turns N --source tool`, timeout 300s.

---

## 3. The Three Pillars

### Pillar A — Tiered Router
JARVIS's callable toolset narrows to:
`{ delegate_to_hermes, play_music, stop_music, *chatgpt_tools, open_application? }`
plus a **fast-path classifier** (like the existing P1 math/time path) so deterministic
intents stay sub-second instead of spawning Hermes.

- **KEEP as JARVIS-local** (fast, or auth-session Hermes lacks):
  1. `play_music` / `stop_music` — Brave+YouTube Playwright session.
  2. `search_chatgpt_history` / `open_chatgpt_conversation` / `ask_chatgpt` — signed-in Chrome on `:9223`.
  3. (Optional) `open_application` / `open_file` — cheap local convenience.
- **MOVE to Hermes** (delete from `tools.py` + `TOOL_DECLARATIONS` + `TOOLS`):
  `get_weather`, `get_system_info`, `search_files`, `list_directory`, `read_file`
  (vault-read overlaps — Hermes reads the markdown directly), `run_shell` (Hermes has
  terminal), `search_vault` / `read_vault_note` (Hermes reads the vault dir), and the
  orchestrators `compose_report` / `launch_project` (re-home or side-route in Phase 3).

### Pillar B — Capability Manifest (Phase 0a)
A `machine_capabilities` **Hermes skill** scans installed software and writes
`capabilities.json`. **Gitignored + local-only** (it leaks installed software — must never
be committed to an OSS repo).

- **Scan sources (per-OS, for portability):**
  - Windows: `where`/PATH, `HKLM\Software\Microsoft\Windows\CurrentVersion\Uninstall\*`, `WOW6432Node`, Start Menu `.lnk` targets, `Program Files`, `AppData\Local\Programs`.
  - Linux: `which`, `.desktop` files, flatpak/snap, `/usr/bin`.
  - macOS: `/Applications`, `brew list`.
- **Manifest entry:** `{ name, bin_path/launch_cmd, version?, category (editor|media|dev|browser|…), detected_capabilities[], confidence }`.
- **"use X" resolution:**
  - *CLI tools* (ffmpeg, git, python, node, docker…): trivial shell invocation.
  - *GUI apps* (Blender, VLC, Photoshop…): driven via `computer_use` (screen + clicks) or launched + automated if it exposes hooks. **Best-effort** — state this in OSS docs.
- **User preferences** (e.g. "prefer VLC for video") become **data** in the profile, layered on the manifest — not code.

### Pillar C — Persistent Memory Layer (Phase 0b)
"JARVIS's memory" *is* Hermes's memory + a shared long-term store = your **Second Brain
(Obsidian vault)**. Three tiers:

| Tier | Backend | Content | Injected when |
|---|---|---|---|
| 1 Knowledge | Second Brain vault (Obsidian) | facts, how-tos, user notes, project context | on-demand RAG **before** answering |
| 2 Profile | `jarvis-profile` note (vault or Hermes MEMORY) | name, role, machine, prefs, manifest summary | every router + Hermes turn |
| 3 Session | JARVIS's own per-session store | this conversation + cross-session recall | every turn (rolling window) |

**Anti-hallucination loop (the mechanism):**
- **Ground-then-answer + cite.** Before any factual/general answer, Hermes does a semantic
  search of the vault (turbovec) and injects top matches as context. If nothing relevant
  exists, it says so instead of inventing. Answers cite the source note name.
- **Write-back = teaching JARVIS.** "Remember I prefer VLC for video" → Hermes writes a
  note (obsidian skill / second-brain-capture). Next time it's retrieved and applied.
  Memory is *earned*, not hardcoded → portable across machines.

---

## 4. Phased Build

> Verification discipline (all phases): each phase gets a fresh **ad-hoc probe** written to
> an OS-safe temp path `C:/Users/deped/AppData/Local/Temp/hermes-verify-<rand>.py`, run via
> the **terminal** tool (not execute_code — its 5-min cap kills the cold-start spawn), then
> **deleted**. No commit until the user says so.

### Phase 0a — Capability manifest (Hermes skill)
- **`machine_capabilities.py`** created: portable scanner (Windows/Linux/macOS) →
  `capabilities.json` (gitignored, local-only). `scan()`, `write_manifest()`, `resolve(name)`.
- **Real scan:** 845 capabilities discovered on this host; `resolve("vlc")` finds the
  real `C:\Program Files\VideoLAN\VLC\vlc.exe`; `resolve("git")` → cli bin. Verified.
- **Destructive-gate narrowed:** removed the over-broad `(write|save|create|update).*(file)`
  clause so a harmless "scan and write a manifest" / "remember this" task is NOT gated.
  Genuine destructives (delete/overwrite/install/git-push/kill/sudo/deploy) still gated.
- **Verify:** 8/8 static (scan writes+gitignored, resolve works, regex narrowing) + 2/2 live
  (WS "scan installed software" → "845 capabilities found, sir."; `capabilities.json` on disk).
- **Status: COMPLETE (ad-hoc verified, uncommitted).**

### Phase 0b — Memory layer (foundation for everything)
- **OQ1 RESOLVED (YES):** the delegated `hermes chat -q` subprocess inherits Hermes `MEMORY.md` (verified: returned `samakatuwid00` from memory). No explicit injection needed.
- **Turbovec semantic search FIXED:** was broken in the subprocess because `python3` resolved to a WindowsApps cpython-3.14 stub (numpy ABI mismatch) instead of the Hermes venv 3.11. Fix: (a) created venv `python3.exe` shim; (b) bridge now prepends venv `Scripts` to `PATH` for the delegated subprocess. Verified: semantic search now returns real vault notes (e.g. `JARVIS — Multi-Provider Brain…`).
- **Tier-2 profile:** `jarvis-profile.md` created (user/machine facts, harness contract, confirm-gate scope). Local to the JARVIS project (NOT the vault — vault is knowledge-only per its AGENTS.md schema). **Gitignored.**
- **Tier-3 session store:** new `session_store.py` (append-only JSONL `sessions/jarvis-YYYY-MM-DD.jsonl` + rolling in-memory window). Wired into both turn handlers in `jarvis_web.py`. **Gitignored. Verified live** (JSONL persisted).
- **Grounding protocol implemented:**
  - Router-side: `JARVIS_SYSTEM` appends the profile as `PERSISTENT MEMORY` at module load → local brain (Ollama/demo) is grounded even without delegating. Verified: "who am I" → "Roger A. Abay Jr., sir. GitHub handle: samakatuwid00." (fixed the previous "Albert" mis-answer from an ungrounded local brain).
  - Delegation-side: new `delegate_to_hermes_grounded()` prepends PROFILE + recent SESSION window + vault-grounding instruction (cite sources, never fabricate) to every general task. Wired into all 4 touchpoints (TOOLS/TOOL_MAP/TOOL_DECLARATIONS/_openai_tools).
- **Live dry-run: 3/4 tiers passed** (T1 vault-grounded+cited, T3 session continuity, T3 session store persisted). T2 initial fail was an ungrounded local-brain name; fixed and re-verified.
- **Verify:** 9/9 static gate (grounded wrapper + 4 touchpoints + profile-in-system + store-writes + profile-present/ignored). Live WS dry-run 3/4 → 4/4 after fix.
- **Status: COMPLETE (ad-hoc verified, uncommitted).**


### Phase 1 — Router-first `think()`
- **`classify_intent`** gained an `app_reference` label (phrases "use/open/run X" + a known
  capability name, or "use X app/tool/software").
- **`think()` P1.5 branch:** `general` + `app_reference` intents now route to
  `delegate_to_hermes_grounded` (the Hermes harness) BEFORE the cloud brain. Simple/voice-fast
  intents (math/time/greeting/thanks/help) keep the instant local path. On any Hermes error it
  falls through to the cloud brain (degrades, doesn't break).
- **Bug found + fixed (grounded-context false-gate):** the destructive regex was matching the
  *injected* profile/grounding text ("delete/install" appear in the safety instructions), so
  harmless general questions got `NEEDS_CONFIRM`. Fix: `delegate_to_hermes` now takes
  `raw_task`; the gate evaluates the USER's original words only, not the prefixed context.
  Verified the fix kills the false positive while keeping real destructives gated.
- **Verify:** 4/4 live — simple 0.0s instant; general "router vs switch" → real answer (38s);
  app_reference "use ffmpeg" → "FFmpeg 8.1.2 (…)" (resolved from manifest, 22s); destructive
  still gated 0.0s.
- **Status: COMPLETE (ad-hoc verified, uncommitted).**

### Phase 2 — Delete replacable hardcoded tools
- **Removed from the tool SURFACE** (TOOLS schema + TOOL_MAP + `TOOL_DECLARATIONS` in both
  `tools.py` and `brain_gemini.py`): `run_shell`, `search_files`, `get_weather`,
  `get_system_info`, `search_vault`, `search_vault_semantic`, `read_vault_note`, `open_file`
  (8 tools). The cloud brain can no longer call them, so these intents now route to Hermes.
- **Underlying functions retained** in `tools.py` (deliberately, for reversibility + so
  `jarvis.py` `print_status()` and `verify.py` keep working). The local Ollama fallback
  still *could* execute them if needed; the cloud brain simply can't request them.
- **`verify_hermes_bridge.py`** updated: its 2 assertions expected the old `[Blocked]`
  wording; now asserts `[NEEDS_CONFIRM]` — suite is **9/9 green**.
- **Verify:** 33/33 static (import-clean; 8 tools absent from all 3 surfaces; functions
  retained; `jarvis.py` import intact; no stray callers). 9/9 live (file-search "what files
  reference delegate" → "6 files reference `delegate`: brain_gemini.py…" via Hermes; live
  surface lacks all 8).
- **Status: COMPLETE (ad-hoc verified + suite green, uncommitted).**

### Phase 3 — Re-home orchestrators
- **Decision (per-tool): KEEP BOTH as JARVIS side-routes** — they are not generic and
  cannot be cleanly delegated to Hermes:
  - `compose_report` runs on JARVIS's *local* `brain_gemini.JarvisBrain().think()` +
    signed-in ChatGPT browser + python-docx. Hermes driving JARVIS's own brain is circular.
  - `launch_project` runs dev-server start + signed-in Brave login + vault creds — JARVIS-side.
- **Regression fixed:** Phase 1 routed `general` to Hermes, which would have swallowed
  "compose a report" / "launch project" and orphaned these tools. Added `report` + `launch`
  intent labels in `classify_intent`, and excluded them from the P1.5 Hermes branch so they
  stay on the local cloud brain's tool surface (`compose_report`/`launch_project` remain in
  TOOLS schema + TOOL_MAP + TOOL_DECLARATIONS).
- **Verify:** 14/14 static (classifier returns `report`/`launch` not `general`; Hermes route
  set excludes both; tools still in all 3 surfaces). 2/2 live (real runs, NOT Hermes-routed):
  "compose a report about the AI assistant work" → "No source convos found, sir…";
  "launch the iRIMS project" → "iRIMS up, sir. Logged in as testreg_acct."
- **Status: COMPLETE (ad-hoc verified, uncommitted).**

### Phase 4 — Async voice pattern (MANDATORY before general tasks are usable)
- **`delegate_to_hermes` / `_grounded`** gained `background=True` + `on_done` callback:
  launches Hermes in a **daemon thread**, returns the `⟳ HERMES_BACKGROUND:` ack
  IMMEDIATELY (no blocking), and calls `on_done(result)` when Hermes finishes. Sync
  path (`background=False`) unchanged. Refactored the Hermes run into `_run_hermes_sync`.
- **`think()`** gained `on_hermes_done=None`; when supplied and the turn routes to
  Hermes (general/app_reference), it delegates in the background and returns the ack.
- **`jarvis_web.py`** WS handler (audio + text paths): captures the event loop, builds
  an `on_hermes_done` that hops back via `asyncio.run_coroutine_threadsafe` to
  `deliver_result()` (new coroutine). On the `⟳ HERMES_BACKGROUND:` ack it sends the
  ack, sets state `idle` (mic freed), and `continue`s; the real answer lands later as
  a `response` event with `"deferred": True` + TTS.
- **Verify:** direct call `delegate_to_hermes_grounded(background=True)` returns ack in
  **0.003s**. Live WS: "what is UDP" → ack at **1.07s** ("On it, sir — working on that
  now…"), real "Hermes reports: UDP…" delivered **29s later** as deferred response.
  5/5 live. (First delegated turn of a cold server showed ~23s ack — a one-off
  subprocess cold-start, not the mechanism; warm re-run confirmed 1.07s.)
- **Status: COMPLETE (ad-hoc verified, uncommitted).**

### Phase 4b — Hotfix: answer hygiene (user-reported)
- **Remove "Hermes reports:" prefix:** `_run_hermes_sync` no longer prepends it; also
  strips a leading "Hermes reports:" if Hermes's own output leads with it. The user hears
  JARVIS speak the answer directly — the label was redundant noise on voice/UI.
- **Raise `max_turns` 6 → 15:** 6 was too low for multi-step tasks (Spotify play, ChatGPT
  lookup) — Hermes hit "Reached maximum iterations (6)" and returned truncated/incoherent
  answers. Both `delegate_to_hermes` and `_grounded` defaults now 15; router call sites
  (brain_gemini P1.5 branch, both branches) pass 15; bridge test bumped 3 → 15.
- **Verify:** 5/5 live — `max_turns` default=15 at all 3 sites; "what is a GPU" answer
  returned clean with NO "Hermes reports:" prefix ("Second Brain vault has GPU mentions…").
- **Status: COMPLETE (ad-hoc verified, uncommitted).**

### Phase 6 — App-control bridge (user-reported: "I cannot utilize my apps")
- **Root cause:** the capability manifest (Phase 0a) was a DEAD artifact — `machine_capabilities.resolve()`
  was never imported into the delegation path, so Hermes got NO app-location data and
  blindly ran `start "spotify:search:…"` (a URI that doesn't reliably focus the GUI).
  Also `computer_use` is a **skill, not a loaded tool** in the delegated harness, so
  Hermes couldn't drive the GUI ("no GUI automation").
- **Fixes:**
  1. `open_application` upgraded to be **manifest-backed** (prefers `resolve()`, falls back
     to `APP_ALIASES`, then `.exe` strip). Added `action`/`query` params; `action="search"`
     for Spotify opens `spotify:search:QUERY` reliably. Schema + TOOL_MAP + brain decl updated.
  2. New `_app_context(task)` scans the task for known app names and injects resolved
     launch paths + a "launch by EXACT path via your terminal tool" block into
     `delegate_to_hermes_grounded` — so the delegated Hermes drives the real path instead
     of guessing (computer_use being unavailable no longer blocks it).
  3. Removed a redundant `launch_application` duplicate (kept the single `open_application` tool).
- **Verify:** static — `_app_context` injects `Spotify.lnk` + search-URI hint; `open_application("vlc")`
  → "Opened vlc from C:\Program Files\VideoLAN\VLC\vlc.exe"; `open_application("chrome")` → chrome.exe.
  Live WS: "open Spotify and search Be All Right by Dean Lewis" → "Spotify opened. Searched
  'Be All Right by Dean Lewis'…" and **Spotify process confirmed running**. 4/4 live.
- **Status: COMPLETE (ad-hoc verified, uncommitted).**
- Once Hermes is reliably the executor, delete `_trim_history` local-only workaround,
  idle-reset, demo-mode fallback crutches.
- **Verify:** long session stays coherent without the hacks.

---

## 5. Improvement Backlog (Roadmap)

### Tier 1 — Fix broken spots (do first)
1. **Async voice pipeline** — unblocks the whole harness design (see Phase 4).
2. **Wake-word false-trigger hardening** — distinct phrase, confirmation blip, barge-in
   (stop current speech when user speaks). TV/other voices currently trigger it.
3. **Confirm-gate UX in voice** — phrase-lock confirmation ("say *yes, delete X* to
   proceed"), not a single "confirm" word that could release the wrong pending task.
4. **Streaming responses** — stream tokens → stream TTS chunks; less perceived lag.

### Tier 2 — Capability depth
5. **Proactive / scheduled intelligence** — "check my calendar every morning"; uses Hermes
   `cronjob`. Moves JARVIS from reactive to assistant.
6. **Multi-step task memory within a turn** — chain tool calls with state ("clip, then
   compress, then upload"). Add intent-decomposition in `classify_intent`.
7. **Clarifying questions instead of guessing** — ambiguous tasks get one targeted question.
8. **Callee result verification** — after Hermes acts, JARVIS verifies the artifact exists
   before claiming success (your "verified outcomes, not claims" standard).

### Tier 3 — Open-source / portability hardening (your stated goal)
9. **Privacy-first defaults** — `capabilities.json`, profile, session store all gitignored +
   local-only; `.env.example` with no secrets; first-run setup wizard; **no telemetry** by
   default (opt-in if ever added, documented).
10. **Pluggable backends** — `BrainProvider` interface (swap 9router/Ollama for OpenAI/
    Anthropic/local-llama); memory backend spec (Obsidian+Hermes default, swap to markdown/
    Qdrant/sqlite); STT/TTS swappable.
11. **Real config + capability spec docs** — `CAPABILITIES.md`, `MEMORY.md` (grounding
    protocol), `CONTRIBUTING.md`. The "works on different machines" promise needs a written
    contract.
12. **Safe-mode / capability gating per machine** — `jarvis-profile` flag ("locked down: no
    destructive, no terminal") so the same JARVIS runs safely on a server vs. a dev laptop.

### Tier 4 — Polish / differentiation (premium, not AI-slop)
13. **Personality consistency** — configurable `persona` note (neutral default for OSS;
    editorial-butler for you).
14. **Failure transparency** — say *what* failed ("Hermes didn't finish in 5 min") instead of
    silently falling back to demo mode.
15. **Offline graceful degradation** — state "local-only mode: music + X work, general tasks
    don't" instead of half-working.

**Honest top-3 if forced to pick:** (1) Async voice pipeline, (2) Privacy-first +
pluggable backends, (3) Result verification. Don't over-build Tier 4 before Tier 1–2 work.

---

## 6. Open-Source & Portability Requirements

- **Local-only data is sacred:** manifest, profile, session store → gitignored, never
  committed, never auto-shared in bug reports.
- **Capabilities/preferences are data, not code** → portable across machines.
- **Pluggable everything** (brain, memory, voice) → contributors swap without forking.
- **Auth side-routes stay special:** music + ChatGPT are *sessions*, not installed software;
  discovery/memory don't replace them — they remain JARVIS-local (or separate re-work).

---

## 7. Verification Discipline (standing rule)

- Fresh ad-hoc probe per change, written to `C:/Users/deped/AppData/Local/Temp/hermes-verify-<rand>.py`.
- Run via **terminal** (foreground cap 600s), never `execute_code` (5-min cap kills the
  Hermes cold-start spawn; `import tools` also pulls heavy deps that blow smaller caps).
- Filter probe stderr: drop `HF_TOKEN` / `Loading weights` lines.
- **Delete the probe after running** (`gone` confirmed).
- No canonical build/lint command exists for this repo → verification = focused evidence
  probe, not a committed-test-suite green. `scripts/verify_hermes_bridge.py` (9 checks) is
  the closest thing and must stay green through Phase 2.

---

## 8. Decision Log

| # | Decision | Context |
|---|---|---|
| D1 | Hermes-as-harness, **scope A (full toolset + confirm gate)** | 2026-08-22; replaces earlier read+report rule. Destructive needs explicit approval; enforced structurally (latched task match). |
| D2 | **Tiered router** (not full discard) | Keeps common tasks fast + preserves auth side-routes; full discard loses speed + offline. |
| D3 | **Capability manifest** is a Hermes skill, gitignored | Makes "works on different machines" real; portable + private. |
| D4 | **Persistent memory = Second Brain vault + Hermes memory + JARVIS session store** | Anti-hallucination via ground-then-answer + write-back teaching. |

---

## 9. Open Questions / Risks

- **OQ1:** Does the delegated `hermes chat -q` subprocess inherit Hermes `MEMORY.md`? Must
  verify in Phase 0b step 5; if not, inject profile/context explicitly.
- **OQ2:** GUI-app driving via `computer_use` is best-effort/slower — set OSS expectations.
- **OQ3:** Manifest is a path to executing discovered binaries — consider a "trusted apps"
  allowlist in the profile alongside the confirm gate.
- **OQ4:** Empty vault = no grounding = same hallucination risk → write-back must be
  first-class, not optional.

---

## 10. Status Tracker

| Phase | Scope | Status |
|---|---|---|
| 0a | Capability manifest skill | planned |
| 0b | Memory layer (vault + profile + session) | planned |
| 1 | Router-first `think()` | planned |
| 2 | Delete replacable tools | planned |
| 3 | Re-home orchestrators | planned |
| 4 | Async voice pattern | planned (blocker) |
| 5 | Retire brain hacks | planned |
| T1–T4 | Improvement backlog | planned (roadmap) |

*Current repo state (pre-build): `delegate_to_hermes` already exists (scope A, 4-touchpoint
wiring, 12/12 ad-hoc verified this session; live WS dry-run passed: safe task returned real
machine time, destructive gated). JARVIS running on :8002. Changes to `tools.py` /
`brain_gemini.py` are uncommitted.*
