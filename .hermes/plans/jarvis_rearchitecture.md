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

---

# AUTONOMY ARC (decided 2026-08-24) — Option C: Hermes autonomy + JARVIS job supervisor

User goal: JARVIS decides and acts like a full agent — multi-step autonomous
execution with self-verification, persistent memory, voice control of running
jobs, real app utilization (not just open/close). Chosen approach: keep ONE
brain (Hermes warm executor) for intelligence; JARVIS supervises jobs.

Key existing primitives this arc builds ON (do not rebuild):
- `jobs.py` registry: create/update/active/recent/listeners + HUD broadcast;
  states queued→running→done/error/timeout; persisted to jobs log.
- `_run_specialist_bg()` in tools.py: the Popen+watcher-thread pattern to copy.
- `warm_harness.py`: warm_send (persistent session), warm_redirect (inject
  instruction mid-session), _HERMES_BIN/_child_env/_run.
- `delegate_to_hermes(background=True, on_done=...)`: async ack pattern.
- Phase 12 evidence rules + audit log; Phase 13 session recall; Phase 14
  desktop_control confirm gate.

## Phase 15 — Autonomous job runner  [PENDING]
Goal: "organize my downloads folder" or "build X" runs as a supervised
background Hermes job with live progress — not one blocking turn.

### Design
1. New module `autonomous.py`:
   - `start_goal(goal_text, timeout_max=1800)` → creates job (tier="autonomous"),
     writes a per-job status file `.hermes-jobs/<jid>.status` (append-only lines).
   - Spawns `hermes chat -q "<autonomy-contract> + goal" --resume <warm sid>
     --max-turns 40` via Popen (copy `_run_specialist_bg` shape), watcher thread
     tails process + status file.
2. **Status-file protocol** (the contract inside the task text):
   - Hermes MUST append `[STEP n] started|done|fail — <what> | <evidence-path>`
     after every step (echo/append from its terminal tool = zero new infra).
   - Contract text also mandates: plan first → per-step verify → evidence before
     "done"; reuse user's verify-script discipline (temp probe, self-delete on pass).
3. Supervisor thread polls status file every ~2s → `jobs.update(state="running",
   note=<latest step line>)` → existing listeners broadcast to HUD free.
4. Completion parsing: final Hermes reply must end with `RESULT: done|failed`
   + evidence summary; supervisor maps to jobs state machine. Missing evidence
   ⇒ job marked `unverified`, never `done` (Phase 12 extension).

### Files
- NEW `autonomous.py` (~150 lines, stdlib only)
- `tools.py`: TOOL_MAP entry `run_autonomous(task)` + TOOLS schema + brain decl
- `jarvis_web.py`: `/status` already shows jobs_active (no change needed)

### Verify gate (re-runnable script, kept in scripts/)
- start_goal creates job + status file; contract text contains STEP protocol +
  evidence mandate; supervisor picks up appended lines into jobs notes;
  RESULT parser maps done/failed/unverified correctly (stub Popen);
  destructive-goal gate still fires through delegate(); py_compile clean.
- LIVE gate: one harmless autonomous goal end-to-end with ≥2 steps in status
  file + spoken progress + evidence-backed RESULT line.

## Phase 16 — Mid-flight voice control  [PENDING]
Goal: while an autonomous job runs: "what's the status?" / "skip that" /
"stop the task" work by voice.

### Design
- `job_status(jid|latest)` tool: reads jobs registry + last 3 status lines,
  speaks them (instant, local — no brain call).
- Stop = kill the Popen for that jid (registry already tracks it in
  `_BG_SPECIALISTS`; add same map for hermes jobs) → state="cancelled".
- Redirect = `warm_redirect("Redirect: <instruction>. Continue under prior "
  "contract.")` — BUT only safe when the session isn't mid-subprocess-turn;
  v1: redirect allowed between steps only (status-file quiet ≥5s), else queues
  as next-turn prefix. Honest limitation, documented.
- Intent routing: classify_intent gains `job_control` label checked BEFORE
  music-stop (mirrors close_app lesson).

### Verify gate
- Stub-process tests: stop kills proc + marks cancelled; redirect during active
  step is deferred (queued), between steps injects; status returns last lines.
- LIVE: speak "what's the status" during a running Phase-15 job.

## Phase 17 — Evidence-gated completion + auto-retry  [PENDING]
Goal: no autonomous job reports success without proof; single auto-retry on
verify-fail.

### Design
- Extend Phase 15 parser: each `[STEP n] done` line must carry non-empty
  evidence field (file path / command output marker). Missing ⇒ step counts fail.
- On job finish with any failed step: ONE automatic retry turn
  (`warm_redirect("Step N failed verification (<reason>). Retry that step only.")`),
  max 1 retry per job (config const). Second failure ⇒ state="needs_human",
  spoken honestly.
- `needs_human` surfaces in /status + HUD chip.

### Verify gate
- Parser units: evidence present/absent/malformed; retry fires exactly once;
  needs_human path. LIVE: force a failing step (e.g. unreadable path) and
  observe retry + honest failure.

## Phase 18 — Durable memory writes ("remember this")  [PENDING]
Goal: close the memory loop — recall exists (P13); adds reliable WRITE path.

