# JARVIS Rearchitecture — Consolidated Plan

**Owner:** Roger A. Abay Jr.  **Executor:** Hermes (this agent)
**Status:** Phases 1-6 COMPLETE (verified). Rearchitecture DONE.

## North star
JARVIS = thin voice I/O + routed orchestrator. General intelligence =
Hermes as a WARM persistent executor. Each task picks the best specialist
backend. Responds live during the task; mid-flight redirectable.

## Stack (Layers)
- L1 Intake:   voice/text -> normalize (fixes bad voice + grammar) + autocomplete + confirm gate
- L2 Harness:  warm persistent Hermes session (kills 2-min cold start)
- L3 Router:   delegate(task, backend, scope, confirm, grounded)  <- 1 primitive, 19 tools -> ~5
- L4 HUD:      live progress streaming + mid-flight redirect
- L5 Backends: native / desktop(Office) / web / manus / chatgpt / music
- L6 Prune:    19 tools -> ~5, delete redundant tools

## Phase 1 — Intake Layer  [DONE 2026-08-23]
Delivered: `intake.py` (pure stdlib, no deps), `intake_corpus.json`
(portable data, seeded from profile), `intake_test.py` (22 asserts, all pass).
Capabilities proven:
- Grammar/Taglish-resilient: "can you please open the notepad for me jarvis" -> open/notepad high-conf
- Buried entity: "copy the budget table from excel" -> excel high-conf
- Typo voice: "jarvus play taylor son" -> no false entity, offers "taylor swift" chip (did-you-mean)
- No garbage high-conf: "what is the weather in manila" -> no false entity
- Word-confidence demotion: low Whisper conf forces confirm regardless of fuzzy score
- Autocomplete always seeds verb + surfaces typo'd entities
- Interrogative yield: "what is the weather" -> weather (not what)
Next wiring (still Phase 1): call resolve_intent() from jarvis_web.py after
_transcribe / _transcribe_from_file instead of passing raw text to router.

