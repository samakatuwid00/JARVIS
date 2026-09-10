# JARVIS — Production Readiness Review

**Date:** 2026-09-09
**Reviewer:** Claude Code (delegated from Hermes)
**Author:** Roger Abay Jr
**Goal:** Open-source release to student community

---

## Executive Summary

JARVIS is a voice-activated AI assistant with impressive depth for a personal project — local Whisper STT, edge-tts TTS, multi-backend LLM routing (9router/Groq/Cerebras/Ollama/Gemini), semantic memory via SQLite + vectors, a rules engine for per-app constraints, and an app registry that scans 854 installed apps. The codebase is ~86 Python files with real engineering depth in config.py (161 lines of documented, tuned parameters) and tools.py (4,268 lines with audit trails, quarantine logic, and thread-safety).

**However, it is NOT production-ready for open-source release.** The project suffers from version drift (README says Gemini, code uses Claude/9router), dead code paths (rules_engine.py is imported nowhere), half-documented features, 2175-line brain_gemini.py with no test coverage, and no packaging story. A student downloading this today would hit API key confusion, dependency version mismatches, and features that don't work as advertised before getting anything running.

---

## 1. Semantic Searching — FINDINGS

### What works
- `semantic_memory.py` is a solid SQLite + FTS5 + cosine-similarity implementation with temporal validity (supersede-on-contradiction), provenance tracking, and reinforcement signals (hits, last_hit, confidence decay). Design is thoughtful: NOOP at cosine >= 0.985, SUPERSEDE at >= 0.90, ADD otherwise. All failures fail open.
- `tools.py` has `search_vault_semantic()` that integrates with the semantic memory, plus a substring fallback.
- `context_assembler.py` pulls vault context into briefs for delegates with per-delegate token budgets (hermes: 2400, opencode: 1800, cli: 1200).

### What's broken / missing
- **The embedding model is never initialized.** `semantic_memory.set_embedder()` exists but nothing in the codebase calls it. Without an embedder, all semantic search falls back to keyword FTS5 — the vector cosine path is dead.
- **No `SentenceTransformer` dependency in requirements.txt.** The semantic memory code imports numpy but the actual embedding model (likely `sentence-transformers`) is not listed as a dependency. Students can't install what's missing.
- **TurboVec mention without integration.** `context_assembler.py` references "turbovec" in `_VAULT_HINT` but the actual semantic search calls `search_vault_semantic()` from tools.py which has its own embedding logic. The two systems are not wired together.
- **`search_vault_semantic` availability is conditional.** `context_assembler.py` checks `hasattr(t, "search_vault_semantic")` — this function exists in tools.py but its availability depends on runtime imports that may fail silently.
- **No evaluation of retrieval quality.** There's no test showing what gets retrieved for what query, no precision/recall measurement, no demonstration that the semantic layer actually improves over keyword search.
- **The vault hint is hardcoded to your personal path.** `_VAULT_HINT = "Second Brain vault at C:/Users/deped/Documents/Second Brain..."` — this path is YOUR machine. For open-source, this must be configurable or removed.

### Production-readiness verdict: PARTIAL
The semantic memory design is good but the embedding pipeline is unwired. Students get keyword search, not semantic search, unless they manually set up an embedder. The hardcoded personal path also breaks for any other user.

---

## 2. Memory Context — FINDINGS

### What works
- `session_store.py` is well-designed: append-only JSONL per day, rolling in-memory window (20 turns), FTS5 indexing via `session_index.py`, fails open on all errors. Clean separation from Hermes's own memory.
- `conversation_window.py` provides fragment completion and clarify-answer tracking for the local fast path — clever feature for multi-turn voice interactions.
- `context_assembler.py` assembles briefs from 4 tiers: profile, recent window, vault semantic, app context — with delegate-specific formatting.
- `brain_gemini.py` has a fast path (P1) that resolves math/time/greetings locally without any cloud call — significant latency win.