### Design
- Voice "remember <X>" / "add to memory: X" → local fast path (no cloud):
  append to `jarvis-profile.md` under `## Learned <date>` section (Tier-2),
  + optional vault note if user says "in the vault".
- Dedup: substring check against existing profile lines; cap profile growth
  (rotate oldest learned entries at N lines).
- Confirm-by-readback: JARVIS speaks the saved line back (evidence rule).

### Verify gate
- Units: append/dedup/readback/cap. LIVE: say "remember that my reseller
  channel is Fourth Estate", restart JARVIS, ask "what is my reseller channel".

## Arc order & dependencies
15 → 16 → 17 → 18 (16/17 depend on 15's job plumbing; 18 independent, can
parallel anytime). Speed work (STT trim, streaming TTS) stays parked unless
user re-prioritizes.

## Standing rules for this arc
- Append-only to THIS plan file (2026-08-24 overwrite incident; recovered via git).
- Every phase ships with its re-runnable verify script BEFORE marking DONE.
- No phase marks DONE without fresh-run gate output quoted in chat.

## 2026-08-25 — App-lookup audit (open_application / web_registry), PENDING FIX
User concern: "exe-only matching, not the real app; keyword guessing instead of lookup."
Audit (ad-hoc gate hermes-verify-applookup-diagnosis.py, PASS exit=0, self-deleted):
- app_registry.json: 861 entries, display-name keyed (141/141 Start Menu names present),
  zero bin:None. Lookup-first confirmed.
- DEFECT 1: 69 junk exes launchable as apps (installer/updater/helper bins) —
  Phase 14b _is_launchable filters dirs/docs/%TEMP% only, never filenames.
- DEFECT 2: substring stage returns dict-order first hit ("git" beats "git-bash").
- DEFECT 3: token stage gaps — 'office word', 'terminal' resolve None.
- aliases=37; web_registry 31 sites, no JARVIS entries anywhere.
FIX PLAN (awaiting user go): (a) scan-time junk-filename filter + purge,
(b) substring stage longest-key match, (c) grow app_aliases.json spoken names.
Gate after fix: durable jarvis-demo/scripts/hermes-verify-aliases-durable.py pattern.

## 2026-08-25 — App-lookup FIX SHIPPED (gate green)
Trigger: 'open snipping tool' failed though registry had 'snippingtool'.
Fixes (tools.py, machine_capabilities.py, app_aliases.json):
1. Junk-component purge: _JUNK_BIN_RE in _is_launchable; purged 861->853
   (installers/updaters/helpers/crash handlers gone); also enforced on the
   capabilities.json fallback path inside open_application.
2. Normalized lookup stage (resolve_normalized): 'snipping tool'=='snippingtool'
   — lookup-only, before any fuzzy.
3. Substring stage: most-specific wins (longest key: git-bash beats git);
   reverse containment only for multi-word requests ('updater' can no longer
   hijack 'epson software updater').
4. Scanner now walks ProgramData all-users Start Menu too (Task Manager et al.).
5. Aliases +3: snipping tool/snip/git bash.
GATE: scripts/hermes-verify-applookup-durable.py (durable, no self-delete)
PASS: 12 spoken names resolve | 8 junk names rejected | registry=853,
0 junk bins, 0 dead bins | aliases=40 | tools+brain import clean. exit=0.

## 2026-08-25 (2) — frcode bug FIXED: 'open vscode' launched Git's frcode
Chain: alias vscode->stem 'code'; no 'code' key (bin was code.cmd, dropped by
.exe/.lnk-only filter) -> product_exe fallback `exe_stem in key` raw substring
matched 'code' INSIDE 'frcode'. THE keyword-guessing failure the user meant.
Fixes in tools.py product_exe stage:
1. exact stem key only;
2. else stem as PATH component / exact filename (regex, case-insens),
   excluding tunnel/helper/crash bins;
3. else whole-word \bstem\b against KEY NAMES excluding component bins,
   shortest key wins ('vs code' -> 'visual studio code').
Verified dry+mocked: vscode/vs code/visual studio code all -> Code.exe;
word/excel/snipping tool/git bash/chrome/notepad all correct.
GATE updated: MUST_LAUNCH_EXACT live-mock check + FRPCODE REGRESSION tripwire.
PASS exit=0: 14 names resolve | 8 junk rejected | registry=853 clean.

## Phase 15 — Autonomous job runner  [DONE 2026-08-25]
Goal: multi-step goals run as supervised background Hermes jobs (Option C).
Deliverables:
- autonomous.py: start_goal() spawns `hermes chat` with an AUTONOMY CONTRACT
  (plan → execute → append `[STEP n] done - what | evidence` to a status file →
  `RESULT: done|failed`). Supervisor thread tails the file every 2s, feeds the
  jobs registry, and flips state on process exit. Status files in .hermes-jobs/.
- Wired 8/8 surfaces: TOOLS schema + TOOL_MAP + brain_gemini DECLARATIONS +
  _openai_tools (×2) + run_autonomous()/job_control() executors + destructive-goal
  NEEDS_CONFIRM gate.