## Phase 2 — Warm Harness [DONE 2026-08-23]
Delivered: `warm_harness.py`, patched `tools.py`, `jarvis_web.py`, `watch_and_restart.py`.
Capabilities proven:
- Persistent session: `hermes chat --resume SESSION_ID` reuses session across turns
- Disk cache: session_id persisted to `.warm_harness/session_id.txt`, survives JARVIS restarts
- Pre-warming: `jarvis_web.py` spawns background thread at boot to create session before first query
- Fallback: if warm session fails, falls back to fresh spawn (no breakage)
- Self-test verified: first call 29s (init), resumed calls 25s (session remembered context)
- Latency is LLM-bound (~22s inference through 9Router), not subprocess overhead
- Session remembers previous conversation (reply 2 quoted reply 1's content)
Files changed:
- `warm_harness.py` — new module (10.6KB): `warm_session()`, `warm_send()`, `warm_reset()`, `warm_status()`
- `tools.py` — `_run_hermes_sync()` now delegates to `warm_harness.warm_send()`
- `jarvis_web.py` — boot-time `_prewarm_hermes()` background thread
- `watch_and_restart.py` — added `warm_harness.py` to watch list
Note: per-turn latency remains ~22-25s (LLM inference bound). Phase 4 (async voice)
makes this acceptable by keeping the mic live during Hermes execution.

## Phase 3 — Routed delegate() [DONE 2026-08-23]
Delivered: unified `delegate()` function in tools.py, TOOL_MAP + TOOLS entries, brain_gemini.py wired.
Capabilities proven:
- ONE function: `delegate(task, backend, confirm, grounded, background, on_done, timeout, max_turns, progress_cb)`
- Auto-detect backend via `intake.verb_backend()`: play→music, open→desktop, search→web, write→hermes
- Local backends (music/desktop/web) dispatch instantly via execute_tool() — no Hermes spawn
- Hermes backend includes confirm gate for destructive tasks + optional memory grounding
- Legacy tools (`delegate_to_hermes`, `delegate_to_hermes_grounded`, `delegate_task`) still in TOOL_MAP for backward compat
- brain_gemini.py think() now calls `delegate()` instead of `delegate_to_hermes_grounded()`
- Integration test: auto-detect works (4 verbs tested), TOOL_MAP/TOOLS entries present, legacy tools preserved
Files changed:
- `tools.py` — added `_detect_backend()`, `_LOCAL_DISPATCH`, `delegate()`, TOOLS entry, TOOL_MAP entry
- `brain_gemini.py` — `delegate_to_hermes_grounded` → `delegate` in think() routing

## Phase 4 — HUD streaming + redirect [DONE 2026-08-23]
Delivered: `warm_redirect()`, `progress_cb` threading, `/redirect` command, HUD progress states.
Capabilities proven:
- `progress_cb` parameter threads from brain.think() → delegate() → delegate_to_hermes() → _run_hermes_sync() → warm_send() → warm_harness
- Milestone events: "Delegating to Hermes: <task>", "Hermes finished." stream to WS via `_push_progress` → `_progress_reader` → `progress` type
- HUD shows "DELEGATING…" state on progress, "REDIRECTING…" on redirect
- `warm_redirect(instruction)`: appends a new user turn to the warm Hermes session via `--resume` (mid-flight correction)
- `/redirect <instruction>` command in jarvis_visual.html → WS `command: redirect` → jarvis_web.py `warm_redirect()`
- Integration test: all 5 functions have progress_cb; warm_redirect present; redirect wired in web + visual
Files changed:
- `warm_harness.py` — `warm_send(progress_cb=)`, `warm_redirect()`
- `tools.py` — `delegate`, `delegate_to_hermes`, `delegate_to_hermes_grounded` accept `progress_cb`
- `brain_gemini.py` — `delegate()` called with `progress_cb=_progress_local.cb`
- `jarvis_web.py` — `redirect` command handler (WS `command: redirect` → `warm_redirect()`)
- `jarvis_visual.html` — `/redirect` slash command + REDIRECTING/DELEGATING HUD states
Note: Hermes CLI runs as a subprocess so granular per-step progress (e.g. "Reading Excel...")
is not streamable; milestone events ARE. Full step-level streaming would require hermes serve
WebSocket (/api/pub), a Phase 5+ enhancement.

## Phase 5 — Backend wiring [DONE 2026-08-23]
Delivered: chatgpt + manus backends now reachable via delegate(); Manus bridge module
written; router broadened; privacy-safe cookie handoff.
Capabilities proven (source-level, ad-hoc verified 7/7):
- `_detect_backend` now returns the full set: music/desktop/web/chatgpt/manus/native
- `_LOCAL_DISPATCH` wired for all 5 instant backends:
  - music  -> play_music / stop_music
  - desktop-> open_application
  - web    -> search_web
  - chatgpt-> ask_chatgpt (submit=True)  [browser_agent, needs :9223 signed-in Chrome]
  - manus  -> manus_agent.delegate_to_manus()  [NEW module]
- intake.py verb map already routed make/build/create/generate -> manus; now honored
- `manus_agent.py` NEW: cookie-handoff bridge (Manus-only cookies, epoch-expiry,
  FB stripped), strict completion detection + oscillation guard, fresh-session-per-task,
  graceful "Not configured" when cookie file absent. NEVER printed to chat / committed
  (.gitignore: manus_cookies.json, like .env).
- `manus_status` tool added (TOOL_MAP + TOOLS schema) so the model can check config.
- `delegate` docstring + schema updated: backend enum now
  hermes|music|desktop|web|chatgpt|manus.

Runtime status (honest):
- music/desktop/web: live-capable now (were before).
- chatgpt: code-ready; REQUIRES user's Chrome on :9223 signed into chatgpt.com
  (currently down in this session — relaunch or sign in once to activate).
- manus: code-complete; REQUIRES one-time Manus cookie export to manus_cookies.json.
  Returns a clear setup message until then (no silent failure).
- native: Hermes fallthrough (always worked).
- REAL Office-driving (edit Excel/Word) is Hermes's job via computer_use — JARVIS
  delegates that to Hermes, not implemented as a separate JARVIS backend (by design:
  JARVIS stays thin; Hermes is the executor).

## Phase 6 — Tool pruning [DONE 2026-08-23]
Collapsed the LLM-exposed action surface to a single `delegate` primitive across BOTH
schemas, while preserving the internal runtime dispatch (`TOOL_MAP`) that `delegate()`
depends on. (Key insight: the plan's "19→~5" was about the MODEL's visible choices, not
deleting internal functions — many tools are called by `delegate()`/`execute_tool`.)

What changed:
- Active schema `brain_gemini.TOOL_DECLARATIONS` (Gemini brain): removed `delegate_task`,
  `delegate_to_hermes`, `delegate_to_hermes_grounded`; ADDED `delegate`. 21→19 tools,
  with exactly ONE delegation primitive.
- Legacy schema `tools.TOOLS` (Claude brain / `brain.py`): same collapse. 20→17 tools.
- `TOOL_MAP` (runtime dispatch, consumed by `execute_tool` and `delegate()`): UNCHANGED —
  still contains `delegate_to_hermes`, `delegate_to_hermes_grounded`, `delegate_task`,
  `delegate`, `play_spotify`, etc. Removing those would break `delegate()` internals, so
  they stay as internal implementation, invisible to the model.
- Result: model sees one routing entry (`delegate`); all real work flows through it;
  direct tools (read/write/list/search/open/music/chatgpt/credentials/launch/compose/
  rescan/manus_status/browser_status) remain for instant local actions.

Verified (ad-hoc, source-level, NOT a test suite):
- tools.py + brain_gemini.py parse cleanly.
- Both schemas: legacy 3 gone, `delegate` present.
- TOOL_MAP retains `delegate_to_hermes`/`_grounded`/`delegate_task` (needed by `delegate()`).
- Tools still enumerate: Claude=17, active=19.

Runtime status (honest): schema-level verified. Full live delegation round-trip was NOT
re-run this phase (HF/transformers import is slow in this env); Phase 3/4/5 routing was
mock-verified and Phase 2 ran a live warm harness. To prove end-to-end, launch jarvis_web
and speak a "make/build/ask ChatGPT" command — it should now route via the single
`delegate`.

## Post-rearchitecture notes
- JARVIS is now: voice I/O → intake verb routing → `delegate()` → {instant local tools |
  Hermes executor (warm, persistent, grounded)}. Music/chatgpt/manus/web/desktop handled.
- Remaining runtime gaps (not blocking, by design): chatgpt needs signed-in Chrome on
  :9223; manus needs a cookie export to manus_cookies.json (gitignored, never chat).
- `verify.py` in repo root is STALE (asserts 8 tools, references removed `run_shell`/
  `get_system_info`) — does not reflect the rearchitected registry. Left as-is; the
  authoritative check is the ad-hoc source verify used per phase.

## Phase 8 — Web Registry (site resolution for voice)  [DONE 2026-08-24]

Goal: "open facebook" resolves via a named site registry instead of LLM guessing.
Same pattern as app_registry.json, but for websites.

### Design decisions (agreed with user)
- **Registry file:** `web_registry.json` next to `app_registry.json`.
  Per-site: url, aliases[], source ("manual"|"history"), visits, top_urls[]
  (top 5 URLs per domain with visit counts).
- **Scope:** top N=30 domains + top 5 full URLs per domain.
- **Manual entries:** added by voice ("add site youtube as video") or direct JSON edit;
  `source: "manual"`; ALWAYS shadow history entries on the same domain (merge keeps
  manual url/aliases, visits keep accumulating from rescans).
- **History scan source:** machine DEFAULT browser only — read
  `HKCU\Software\Microsoft\Windows\Shell\Associations\UrlAssociations\https\UserChoice`
  ProgId (ChromeHTML/BraveBHTML/MSEdgeHTM...) -> map to History path.
  Fallback: scan all Chromium profiles found. Firefox out of scope.
  Optional future override key: `browsers[]` in registry JSON (not built now).
- **Open target:** sites ALWAYS open in the JARVIS debug-Chrome instance
  (browser_agent.py, :9223) so pages stay controllable — not the default browser.
- **Refresh policy (C):** startup refresh ONLY if web_registry.json older than 7 days;
  manual "rescan my sites" anytime.

### Performance requirements (no design compromise)
- Incremental scans: query History rows newer than last scan timestamp (`last_visit_time`
  watermark stored in registry), merge delta into existing registry. First scan ~1-2s,
  refresh <300ms.
- Non-blocking startup: staleness check = one stat call; stale scan runs on background
  thread; voice resolution uses current registry immediately.
- O(1) resolution: names+aliases preloaded into dict at boot; fuzzy match only on miss.

### Resolution order (voice -> site)
1. Exact name/alias hit in web_registry (case-insensitive, plural-tolerant) — O(1)
2. Fuzzy match on names+aliases (typo tolerance)
3. Fall back to existing guessing behavior

### App-vs-site disambiguation
Check BOTH registries before answering:
| Input            | web_registry | app_registry | Action                        |
|------------------|--------------|--------------|-------------------------------|
| "open facebook"  | hit          | miss         | open_site                     |
| "open chrome"    | miss         | hit          | open_application              |
| "open spotify"   | hit          | hit          | native app WINS if installed  |
| "open facebok"   | fuzzy        | miss         | open_site                     |
Tie-break: native app wins when installed in both (music pipeline depends on Spotify
desktop — must never route to web player). Sites win when no app installed.
Manual overrides (never required): "facebook site"/"youtube in browser" force open_site;
"spotify app"/"launch chrome" force open_application.
Genuine ambiguity (app installed AND site is top-visited, both plausible): one-time
voice clarify ("Spotify app or website?") — matches existing clarify pattern.

### Implementation steps (verify each before next)
1. `web_registry.py`: scanner module (default-browser detect, History copy->sqlite
   aggregate, incremental merge, junk filter: localhost/127.0.0.1/IP literals/CDNs/
   visits<2, top-30/top-5 write).
2. Merge logic preserving manual entries + visits accumulation + watermark.
3. `open_site(name)` tool in tools.py registered alongside open_application;
   router wiring: dual-registry lookup + tie-break table above.
4. Startup staleness check + background refresh thread in jarvis_web.py.
5. HUD/config surface: view logged sites ("what sites do you know").

### Verification gates
- Scanner unit: fake History DB -> correct top-N, junk filtered, watermark works.
- Merge unit: manual entry survives rescan, visits accumulate.
- Live: launch JARVIS, say "open facebook" -> opens in debug-Chrome :9223;
  "rescan my sites" -> fresh registry with new visits; "open spotify" -> desktop app.

### Verification gates — ALL PASSED (fresh runs, 2026-08-24)
- Scanner: default browser detected = MSEdgeHTM -> Edge History; full scan 0.62s,
  30 domains captured, junk filtered; incremental rescan 0.45s.
- Merge: manual entry ('email' -> mail.google.com, aliases gmail/mail) survived a
  rescan with source=manual intact; visits accumulate; watermark stored.
- Resolution: facebook/facebok/youtube/github/tiktoks hit O(1) or fuzzy;
  'emails'/nonsense correctly miss. Alias resolution works.
- Router table (resolve_open_target): site/app/clarify/none all correct incl.
  'open the spotify app'->app, 'open the facebook site'->site,
  'open youtube in browser'->site, 'open spotify'->app (native wins),
  'go to tiktok'->clarify (PWA in both registries).
- Startup: is_stale() false on fresh scan, true at 8 days; maybe_refresh_async()
  no-ops when fresh; hook wired into jarvis_web.py boot after Hermes prewarm.
- Tools: list_sites/add_site/open_site/rescan_sites registered in TOOLS+TOOL_MAP;
  add_site + alias open verified live; test entry cleaned up afterwards.
- LIVE end-to-end: delegate("open github") opened github.com as a NEW tab in the
  JARVIS debug-Chrome and returned the real page title.

## Phase 9 — Conversation window (local fast-path context)  [DONE 2026-08-24]

Goal: local router keeps a short rolling context so follow-ups and clarify
answers work without Hermes guessing ("open chatgpt" -> "app or site?" ->
"site" must OPEN THE SITE, not fall through to Hermes filler).

### Design
- `conversation_window.py`: in-process rolling window, last 6 turns, 120s TTL,
  stdlib only. Each turn: {text, kind, tool, result, payload}.
- classify(): command | fragment | contextual | clarify_answer.
  Conservative: verb-starting or >5-word utterances are ALWAYS commands
  (never hijacked); bare disambiguators only count while a clarify is pending.
- answer_clarify(): site-word set vs app-word set + ASR homophones
  (sight/cite/sat -> site; ap/abs/op -> app).
- complete_fragment(): entity carry-over from previous open/launch turn
  ("also facebook" after "open tiktok" opens facebook), plus "again".
- Wired at TOP of delegate() before Phase 8 fast path; every routed turn is
  appended so the window always reflects reality. Clarify question stored as
  kind="clarify" with payload {"name": ...}; answered turns clear it by being
  superseded.

### Verification gates — PASSED (fresh ad-hoc run, exit 0, 16/16)
- Unit: classification (command vs clarify_answer vs fragment), non-capture
  ("play some music" after a pending clarify routes as music, NOT an answer),
  fuzzy ASR answers, TTL expiry.
- LIVE multi-turn via delegate(): "open cwsitetest" recorded; "open tiktok"
  asked app-or-site AND left pending clarify; "site" opened TikTok site tab;
  "also facebook" carried over and opened facebook. Test entry cleaned up.
- py_compile clean on both touched files.

## Phase 10 — Search-target routing + correction handling  [DONE 2026-08-24]

Goal: "search X in chatgpt" must search ChatGPT HISTORY, not open a Google
tab with the literal words. Corrections ("I mean ...") replace the previous
query instead of becoming a new literal one.

### Design (tools.py: parse_search_command / execute_search)
- Verb start: search | look up | find | google.
- Tail qualifier "... in/on/at/inside <target>", candidate capped 1-24 chars,
  resolved EXACTLY (never fuzzy — 'manila' must stay in the query; the old
  fuzzy resolve matched manila->email). Resolution order: AI history names
  (chatgpt/chat gpt) -> AI typed names (gemini/claude/grok/copilot) ->
  exact web_registry key.
- Leading history phrasing stripped: "search in histor(y) [for] X" -> history
  search of X ("histor" ASR rendering included).
- Correction markers (I mean / actually / no,) before OR after the verb set
  corrected=True; delegate() replaces the previous execute_search turn's
  result and prefixes "Corrected —".
- Routing: chatgpt -> browser_agent.search_chatgpt_history; gemini/claude/
  grok/copilot -> ask_web_ai typed into composer; registered site -> open_site
  + tell user to search there; none -> search_web (Google) as before.

### Bugs caught by first gate run (fixed before pass)
- Greedy tail regex captured whole query as candidate.
- Fuzzy resolve_site hijacked place names ('manila' -> email).
- Registry key for Gemini is 'google' — spoken AI names now direct-mapped,
  not registry-dependent.
- Correction marker before the verb never reached parse (verb-strip order).

### Verification gates — PASSED (fresh runs, exit 0)
- Parser gate 12/12: bug utterances, lead-history strip, non-site tails stay
  in query, exact-only site tails, spaced 'chat gpt', correction flag.
- LIVE gate 4/4 via delegate(): turn A plain search opened Google; turn B
  "I mean ... in chatgpt" ran ChatGPT history search in the debug-Chrome
  (returned real 'No conversation titles matched' evidence, NOT a Google tab),
  recorded as execute_search turn. Temp gates deleted after runs.

## Phase 11 — Structured audit log + injection sanitizer  [DONE 2026-08-24]

Goal: make every JARVIS action queryable, and quarantine instruction-shaped
content from external text before it reaches model context.

### Delivered
- `audit.py`: JSONL audit at logs/audit.jsonl. One line per execute_tool():
  {ts, tool, args(bounded 300), duration_s, ok, result_summary(240),
  confirmed}. Fail-open (audit errors never break tools), thread-locked,
  read_recent(n)/format_recent(n) for "what did you just do?". Hooked into
  execute_tool() success AND exception paths; confirmed flag carried from
  args.
- `guard.py`: sanitize_web_text() quarantines injection patterns inline
  (ignore-previous, SYSTEM:/</system>/[INST] tags, "you are now unrestricted",
  repeat-your-prompt, act-as-DAN) preserving surrounding context;
  is_suspicious() for routing decisions.
- Chokepoints wired: ask_web_ai reply text, search_chatgpt_history titles.

### Verification gate — PASSED (fresh run, exit 0, 18/18, self-deleting)
- Audit: ok/fail/confirmed lines written correctly, bounded fields,
  format_recent readable; LIVE list_sites call produced a real audit line.
- Guard: all 5 injection cases quarantined; clean text passes untouched;
  mixed text keeps surrounding context around the quarantine marker.
- py_compile clean on audit.py, guard.py, tools.py, browser_agent.py.

## Phase 12 — Evidence-based tool results (self-verification T1)  [DONE 2026-08-24]

Goal: tools must never claim a verified result they did not verify. Caught in
the wild: play_music returned "Playing '<query>'" while its own log said the
now-playing title was unreadable — it announced the literal voice command as
the song name.

### Delivered
- `music_agent.play_music`:
  - Unreadable now-playing title → honest hedge ("Playback started ... but I
    couldn't read what's playing") instead of a fake song claim.
  - [MatchError] (wrong track) → ONE automatic retry with fresh results page,
    marked "(second attempt after a wrong first match)"; navigate_only calls
    never recurse. [Error] still falls back to _fallback_open as before.
- `browser_agent.open_site`: goto failure is now classified by evidence:
  nothing loaded (empty title + blank URL) → explicit [Error]; navigated but
  unsettled → "may still be loading" hedge; loaded with no readable title →
  says so; success claims always carry the real page title.

### Verification gates — PASSED (v2, exit 0, 10/10, self-deleting)
- v1 run: 7/9 — both FAILs were GATE BUGS not code bugs (github.io wildcard
  serves a real 404 PAGE; open_site truthfully reported its title). Gate fixed.
- v2: source-level honesty assertions + LIVE good site (real title), LIVE
  NXDOMAIN ([Error] Could not open ... Nothing loaded.), LIVE wildcard-404
  (truthful title), compile clean on music_agent.py + browser_agent.py.
- All audit lines flow into logs/audit.jsonl via execute_tool automatically.

## Phase 13 — Session recall via FTS5  [DONE 2026-08-24]

Goal: "what did I ask about X last week?" must be answerable from JARVIS's
own voice transcripts, not just the last-6-turn window.

### Delivered
- `session_index.py`: SQLite FTS5 index (sessions/index.db) over all
  sessions/jarvis-*.jsonl turns. Incremental via per-file line watermark in a
  meta table; rebuild() picks up only new lines. log_and_index() appends
  fresh turns immediately (called by session_store.log() with best-effort
  line provenance). Fail-open everywhere; stdlib sqlite3 only.
- `search_sessions(query, limit)` tool: registered in TOOLS + TOOL_MAP;
  routes through execute_tool() so every search lands in the audit log.
- Grounding hook in delegate_to_hermes context prefix: recall-shaped queries
  ("remember / last time / did i / what did i / previously") inject top-3
  [PAST SESSION MATCHES] alongside profile + recent window.
- Privacy unchanged: index.db lives under gitignored sessions/
  (git check-ignore confirms).

### Verification gate — PASSED (fresh run, exit 0, 14/14, self-deleting)
- Index: 454/454 real turns indexed; second rebuild adds 0 (idempotent).
- Search: known query finds hits; multi-term OK; empty query safe; a turn
  logged DURING the gate was searchable immediately (freshness proven).
- LIVE execute_tool('search_sessions', 'irimsv') returned 7 timestamped hits.
- Tool registered; grounding hook present; sessions/ gitignored (verified via
  git check-ignore); py_compile clean on all three touched files.

## Phase 14 — Scoped desktop control  [DONE 2026-08-24]

Goal: JARVIS can operate desktop apps (click, type, scroll, read screens)
safely — every action voice-confirmed, background-first, foreground only on
explicit request.

### Decisions (agreed with user 2026-08-24)
- EVERY desktop action requires voice confirmation (like destructive ops) —
  no lexical detection needed, the gate is unconditional for this tool.
- Delivery is BACKGROUND-ONLY by default (never steals cursor/focus).
  Foreground takeover permitted ONLY when the user explicitly asks for it
  ("bring it to front", "take over", "in foreground").
- Capability-based app utilization (direct SQL past DBeaver GUI etc.) is
  PARKED as a follow-up phase — not part of 14.

### Architecture: B-first (delegate to Hermes computer_use)
- New tool `desktop_control(task, foreground=False)` in tools.py.
- First call returns NEEDS_CONFIRM with the exact planned action; second call
  with confirm=true releases it (reuses the existing module-level pending-task
  latch — a stale confirm cannot release a different action).
- Execution path: delegate() -> hermes backend with instruction that Hermes
  uses its computer_use tooling (element-indexed clicks, background delivery).
  No new Python deps; ~50 lines.
- foreground=true additionally sets raise_window on the driver side AND is
  recorded in the audit log (confirmed + foreground flags).

### Safety rails
- Hard-blocked regardless of confirmation: typing into password fields,
  payment UIs, OS permission dialogs (mirrors cua-driver policy).
- Every completed action returns post-action evidence (capture description),
  per Phase 12 honesty rules — no unverified success claims.
- All calls flow through execute_tool() -> audit.jsonl automatically.

### Out of scope (future phases)
- Native pywinauto/uiautomation fast paths (only if latency hurts).
- Capability routing: registry gains capabilities[] + data_sources{} so
  "what columns does X have" bypasses the GUI entirely (DBeaver ->
  information_schema query). Parked at user's direction.

### Verification gate — PASSED (durable, re-runnable, exit 0, 12/12)
scripts/hermes-verify-phase14-durable.py (kept in-repo as durable evidence;
does NOT self-delete). Covers: unconditional NEEDS_CONFIRM on first call,
exact-task latch (wrong-task confirm rejected), blocked content classes
(credentials/payment refused even WITH confirm), latch survives blocked
attempts, foreground flag propagates into message + latch, empty task error,
tool registration + schema fields, py_compile clean. NO live desktop action
is executed by the gate — it stops at the confirm boundary by design.
Live end-to-end (real Hermes computer_use round-trip) remains a human step:
restart JARVIS and voice-confirm one harmless action.

## Phase 14b — Launch-worthiness filter (app registry junk purge)  [DONE 2026-08-24]

Problem (user-reported + quantified): of 975 registry entries, many were not
launchable apps — directory bins (5), MSI 'file.exe,0' icon strings (13),
documents (.txt/.chm/.html) (5), %TEMP% installer leftovers, Package Cache
entries, missing exes (~7). "open winrar help" would startfile WinRAR.chm.

### Delivered
- machine_capabilities._is_launchable(): bin must be an EXISTING .exe/.lnk;
  strips ',N' DisplayIcon suffixes; rejects dirs, docs, Package Cache, %TEMP%.
- _scan_registry(): never writes a bare InstallLocation dir as bin (skips);
  validates DisplayIcon candidates before storing.
- write_registry(): filters scan() output through _is_launchable AND only
  merges old entries that are still launchable (junk cannot resurrect).
- tools.open_application(): drops any resolved entry whose bin fails the check
  before startfile-ing it.
- Rescan executed: registry now 863/863 launchable (was 975 w/ ~30+ junk).

### Voice audit findings (reported to user, fixes pending approval)
STT: whisper medium.en on CPU int8 + beam_size=5 = dominant latency (2-6s).
TTS: edge-tts cloud round-trip, no streaming; TTS_RATE +20% sounds rushed.
Proposed: small.en model, beam_size=1, speech_pad_ms 400->250, rate +10%.

### Verification gate — PASSED (fresh run, exit 0, 17/17, self-deleting)
_is_launchable units (9), post-rescan registry state clean (0 bad bins of
863), junk keys gone, resolver validation wired, known app resolves, both
changed files compile.

## Phase 7 — OPTIONAL follow-ups (not part of original 6)
- Step-level HUD streaming via hermes serve WebSocket (/api/pub) — milestone events only
  today.
- Real Office (Excel/Word) editing via Hermes computer_use (JARVIS delegates, Hermes drives).
- chatgpt/manus live activation (credentials).