### What's broken / missing
- **Memory is per-process, not cross-session by default.** The in-memory window (`_recent` in session_store.py, `_turns` in conversation_window.py) is lost on restart. Only the JSONL files survive — but nothing automatically replays them into the next session's context. The "context across sessions" the user wants requires the next session to explicitly load prior JSONL, which doesn't happen automatically.
- **IDLE_RESET_MINUTES = 10** means context silently clears after 10 minutes of inactivity. For voice interaction this may be too aggressive — a user walking away for 11 minutes loses all context.
- **LOCAL_HISTORY_TOKEN_BUDGET = 3500** for Ollama is tight. The system prompt + 24 tool schemas already consume ~3.7k tokens of the 8192 window, leaving only -100 tokens for history before truncation kicks in. This means **local turns have effectively zero conversation history** — every turn is stateless. Measured behavior confirms: fresh turn 8.8s, 1.9k history 45.7s, 11.5k history 162.4s (times out).
- **`brain_gemini.py` think() is 2,175 lines with no test coverage.** The multi-backend routing logic (9router → Groq → Cerebras → Ollama → Gemini → demo), consent policy, tool result handling, progress callbacks — all untested. This is the core of JARVIS and it has zero verification.
- **Context assembler can silently return empty.** Every tier in `assemble_brief()` is wrapped in try/except that returns "" on failure. A student with a misconfigured path gets an empty brief and no indication why.
- **No context visualization.** There's no `/context` command like Claude Code has. Users can't see how much context is being used or when truncation kicks in.

### Production-readiness verdict: PARTIAL
Session storage is solid. Cross-session context continuity is broken (no auto-replay of prior sessions). The local backend is effectively stateless due to token budget math. Core think() logic is untested. These are fundamental issues for a "memory" feature.

---

## 3. App Features — FINDINGS

### What works
- `app_registry.json` auto-generates with 854 detected apps, including bin paths, categories, confidence scores, and versions. This is impressive discovery work.
- `tools.py` has `install_app()`, `uninstall_app()`, `open_application()`, `app_lookup()` — real app lifecycle management.
- `rules_engine.py` + `rules_compiler.py` provide per-app rule enforcement (hard/soft, launch/close scoped).
- `apps_panel.html` is a polished web UI for managing apps and rules — dark theme, app cards, rule editors, toggle/install/open buttons. Looks production-quality visually.
- `rules_engine.py` supports action-scoped rules (pre_play/pre_launch block launch but not close; pre_close blocks close but not launch) — thoughtful design preventing false blocks.

### What's broken / missing
- **rules_engine.py is DEAD CODE.** `grep -rn "rules_engine" --include="*.py"` finds it ONLY in rules_engine.py itself and rules_engine_test.py. Nothing imports it. The app rules system exists but is never called at runtime. `tools.py` open_application does NOT call evaluate_app_rules(). The entire rules feature is disconnected from the app launch flow.
- **install_app() is scaffolding, not implemented.** Reading the install_app section of tools.py (lines 3289-3400) shows it's a shell — the actual installation logic for arbitrary apps is not implemented. It can install from known package managers but has no generic "install any app from URL/repo" capability.
- **"reads apps and creates scripts to operate them" is aspirational.** There's no code that reads an installed app's capabilities and generates operation scripts. The app registry stores what's installed but doesn't introspect what each app CAN do. Voice commands like "maximize Spotify" would need JARVIS to know Spotify's CLI/media key interface — this mapping doesn't exist programmatically.
- **App capability listing is manual, not automatic.** `apps_panel.html` shows app cards but the "what I can do" list per app would need to be hand-authored or generated from some capability manifest. No such manifest exists in the codebase.
- **`app_registry.json` is user-specific.** Paths like `C:\Program Files\Git\mingw64\bin\git.EXE` and `C:\Users\deped\AppData\Local\hermes\...` are YOUR machine. For open-source, this must be regenerated per-install or made relative.
- **`capabilities.json` is stale.** Generated 2026-08-22, app_registry.json is from 2026-08-28. Two competing registries with different timestamps — confusing which is authoritative.