- Multi-step goals route DETERMINISTICALLY to run_autonomous via _is_multistep_goal()
  (cloud model no longer chooses). Single-step + simple intents keep fast paths.
- Scripts verify_phase15.py: 27/27 PASS (supervisor states, cancel, destructive
  gate, status-file protocol) + Phase 17 verifier cases.
- LIVE proof (job 8a78f407): goal created jarvis-phase15-final/hello.txt on disk;
  job resolved to `done` — "4 step(s) verified by JARVIS".

## Phase 17 slice (independent verification) — SHIPPED INSIDE P15
Root-cause from first live run: Hermes self-reported artifacts that did NOT exist
on disk (job 5226cf7e said "done" but folder was absent). Fix: supervisor does NOT
trust Hermes's evidence text — _verify_step() re-checks each claimed path with
os.path.exists() and greps file content. A step counts verified ONLY if JARVIS's own
check passes. RESULT:done with any unverified/failed step → state `unverified`
(honest), never `done`. Handles MSYS /c/Users/... → C:\Users\... normalization.
Watcher guard: watch_and_restart.py now defers restart while autonomous.any_running()
(auto.py added to WATCH list) so a live job is never killed mid-run.

## Bugs fixed this session (all verified)
1. classify_intent() false-matched "hello" inside hello.txt → greeting. Now requires
   short utterance + no action verb/filename.
2. _detect_backend() routed "create folder" → manus. Filesystem goals now force hermes.
3. Verifier trusted Hermes claims → now independently checks disk.
4. autonomous.py + watch_and_restart.py not in WATCH list → watcher never restarted
   for their edits; added.

## Phase 16 — Voice job control  [DONE 2026-08-25]
job_control(action=status|stop) already wired + latest_status() exists. Needs: WS
voice path to map "what's the status?"/"stop the task" → job_control(); mid-flight
redirect via warm_redirect() between steps (v1 limitation: not mid-thought).

### IMPLEMENTED 2026-08-25:
- classify_intent(): new `job_status` + `job_stop` intents BEFORE close_app/stop-music
  checks; require task/job/goal noun or bare status-question shape so ordinary
  sentences never hijack.
- think(): deterministic LOCAL routing branch `if intent in ("job_status","job_stop","remember")`
  → tools.job_control(), last_backend="instant", no Hermes/cloud round-trip.
- tools.job_control(stop): jid=None now resolves the single active job
  (autonomous.active_jobs()) instead of failing — bare "stop the task" works.
- Probe ALL PASS: 6 classify cases, 4 negatives not hijacked, honest no-job error,
  think() wiring present. Live WS turn pending next server boot.

## Phase 18 — Durable memory writes  [PENDING]
"remember X" → jarvis-profile.md append (dedup + cap); recall already works (P13).

### IMPLEMENTED 2026-08-25 (supersedes PENDING above):
- tools.remember_fact(): appends "- Fact" under ## Remembered in jarvis-profile.md;
  strips the imperative opener; case-insensitive dedup ("Already noted, sir");
  cap _REMEMBER_CAP=40 bullets; honest [Error] paths; real profile untouched by tests
  (_PROFILE_PATH monkey-patched in probes).
- classify_intent(): `remember` intent, IMPERATIVE-ONLY ^ anchor — questions
  ("do you remember my email?") fall through to recall (P13), never hijack.
- Routed through the same instant LOCAL branch as P16.
- Probe ALL PASS: 3 imperative classifies, 2 question negatives, live write/dedup/
  cap-40 round-trip on temp file, real profile untouched.

## Phase 16.5 — External CLI agent tier (gemini/codex/claude)  [DONE 2026-08-25]
- cli_agents.json (NEW, data-driven): spoken name -> bin + prompt_flag. Binaries
  resolved via shutil.which AT CALL TIME — install a CLI later, zero code changes.
  Entries: gemini, chatgpt(=codex bin), codex, claude(-p).
- tools._explicit_cli_agent(): "using/via/with gemini|chatgpt|codex|claude [code]"
  forces the tier; auto-detection NEVER routes here (Hermes stays default executor).
- tools._run_cli_agent(): one-shot subprocess, honest errors (not-installed /
  timeout / exit!=0), verbatim output; runs BEFORE opencode specialist check in
  delegate() so user's explicit choice is authoritative with NO Hermes fallback.
- Probe ALL PASS: 3 detections, plain multistep NOT hijacked, task text cleaned,
  gemini-absent honest error, registry loads dynamically, live claude one-shot
  reached the CLI (its OAuth session expiry reported verbatim — honest boundary).

## Phase 16.6 — Local real-time TTS (piper)  [DONE 2026-08-25]
- piper-tts 1.7.0 installed; voice en_GB-northern_english_male-medium downloaded to
  ~/.jarvis-tts/ (60MB, offline forever after).
- jarvis_web.tts_to_b64(): PRIMARY piper WAV path (asyncio.to_thread, event loop
  stays responsive), edge-tts MP3 kept as automatic fallback.
- Measured on this box: RTF 0.12 raw; end-to-end tts_to_b64 = 0.66s vs ~2-4s
  edge-tts network round-trip. Probe ALL PASS incl. fallback-path check.

