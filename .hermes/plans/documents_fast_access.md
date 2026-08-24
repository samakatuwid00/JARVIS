# JARVIS Documents Folder Fast-Access — Implementation Plan

**Goal:** JARVIS can open and list the Documents folder contents. The path lives in
`config.py` (single source of truth, fast lookup). A Documents registry is generated
during the full computer scan so listing is instant without hitting the filesystem.

**Date:** 2026-08-24

---

## Current state (explored)

| What | Where | Notes |
|---|---|---|
| `DEFAULT_WRITE_DIR` | `tools.py:105` | `Path.home() / "Documents"` — write-path only, not exposed to model |
| `_USER_FOLDERS` | `tools.py:110` | `{"documents","desktop","downloads",...}` — expands relative writes |
| `list_directory(path)` | `tools.py:237` | Generic, works on any path, no cache |
| `open_application(app)` | `tools.py:1236` | Resolves via `machine_capabilities.resolve()` — "explorer" in corpus |
| `app_registry.json` | repo root | ~975 entries, apps only (not folders/files) |
| `machine_capabilities.py` | repo root | Scans installed apps, NOT user folders |
| `intake_corpus.json` | repo root | commands/apps/projects — no "documents" section |
| `config.py` | repo root | API keys, audio, paths — NO documents folder constant |

## Plan (5 phases)

### Phase A — Config: add `DOCUMENTS_FOLDER` to `config.py`

Add one constant at the bottom of `config.py`:

```python
# User folders — static paths for fast lookup (resolved at import time)
DOCUMENTS_FOLDER = Path(os.path.expanduser("~/Documents"))
```

**Why config.py:** All static paths live here (`BASE_DIR`, `ENV_FILE`). This is the
single source of truth — every other module imports from config.

**Verify:** `python -c "from config import DOCUMENTS_FOLDER; print(DOCUMENTS_FOLDER)"`

---

### Phase B — Scan: add Documents folder to `machine_capabilities.py`

Add a `scan_documents_folder()` function that:
1. Reads `DOCUMENTS_FOLDER` from config
2. Lists top-level contents (files + subdirs) with metadata: name, path, type (file/dir),
   size, last modified
3. Returns a dict like `{"files": [...], "subdirs": [...], "generated": "...", "path": "..."}`
4. Writes to `documents_registry.json` (gitignored, like `app_registry.json`)
5. Gets called inside `write_registry()` so the full scan includes it

The registry is a **flat snapshot** — not recursive (Documents can have many files).
The model can then call `list_documents` which reads from this cache.

**Also add to `.gitignore`:** `documents_registry.json`

**Verify:** `python -c "import machine_capabilities; print(machine_capabilities.scan_documents_folder())"`

---

### Phase C — Tool: add `list_documents` to `tools.py`

New function + TOOL_MAP entry + TOOLS schema + TOOL_DECLARATIONS entry:

```python
def list_documents(refresh=False):
    """List Documents folder contents. Uses cached registry for speed.
    Pass refresh=True to rescan the live filesystem."""
```

Logic:
- If `refresh=False` and `documents_registry.json` exists → read cache, return formatted list
- If `refresh=True` or cache missing → call `scan_documents_folder()`, write cache, return
- Output: formatted list grouped by type (subdirs first, then files), with sizes

**Also add `open_documents` shortcut:** opens `explorer.exe` at `DOCUMENTS_FOLDER`.
This reuses `open_application` logic but with a hardcoded path from config.

**Verify:** `python -c "from tools import list_documents; print(list_documents())"`

---

### Phase D — Wiring: intake + TOOL_DECLARATIONS + TOOL_MAP

1. **`intake_corpus.json`:** add `"documents"` to `apps` list (so "open documents" resolves)
2. **`intake.py` `_VERB_BACKEND`:** add `"documents": "desktop"` (open = explorer)
3. **`tools.py` TOOL_MAP:** add `"list_documents"` and `"open_documents"` lambdas
4. **`tools.py` TOOLS:** add schema entries for both
5. **`brain_gemini.py` TOOL_DECLARATIONS:** add `_make_tool("list_documents", ...)` and
   `_make_tool("open_documents", ...)`

**Model sees:** two new tools — `list_documents` (read-only listing) and `open_documents`
(opens Explorer at Documents). Both read from config constant.

**Verify:** syntax + tool count + TOOL_MAP entries + schema presence

---

### Phase E — Verify end-to-end

Ad-hoc verification script (no live spawn):
1. `ast.parse` all changed files
2. `config.DOCUMENTS_FOLDER` resolves to `C:\Users\deped\Documents`
3. `scan_documents_folder()` returns valid dict with files + subdirs
4. `list_documents()` returns formatted output (cached)
5. `list_documents(refresh=True)` returns live listing
6. `open_documents` resolves in TOOL_MAP
7. TOOL_DECLARATIONS count incremented
8. `_detect_backend` + `delegate` routing unaffected

Clean up temp scripts after run.

---

## What this does NOT cover (by design)

- **Recursive file indexing:** too expensive for a flat folder. Model uses
  `list_directory(path)` for deep traversal if needed.
- **Desktop/Downloads/Pictures:** same pattern, add later if needed.
- **File search within Documents:** model can use `delegate("search ... in documents")`
  which routes to Hermes → `list_directory` + grep.

## Files changed

| File | Change |
|---|---|
| `config.py` | Add `DOCUMENTS_FOLDER` constant |
| `machine_capabilities.py` | Add `scan_documents_folder()`, wire into `write_registry()` |
| `tools.py` | Add `list_documents()`, `open_documents()`, TOOL_MAP, TOOLS entries |
| `brain_gemini.py` | Add `_make_tool` entries for both new tools |
| `intake_corpus.json` | Add "documents" to apps |
| `.gitignore` | Add `documents_registry.json` |
