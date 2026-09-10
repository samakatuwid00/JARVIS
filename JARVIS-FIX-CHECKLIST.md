# JARVIS — P0/P1 Fix Checklist

**Generated:** 2026-09-09 from production readiness review
**Goal:** Open-source release to student community

---

## P0 — Block Release (must fix first)

These block students from using JARVIS at all.

### P0-1: Fix README to match actual code

**Problem:** README says "Powered by Google Gemini (FREE)" with a simple architecture diagram. Actual code uses 9router/mimo as primary, with Groq/Cerebras/Ollama/Gemini fallback chain. Students following README will be confused.

**Files:**
- `README.md` — rewrite entirely

**Action:** Rewrite README with:
- Accurate architecture (multi-backend routing chain)
- Which backends are required vs optional
- Honest feature list (what works, what's partial)
- Setup sections per backend option
- Screenshot of apps_panel.html

---

### P0-2: Wire or remove semantic embeddings

**Problem:** `semantic_memory.py` has vector cosine search but no embedder is initialized. `set_embedder()` exists but nothing calls it. `requirements.txt` doesn't list `sentence-transformers`. Students get keyword FTS5 search only — the semantic path is dead code.

**Files:**
- `semantic_memory.py` — either initialize embedder or document keyword-only mode
- `brain_gemini.py` — call `set_embedder()` at startup OR remove the semantic path
- `requirements.txt` — add `sentence-transformers` if keeping semantic, OR remove all vector references
- `tools.py` — `search_vault_semantic()` needs embedder wired

**Action (option A — keep semantic):**
1. Add `sentence-transformers` to requirements.txt
2. In brain_gemini.py startup, load a lightweight model (e.g. `all-MiniLM-L6-v2`) and call `semantic_memory.set_embedder()`
3. Test that `search_vault_semantic()` returns results

**Action (option B — remove):**
1. Replace `search_vault_semantic()` calls with `search_vault()` (FTS5 keyword)
2. Remove vector-related fields from semantic_memory.py facts table
3. Update docs to say "keyword search" not "semantic search"

---

### P0-3: Remove/replace hardcoded personal paths

**Problem:** Multiple files reference YOUR machine specifically:
- `context_assembler.py` line 22: `_VAULT_HINT = "Second Brain vault at C:/Users/deped/Documents/Second Brain..."`
- `config.py` lines 37, 60: `_GROQ_ENV_PATH` and `_CEREBRAS_ENV_PATH` point to your schema-mapper .env
- `app_registry.json` — generated for your machine's 854 apps
- `capabilities.json` — same

**Files:**
- `context_assembler.py` — make `_VAULT_HINT` configurable or remove
- `config.py` — make Groq/Cerebras key paths configurable via env vars with no defaults
- `app_registry.json` — regenerate at runtime per-install, or provide empty template

**Action:**
1. Change `_VAULT_HINT` to a placeholder or env-configurable path
2. Change Groq/Cerebras key paths to empty defaults (user must set env vars)
3. Ship with an empty `app_registry.json` template that gets populated on first run

---

### P0-4: Connect rules_engine to app launch flow

**Problem:** `rules_engine.py` evaluates app rules but nothing calls it. `tools.py` `open_application()` doesn't call `evaluate_app_rules()`. The entire rules feature (compiler + engine + web UI) is disconnected from runtime.

**Files:**
- `tools.py` — `open_application()` and `play_media()` should call `evaluate_app_rules()` before acting
- `rules_engine.py` — already implemented, just needs to be called

**Action:**
1. Import `rules_engine` in `tools.py`
2. In `open_application()`, before launching, call `evaluate_app_rules(app_entry, "launch")`
3. If `allowed == False`, return the block message instead of launching
4. If `notices` non-empty, include them in the response
5. Same for `play_media()` and any other app-triggering tools

---

## P1 — Important for Student Experience

### P1-1: Split large files

**Problem:** `tools.py` is 4,268 lines. `brain_gemini.py` is 2,175 lines. Both are too large to navigate, test, or maintain.

**Files to split:**
- `tools.py` → `app_ops.py`, `file_ops.py`, `shell_ops.py`, `web_ops.py`, `media_ops.py`, `system_ops.py`
- `brain_gemini.py` → `brain_router.py`, `brain_fastpath.py`, `brain_toolhandler.py`, `brain_context.py`

**Action:** One file at a time. Start with `tools.py` since it's the largest. Keep backward-compatible imports during transition.

---

### P1-2: Cross-session context replay

**Problem:** Session JSONL files survive restarts but are never loaded into the in-memory window on startup. Each session starts fresh — context is lost.

**Files:**
- `session_store.py` — add `load_recent()` that reads today's and yesterday's JSONL into `_recent` on init
- `brain_gemini.py` — call `session_store.load_recent()` at startup

**Action:**
1. Add `load_recent()` to session_store.py that reads last N turns from JSONL
2. Call it at brain startup
3. Cap at same `_WINDOW` limit (20 turns)

---

### P1-3: Fix token budget math for local backend

**Problem:** System prompt + 24 tool schemas = ~3.7k tokens. `LOCAL_HISTORY_TOKEN_BUDGET = 3500`. Net: -200 tokens for history. Local turns are effectively stateless.

**Files:**
- `config.py` — increase `LOCAL_HISTORY_TOKEN_BUDGET` or reduce tool schema footprint
- `Modelfile.jarvis` — increase `num_ctx` beyond 8192

**Action:**
1. Measure actual token consumption of system prompt + tool schemas precisely
2. Either raise `num_ctx` in Modelfile.jarvis to 16384+ and set budget accordingly
3. Or trim tool schemas to only include what's needed for local turns

---

### P1-4: Add .env.example with all keys

**Problem:** Students don't know which API keys are needed. Current .env.example (if it exists) only covers Gemini.

**Files:**
- `.env.example` — complete template

**Action:** Create `.env.example` with:
```bash
# Required: pick at least one backend
# Option A: 9router (local proxy)
JARVIS_USE_9ROUTER=true
ROUTER_BASE_URL=http://localhost:20128/v1
ROUTER_MODEL=oc/mimo-v2.5-free

# Option B: Gemini (free tier)
GEMINI_API_KEY=your-key-here

# Option C: Groq (free tier)
JARVIS_USE_GROQ=true
GROQ_API_KEY=your-key-here

# Option D: Cerebras (free tier)
JARVIS_USE_CEREBRAS=true
CEREBRAS_API_KEY=your-key-here

# Option E: Ollama (local, no key needed)
JARVIS_USE_OLLAMA=true
OLLAMA_MODEL=jarvis-qwen3

# Optional: wake word
WAKE_WORD=jarvis
```
Also document which backends are mutually compatible and the fallback order.

---

### P1-5: Add basic brain_gemini.py tests

**Problem:** Core brain logic has zero test coverage.

**Files:**
- New: `test_brain_router.py`

**Action:** Write tests for:
1. Fast-path intents (math, time, greeting) return without cloud call
2. Tool use round-trips correctly (tool call → result → final response)
3. Multi-backend fallback order (mock each backend)
4. Consent policy blocks correctly
5. History truncation at MAX_HISTORY

---

## P2 — Polish

- [ ] Add `__version__` and pyproject.toml for pip install
- [ ] Add GitHub Actions CI (lint + test on push)
- [ ] Document app capability model (how apps declare what they can do)
- [ ] Add `/status` health endpoint
- [ ] Cross-platform audit (identify Windows-specific code)
- [ ] Add type hints to public functions
- [ ] Replace bare `except Exception: pass` with specific exceptions + logging
- [ ] Add `--version` flag to jarvis.py

---

*End of checklist.*