## Status after this build
P15 done | P16 DONE | P17 shipped-in-P15 | P18 DONE | CLI tiers DONE | local TTS DONE.
Open: live WS turn proof at next server boot (all changes are restart-picked-up);
Whisper STT remains the dominant voice latency cost (~2-6s) — small.en trade-off
is a USER DECISION per skill note, not silently flipped.

## Durable gate (2026-08-25, post-build)
scripts/hermes-verify-orchestrator-durable.py — 34 checks across ALL four changed
files (brain_gemini, tools, cli_agents.json, jarvis_web): compile-clean, P16/P18
classify + negatives, job_control honest paths, remember_fact live write/dedup/cap
on temp profile, CLI tier detect + honest-error + dynamic registry + delegate()
ordering, piper TTS live synth 0.5s. RESULT: ALL PASS, exit=0.
Gate caught one stale assertion of its own (gemini IS now on PATH; tool honestly
surfaced its IneligibleTierError verbatim) — fixed the gate, not the code.
NOTE: gemini-cli installed but DEAD for this account (IneligibleTierError,
migration required); codex not yet authed; claude OAuth expired. Tier routing is
live and honest regardless — each reports its true blocker when invoked.

## 2026-08-25 (3) — TTS REV A: original voice restored + gate re-run
USER REPORT: "voice of JARVIS has changed, bring back the old." Cause: Phase 16.6
made piper (northern_english_male) the PRIMARY TTS, replacing edge-tts en-GB-Ryan.
Fix in jarvis_web.py: edge-tts restored as DEFAULT engine; piper now OPT-IN via
JARVIS_TTS_ENGINE=piper env flag (auto edge-fallback on failure retained).
Verified: default output = MP3 (Ryan voice family), opt-in piper = WAV works,
missing-model fallback -> edge. Probe ALL PASS.
Durable gate updated to assert edge-default contract. Re-run: 1 FAIL =
"absent/broken CLI -> honest tagged error". ROOT CAUSE: NOT a code regression —
gemini-cli was UPDATED to v0.56.0 between runs and now AUTHENTICATES successfully
(previously IneligibleTierError), so _run_cli_agent returned a REAL reply
("Hello! How can I assist you...") instead of a tagged error. The tool worked
correctly; the gate's fixture assumption went stale. Gate lesson repeated: fix the
gate's stale expectation, not honest code. Gate assertion revised to accept either
honest-tagged-error OR genuine-reply outcomes; full gate re-run below.
GATE RE-RUN after fixture fix: ALL PASS, exit=0. TTS end-to-end 0.54s, MP3
(Ryan voice) confirmed on the default path.

## Phase 3.5 — Compile-with-JARVIS wizard (apps rules UI)  [DONE 2026-08-26]
Goal: bridge "what the user mumbled" -> "structured, confirmable ruleset" the
user owns — surfaced in the Phase 7 apps panel. Built during session
20260825_204936_a69c29 (uncommitted), then verified + committed 2026-08-26.

### Delivered
- `jarvis_web.py`: new `POST /apps/compile`. Body `{key, rule_drafts}` ->
  `rules_compiler.parse_scaffold` -> auto-resolve ambiguous clauses with a SAFE
  DEFAULT (volume-ish phrase -> "under 60% <key> volume all sessions"; else
  phrase-as-intent, all sessions) -> `apply_clarifications` -> returns
  `{proposed, questions, proposal_markdown}`. `questions` surfaces JARVIS's
  assumptions ("JARVIS assumed: … — correct me if wrong") so the user owns the
  commit. User accepts via existing `POST /apps/rules` with `compiled_rules`.
- `apps_panel.html`: "Compile with JARVIS" button + proposal section + JS
  `compileRules()` / `acceptProposal()` fetching `/apps/compile` then `/apps/rules`.
- `apps_panel_test.py`: panel sanity test.

### Verification gate — ALL PASS (fresh run, exit 0, 10 checks)
`scripts/hermes-verify-phase35-durable.py` (kept on disk, re-runnable):
- A. rules_compiler imports (jarvis_web full import skipped — standalone
  pydantic_core._pydantic_core resolve is an isolated-sys.path env artifact,
  NOT a code defect; route verified via source check instead)
- B. `@app.post("/apps/compile")` defined; handler wires parse_scaffold ->
  apply_clarifications -> JSONResponse
- C. end-to-end: `"no explicit stuff and keep it quiet"` -> 2 candidate rules
  (avoid explicit / keep it quiet) -> safe-default `keep_it_quiet` resolves to
  "cap playback volume / cap music volume at 60% / all_sessions"; 1 clarification
  question surfaced; propose_ruleset renders markdown
- D. panel defines compileRules/acceptProposal + fetches /apps/compile + /apps/rules

GATE CAUGHT: first run had a STALE assertion of its own (checked for the literal
"under 60%" string; the route actually emits "cap music volume at 60%"). Fixed
the gate, not the code — same lesson as the TTS gate above.

### Committed
`3014a30 feat(phase 3.5): Compile-with-JARVIS wizard` — 3 files, 635 insertions
(apps_panel.html new, apps_panel_test.py new, jarvis_web.py +227/-2).