### Production-readiness verdict: NOT READY
The app discovery (registry) is excellent. But the app automation (install/operate/script generation) is mostly scaffold. The rules engine is built but disconnected from the launch flow. A student would see apps listed but find that "install this app" and "what can this app do" don't actually work.

---

## 4. Rules From User Input — FINDINGS

### What works
- `rules_compiler.py` parses natural language ("keep it quiet after 10pm", "never play explicit") into structured rules with clause splitting (handles "and/then/also/,/;"), ambiguity detection (quiet/calm/appropriate markers), and hard-rule detection (never/even if i ask/always block).
- `_is_ambiguous()` is a smart feature — detects vague rules like "keep it quiet" and flags them for clarification rather than silently compiling a useless rule.
- `rules_engine.py` evaluates compiled rules at runtime with action-scoping (launch vs close), hard vs soft enforcement, and returns structured results (allowed, notices, blocks).
- `rules_compiler_test.py` and `rules_engine_test.py` exist — rare in this codebase, good sign.

### What's broken / missing
- **Only ONE rule type is implemented: explicit content filtering.** `parse_scaffold()` only has a special case for "explicit" — everything else falls through to the generic clause handler that sets `needs_clarification: True` with a generic question. The compiler can't actually compile most user rules into actionable adapter_check values.
- **adapter_check values are not standardized.** The compiler generates strings like `"pre_play: skip if track.explicit"` but there's no schema for what adapter_check values mean, no registry of available checks, and no validation that a generated check is implementable.
- **No adapter/hook system exists.** `rules_engine.py` evaluates rules but `tools.py` never calls it. There's no mechanism for rules to actually affect app behavior because there are no adapter hooks in the app operations that rules could plug into.
- **Scope is limited to "all_sessions / current_session / app_only"** but current_session is never persisted — it's set per-compilation and lost after the setup session. It has no runtime effect.
- **No rule versioning or rollback.** If a user compiles rules and something breaks, there's no way to revert to a previous rule set.
- **Only Spotify is demonstrated.** The rules system is show-cased only for Spotify explicit-content filtering. No other app has rule examples, so students won't know how to use it for other apps.
- **`_is_ambiguous()` only covers volume-related vagueness.** Words like "appropriate", "calm", "chill" are flagged, but other ambiguous phrasings ("make it nice", "do the right thing", "be careful") are not detected.

### Production-readiness verdict: NOT READY
The compiler can parse natural language and detect some ambiguity — good start. But it only produces actionable rules for one case (explicit content) and the entire enforcement pipeline is disconnected from the app runtime. For open-source, this feature would confuse users: they can "set rules" but nothing happens when they use apps.

---

## 5. Production Readiness Assessment

### Code Quality & Structure
- **Mixed.** config.py is modelship (documented, tuned, thoughtful). tools.py is 4,268 lines — too large, should be split into modules (app_ops.py, file_ops.py, shell_ops.py, web_ops.py, media_ops.py). brain_gemini.py at 2,175 lines is also too large.
- Good patterns: audit trail (audit.jsonl), quarantine logic for external text, thread-local progress callbacks, fail-open design throughout.
- Bad patterns: circular imports possible (context_assembler imports tools which may import context_assembler), bare `except Exception: pass` in many places, no type hints on most functions.

### Security
- **run_shell() in tools.py executes arbitrary commands.** This is the core feature but also the biggest risk. There's a `confirm` gate in some paths but no comprehensive sandboxing. A student deploying this could accidentally (or maliciously via prompt injection) run destructive commands.
- **API keys in .env** — standard practice, but .env.example exists? Need to verify. .gitignore must exclude .env — verify this is set.
- **`guard.sanitize_web_text()` exists** for quarantining browser/chat text — good, but its coverage is unclear. Does it cover all external input paths?
- **No input length limits** on shell commands or file paths — a maliciously long input could cause issues.