### Standing-rule correction (gitignore)
+The `hermes-verify-*.py` rule in .gitignore blocks ALL durable gates, so the
+plan's earlier "durable gate is tracked" claims (P13/P14/aliases/orchestrator)
+were STALE — those gates exist only on disk, never committed. This is fine:
+gates are re-runnable local evidence, not product code. Phase 3.5's gate follows
+the same on-disk-only convention (NOT force-added past gitignore).
+
+## Phase 3.6 — OpenCLI integration ("turn any website into a CLI")  [DONE 2026-08-26]
+Goal (from a Facebook post the user wanted JARVIS to use): let JARVIS drive
+websites as CLIs via the user's logged-in Chrome. Tool identified as
+**OpenCLI** (jackwener/OpenCLI, Apache-2.0; npm `@jackwener/opencli` v1.8.7;
+28.6k★). Distinct from the session-1 `ultimatewebscraper.com` link and from
+the session-3 read-only `oc` only-cli viewer — OpenCLI is the real "website→CLI"
+tool. User approved FULL logged-in automation (will install Chrome Bridge
+extension + accept JARVIS operating logged-in sites).
+
+### Delivered
+- `tools.py`:
+  - `run_opencli(command, confirm, foreground)`: shells out to `opencli`.
+    **Unconditional confirm gate (Phase-14 style)**: first call returns
+    `NEEDS_CONFIRM` with exact command + risk class (public read / LOGGED-IN
+    read / WRITE); second call with `confirm=true` runs it (latched to the
+    exact command). Hard-block on credential/payment phrases even with confirm.
+  - `_opencli_classify()`: read vs write vs sensitive-read (logged-in site).
+  - `_dispatch_opencli()`: translates natural phrases
+    ("scrape reddit for python news" → `reddit search "python news"`,
+    "github trending" → `github trending`, "wikipedia summary Web scraping" →
+    `wikipedia summary "web scraping"`) into `opencli <site> <sub>` commands.
+  - `_detect_backend()`: routes explicit "opencli …" and "<known-site>
+    search/scrape/read" phrases to a new `opencli` local backend.
+  - Windows-safe arg quoting: double-quote multi-word titles (shlex's single
+    quotes break Node/Commander); rejoin trailing tokens into one quoted arg.
+- `TOOL_MAP["run_opencli"]` + both model schemas (`tools.TOOLS`,
+  `brain_gemini.TOOL_DECLARATIONS`).
+
+### Verification gate — ALL PASS (fresh run, exit 0, 16 checks)
+`scripts/hermes-verify-phase36-durable.py` (on-disk, re-runnable; gitignored
+like the other phase gates):
+- A. tools.py + brain_gemini.py compile clean
+- B. run_opencli registered in TOOL_MAP + both schemas
+- C. _detect_backend: 'use opencli to scrape reddit' / 'scrape github trending'
+  / 'read my facebook feed' → opencli; 'open notepad' → desktop (not hijacked);
+  'search the web for cats' → web (not hijacked)
+- D. _dispatch_opencli maps phrases → correct `opencli <site> <sub>` commands
+- E. gate: first call NEEDS_CONFIRM; wrong-task confirm rejected (latch);
+  credential/payment phrase blocked even with confirm
+- F. LIVE public read `opencli wikipedia summary "Web scraping"` → real
+  extracted content, rc=0 (daemon serves public sites without the extension)
+
+GATE CAUGHT 5 REAL BUGS (all fixed before green): (1) doubled site name in
+query tail; (2) wikipedia defaulted to `feed` (no such sub) instead of
+`summary`; (3) `--window` flag invalid for site adapters (only `browser`
+primitive); (4) `shlex.quote` single-quotes broke Node arg parsing → switched
+to double-quote; (5) unquoted multi-word titles split into N args → rejoin
+trailing tokens. Each fix verified by re-run.
+
+### Runtime status (honest)
+- `opencli` v1.8.7 installed globally on the host; daemon runs.
+- **Public-site reads work NOW** (verified live: wikipedia).
+- **Logged-in site actions (facebook/reddit/etc.) are NOT yet executable**:
+  `opencli doctor` shows "Extension: not connected" — the user must install
+  the OpenCLI Chrome Bridge extension (Chrome Web Store or unpacked release)
+  and connect it. Until then, logged-in commands return a connectivity error
+  (honest, not a silent failure). This is the user's manual install step.
+- 166 site adapters + 10 app adapters + 13 external CLIs available.

### Phase 19 (2026-08-26): `oc` web-browse tool wired into JARVIS + Hermes skill

**Trigger:** User shared a Facebook video pitching `ultimatewebscraper.com`, traced to
the open-source `oc` CLI (only-cli/oc, MIT, ~300 stars) — a token-cheap web reader that
turns a page into a compact numbered view instead of raw HTML. Wired it into both JARVIS
(local fast path) and as a Hermes skill (for my own scrape prompts).

**What was built:**
- `tools.web_browse(url, mode, query)`: shells to the local `oc` CLI (resolved via
  `shutil.which("oc")` or the npm `.CMD` path, `shell=True`). modes: `open` (numbered
  view, default), `raw` (whole-page markdown), `read` (deep region). Honest `[Error]`
  on oc-missing / login-walled / blocked pages — no fake results.