### Reliability
- **Fail-open everywhere** — good for stability, bad for debuggability. When something fails, JARVIS silently continues with reduced capability. No logging of failures beyond audit.jsonl (which redacts content).
- **No health check endpoint.** There's no `/health` or status API that external dashboards could poll.
- **No graceful shutdown.** KeyboardInterrupt handling in jarvis.py is minimal — in-progress tool calls may be left dangling.
- **Ollama timeout at 180s** — if the local model is cold, a turn can hang for 3 minutes before failing. No user feedback during this wait (except voice cue system which is separate).

### Documentation
- **README is outdated.** Says "Powered by Google Gemini (FREE)" but the actual brain is multi-backend (9router/mimo primary, Gemini fallback). The architecture diagram in README doesn't match the code.
- **No setup guide for students.** README has quick-start but no troubleshooting, no platform-specific notes (Windows-only? Linux?), no explanation of the backend ecosystem (9router, Groq, Cerebras, Ollama — students won't know what these are).
- **No API documentation.** Tools, brain interface, voice engine — no docstrings on public APIs, no separate docs folder.
- **CLAUDE_TASK.md and HARNESS_PLAN.md exist** but are task-specific notes, not user-facing docs.

### Testing
- **Sparse.** rules_engine_test.py, rules_compiler_test.py, semantic_memory_test.py, voice_feedback_test.py, music_rules_test.py, intake_test.py exist — but these cover maybe 5% of the codebase.
- **No tests for:** brain_gemini.py (the core), tools.py (the largest module), voice_engine.py (Whisper/TTS), jarvis.py (the entry point), context_assembler.py, session_store.py.
- **No CI.** No .github/workflows, no pytest config, no GitHub Actions.
- **Test style is inconsistent.** Some tests use pytest patterns, others use bare asserts.

### Packaging
- **requirements.txt exists but is incomplete.** Missing: sentence-transformers (for semantic embeddings), session_index dependencies, any dependencies for the web panel.
- **No setup.py / pyproject.toml.** Can't `pip install jarvis`. No version number. No entry points defined.
- **No Docker.** Despite docker.exe being in the app registry, there's no Dockerfile for JARVIS itself.
- **No distribution packaging.** No wheel, no sdist, no PyPI plan.
- **Windows-specific paths throughout.** app_registry.json has Windows paths. config.py has Windows .env paths. Cross-platform support is unclear.

---

## 6. Prioritized Fix List

### P0 — Block release (must fix before students can use this)

1. **Fix README to match reality.** Current README says Gemini-only, Free API. Actual code is multi-backend with 9router primary. Students following README will be confused when code doesn't match. Rewrite README with accurate architecture, backend setup, and honest feature list.

2. **Wire the semantic embedder.** Either add `sentence-transformers` to requirements.txt and initialize it in brain_gemini.py startup, or remove the semantic_memory vector path and document that only FTS5 keyword search is available. Don't leave dead code paths that look functional.

3. **Remove or fix hardcoded personal paths.** `_VAULT_HINT` in context_assembler.py points to YOUR Second Brain. app_registry.json has YOUR app paths. config.py references YOUR schema-mapper .env path. These must be configurable or removed for open-source.

4. **Connect rules_engine to app launch flow.** Either call `evaluate_app_rules()` from `open_application()` in tools.py, or remove rules_engine.py and apps_panel.html rules UI. Don't ship a rules feature that doesn't enforce anything.

### P1 — Important for student experience

5. **Split tools.py into modules.** 4,268 lines is too much. Split into: app_ops.py, file_ops.py, shell_ops.py, web_ops.py, media_ops.py, system_ops.py. Same for brain_gemini.py (brain_router.py, brain_fastpath.py, brain_toolhandler.py).

6. **Add cross-session context replay.** On startup, load prior session JSONL files into the in-memory window so context survives restarts. Currently each session starts fresh.

7. **Fix LOCAL_HISTORY_TOKEN_BUDGET math.** With 3.7k consumed by system prompt + tool schemas before any history, the 3500 budget is negative. Either increase Ollama context window (rebuild model with higher num_ctx) or reduce tool schema footprint.

8. **Add .env.example with all keys documented.** Currently students won't know which API keys are needed for which backend. Document: GEMINI_API_KEY (optional), ROUTER_* (9router), GROQ_API_KEY (optional), CEREBRAS_API_KEY (optional), OLLAMA (local, no key needed), PORCUPINE_ACCESS_KEY (optional wake word).

9. **Add basic tests for brain_gemini.py think() path.** At minimum: test that fast-path intents (math, time, greeting) return without cloud call, test that tool-use rounds trip correctly, test multi-backend fallback order.

### P2 — Polish for release

10. **Add version number.** pyproject.toml with version, or at minimum a `__version__` in a module. Currently no version anywhere.

11. **Add packaging.** pyproject.toml, `pip install -e .` support, entry point for `jarvis` command.

12. **Add CI.** GitHub Actions workflow: lint (ruff), typecheck (mypy optional), test (pytest), on push to main.

13. **Document the app capability model.** If "maximize app usage via voice" is a goal, define how app capabilities are described (JSON manifest per app type?), how scripts are generated, and what apps are supported out of the box.

14. **Add health check / status API.** A `/status` endpoint or CLI command that shows: which backend is active, latency, context window usage, session state, app registry health.

15. **Cross-platform audit.** Identify all Windows-specific code (paths, shell commands, audio APIs) and either abstract or document platform requirements.

---

## 7. Layman's Summary

### What JARVIS actually is right now

JARVIS is a voice-controlled AI assistant that runs on your computer. You say "Hey JARVIS" and it listens, transcribes your voice using Whisper (runs locally on your CPU), sends what you said to an AI model (tries several free ones in order), and responds with voice synthesized through edge-tts (Microsoft's free TTS service). It can open apps, run commands, search the web, remember facts you tell it, and follow rules you set (like "don't play explicit music").

It's built by one person (Roger) and runs on his machine. It knows about 854 apps installed on his computer. It has a nice web panel for managing apps and rules. The code has real engineering depth — config tuning, audit trails, fail-safe design.

### What's missing for production

1. **The README lies.** It tells students JARVIS uses Gemini's free API. The actual code uses a completely different setup with multiple backends. Students following the README will fail.

2. **The semantic search doesn't work out of the box.** The code for vector-based semantic memory exists but the embedding model isn't installed or initialized. Students get keyword search, not the semantic search the code promises.

3. **The rules feature is built but disconnected.** Students can set rules for apps (like "no explicit content") but the rules don't actually get enforced when apps run. The rule-checking code exists but nothing calls it.

4. **App automation is partial.** JARVIS can list your apps and open them. But "install any app I name" and "figure out what this app can do and script it" are not fully implemented — they're scaffolding.

5. **Memory doesn't survive restarts well.** JARVIS remembers your conversation during a session, but when you restart it, the context is largely gone. The storage exists but isn't replayed into the new session.

6. **It's Windows-only and machine-specific.** Paths, configs, and the app registry are all tailored to Roger's specific Windows machine. A student on Linux or even another Windows machine would need significant setup.

7. **No tests for the core.** The most important file (brain_gemini.py — the AI brain) has zero tests. If it breaks, there's no safety net.

8. **It's not pip-installable.** Students can't `pip install jarvis`. They'd need to clone the repo, manually install dependencies (some missing from requirements.txt), configure .env files, and figure out the backend setup from code, not docs.

### What a student would experience downloading this today

They'd clone the repo, read the README, try to follow the "get your free Gemini API key" instructions, run `python jarvis.py`, and... the code would try to connect to a local 9router proxy that doesn't exist on their machine, fall through to Groq/Cerebras (which also need keys they don't have), potentially reach Ollama (which they may not have installed), and finally fall back to Gemini (if they got the API key). The semantic memory wouldn't work. The rules panel would let them set rules that don't do anything. The app registry would be empty (it's pre-generated for Roger's machine). They'd spend hours debugging before getting a basic "hello" response.

**Bottom line:** JARVIS has excellent bones and real engineering depth, but it's a personal project, not a student-ready open-source package. The code works for its author. For anyone else, it needs the P0 fixes above before it's usable, and the P1/P2 fixes to be genuinely good.

---

*End of review.*