- Registered in BOTH layers (mandatory two-layer rule): `TOOL_MAP` entry + `TOOL_DECLARATIONS`
  entry (`web_browse`) in brain_gemini.py.
- Added to `execute_tool`'s quarantine set so `oc` page text is treated as DATA (guard.py),
  not instructions (prompt-injection guard — matches oc's own posture).
- `classify_intent`: new `web_browse` intent for scrape/read/browse/extract/summarize +
  URL; placed BEFORE `general` and excludes pure `search`/`open site` phrasing.
- `think()`: new fast local branch (before the Hermes delegation) routes `web_browse`
  intent to `execute_tool("web_browse", ...)` instantly — no 22s cloud round-trip. Falls
  back to Hermes only if the local read fails.
- Hermes skill `development/oc-web-scraper/SKILL.md`: directs ME (Hermes) to use the local
  `oc` CLI for scrape/read-a-page prompts (the managed `web_search`/`web_extract` tools are
  Firecrawl-gated and unavailable without Nous Portal credits).

**Verification (mandatory discipline):**
- Durable gate `scripts/hermes-verify-phase19-oc-durable.py`: **ALL PASS (8/8), exit 0**
  (registration in both layers, intent routing incl. negative `search` case, live `web_browse`
  open+raw against example.com, guard quarantine preserves content).
- **Live WS turn** `ws://localhost:8000/ws` "scrape https://example.com and tell me the
  page title" → `oc_content=True | refused=False` — returned the real `# Example Domain`
  numbered view from `oc`. No cloud round-trip.
- 2 bug-fixes made during wiring (caught by live log, not shipped broken): (1) `_tools_mod`
  referenced before import in the think() branch; (2) `execute_tool` called with a stray
  3rd positional arg (signature is 2-arg). Both fixed; gate + live turn re-greened.

**Runtime status (honest):**
- `oc` v0.5.0 installed globally; Chrome-impersonation optional dep (`impers`) removed so
  the native-fetch fallback engages (hard bot-walls not bypassed — same as base stack).
- Public pages: works NOW (verified live). Login-walled pages (Facebook/Claude/etc.):
  returns honest `[Error]`, same limit as base stack.
- JARVIS server restarted (watcher), running fresh with Phase 19 code.

**Open items / user decisions:**
- Voice UX: how should JARVIS *speak* a numbered `oc` view? Options: read the summary
  line, read top N items, or say "I pulled the page — here's the gist" + dump to Notepad.
  Not yet decided — current path returns the view text to the cloud brain which summarizes.
- Authed pages: if the user wants `oc login --cookie` wired for specific sites, that's a
  follow-up (cookie handling + storage decision needed).

### Phase 20 (2026-08-26): Honcho as self-hosted memory augmentation layer for JARVIS

**Trigger:** User evaluated honcho.dev as JARVIS memory. Verified (read-only) that Honcho
is open-source (AGPL-3.0), self-hostable via Docker, and ships a native Hermes integration
(`hermes memory setup` -> select "honcho" -> point at api.honcho.dev OR local server).
Resolves the earlier SaaS-vs-self-host objection: if self-hosted, all data stays on-box.

**VERIFIED FACTS (read-only investigation, no code changes yet):**
- Honcho = "open source memory library with a managed service." Self-host: `git clone
  plastic-labs/honcho`, `cp docker-compose.yml.example docker-compose.yml`, fill `.env`,
  `docker compose up` -> serves `http://localhost:8000`. SDK: `pip install honcho-ai`,
  `Honcho(workspace_id=..., base_url="http://localhost:8000")`.
- LLM requirement (from .env.template, authoritative): "Supported transports: openai,
  anthropic, gemini." Exactly ONE LLM_* key is mandatory ("server will fail to start
  without a provider"); defaults to OpenAI but Gemini is a first-class option.
  CONSTRAINT: "Models must support tool calling (function calling)." -> JARVIS's
  GEMINI_MODEL = `gemini-2.5-flash` DOES support function calling. So Honcho's reasoning
  tier can run on the SAME existing Gemini key + model. No new API account needed.
- Hermes integration is for NOUS Hermes (user's runtime), not a namesake: `hermes memory
  setup` selects "honcho" and points at local server. Plus `honcho-memory` agent skill.
- Migration bridge: Honcho can import existing MEMORY.md/USER.md/IDENTITY.md (non-destructive).
  JARVIS's `jarvis-profile.md` is exactly that shape -> direct import path.
- API shape: peer -> session -> add_messages() [store] -> background Neuromancer reasoning
  -> session.context(tokens=...) / peer.chat(...) [query] -> inject into model. Replaces
  JARVIS's current "dump whole jarvis-profile.md into every prompt" with budgeted, reasoned
  context (the token win, complementary to Phase 19 `oc` which attacks web-read tokens).

**Prerequisites CONFIRMED on host:**
- Docker 29.6.1 + Compose v5.2.0: available.
- GEMINI_MODEL = gemini-2.5-flash (function-calling capable): available.

**DESIGN DECISION (not yet built):**
- Honcho is an AUGMENTATION layer, NOT a replacement of the system of record.
  - Keep `remember_fact` -> deterministic append to `jarvis-profile.md` (certain, auditable,
    offline). ALSO mirror the fact into Honcho (`session.add_messages`) so it gets reasoned.
  - Replace Phase 18 "always-true full profile inject" with a budgeted `session.context(
    tokens=...)` call per turn (fewer tokens, richer relevance). Keep `jarvis-profile.md`
    as fallback if Honcho is down.
- Privacy: self-hosted -> data on-box, no training exposure. "Peers" model = humans+agents
  first-class; only enable cross-agent peer sharing deliberately, not by default.

**PLANNED WORK (Phases 20a-20c) — NOT STARTED:**
- 20a: Self-host Honcho locally (Docker), Gemini reasoning tier, `localhost:8000`, smoke test
  `session.context()` returns reasoned summary. Verify Honcho runs with Gemini-only (no
  OpenAI key) — this is the ONE remaining unknown (template says gemini supported, but the
  default example uses OpenAI; must confirm a Gemini-only .env boots cleanly).
- 20b: JARVIS augmentation — `remember_fact` mirrors to Honcho; new `get_memory_context()`
  tool wraps `session.context(tokens=...)`; wire into brain_gemini.py persistent-memory
  injection (Phase 18 site) as the primary source, `jarvis-profile.md` as fallback.
- 20c: Hermes skill `honcho-memory` wiring for MY (Hermes) use; decide voice UX for memory
  recall (read summary vs top-N).

**VERIFICATION DISCIPLINE (carried from Phase 19):**
- Edit-splice scripts via Python (NOT patch()) to avoid corrupting tools.py/brain_gemini.py.
- Durable gate script `scripts/hermes-verify-phase20-*.py` (kept on-disk per convention).
- Live WS turn proving memory recall routes through Honcho context, not raw profile dump.
- Restart server after edits (watcher auto-restarts; confirm new PID + startup complete).
- Ad-hoc re-verify of changed behavior (NOT project suite green — no canonical suite exists).

**OPEN ITEMS / USER DECISIONS:**
- Confirm Gemini-only Honcho .env boots (20a first step).
- Docker Desktop running on this Windows host? (docker CLI present; daemon must be up to
  `compose up`.)
- Cross-agent "Peers" sharing: opt-in only, default off.
- Voice UX for memory recall (same open question as Phase 19).

### Phase 20b (2026-08-26 reconciliation): PENDING-phase code audit

**Triggered by:** user asked to reconcile PENDING phases against actual code. Done read-only
(grep of tools.py / brain_gemini.py / autonomous.py / jobs.py). Result: the plan's
"PENDING" labels for Phases 15/16/17/18 are STALE — all four are implemented and wired in
code. The real gap is verification evidence (durable gate + live turn), not missing code.

**Evidence found (grep, not memory):**
- P15 autonomous runner: `run_autonomous()` (tools.py:2135) + `autonomous.py` module +
  `jobs.py` + `think()` routes `_is_multistep_goal` to it + allowlist NEEDS_CONFIRM gate.
  -> CODE PRESENT.
- P16 voice job control: `stop_music`/`stop_spotify` in TOOL_MAP; `job_control()` (tools.py:2156);
  `think()` stop/close_app branches; pause at tools.py:3137. -> CODE PRESENT.
- P17 evidence-gated + auto-retry: `autonomous.py` `_verify_step()` (line 197) re-checks the
  artifact itself; rejects `RESULT: done` without matching `[STEP] done` evidence lines
  (lines 78, 182, 261, 299, 336); `MAX_TURNS=40`. -> CODE PRESENT (real gate, not stub).
- P18 durable memory: `remember_fact()` (tools.py:2182) live in TOOL_MAP; writes
  `jarvis-profile.md` (## Remembered, deduped, capped _REMEMBER_CAP=40); injected into
  brain_gemini persistent-memory context. -> CODE PRESENT AND WIRED.
- App-lookup audit (2026-08-25, PENDING FIX): `open_application` (tools.py:2635) now resolves
  via `machine_capabilities.resolve()` + APP_ALIASES, no junk startfile (Phase 14b purge
  applied). -> FIXED IN CODE.

**CORRECTED STATUS (supersedes the PENDING labels above):**
- Phase 7: intentional umbrella, not a build item.
- Phases 15, 16, 17, 18: IMPLEMENTED IN CODE. Status = "DONE (code present; durable
  verify-gate NOT yet run)". To flip to fully DONE, run the Phase-19-style gate for each.
- App-lookup audit: RESOLVED in code.

**CONCLUSION:** Zero phases are truly missing/unbuilt. The only honest gap is verification
evidence for 15/16/17/18 (no green durable gate on record). Next advisable step before any
new build (Phase 20): run the missing verify-gates for 15/16/17/18 so the plan reflects
reality, OR proceed to Phase 20a (self-host Honcho) since that is independent new work.

### Phase 20a (2026-08-26): Honcho self-hosted — Gemini-only boot TESTED & PASS

**Riskiest unknown resolved:** Does Honcho boot with Gemini-only (no OpenAI key)?
**ANSWER: YES.** Verified end-to-end against the live local server.

**Setup performed (read-only intent, then executed):**
- Cloned plastic-labs/honcho -> `C:\Users\deped\Documents\honcho` (separate from jarvis-demo).
- `docker compose up -d`: 4 containers — database (pgvector:pg15), redis, api, deriver.
  All report healthy. API health `http://localhost:8000/health` -> `{"status":"ok"}`.
- `.env` built GEMINI-ONLY: `LLM_GEMINI_API_KEY` (reused JARVIS's exported GEMINI_API_KEY,
  len 53; NOT hardcoded — pulled from env at build time), and every module transport
  forced to gemini: LLM_DEFAULT_TRANSPORT, DERIVER_MODEL_CONFIG__TRANSPORT,
  DIALECTIC_LEVELS__{minimal,low,medium,high,max}__MODEL_CONFIG__TRANSPORT,
  EMBEDDING_MODEL_CONFIG__TRANSPORT=gemini. Models pinned to gemini-2.5-flash (same
  function-calling model JARVIS uses); embeddings auto-default to gemini-embedding-001
  (confirmed in src/config.py `_default_embedding_model_for_transport("gemini")`).
- Bogus `LLM_OPENAI_API_KEY=your-api-key-here` removed so no openai fallback is attempted.

**PORT CONFLICT RESOLVED:** Native Postgres already owns host 127.0.0.1:5432 (PID 8064).
Honcho's compose mapped `127.0.0.1:5432:5432` -> COLLISION. Fix: remapped Honcho Postgres
host port to `127.0.0.1:5433:5432` in docker-compose.yml (container-internal stays 5432;
API/Deriver reach DB via docker-network hostname `database:5432`, unaffected). Native PG
left untouched. NOTE: if connecting to Honcho's PG from the host, use port 5433.

**E2E PROOF (honcho-ai SDK 2.3.0, base_url=http://localhost:8000):**
- Stored: peer "alice" + session "s1" + message "I prefer morning meetings and I'm based
  in Manila, Philippines."
- `peer.chat("What timezone is the user in and when do they like meetings?")` ->
  "Alice is based in Manila, Philippines, which is in the Philippine Standard Time (PST)
  zone (UTC+8). She prefers morning meetings."  -> Gemini reasoning + retrieval WORKING,
  NO OpenAI key involved.
- Embeddings worked (message embedded + retrieved via context()).
- CONCLUSION: Gemini-only Honcho is viable as JARVIS's memory layer. The earlier worried
  "Gemini-only may need OpenAI" hypothesis was FALSE — code supports gemini embeddings.

**Status: Phase 20a COMPLETE (verified live).** Stack is persistent (restart: unless-stopped).
Next: Phase 20b (wire JARVIS augmentation: remember_fact mirror + get_memory_context()).

### Phase 20b (2026-08-26): JARVIS durable memory wired into self-hosted Honcho

**Executed via /opencode** (user standing rule: coding tasks -> OpenCode). Dispatched with
`opencode run --model opencode-go/hy3` from the jarvis-demo project dir (NOT home; OS-
redirect to file, no PIPE — avoids the documented deadlock). NOTE: correct Go namespace is
`opencode-go/hy3`, not `opencode/hy3` (the latter is unregistered -> ProviderModelNotFound).
OpenCode rc=0, did its own verify; Hermes then RE-VERIFIED independently (no trust of self-report).

**Changes (tools.py + brain_gemini.py), all verified on disk:**
- `tools._get_honcho()` lazy cached Honcho client -> http://localhost:8000, workspace "jarvis",
  peer "user". Returns None on any failure (sentinel False after first try).
- `tools.remember_in_honcho(fact)` best-effort mirror; NEVER raises. Called from `remember_fact`
  AFTER the deterministic jarvis-profile.md write (system of record preserved).
- `tools.get_memory_context(tokens=4000)` -> Honcho `session.context(summary=True)`; on ANY
  failure falls back to reading jarvis-profile.md. Output is memory DATA (quarantined in
  execute_tool's tuple, alongside web_browse).
- TOOL_MAP entry `"get_memory_context"` + added to execute_tool quarantine set.
- brain_gemini.py: `get_memory_context` TOOL_DECLARATIONS entry + wired into the Phase 18
  persistent-memory injection (Honcho context augmented ABOVE the existing profile read;
  profile read preserved as fallback).
- remember_fact's confirmation string untouched; jarvis-profile.md content never modified by
  the augmentation.

**VERIFICATION (Hermes durable gate scripts/hermes-verify-phase20b.py): ALL PASS 9/9, exit 0:**
- symbols _get_honcho / remember_in_honcho / get_memory_context defined (no spec drift).
- remember_fact calls remember_in_honcho(fact).
- get_memory_context in TOOL_MAP + quarantine set + TOOL_DECLARATIONS.
- brain injects get_memory_context.
- LIVE round-trip: remember_fact("<unique fact>") -> get_memory_context() returned it from
  Honcho (proves mirror + retrieve work). Test token cleaned from jarvis-profile.md after.

**Status: Phase 20b COMPLETE (verified live).** Honcho is now JARVIS's reasoned memory
augmentation layer; jarvis-profile.md remains the offline system of record.
Next optional: Phase 20c — Hermes `honcho-memory` skill + voice UX for memory recall.
