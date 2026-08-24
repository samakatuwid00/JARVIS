# System Tools - File ops, shell commands, web search
import os
import shutil
import subprocess
import threading
import json
import platform
import re
import time
import datetime
import difflib
import webbrowser
import urllib.parse
import urllib.request
from pathlib import Path

# Thread-local progress callback — set by brain_gemini.think() so tools can
# stream mid-task status ("Searching YouTube Music...") back to the WS layer.
_tools_tls = threading.local()


def set_progress_cb(cb):
    """Store the progress callback for the current thread (called by brain)."""
    _tools_tls.cb = cb


def progress(msg: str):
    """Emit a mid-task status update if a callback is set. Safe to call anytime."""
    cb = getattr(_tools_tls, "cb", None)
    if cb:
        try:
            cb(msg)
        except Exception:
            pass


def get_system_info():
    """Get basic system information."""
    return {
        "os": platform.system(),
        "os_version": platform.version(),
        "machine": platform.machine(),
        "python": platform.python_version(),
        "cwd": os.getcwd(),
        "user": os.getenv("USER") or os.getenv("USERNAME", "unknown"),
        "time": datetime.datetime.now().isoformat(),
    }


def run_shell(command: str) -> str:
    """Execute a shell command and return output."""
    try:
        result = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30
        )
        output = result.stdout.strip()
        if result.returncode != 0:
            output += f"\n[Exit code: {result.returncode}]"
            if result.stderr:
                output += f"\n{result.stderr.strip()}"
        return output or "(no output)"
    except subprocess.TimeoutExpired:
        return "[Error] Command timed out after 30s"
    except Exception as e:
        return f"[Error] {str(e)}"


def read_file(path: str) -> str:
    """Read a file's contents, tolerating non-UTF-8 text encodings."""
    try:
        p = Path(path).expanduser()
        if not p.exists():
            return f"[Error] File not found: {path}"
        if p.is_dir():
            return f"[Error] That is a directory, not a file: {path}"
        if p.stat().st_size > 1_000_000:
            return "[Error] File too large (>1MB)"

        raw = p.read_bytes()
        # NUL bytes in the first block mean this is binary, not mis-encoded text.
        if b"\x00" in raw[:4096]:
            return (f"[Error] {p.name} is a binary file, not text. I can only read "
                    "plain text files - PDFs, Word documents, images and archives "
                    "need a converter I do not have.")
        for enc in ("utf-8", "utf-8-sig", "cp1252", "latin-1"):
            try:
                return raw.decode(enc)
            except UnicodeDecodeError:
                continue
        return raw.decode("utf-8", "replace")
    except Exception as e:
        return f"[Error] {str(e)}"


# A bare filename used to resolve against the server's working directory, which
# is the JARVIS source folder — "save my notes as notes.txt" buried the file
# among the .py files where nobody would look for it. Spoken requests almost
# never carry a directory, so an unqualified name belongs somewhere the user
# actually opens.
DEFAULT_WRITE_DIR = Path.home() / "Documents"


# Folder names the model commonly prefixes ("Documents/notes.txt"). These are
# meant relative to the user's home, never to the server's working directory.
_USER_FOLDERS = {"documents", "desktop", "downloads", "pictures", "music",
                 "videos", "onedrive"}


def _resolve_write_path(path: str) -> Path:
    """Expand `path`, keeping relative writes out of the JARVIS source folder.

    Absolute paths are honoured as given. Everything else is anchored to the
    user's own folders: a bare name goes to Documents, and a path that already
    starts with a known user folder ("Documents/notes.txt") is joined onto the
    home directory. Resolving those against the CWD produced files nested at
    jarvis-demo\\Documents\\ while JARVIS told the user they were in Documents.
    """
    p = Path(str(path)).expanduser()
    if p.is_absolute():
        return p
    parts = [q for q in p.parts if q not in (".", "")]
    if not parts:
        return DEFAULT_WRITE_DIR
    if parts[0].lower() in _USER_FOLDERS:
        return Path.home().joinpath(*parts)
    if len(parts) > 1:
        return DEFAULT_WRITE_DIR.joinpath(*parts)
    return DEFAULT_WRITE_DIR / parts[0]


def write_file(path: str, content: str, overwrite: bool = False) -> str:
    """Write content to a file. Refuses to clobber an existing file unless told to.

    JARVIS is driven by speech, and a misheard filename should not silently
    destroy an existing file, so replacing one is an explicit opt-in.

    A filename with no directory lands in Documents (see _resolve_write_path),
    not in whatever directory the server happens to be running from.
    """
    try:
        p = _resolve_write_path(path)
        if p.exists() and not overwrite:
            size = p.stat().st_size
            return (f"[Blocked] {p.name} already exists ({size:,} bytes) and I did not "
                    "overwrite it. Confirm you want it replaced and I will.")
        p.parent.mkdir(parents=True, exist_ok=True)
        existed = p.exists()
        p.write_text(content, encoding="utf-8")
        verb = "Replaced" if existed else "Written to"
        # Report the resolved path: saying "notes.txt" leaves the user hunting.
        return f"{verb} {p} ({len(content)} chars)"
    except Exception as e:
        return f"[Error] {str(e)}"


# Bounds so a search rooted at a home directory cannot stall a voice turn.
SEARCH_TIME_BUDGET = 20.0
SEARCH_MAX_VISITED = 40_000
SKIP_DIRS = {
    ".git", "node_modules", "__pycache__", ".venv", "venv", "AppData",
    "site-packages", "Windows", "Program Files", "Program Files (x86)",
    "$Recycle.Bin", "OneDrive", "dist", "build",
}

_SECRET_LINE = re.compile(
    # Key name may be quoted (JSON) or bare (dotenv); value likewise.
    r"""(?i)([A-Z0-9_-]*(?:KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL)[A-Z0-9_-]*)"""
    r"""(["']?\s*[=:]\s*)(["']?)([^\s"',]+)"""
)


def _redact(line: str) -> str:
    """Blank out secret values so search results never speak a key aloud."""
    return _SECRET_LINE.sub(lambda m: f"{m.group(1)}=<redacted>", line)


def search_files(query: str, path: str = ".", max_results: int = 20) -> str:
    """Search file contents recursively for a string. Case-insensitive."""
    try:
        root = Path(path).expanduser()
        if not root.exists():
            return f"[Error] Path not found: {path}"

        needle = query.lower()
        hits = []
        scanned = 0
        visited = 0
        deadline = time.monotonic() + SEARCH_TIME_BUDGET
        stopped = None

        # Streamed, not materialised: rglob on a home directory would build a list
        # of hundreds of thousands of paths before a single line was ever matched,
        # which stalls the whole voice turn.
        walker = [root] if root.is_file() else root.rglob("*")
        for f in walker:
            visited += 1
            if visited > SEARCH_MAX_VISITED:
                stopped = f"stopped after walking {SEARCH_MAX_VISITED:,} paths"
                break
            if time.monotonic() > deadline:
                stopped = f"stopped after {SEARCH_TIME_BUDGET}s"
                break
            if any(part in SKIP_DIRS or part.startswith(".") for part in f.parts[-4:]):
                continue
            try:
                if not f.is_file() or f.stat().st_size > 2_000_000:
                    continue
                raw = f.read_bytes()
                if b"\x00" in raw[:2048]:
                    continue
                text = raw.decode("utf-8", "replace")
            except Exception:
                continue
            scanned += 1
            for n, line in enumerate(text.splitlines(), 1):
                if needle in line.lower():
                    hits.append(f"{f.name}:{n}: {_redact(line.strip())[:120]}")
                    break
            if len(hits) >= max_results:
                stopped = f"stopped at the {max_results}-result limit"
                break

        note = f" (search {stopped})" if stopped else ""
        if not hits:
            return f"No matches for '{query}' in {scanned} text files under {root}.{note}"
        return (f"Found '{query}' in {len(hits)} of {scanned} files searched{note}: "
                + " | ".join(hits))
    except Exception as e:
        return f"[Error] {str(e)}"


def list_directory(path: str = ".") -> str:
    """List directory contents."""
    try:
        p = Path(path).expanduser()
        items = []
        for item in sorted(p.iterdir()):
            prefix = "[dir]" if item.is_dir() else "[file]"
            size = item.stat().st_size if item.is_file() else 0
            items.append(f"{prefix} {item.name} ({size:,} bytes)" if size else f"{prefix} {item.name}/")
        return "\n".join(items) or "(empty directory)"
    except Exception as e:
        return f"[Error] {str(e)}"


# --- Second Brain vault (READ-ONLY) ------------------------------------------
# READ-ONLY INTEGRATION. Nothing in this module may write, edit, delete, move or
# rename anything under VAULT_ROOT. The vault is the user's personal knowledge
# base; JARVIS may only read from it. Any future write path belongs elsewhere.
VAULT_ROOT = Path(os.getenv("JARVIS_VAULT_ROOT",
                            r"C:\Users\deped\Documents\Second Brain"))
# Machine noise and staging areas: they bury real notes in the results.
VAULT_SKIP_DIRS = {".obsidian", ".git", ".trash", "_scratch", "_to_delete"}
VAULT_SKIP_PREFIXES = ("attachments/social-",)


def _vault_notes():
    """Yield every eligible .md path under the vault. Read-only traversal."""
    for f in VAULT_ROOT.rglob("*.md"):
        rel = f.relative_to(VAULT_ROOT).as_posix()
        if any(part in VAULT_SKIP_DIRS for part in f.relative_to(VAULT_ROOT).parts[:-1]):
            continue
        if any(rel.lower().startswith(p) for p in VAULT_SKIP_PREFIXES):
            continue
        if not f.is_file():
            continue
        yield f


def _vault_text(path: Path) -> str:
    """Read a note, tolerating the odd non-UTF-8 byte. Never writes."""
    raw = path.read_bytes()
    for enc in ("utf-8", "utf-8-sig", "cp1252", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


def search_vault(query: str, limit: int = 10) -> str:
    """Search the Second Brain vault for text. Case-insensitive, READ-ONLY.

    Scans every .md note under the vault and returns up to `limit` notes, each
    with its path and the first line that matched.
    """
    try:
        if not VAULT_ROOT.exists():
            return f"[Error] Vault not found at {VAULT_ROOT}"

        needle = query.lower()
        hits = []
        scanned = 0
        deadline = time.monotonic() + SEARCH_TIME_BUDGET
        stopped = None

        for f in _vault_notes():
            if time.monotonic() > deadline:
                stopped = f"stopped after {SEARCH_TIME_BUDGET}s"
                break
            try:
                text = _vault_text(f)
            except Exception:
                continue
            scanned += 1
            rel = f.relative_to(VAULT_ROOT).as_posix()
            matched = needle in rel.lower()
            for n, line in enumerate(text.splitlines(), 1):
                if needle in line.lower():
                    hits.append(f"{rel}:{n}: {_redact(line.strip())[:120]}")
                    matched = False
                    break
            if matched:
                # Filename match with no matching body line still worth reporting.
                hits.append(f"{rel}: (matched the note title)")
            if len(hits) >= limit:
                stopped = f"stopped at the {limit}-note limit"
                break

        note = f" (search {stopped})" if stopped else ""
        if not hits:
            return f"No vault notes matched '{query}' in {scanned} notes searched.{note}"
        return (f"Found '{query}' in {len(hits)} of {scanned} vault notes searched{note}: "
                + " | ".join(hits))
    except Exception as e:
        return f"[Error] {str(e)}"


# --- turbovec semantic search ------------------------------------------------
# The embedding model is loaded ONCE at import time. Loading it per query cost
# 3-8s and was the reason semantic search took ~1:45s on a cache miss.
_DEFAULT_EMBEDDING_MODEL = "sentence-transformers/all-MiniLM-L6-v2"

try:
    from sentence_transformers import SentenceTransformer
    _EMBEDDING_MODEL = SentenceTransformer(_DEFAULT_EMBEDDING_MODEL)
    _EMBEDDING_DIM = 384
except Exception:
    _EMBEDDING_MODEL = None
    _EMBEDDING_DIM = None

# Any non-default model named in turbovec_meta.json is loaded at most once.
_extra_models = {}


def _normalize_model_name(name: str) -> str:
    """turbovec_meta.json may store the short name ('all-MiniLM-L6-v2')."""
    return name.split("/")[-1] if name else ""


def _get_embedding_model(name: str = None):
    """Return a cached SentenceTransformer. Never loads the same model twice."""
    if _EMBEDDING_MODEL is None:
        return None
    if not name or _normalize_model_name(name) == _normalize_model_name(_DEFAULT_EMBEDDING_MODEL):
        return _EMBEDDING_MODEL
    if name not in _extra_models:
        try:
            _extra_models[name] = SentenceTransformer(name)
        except Exception:
            _extra_models[name] = None
    return _extra_models[name] or _EMBEDDING_MODEL


# Module-level cache for turbovec index + model (avoids reload on every search)
_turbovec_cache = None
_turbovec_cache_vault = None


def _load_turbovec_index(vault_root: Path):
    """Load the persisted turbovec index and embedding model if available.

    Returns (index, model, dim) or None if not available.
    Never mutates the vault — read-only.

    Uses module-level cache so the model + index are only loaded once per
    process. Subsequent calls return the cached tuple instantly.

    NOTE: turbovec 0.8.0's load() has a bug where search() returns empty
    results after loading from disk. We work around this by rebuilding the
    in-memory index from saved embeddings (.npy) — which is just a fast
    prepare()+add() call, no re-embedding needed.
    """
    global _turbovec_cache, _turbovec_cache_vault

    # Return cached result if same vault
    if _turbovec_cache is not None and _turbovec_cache_vault == vault_root:
        return _turbovec_cache

    try:
        import turbovec as tv
        import numpy as np
    except ImportError:
        return None

    if _EMBEDDING_MODEL is None:
        return None

    index_path = vault_root / "turbovec_index.tv"
    if not index_path.exists():
        return None

    # Try the in-memory rebuild approach (works around the load() bug)
    emb_path = vault_root / "turbovec_embeddings.npy"
    notes_path = vault_root / "turbovec_notes.txt"
    # Also check .automation/ subdirectory (where deploy script writes it)
    if not notes_path.exists():
        notes_path = vault_root / ".automation" / "turbovec_notes.txt"
    meta_path = vault_root / "turbovec_meta.json"

    if not emb_path.exists() or not notes_path.exists():
        # Fallback: try the broken load() — may return empty search results
        try:
            import struct
            with open(index_path, "rb") as f:
                header = f.read(16)
                if len(header) >= 8 and header[:4] == b"TVPI":
                    dim = struct.unpack("<H", header[6:8])[0]
                else:
                    dim = 384
        except Exception:
            dim = 384

        model = _get_embedding_model()
        if model is None:
            return None

        try:
            index = tv.TurboQuantIndex(dim)
            try:
                index.prepare()
            except TypeError:
                pass
            index.load(str(index_path))
            _turbovec_cache = (index, model, dim)
            _turbovec_cache_vault = vault_root
            return index, model, dim
        except Exception:
            return None

    # Primary path: rebuild from saved embeddings (works)
    try:
        import struct
        with open(index_path, "rb") as f:
            header = f.read(16)
            if len(header) >= 8 and header[:4] == b"TVPI":
                dim = struct.unpack("<H", header[6:8])[0]
            else:
                dim = 384
    except Exception:
        dim = 384

    # Read metadata for model name
    model_name = _DEFAULT_EMBEDDING_MODEL
    if meta_path.exists():
        import json as _json
        try:
            meta = _json.loads(meta_path.read_text(encoding="utf-8"))
            model_name = meta.get("model", model_name)
            dim = meta.get("dim", dim)
        except Exception:
            pass

    # Embedding model comes from the module-level cache — no per-query load.
    model = _get_embedding_model(model_name)
    if model is None:
        return None

    # Load saved embeddings and rebuild index in-memory
    try:
        embeddings = np.load(str(emb_path))
        if embeddings.shape[1] != dim:
            dim = embeddings.shape[1]
    except Exception:
        return None

    index = tv.TurboQuantIndex(dim)
    try:
        index.prepare()
    except TypeError:
        pass
    index.add(embeddings)

    result = (index, model, dim)
    _turbovec_cache = result
    _turbovec_cache_vault = vault_root
    return result


def search_vault_semantic(query: str, limit: int = 10) -> str:
    """Search the vault semantically using turbovec vector index (READ-ONLY).

    Uses the persisted turboquant index (turbovec_index.tv) if it exists in the
    vault root. Embeds the query with sentence-transformers, then performs a
    SIMD-accelerated nearest-neighbor search — returns results in sub-50ms.

    Falls back to search_vault() if no index is available.
    """
    try:
        if not VAULT_ROOT.exists():
            return f"[Error] Vault not found at {VAULT_ROOT}"

        result = _load_turbovec_index(VAULT_ROOT)
        if result is None:
            # Fallback to substring search
            return search_vault(query, limit=limit)

        index, model, dim = result

        # Embed the query
        q_vec = model.encode([query], normalize_embeddings=True).astype("float32")

        # Vector search
        scores, indices = index.search(q_vec, k=limit)

        # Build note list from saved paths (must match how index was built)
        notes_path = VAULT_ROOT / "turbovec_notes.txt"
        # Also check .automation/ subdirectory
        if not notes_path.exists():
            notes_path = VAULT_ROOT / ".automation" / "turbovec_notes.txt"
        if notes_path.exists():
            note_rels = notes_path.read_text(encoding="utf-8").strip().split("\n")
            notes = [(rel, VAULT_ROOT / rel.replace("/", "\\")) for rel in note_rels]
        else:
            notes = list(_vault_notes())

        hits = []
        for rank, (score, idx) in enumerate(zip(scores[0], indices[0])):
            if idx >= len(notes):
                continue
            # Handle both tuple (rel, path) and bare Path
            entry = notes[idx]
            if isinstance(entry, tuple):
                rel = entry[0]
                note = entry[1]
            else:
                note = entry
                rel = note.relative_to(VAULT_ROOT).as_posix()

            text = _vault_text(note)
            snippet = ""
            for line in text.splitlines():
                clean = line.strip()
                if len(clean) > 10:
                    snippet = _redact(clean[:120])
                    break
            hits.append(f"{rel} (score={float(score):.4f}): {snippet}")

        if not hits:
            return search_vault(query, limit=limit)

        return (f"Semantic search found {len(hits)} results for '{query}': "
                + " | ".join(hits))

    except Exception as e:
        # Graceful fallback to substring search on any error
        return search_vault(query, limit=limit)


def read_vault_note(name: str) -> str:
    """Read one Second Brain note in full by fuzzy filename match. READ-ONLY.

    Matches any .md note whose path contains `name`, case-insensitive. If more
    than one matches, the candidates are listed instead of guessing.
    """
    try:
        if not VAULT_ROOT.exists():
            return f"[Error] Vault not found at {VAULT_ROOT}"

        needle = name.lower().removesuffix(".md")
        matches = [f for f in _vault_notes()
                   if needle in f.relative_to(VAULT_ROOT).as_posix().lower()]
        if not matches:
            return f"[Error] No vault note matches '{name}'. Try search_vault first."

        if len(matches) > 1:
            # An exact stem match beats its near-namesakes; only ask when truly ambiguous.
            exact = [f for f in matches if f.stem.lower() == needle]
            if len(exact) == 1:
                matches = exact
            else:
                listed = "; ".join(f.relative_to(VAULT_ROOT).as_posix() for f in matches[:15])
                return (f"{len(matches)} vault notes match '{name}'. "
                        f"Say which one: {listed}")

        note = matches[0]
        text = _vault_text(note)
        rel = note.relative_to(VAULT_ROOT).as_posix()
        if len(text) > 20000:
            text = text[:20000] + "\n... (note truncated)"
        return f"{rel}:\n{text}"
    except Exception as e:
        return f"[Error] {str(e)}"


# --- Credentials stored in the vault (READ-ONLY) ------------------------------
# The credential notes live at wiki/secrets/ inside the vault and are local-only
# (gitignored). Same rule as the rest of the vault integration applies, only more
# so: JARVIS reads these and never writes, edits, moves or renames them.
VAULT_SECRETS_DIR = VAULT_ROOT / "wiki" / "secrets"
# Field names the notes use for the two things callers actually need.
_USERNAME_KEYS = ("username", "user", "login", "account", "email", "user_name")
_PASSWORD_KEYS = ("password", "pass", "pwd", "passphrase")


def _clean_cell(cell: str) -> str:
    """Strip the markdown a table cell is dressed in: bold, code ticks, spaces."""
    return cell.replace("**", "").replace("`", "").strip()


def _parse_credential_table(text: str) -> dict:
    """Pull every 'key | value' row out of a markdown note into a flat dict.

    The notes are hand-written, so nothing here assumes a fixed row order or a
    fixed set of fields - anything beyond username and password comes back as
    extra rather than being dropped.
    """
    fields = {}
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("|"):
            continue
        cells = [c for c in line.split("|")]
        # A well-formed row is '| key | value |', so split gives ['', k, v, ''].
        if len(cells) < 4:
            continue
        key = _clean_cell(cells[1])
        value = _clean_cell(cells[2])
        if not key or not value:
            continue
        if set(key) <= set("-: ") or set(value) <= set("-: "):
            continue  # the ---|--- separator row
        norm = re.sub(r"[^a-z0-9]+", "_", key.lower()).strip("_")
        if norm and norm not in fields:
            fields[norm] = value
    return fields


def get_credentials(project: str) -> dict:
    """Read one credential note from the vault and return its fields. READ-ONLY.

    Returns a dict for programmatic callers - {"ok", "username", "password",
    "extra", "summary"} - where "summary" is safe to speak aloud because the
    password is masked. On failure "ok" is False and "summary" carries the
    error, so a caller can always speak summary without checking anything else.
    """
    try:
        if not VAULT_SECRETS_DIR.exists():
            msg = (f"[Error] No credentials folder in the vault at {VAULT_SECRETS_DIR}. "
                   "The notes live in wiki/secrets.")
            return {"ok": False, "error": msg, "summary": msg}

        needle = project.lower().removesuffix(".md").strip()
        notes = sorted(VAULT_SECRETS_DIR.glob("*.md"))
        exact = [f for f in notes if f.stem.lower() == needle]
        matches = exact or [f for f in notes if needle in f.stem.lower()]
        if not matches:
            names = "; ".join(f.stem for f in notes) or "none"
            msg = (f"[Error] No credential note matches '{project}'. "
                   f"Notes on file: {names}")
            return {"ok": False, "error": msg, "summary": msg}
        if len(matches) > 1:
            names = "; ".join(f.stem for f in matches[:10])
            msg = (f"{len(matches)} credential notes match '{project}'. "
                   f"Say which one: {names}")
            return {"ok": False, "error": msg, "summary": msg}

        note = matches[0]
        fields = _parse_credential_table(_vault_text(note))
        username = next((fields[k] for k in _USERNAME_KEYS if k in fields), None)
        password = next((fields[k] for k in _PASSWORD_KEYS if k in fields), None)
        extra = {k: v for k, v in fields.items()
                 if k not in _USERNAME_KEYS and k not in _PASSWORD_KEYS}

        if username is None and password is None:
            msg = (f"[Error] Found the note {note.stem} but no username or password "
                   "row in it. The table may use different field names.")
            return {"ok": False, "error": msg, "note": note.stem, "extra": extra,
                    "summary": msg}

        # Never put the real password in the spoken summary - it is read aloud.
        parts = [f"username: {username}" if username else "username: not in the note",
                 "password: ***" if password else "password: not in the note"]
        for k in ("url", "server", "environment", "env", "host"):
            if k in extra:
                parts.append(f"{k}: {extra[k]}")
        summary = f"{note.stem} - " + ", ".join(parts)

        return {"ok": True, "note": note.stem, "username": username,
                "password": password, "extra": extra, "summary": summary}
    except Exception as e:
        msg = f"[Error] Could not read credentials for '{project}': {e}"
        return {"ok": False, "error": msg, "summary": msg}


def _get_credentials_tool(**kw) -> str:
    """Tool wrapper: hands back only the redacted summary, never the password."""
    return get_credentials(kw["project"])["summary"]


# --- Open a file by name ------------------------------------------------------
OPEN_FILE_ROOTS = [
    Path(r"C:\Users\deped\Documents"),
    Path(r"C:\Users\deped\Desktop"),
    Path(r"C:\Users\deped\Downloads"),
    Path(r"C:\Users\deped\Documents\Second Brain"),
    Path(r"C:\Users\deped\Documents\Portfolio"),
]
# Spoken requests almost always mean a document, not a stray .tmp beside it.
DOCUMENT_SUFFIXES = (".pptx", ".ppt", ".docx", ".doc", ".pdf", ".xlsx", ".xls",
                     ".md", ".txt", ".csv", ".png", ".jpg", ".jpeg", ".mp4")


def find_file(name: str):
    """Return (best_match_path_or_None, candidate_paths) for a filename fragment.

    Bounded by the same time budget and visit cap as search_files so a mistaken
    request cannot stall a voice turn.
    """
    needle = name.lower()
    deadline = time.monotonic() + SEARCH_TIME_BUDGET
    visited = 0
    hits = []

    for root in OPEN_FILE_ROOTS:
        if not root.exists():
            continue
        for f in root.rglob("*"):
            visited += 1
            if visited > SEARCH_MAX_VISITED or time.monotonic() > deadline:
                break
            if any(part in SKIP_DIRS or part.startswith(".") for part in f.parts[-4:]):
                continue
            try:
                if not f.is_file():
                    continue
            except OSError:
                continue
            if needle in f.name.lower() and f not in hits:
                hits.append(f)
                if len(hits) >= 40:
                    break
        if visited > SEARCH_MAX_VISITED or time.monotonic() > deadline:
            break

    if not hits:
        return None, []
    # Prefer a document-like file, then an exact stem match, then the shortest path.
    docs = [f for f in hits if f.suffix.lower() in DOCUMENT_SUFFIXES] or hits
    exact = [f for f in docs if f.stem.lower() == needle] or docs
    best = min(exact, key=lambda f: (len(f.name), len(str(f))))
    return best, hits


def open_file(name: str) -> str:
    """Find a file by part of its name across the user's usual folders and open it."""
    try:
        best, hits = find_file(name)
        if best is None:
            return (f"[Error] No file matching '{name}' under Documents, Desktop, "
                    "Downloads, the Second Brain vault or Portfolio.")
        if platform.system() == "Windows":
            os.startfile(str(best))
        else:
            subprocess.run(["xdg-open", str(best)])
        extra = f" ({len(hits) - 1} other matches ignored)" if len(hits) > 1 else ""
        return f"Opened {best}{extra}"
    except Exception as e:
        return f"[Error] Could not open '{name}': {str(e)}"


def search_web(query: str) -> str:
    """Open web search in browser."""
    url = f"https://www.google.com/search?q={query.replace(' ', '+')}"
    webbrowser.open(url)
    return f"Opened search: {query}"


# ---- Web registry (Phase 8): named site resolution ------------------------

def open_site(name: str, url: str = None) -> str:
    """Resolve a spoken site name against web_registry.json and open it in
    the JARVIS debug-Chrome. Falls back to treating the input as a URL."""
    import web_registry as wr
    key = wr.resolve_site(name) if url is None else None
    if key:
        site = wr.get_site(key)
        return __import__("browser_agent").open_site(site["url"], name=key)
    raw = (url or name or "").strip()
    if "://" not in raw and "." in raw:
        raw = "https://" + raw
    if "://" in raw:
        return __import__("browser_agent").open_site(raw, name=name)
    return (f"[Error] No registered site matches '{name}'. "
            "Say 'add site <name> <url>' to register it, or 'rescan my sites'.")


def rescan_sites() -> str:
    """Full incremental rescan of browser history into web_registry.json."""
    import web_registry as wr
    reg = wr.scan_sites()
    wr.build_index()
    n = len(reg.get("sites", {}))
    return f"Rescanned sites — {n} known now (last scan {reg.get('last_scan')})."


# ---- Phase 13: session recall (FTS5 over JARVIS's own transcripts) ---------

def search_sessions(query: str, limit: int = 10) -> str:
    """Full-text search over past JARVIS conversations."""
    import session_index as si
    si.rebuild()  # cheap incremental; picks up any lines written by other processes
    hits = si.search_sessions(query, int(limit or 10))
    if not hits:
        return f"No past conversation turns matched '{query}'."
    return (f"Found {len(hits)} past turn(s) matching '{query}':\n"
            + si.format_hits(hits))


# ---- Phase 10: search-target routing ---------------------------------------
# "search X in chatgpt" must search ChatGPT HISTORY, not open a Google tab
# with the literal words "in chatgpt" in the query.

_SEARCH_START = re.compile(
    r"^(search|look ?up|find|google)\b[,:]?\s*", re.I)
_CORRECTION_START = re.compile(r"^i mean\b|^actually\b|^no[, ]+", re.I)
_TAIL_TARGET = re.compile(r"\b(?:inside|in|on|at)\s+([a-z0-9 .'-]{1,24}?)\s*$", re.I)

# Spoken names for AI chats. History-searchable ones first.
_AI_HISTORY_SITES = {"chatgpt": "chatgpt", "chat gpt": "chatgpt",
                     "chat gpts": "chatgpt"}
_AI_TYPED_SITES = {"gemini": "gemini", "claude": "claude", "grok": "grok",
                   "copilot": "copilot"}
# Leading noise from natural phrasing: "search in history for X" / "in histor"
_LEAD_HISTORY = re.compile(
    r"^(?:in\s+|inside\s+)?(?:my\s+)?(?:chat\s*g?pt\s+)?h(?:is|isto)ry\b[, :]?\s*(?:for\b\s*)?",
    re.I)


def _exact_site_key(cand: str):
    """Exact-only registry lookup (name or alias). NEVER fuzzy here: a search
    tail like 'manila' must stay part of the query, not become a website."""
    import web_registry as wr
    wr.ensure_index()
    q = cand.strip().lower().rstrip(".!?")
    return wr._index["exact"].get(q)


def parse_search_command(text: str):
    """Parse a spoken search command.

    Returns dict: {query, target, is_history_search, corrected}
    or None when this isn't a search command. Target may be None (= Google).
    """
    raw = (text or "").strip()
    # A correction marker may precede the verb ("I mean search ...").
    m_corr = _CORRECTION_START.match(raw)
    if m_corr:
        raw = re.sub(_CORRECTION_START, "", raw).strip()
        corrected = True
    else:
        corrected = False
    m = _SEARCH_START.match(raw)
    if not m:
        return None
    body = raw[m.end():].strip()

    # correction marker: replace previous turn's query rather than append.
    # The marker may sit before the verb ("I mean search ...") or after it
    # (caller strips the verb first) — handle both.
    if not corrected:
        corrected = bool(_CORRECTION_START.match(body))
        if corrected:
            body = re.sub(_CORRECTION_START, "", body).strip()
            m2 = _SEARCH_START.match(body)
            if m2:
                body = body[m2.end():].strip()

    # leading "history" phrasing: "search in history for irimsv report"
    lead_history = bool(_LEAD_HISTORY.match(body))
    if lead_history:
        body = re.sub(_LEAD_HISTORY, "", body).strip()
        if not body:
            return {"query": "", "target": "chatgpt",
                    "is_history_search": True, "corrected": corrected}

    # tail qualifier: "... in <target>" — candidate capped at a few words,
    # resolved EXACTLY against AI names then the site registry.
    target = None
    is_hist = lead_history
    tail = _TAIL_TARGET.search(body)
    if tail:
        cand = tail.group(1).strip().lower().rstrip(".!?")
        hit = (_AI_HISTORY_SITES.get(cand) or _AI_HISTORY_SITES.get(cand + " gpt"))
        typed = None
        if not hit:
            # try the last 1-3 words of the candidate as an AI/site name
            words = cand.split()
            for n in (3, 2, 1):
                frag = " ".join(words[-n:]) if len(words) >= n else cand
                if frag in _AI_HISTORY_SITES:
                    hit = _AI_HISTORY_SITES[frag]
                    break
                if frag in _AI_TYPED_SITES:
                    typed = _AI_TYPED_SITES[frag]
                    break
                key = _exact_site_key(frag)
                if key:
                    typed = key
                    break
        if hit:
            target = hit
            is_hist = True
            body = body[:tail.start()].strip().rstrip(",.")
        elif typed:
            target = typed
            body = body[:tail.start()].strip().rstrip(",.")

    return {"query": body, "target": target,
            "is_history_search": is_hist,
            "corrected": corrected}


def execute_search(parsed: dict) -> str:
    """Run a parsed search command against the right backend."""
    import web_registry as wr
    q, target = parsed["query"], parsed.get("target")

    if parsed.get("is_history_search"):
        # ChatGPT has real conversation-history search via browser_agent
        return __import__("browser_agent").search_chatgpt_history(q)

    if target:
        site = wr.get_site(target)
        url = site["url"] if site else f"https://{target}.com"
        # AI web chats get the query typed into the composer; plain sites just open
        if target in _AI_TYPED_SITES.values():
            return __import__("browser_agent").ask_web_ai(prompt=q, site=target)
        result = __import__("browser_agent").open_site(url, name=target)
        return f"{result} — search for '{q}' manually there; I can't search inside that site yet."

    return search_web(q)


def resolve_open_target(text: str, force: str | None = None):
    """Dual-registry lookup for 'open X' style commands.

    Returns ('site', key) | ('app', name) | ('clarify', candidates) |
    (None, text) when nothing matches — caller falls back to existing behavior.
    force: 'app' | 'site' honours explicit 'open the spotify app' /
    'facebook site' phrasing.
    """
    t = text.strip().lower().rstrip(".!?")
    # strip filler words
    for w in ("please", "jarvis", "the ", "my ", "up ", "now "):
        if t.startswith(w):
            t = t[len(w):]
    for suffix, forced in ((" app", "app"), (" application", "app"),
                           (" program", "app"), (" site", "site"),
                           (" website", "site"), (" in browser", "site")):
        if t.endswith(suffix):
            t = t[: -len(suffix)].strip()
            force = force or forced

    import web_registry as wr
    site_key = wr.resolve_site(t)

    app_hit = False
    try:
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "app_registry.json"), encoding="utf-8") as f:
            apps = json.load(f).get("apps", {})
        app_hit = t in apps
        if not app_hit:
            # fuzzy on app names too (spotify vs spotifly etc.)
            close = difflib.get_close_matches(t, list(apps.keys()), n=1, cutoff=0.85)
            app_hit = bool(close)
    except Exception:
        pass

    if force == "site" and site_key:
        return ("site", site_key)
    if force == "app":
        return ("app", t) if app_hit else (None, text)

    if site_key and app_hit:
        return ("clarify", {"name": t, "site": site_key})
    if site_key:
        return ("site", site_key)
    if app_hit:
        return ("app", t)
    return (None, text)


# Agents JARVIS can hand a job to when it cannot do it itself. Resolved through
# the PATH entry, not a hardcoded location, so a reinstall does not break this.
DELEGATE_AGENTS = {
    "claude": ["claude", "-p"],
    "hermes": ["hermes", "-p"],
}
# A delegated agent runs a whole session of its own. Long, but bounded: a voice
# turn that never returns is worse than one that reports a timeout.
DELEGATE_TIMEOUT = int(os.getenv("JARVIS_DELEGATE_TIMEOUT", "300"))


def delegate_task(task: str, agent: str = "claude") -> str:
    """Hand a task to a CLI coding agent and return what it reports back.

    For work beyond JARVIS's own tools — multi-file edits, debugging, anything
    needing a full agent loop. It is deliberately NOT the fast path: the agent
    runs a complete session, so this costs far more than a normal reply.
    """
    key = str(agent or "claude").strip().lower()
    if key not in DELEGATE_AGENTS:
        return (f"[Error] I can delegate to {' or '.join(sorted(DELEGATE_AGENTS))}, "
                f"not '{agent}'.")
    if not str(task).strip():
        return "[Error] No task given to delegate."

    exe = shutil.which(DELEGATE_AGENTS[key][0])
    if not exe:
        return (f"[Error] The {key} CLI is not on PATH, so I cannot delegate to it.")

    cmd = [exe] + DELEGATE_AGENTS[key][1:] + [str(task)]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=DELEGATE_TIMEOUT, encoding="utf-8",
                              errors="replace")
    except subprocess.TimeoutExpired:
        return (f"[Error] {key} did not finish within {DELEGATE_TIMEOUT}s. "
                "The task may be too large to delegate from a voice turn.")
    except Exception as e:
        return f"[Error] Could not run {key}: {type(e).__name__}: {e}"

    out = (proc.stdout or "").strip() or (proc.stderr or "").strip()
    if proc.returncode != 0 and not out:
        return f"[Error] {key} exited with code {proc.returncode} and said nothing."
    if not out:
        return f"{key} finished but returned no output."
    # Spoken aloud, so hand back a readable slice rather than a wall of text.
    return f"{key} reports: {out[:1500]}"


# ── Hermes delegation bridge (full harness, approval-gated) ────────────────
# JARVIS is the voice peripheral (mic in, speaker out); Hermes is the EXECUTOR
# with the full machine toolkit (terminal, file, browser, code_execution,
# delegation/subagents, cron, skills, memory, computer_use). JARVIS hands a
# self-contained task to the local Hermes agent, which runs as a SEPARATE
# process with the FULL toolset, so it can actually get work done on this box.
#
# SAFETY MODEL (scope A, approved by user 2026-08-22):
#   * READ / compute / safe-local actions run immediately — "fully utilize".
#   * DESTRUCTIVE actions (delete/overwrite/install/git push/kill/sudo/...) are
#     NOT auto-run. The first call returns a NEEDS_CONFIRM notice describing the
#     action; only a second call with confirm=true executes it. This is the
#     user's "I approve destructive acts" gate, enforced structurally — Hermes
#     never sees the destructive task until confirmation is given.
#
# This is intentionally distinct from the broken delegate_task(agent="hermes"),
# which used `hermes -p` (not a real flag) and had no confirm gate.
_HERMES_BIN = shutil.which("hermes") or "hermes"

# Full toolset: Hermes may use everything. (Keep this as the live default; the
# confirm-regex below is what restrains destructive work, not tool stripping.)
_HERMES_FULL_TOOLSETS = (
    "web,browser,terminal,file,code_execution,vision,video,image_gen,"
    "video_gen,bfl,x_search,tts,skills,todo,memory,session_search,clarify,"
    "delegation,cronjob,computer_use"
)

# Lexical guard: a task matching any of these is genuinely destructive and MUST
# be confirmed before Hermes is launched with it (scope A). Narrow on purpose —
# "create/write a NEW file" (e.g. manifest, a remembered note) is SAFE and must
# NOT be gated, so we only match delete/overwrite/install/git-push/kill/sudo/deploy.
_DESTRUCTIVE_RE = re.compile(
    r"""(?ix)
      \b(rm|del|delete|remove|erase|wipe|purge|shred|trash|format|drop|unlink)\b
    | \b(overwrite|clobber|replace\s+(the\s+)?file)\b
    | \b(kill|terminate|shutdown|reboot|halt|stop\s+the\s+(process|service))\b
    | \b(git\s+(push|reset|clean|checkout|rm|branch\s+-D))\b
    | \b(chmod|chown|mkfs|sudo|su\s)\b
    | \b(install|pip\s+install|npm\s+(i|install)|curl\b.*\|\s*(sh|bash))\b
    | \b(deploy|migrate\s+database|drop\s+database)\b
    """
)

# Module-level latch so a confirm only releases the EXACT pending task.
_PENDING_DESTRUCTIVE = {"task": None}


# Phase 4: fire-and-forget acknowledgment token. When Hermes is delegated in the
# background, think() returns this immediately so the voice/mic path stays live;
# the real answer arrives later via the on_done callback.
HERMES_BACKGROUND_ACK = "⟳ HERMES_BACKGROUND: On it, sir, working on that now…"


def _run_hermes_sync(task: str, timeout: int, max_turns: int,
                      progress_cb=None) -> str:
    """Synchronously launch Hermes and return its parsed reply (blocking).

    Phase 2 (warm harness): uses persistent session via warm_harness module
    instead of spawning a fresh subprocess each turn. Session survives JARVIS
    restarts via disk cache. Falls back to fresh spawn if warm session fails.
    Phase 4: progress_cb streams milestone status to the caller (WS/voice).
    """
    from warm_harness import warm_send
    return warm_send(task, timeout=timeout, max_turns=max_turns,
                     progress_cb=progress_cb)


def delegate_to_hermes(task: str, timeout: int = 300, max_turns: int = 15,
                       confirm: bool = False, raw_task: str = None,
                       background: bool = False, on_done=None,
                       progress_cb=None) -> str:
    """Hand a task to the local Hermes agent (full toolset) and report its answer.

    Hermes is the executor: it can use the terminal, files, browser, code,
    subagents, and more to actually do the work on this machine.

    APPROVAL GATE: if the task looks destructive (delete/overwrite/install/git
    push/kill/sudo/...), the first call does NOT launch Hermes. It returns a
    NEEDS_CONFIRM notice. Call again with the SAME task and confirm=true to run
    it. Read-only / safe tasks run immediately with no confirmation.

    `raw_task` (internal): when a wrapper prepends context (profile/session/
    grounding) to `task`, the destructive check must run on the USER's original
    words, not on the injected context (which legitimately contains words like
    "delete"/"install" inside the safety instructions and would false-positive).
    If provided, the gate is evaluated against `raw_task`.

    `progress_cb` (Phase 4): callable invoked with status strings at milestones
    ("Delegating to Hermes...", "Hermes finished.") so the caller can stream
    progress to the user while Hermes runs.

    ASYNC / fire-and-forget (Phase 4): when `background=True` and `on_done` is a
    callable, Hermes is launched in a daemon thread and this returns IMMEDIATELY
    with a "working" acknowledgment token (HERMES_BACKGROUND_ACK). The real answer
    is delivered later by calling `on_done(result)`. This keeps the voice/mic path
    responsive instead of freezing for the ~2-min Hermes cold start. The sync path
    (background=False) is unchanged and blocks until Hermes returns.
    """
    task = str(task or "").strip()
    if not task:
        return "[Error] No task given to delegate to Hermes."

    # Gate the user's real intent, never the injected context.
    gate_target = raw_task if raw_task is not None else task
    gate_target = str(gate_target or "").strip()

    try:
        timeout = max(15, min(int(timeout), 600))
    except (TypeError, ValueError):
        timeout = 300
    try:
        max_turns = max(1, min(int(max_turns), 30))
    except (TypeError, ValueError):
        max_turns = 6

    destructive = bool(_DESTRUCTIVE_RE.search(gate_target))

    # --- Destruction path: enforce the approval gate structurally -----------
    if destructive:
        if not confirm:
            _PENDING_DESTRUCTIVE["task"] = task
            return ("[NEEDS_CONFIRM] That task would change or delete something "
                    "on this machine. If you want me to proceed, say or type "
                    "'confirm' and I'll run it through Hermes: "
                    f"\"{task}\"")
        # confirm=True: only release it if it matches the latched task, so a
        # stray "confirm" can't authorize a different destructive command.
        if _PENDING_DESTRUCTIVE["task"] != task:
            _PENDING_DESTRUCTIVE["task"] = task
            return ("[NEEDS_CONFIRM] Please re-issue the exact task and then "
                    "confirm, so I run the right one.")
        _PENDING_DESTRUCTIVE["task"] = None  # consumed

    # --- Execution path -----------------------------------------------------
    # Phase 4: fire-and-forget. Launch Hermes in a daemon thread, return the
    # "working" ack NOW, and deliver the real answer via on_done when it lands.
    if background and callable(on_done):
        def _bg():
            try:
                result = _run_hermes_sync(task, timeout, max_turns, progress_cb)
            except Exception as e:
                result = f"[Error] Hermes background task failed: {type(e).__name__}: {e}"
            try:
                on_done(result)
            except Exception as e:
                print(f"[HERMES] on_done callback raised: {e}", flush=True)
        threading.Thread(target=_bg, daemon=True).start()
        return HERMES_BACKGROUND_ACK

    return _run_hermes_sync(task, timeout, max_turns, progress_cb)


# ── Grounded Hermes delegation (Pillar C: memory layer) ─────────────────────
# Wraps delegate_to_hermes for JARVIS's use: injects the Tier-2 profile + Tier-3
# rolling session context into the task so Hermes has continuity and ground truth,
# plus an instruction to ground answers in the Second Brain vault and cite sources.
# Keep this import-local so importing tools.py never hard-fails if session_store
# is absent on some deployment.
_PROFILE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "jarvis-profile.md")
_VAULT_HINT = ("Second Brain vault at C:/Users/deped/Documents/Second Brain "
               "(semantic search via turbovec; read/write wiki/evergreen).")


def _load_profile() -> str:
    try:
        with open(_PROFILE_PATH, "r", encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return "(jarvis-profile.md not found)"


def _app_context(task: str) -> str:
    """If the task names any installed app, append resolved launch locations + a
    how-to so the delegated Hermes drives the EXACT path (via its terminal tool)
    instead of guessing. computer_use is a skill, not a loaded tool in the harness,
    so deterministic path/URI guidance is what makes app control actually work."""
    try:
        from machine_capabilities import resolve as _resolve
    except Exception:
        try:
            sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
            from machine_capabilities import resolve as _resolve
        except Exception:
            return ""
    # Known app keywords to probe (broad; resolve() does fuzzy match).
    probes = ["spotify", "vlc", "chrome", "brave", "edge", "firefox", "notepad",
              "word", "excel", "powerpoint", "discord", "telegram", "slack",
              "whatsapp", "teams", "zoom", "obs", "blender", "gimp", "vscode",
              "code", "terminal", "calculator", "photos", "paint", "explorer",
              "steam", "epic", "netflix", "youtube", "music", "browser", "app"]
    found = []
    low = (task or "").lower()
    for p in probes:
        if p in low:
            e = _resolve(p)
            if e:
                line = f"- {e.get('name', p)} ({e.get('kind')}): {e.get('bin')}"
                if "spotify" in e.get("name", "").lower():
                    line += ("  | music search: run `start spotify:search:QUERY` "
                             "(opens the Search pane); then the user picks & plays.")
                found.append(line)
    if not found:
        return ""
    return ("\n[INSTALLED APPS — launch these by EXACT path/URI via your terminal tool]\n"
            + "\n".join(found) +
            "\nTo open: `start \"\" \"<path>.lnk\"` or `start \"\" \"<path>.exe\"`. "
            "Do NOT guess paths.\n")


def delegate_to_hermes_grounded(task: str, timeout: int = 300, max_turns: int = 15,
                                confirm: bool = False, background: bool = False,
                                on_done=None, progress_cb=None) -> str:
    """Delegate a task to Hermes WITH JARVIS's memory context attached.

    Prepends: the durable profile (who the user is, machine facts, harness
    contract), the recent session window (so Hermes isn't amnesiac between
    turns), an app-location block (so it can drive installed apps by exact path),
    and a grounding instruction (cite the Second Brain vault, do not
    fabricate). The actual execution/confirm-gate is delegate_to_hermes.

    Use this instead of delegate_to_hermes for any general JARVIS task so the
    harness remembers context and stays grounded against hallucinations.
    """
    from session_store import recent_summary
    profile = _load_profile()
    recent = recent_summary(6)
    appctx = _app_context(task)
    # Phase 13: recall-shaped queries pull matching past turns into context.
    recall_block = ""
    if re.search(r"\b(remember|last time|did i|what did i|previously|"
                 r"before|history of (?:my|our) (?:chats?|conversations?))\b",
                 task, re.I):
        try:
            import session_index as si
            si.rebuild()
            hits = si.search_sessions(task, 3)
            if hits:
                recall_block = ("[PAST SESSION MATCHES]\n" + si.format_hits(hits)
                                + "\n\n")
        except Exception:
            pass
    prefix = (
        "CONTEXT (JARVIS persistent memory):\n"
        f"[PROFILE]\n{profile}\n\n"
        f"[RECENT SESSION]\n{recent}\n\n"
        + (f"{recall_block}" if recall_block else "")
        + f"[KNOWLEDGE BASE] {_VAULT_HINT}\n"
        "Ground factual answers in the Second Brain vault (semantic search it) and "
        "cite the source note name. If the vault has nothing relevant, say so rather "
        "than inventing. Respect the user's stated preferences in [PROFILE].\n\n"
    )
    if appctx:
        prefix += appctx + "\n"
    prefix += "TASK:\n"
    return delegate_to_hermes(prefix + task, timeout=timeout, max_turns=max_turns,
                              confirm=confirm, raw_task=task,
                              background=background, on_done=on_done,
                              progress_cb=progress_cb)


# ── Phase 3: Unified delegate() — single routing primitive ────────────────
# Collapses delegate_task / delegate_to_hermes / delegate_to_hermes_grounded
# into ONE function. The router picks the backend; delegate() executes.

def _detect_backend(task: str) -> str:
    """Auto-detect backend from task text using intake's verb→backend map.
    Returns 'hermes' for anything that doesn't match a local tool."""
    try:
        from intake import verb_backend, resolve_intent, load_corpus
        intent = resolve_intent(task, load_corpus())
        be = verb_backend(intent.verb)
        if be and be in ("music", "desktop", "web", "manus", "chatgpt", "native"):
            return be
    except Exception:
        pass
    return "hermes"


# Local / direct backends (no Hermes spawn). Each returns a string result.
_LOCAL_DISPATCH = {
    "music": lambda task: (
        execute_tool("stop_music", {}) if "stop" in task.lower()
        else execute_tool("play_music", {"query": task})
    ),
    "desktop": lambda task: execute_tool("open_application", {"app": task}),
    "web": lambda task: execute_tool("search_web", {"query": task}),
    "chatgpt": lambda task: execute_tool("ask_chatgpt",
                                         {"prompt": task, "submit": True}),
    "manus": lambda task: __import__("manus_agent").delegate_to_manus(task),
}


def delegate(
    task: str,
    backend: str | None = None,
    confirm: bool = False,
    grounded: bool = True,
    background: bool = False,
    on_done=None,
    timeout: int = 300,
    max_turns: int = 15,
    progress_cb=None,
) -> str:
    """Unified delegation — the ONE primitive for routing tasks.

    backend: "hermes" | "music" | "desktop" | "web" | None (auto-detect).
    grounded: inject JARVIS memory context (profile, session, vault) — default True.
    confirm: pass True to run a previously-confirmed destructive task.
    background/on_done: async fire-and-forget (Phase 4 voice pattern).
    progress_cb: Phase 4 — stream milestone status ("Delegating to Hermes...").

    Local backends (music/desktop/web) run instantly via execute_tool().
    Hermes backend runs the full agent with confirm gate + optional grounding.
    """
    task = str(task or "").strip()
    if not task:
        return "[Error] No task given."

    # ---- Phase 9: conversation window (local fast path context) ----------
    try:
        import conversation_window as cw
    except Exception:
        cw = None

    if cw is not None:
        kind = cw.classify(task)

        # 1) Answering our own app-vs-site question ("site" / "the app")
        if kind == "clarify_answer":
            pend = cw.pending_clarify()
            choice = cw.answer_clarify(task)
            if pend and choice:
                name = pend.get("payload", {}).get("name", "")
                cw.append(task, "clarify_answer")
                result = (open_site(name) if choice == "site"
                          else execute_tool("open_application", {"app": name}))
                cw.append(name, "command",
                          tool=("open_site" if choice == "site"
                                else "open_application"), result=result)
                return result
            # unclear answer -> fall through to normal routing, drop the ask
            cw.append(task, "command")

        # 2) Fragment / follow-up ("also youtube", "again")
        elif kind in ("fragment", "contextual"):
            comp = cw.complete_fragment(re.sub(r"^(and|also|too|then)\s+", "",
                                               task.strip(), flags=re.I))
            if comp:
                tag, target = comp
                if tag == "repeat":
                    cw.append(task, "fragment", tool=tag)
                    return f"Repeating: {target or 'previous action'}."
                if tag == "open_target":
                    resolved = resolve_open_target(target)
                    if resolved[0] == "site":
                        cw.append(task, "fragment")
                        result = open_site(resolved[1])
                        cw.append(target, "command", tool="open_site",
                                  result=result)
                        return result
                    if resolved[0] == "app":
                        cw.append(task, "fragment")
                        result = execute_tool("open_application",
                                              {"app": resolved[1]})
                        cw.append(target, "command", tool="open_application",
                                  result=result)
                        return result
                    # fragment named something unknown -> let normal flow try
            cw.append(task, "fragment")

    # ---- Phase 10: search-target routing ---------------------------------
    # "search X in chatgpt" -> ChatGPT history; "... in gemini" -> typed into
    # Gemini; correction ("I mean ...") replaces the previous query.
    if cw is not None:
        try:
            parsed = parse_search_command(task)
        except Exception:
            parsed = None
        if parsed is not None:
            corrected = bool(parsed.get("corrected"))
            prev = cw.last()
            if corrected and prev and prev.get("tool") in ("search_web",
                                                           "execute_search"):
                # replace the previous turn's query with this one
                result = execute_search(parsed)
                cw.append(task, "command", tool="execute_search", result=result)
                return (f"Corrected — {result}" if "[error]" not in result.lower()
                        else result)
            result = execute_search(parsed)
            cw.append(task, "command", tool="execute_search", result=result)
            return result

    # ---- Phase 8 fast path: dual-registry "open X" -----------------------
    if re.match(r"^(open|launch|go to|goto)\b", task.lower()):
        stripped = re.sub(r"^(open|launch|go to|goto)\s+", "", task, flags=re.I)
        kind8, target8 = resolve_open_target(stripped)
        if kind8 == "site":
            result = open_site(target8)
            if cw is not None:
                cw.append(task, "command", tool="open_site", result=result)
            return result
        if kind8 == "clarify" and isinstance(target8, dict):
            if cw is not None:
                cw.append(task, "clarify", payload={"name": target8["name"]})
            return (f"Found '{target8['name']}' both as an installed app and a "
                    f"website — which one, the app or the site?")
        if kind8 == "app":
            result = execute_tool("open_application", {"app": target8})
            if cw is not None:
                cw.append(task, "command", tool="open_application",
                          result=result)
            return result
        if cw is not None:
            cw.append(task, "command")

    if backend is None:
        backend = _detect_backend(task)

    # Fast path: local tools (no Hermes spawn)
    handler = _LOCAL_DISPATCH.get(backend)
    if handler:
        return handler(task)

    # Hermes path: full agent with confirm gate + grounding
    if grounded:
        return delegate_to_hermes_grounded(
            task, timeout=timeout, max_turns=max_turns,
            confirm=confirm, background=background, on_done=on_done,
            progress_cb=progress_cb)
    return delegate_to_hermes(
        task, timeout=timeout, max_turns=max_turns,
        confirm=confirm, raw_task=task,
        background=background, on_done=on_done,
        progress_cb=progress_cb)


def write_to_notepad(content: str, filename: str = "") -> str:
    """Put text into a Notepad window and show it to the user.

    Deliberately file-backed rather than keystroke-driven: JARVIS is a voice
    assistant, so a SendKeys approach would type into whatever window happened
    to have focus when the user spoke, mangle any character needing a modifier,
    and silently lose the text if Notepad was slow to appear. Writing the file
    first and handing it to Notepad is atomic, survives a crash, and leaves the
    user with something they can actually save.
    """
    try:
        if filename and str(filename).strip():
            p = _resolve_write_path(str(filename).strip())
            if p.suffix == "":
                p = p.with_suffix(".txt")
        else:
            stamp = datetime.datetime.now().strftime("%Y-%m-%d %H%M")
            p = DEFAULT_WRITE_DIR / f"JARVIS Note {stamp}.txt"

        p.parent.mkdir(parents=True, exist_ok=True)
        # Notepad on older Windows builds only renders CRLF correctly.
        text = str(content).replace("\r\n", "\n").replace("\n", "\r\n")
        p.write_text(text, encoding="utf-8")

        if platform.system() == "Windows":
            subprocess.Popen(["notepad.exe", str(p)])
        else:
            os.startfile(str(p))  # pragma: no cover - non-Windows dev only
        return f"Opened Notepad with {len(str(content))} characters, saved as {p}"
    except Exception as e:
        return f"[Error] Could not write to Notepad: {str(e)}"


# Spoken name -> what Windows can actually launch. A user says "open Microsoft
# Word"; ShellExecute needs "winword". Without this the model passes the human
# name through verbatim and every Office app fails to open.
APP_ALIASES = {
    "word": "winword", "microsoft word": "winword", "ms word": "winword",
    "excel": "excel", "microsoft excel": "excel", "ms excel": "excel",
    "powerpoint": "powerpnt", "microsoft powerpoint": "powerpnt",
    "power point": "powerpnt", "ms powerpoint": "powerpnt",
    "outlook": "outlook", "microsoft outlook": "outlook",
    "notepad": "notepad.exe", "note pad": "notepad.exe",
    "calculator": "calc", "calc": "calc",
    "paint": "mspaint", "ms paint": "mspaint",
    "file explorer": "explorer", "explorer": "explorer", "files": "explorer",
    "command prompt": "cmd", "cmd": "cmd", "terminal": "cmd",
    "task manager": "taskmgr",
    "edge": "msedge", "microsoft edge": "msedge",
    "chrome": "chrome", "google chrome": "chrome",
    "brave": "brave", "brave browser": "brave",
    "settings": "ms-settings:",
}


# Leading verbs/filler the voice model often includes in the app argument.
_APP_VERB_RE = re.compile(
    r"^(?:please\s+)?(?:can you\s+)?"
    r"(?:open|launch|start|run|boot|fire up|bring up|pull up|show me|"
    r"open up|start up)\s+(?:the\s+|my\s+|a\s+)*", re.IGNORECASE)
_APP_TRAIL_RE = re.compile(r"[.\s]+$|[.,!?;:]+")


def _clean_app_name(app: str) -> str:
    """Strip command verbs, articles and punctuation from an app request.

    The brain sometimes passes a whole sentence ('Open the Microsoft Word.')
    instead of just the app name; this reduces it to 'Microsoft Word'.
    """
    s = str(app).strip()
    prev = None
    while prev != s:
        prev = s
        s = _APP_VERB_RE.sub("", s).strip()
        s = s.strip('"').strip("'").strip()
        s = _APP_TRAIL_RE.sub("", s).strip()
    return s or str(app).strip()


def open_application(app: str, action: str = None, query: str = None) -> str:
    """Open an application by friendly name, resolving it via the capability manifest.

    Prefers machine_capabilities.resolve() (live scan of PATH / Program Files /
    Start-Menu .lnk) and falls back to APP_ALIASES, then a last-chance .exe strip.
    Non-destructive. For music apps, action="search" opens the in-app search pane.
    """
    import os as _os
    system = platform.system()
    requested = _clean_app_name(app)
    target = APP_ALIASES.get(requested.lower(), requested)

    # 1) Try app_registry.json first (fresh scan), then capabilities.json.
    entry = None
    try:
        from machine_capabilities import load_registry
        reg = load_registry()
        if reg:
            n = requested.lower().strip()
            apps = reg.get("apps", {})
            # exact match, then substring
            for key in (n, requested.lower()):
                if key in apps:
                    entry = apps[key]
                    break
            if not entry:
                for key in apps:
                    if n in key or key in n:
                        entry = apps[key]
                        break
            # Friendly product names whose exe keys share no word with them
            # ("microsoft word" vs key "winword"). Map to the exe stem and
            # retry exact/substring before falling to token scoring.
            if not entry:
                _PRODUCT_EXE = {
                    "word": "winword", "excel": "excel", "powerpoint": "powerpnt",
                    "outlook": "outlook", "onenote": "onenote", "access": "msaccess",
                    "teams": "teams", "edge": "msedge", "paint": "mspaint",
                    "calculator": "calculator", "vs code": "code",
                    "visual studio code": "code", "vscode": "code",
                }
                for phrase, exe_stem in _PRODUCT_EXE.items():
                    if phrase in n:
                        if exe_stem in apps:
                            entry = apps[exe_stem]
                            break
                        for key in apps:
                            if exe_stem in key:
                                entry = apps[key]
                                break
                        if entry:
                            break
            # Token-overlap scoring: fraction of the registry key's words found
            # in the request ("microsoft office home 2024" vs "office 2024").
            if not entry:
                n_words = set(n.split()) - {"microsoft", "ms", "the", "app", "application"}
                best, best_score = None, 0.0
                for key in apps:
                    k_words = set(key.split())
                    if not k_words:
                        continue
                    inter = n_words & k_words
                    if not inter:
                        continue
                    score = len(inter) / min(len(k_words), max(1, len(n_words)))
                    if score > best_score:
                        best_score = score
                        best = key
                if best and best_score >= 0.5:
                    entry = apps[best]
    except Exception:
        pass
    if not entry:
        try:
            from machine_capabilities import resolve as _resolve
        except Exception:
            try:
                sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
                from machine_capabilities import resolve as _resolve
            except Exception:
                _resolve = None
        entry = _resolve(requested) if _resolve else None
    binp = entry.get("bin") if entry else None
    name = entry.get("name", requested) if entry else requested

    # 2) Spotify (or other music) search: reliable URI opens the Search pane.
    if action == "search" and "spotify" in name.lower():
        q = (query or "").strip()
        uri = "spotify:search:" + q.replace(" ", "%20")
        try:
            _os.startfile(uri)
            return (f"Opened Spotify search for '{q}'. The Search pane is showing "
                    f"results for '{q}' — pick the track and press play.")
        except Exception as e:
            return f"[Error] Could not open Spotify search URI: {e}"

    # 3) Launch the resolved binary / alias / raw name.
    cand = binp or target
    try:
        if system == "Windows":
            _os.startfile(cand)
        elif system == "Darwin":
            subprocess.run(["open", "-a", cand])
        else:
            subprocess.run(["xdg-open", cand])
        return f"Opened {name}" + (f" from {binp}" if binp else "")
    except Exception as e:
        # Last chance: strip/add .exe before giving up.
        alt = cand[:-4] if cand.lower().endswith(".exe") else cand + ".exe"
        try:
            if system == "Windows":
                _os.startfile(alt)
                return f"Opened {name}"
        except Exception:
            pass
        # 6) Broken shortcut? Try to find a replacement by re-scanning.
        try:
            from machine_capabilities import find_replacement, REGISTRY_PATH
            repair = find_replacement(requested)
            if repair:
                # Update the registry with the fix
                from machine_capabilities import load_registry
                reg = load_registry() or {"apps": {}}
                reg["apps"][repair["name"]] = {
                    "bin": repair["bin"], "kind": "gui",
                    "category": _categorize(repair["name"]),
                    "confidence": "repaired"}
                with open(REGISTRY_PATH, "w", encoding="utf-8") as f:
                    json.dump(reg, f, indent=2)
                if system == "Windows":
                    _os.startfile(repair["bin"])
                return f"Repaired and opened {repair['name']} from {repair['bin']}"
        except Exception:
            pass
        if not entry:
            return (f"[Error] Could not open {requested} (tried '{cand}'): {e}. "
                    f"Say 'scan installed software' to refresh the manifest.")
        return f"[Error] Could not open {name} from {binp}: {e}"


def rescan_applications() -> str:
    """Rescan all installed software and update the app registry.
    Reports broken shortcuts found during the scan."""
    from machine_capabilities import write_registry, load_registry
    result = write_registry()
    reg = load_registry()
    if reg:
        broken = []
        for name, entry in reg.get("apps", {}).items():
            binp = entry.get("bin")
            if binp and not os.path.exists(binp):
                broken.append(name)
        if broken:
            result += f"\n{len(broken)} broken shortcuts: {', '.join(broken[:15])}"
            if len(broken) > 15:
                result += f" (and {len(broken)-15} more)"
    return result


def close_application(app: str) -> str:
    """Close a running application by friendly name, via registry lookup.

    Resolves the app exactly like open_application, derives its exe name,
    then asks it to quit gracefully (WM_CLOSE) before force-killing.
    """
    requested = _clean_app_name(app)
    # Strip the close verb itself — callers may pass the whole utterance
    # ('close it', 'close the spotify') rather than a bare app name.
    requested = re.sub(
        r"^(?:please\s+)?(?:close|quit|exit|kill|shut down)\s+(?:the\s+|my\s+|a\s+)*",
        "", requested, flags=re.IGNORECASE).strip()
    if requested.lower() in {"it", "this", "that", "app", "application",
                             "the", "a", "my", ""}:
        return "[Error] Which app should I close? Name it, e.g. 'close Spotify'."

    # Resolve through the same chain as open_application.
    entry = None
    try:
        from machine_capabilities import load_registry, resolve as _resolve
        reg = load_registry()
        if reg:
            n = requested.lower().strip()
            apps = reg.get("apps", {})
            if n in apps:
                entry = apps[n]
            else:
                for key in apps:
                    if n in key or key in n:
                        entry = apps[key]
                        break
            if not entry:
                _PRODUCT_EXE = {
                    "word": "winword", "excel": "excel", "powerpoint": "powerpnt",
                    "outlook": "outlook", "onenote": "onenote", "edge": "msedge",
                }
                for phrase, exe_stem in _PRODUCT_EXE.items():
                    if phrase in n and exe_stem in apps:
                        entry = apps[exe_stem]
                        break
        if not entry:
            entry = _resolve(requested)
    except Exception:
        entry = None

    binp = (entry or {}).get("bin") or ""
    exe_name = os.path.splitext(os.path.basename(binp.strip('"')))[0] if binp else ""
    name = (entry or {}).get("name", requested)

    if not exe_name:
        # No registry entry: try the raw name as an image name.
        exe_name = requested

    def _running(image: str) -> bool:
        r = subprocess.run(["tasklist", "/FI", f"IMAGENAME eq {image}.exe"],
                           capture_output=True, text=True,
                           encoding="utf-8", errors="replace")
        return image.lower() in (r.stdout or "").lower()

    if not _running(exe_name):
        return f"{name} is not running."

    # 1) Graceful: WM_CLOSE to all windows of the process.
    ps = (f"Get-Process -Name '{exe_name}' -ErrorAction SilentlyContinue | "
          "ForEach-Object { $_.CloseMainWindow() } | Out-Null")
    subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                   capture_output=True, timeout=15)
    time.sleep(2)
    if not _running(exe_name):
        return f"Closed {name}."

    # 2) Force: taskkill without /F first (sends WM_QUIT), then /F as last resort.
    subprocess.run(["taskkill", "/IM", f"{exe_name}.exe"],
                   capture_output=True, timeout=15)
    time.sleep(1)
    if not _running(exe_name):
        return f"Closed {name}."

    subprocess.run(["taskkill", "/IM", f"{exe_name}.exe", "/F"],
                   capture_output=True, timeout=15)
    time.sleep(1)
    if not _running(exe_name):
        return f"Force-closed {name}."
    return f"[Error] Could not close {name} ({exe_name}.exe is still running)."


def _resolve_spotify_track(query: str) -> str:
    """Resolve a free-text song/artist query to a spotify:track:<id> URI using the
    public open.spotify.com search page rendered headlessly (no auth, no Premium).
    Returns the URI string or '' if resolution failed."""
    import re as _re
    try:
        from playwright.sync_api import sync_playwright
    except Exception as e:
        return ""
    q = (query or "").strip()
    if not q:
        return ""
    # Already a URI? pass through.
    if q.startswith("spotify:"):
        return q
    uri = ""
    try:
        import tempfile as _tf
        _prof = _tf.mkdtemp(prefix="jv_spotify_res_")
        with sync_playwright() as p:
            # launch_persistent_context (NOT launch + --user-data-dir, which Playwright rejects)
            ctx = p.chromium.launch_persistent_context(_prof, headless=True)
            pg = ctx.new_page()
            # Spotify is JS-rendered and rate-limits headless scrapers, so retry a few
            # times, each waiting for a real track link + scrolling to force lazy-load.
            enc = _re.sub(r"\s+", "%20", q)
            seen = []
            for attempt in range(3):
                try:
                    pg.goto(f"https://open.spotify.com/search/{enc}",
                            wait_until="domcontentloaded", timeout=20000)
                except Exception:
                    pass
                # wait for a track anchor, up to ~8s
                try:
                    pg.wait_for_selector("a[href*='/track/']", timeout=8000)
                except Exception:
                    pass
                # scroll the results grid to trigger lazy rendering
                for _ in range(3):
                    pg.mouse.wheel(0, 1200)
                    pg.wait_for_timeout(800)
                uris = _re.findall(r"spotify:track:[A-Za-z0-9]+", pg.content())
                if not uris:
                    # open.spotify.com serves results as JS-rendered <a href="/track/<id>">
                    # links; the spotify:track: form is no longer in the HTML, so read
                    # the hrefs directly and convert them to spotify:track: URIs.
                    try:
                        hrefs = pg.eval_on_selector_all(
                            'a[href*="/track/"]',
                            'els => els.map(e => e.getAttribute("href") || "").filter(Boolean)')
                        for h in hrefs:
                            m = _re.search(r"/track/([A-Za-z0-9]+)", h or "")
                            if m:
                                uris.append("spotify:track:" + m.group(1))
                    except Exception:
                        pass
                for u in uris:
                    if u not in seen:
                        seen.append(u)
                if seen:
                    break
                pg.wait_for_timeout(2500)  # back off before retry
            uri = seen[0] if seen else ""
            ctx.close()
    except Exception:
        uri = ""
    return uri


def play_spotify(query: str) -> str:
    """Play a song/artist/album on the user's DESKTOP Spotify app (the spicetify-
    patched install), hands-free — no Premium, no credentials, no GUI clicks.

    Flow: if `query` is already a spotify: URI, open it directly; otherwise resolve
    the name to a track URI via the public search page, then `os.startfile` the
    track URI, which makes the desktop app start playback. Returns a short status
    string for speech. On any failure it says plainly what went wrong."""
    import os as _os
    q = (query or "").strip()
    if not q:
        return "Say what you'd like to play, sir."
    # Ensure Spotify is running (the desktop app must be open to receive the URI).
    try:
        from machine_capabilities import resolve as _resolve
    except Exception:
        try:
            sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
            from machine_capabilities import resolve as _resolve
        except Exception:
            _resolve = None
    spot = _resolve("spotify") if _resolve else None
    if spot and spot.get("bin"):
        try:
            _os.startfile(spot["bin"])
        except Exception:
            pass
        import time as _t
        _t.sleep(3)
    uri = _resolve_spotify_track(q)
    if not uri:
        return (f"I couldn't find a Spotify track for '{q}', sir. Try a more "
                f"specific title or artist.")
    try:
        _os.startfile(uri)
        return f"Playing {q} on Spotify, sir."
    except Exception as e:
        return f"[Error] Could not start playback: {e}"


def stop_spotify() -> str:
    """Pause the desktop Spotify app by sending the system MediaPlayPause key to the
    foreground window (no auth, no GUI clicks). Returns a short status string."""
    try:
        import ctypes
        # VK_MEDIA_PLAY_PAUSE = 0xB3
        ctypes.windll.user32.keybd_event(0xB3, 0, 0, 0)
        ctypes.windll.user32.keybd_event(0xB3, 0, 2, 0)
        return "Paused Spotify, sir."
    except Exception as e:
        return f"[Error] Could not pause Spotify: {e}"


def get_weather(city: str = "Manila") -> str:
    """Get current weather for a city via wttr.in.

    Fetched in-process rather than by shelling out to curl: wttr.in replies in
    UTF-8, and subprocess text mode decodes with the Windows cp1252 locale,
    which blows up on the weather glyphs and loses the whole response.
    """
    url = f"https://wttr.in/{urllib.parse.quote(city)}?format=%l:+%C+%t+feels+like+%f+humidity+%h+wind+%w"
    req = urllib.request.Request(url, headers={"User-Agent": "curl/8"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            text = r.read().decode("utf-8", "replace").strip()
    except Exception as e:
        return f"[Error] Weather lookup failed for {city}: {type(e).__name__}: {e}"
    if not text or "Unknown location" in text:
        return f"[Error] Weather service returned no data for {city}"
    # wttr encodes wind direction as an arrow glyph; spell it out for text-to-speech.
    for arrow, word in (("↑", "from the south"), ("↓", "from the north"),
                        ("←", "from the east"), ("→", "from the west"),
                        ("↖", "from the southeast"), ("↗", "from the southwest"),
                        ("↘", "from the northwest"), ("↙", "from the northeast")):
        text = text.replace(arrow, word + " ")
    return text


# Tool definitions for Claude API
TOOLS = [
    {
        "name": "read_file",
        "description": "Read the contents of a file.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Path to the file to read"
                }
            },
            "required": ["path"]
        }
    },
    {
        "name": "write_file",
        "description": "Write content to a file. Creates parent directories if needed.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Path to write to"
                },
                "content": {
                    "type": "string",
                    "description": "Content to write"
                },
                "overwrite": {
                    "type": "boolean",
                    "description": "Replace an existing file. Explicit requests only."
                }
            },
            "required": ["path", "content"]
        }
    },
    {
        "name": "list_directory",
        "description": "List contents of a directory.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Directory path (default: current directory)"
                }
            }
        }
    },
    {
        "name": "search_web",
        "description": (
            "Open a Google search in the user's browser. Returns only a confirmation that "
            "the tab was opened - it does NOT return search results or page content."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Search query"
                }
            },
            "required": ["query"]
        }
    },
    {
        "name": "write_to_notepad",
        "description": (
            "Put text into a Notepad window for the user to read or keep. Use this "
            "for any request to write something in Notepad, jot a note, or show text "
            "in Notepad. The text is saved to a .txt file and opened in Notepad."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "content": {"type": "string", "description": "The text to put in Notepad"},
                "filename": {
                    "type": "string",
                    "description": "Optional file name; defaults to a timestamped note in Documents"
                }
            },
            "required": ["content"]
        }
    },
    {
        "name": "open_application",
        "description": ("Open an installed application by friendly name (resolves via the "
                        "capability manifest). For music apps, action='search' opens the "
                        "in-app search pane with 'query'."),
        "input_schema": {
            "type": "object",
            "properties": {
                "app": {
                    "type": "string",
                    "description": "Application name (e.g. 'spotify', 'vlc', 'chrome') or path"
                },
                "action": {
                    "type": "string",
                    "description": "Optional: 'search' (music apps) or omit to just launch"
                },
                "query": {
                    "type": "string",
                    "description": "Search term when action='search'"
                }
            },
            "required": ["app"]
        }
    },
    {
        "name": "open_site",
        "description": ("Open a website by name from the user's site registry "
                        "(web_registry.json — manual entries + their most-visited "
                        "domains) in a new tab of the JARVIS browser. Also accepts "
                        "a raw URL. Use for 'open facebook', 'open my email', etc."),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Site name or alias (e.g. 'facebook', 'email')"},
                "url": {"type": "string", "description": "Direct URL; bypasses registry lookup"}
            },
            "required": ["name"]
        }
    },
    {
        "name": "rescan_sites",
        "description": ("Rescan the default browser's history and update the user's "
                        "site registry (most-visited domains). Use when the user says "
                        "'rescan my sites' or asks JARVIS to learn their sites."),
        "input_schema": {"type": "object", "properties": {}}
    },
    {
        "name": "list_sites",
        "description": ("List the sites in the user's site registry with visit counts. "
                        "Use for 'what sites do you know'."),
        "input_schema": {"type": "object", "properties": {}}
    },
    {
        "name": "add_site",
        "description": ("Manually register a website (manual entries always win over "
                        "history scans). Use when the user says 'add site <name> <url>'."),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Short name to call it by (e.g. 'school')"},
                "url": {"type": "string", "description": "The site URL"},
                "aliases": {"type": "array", "items": {"type": "string"},
                            "description": "Optional alternative names"}
            },
            "required": ["name", "url"]
        }
    },
    {
        "name": "search_sessions",
        "description": ("Search past JARVIS voice conversations by keyword "
                        "(full-text over all session transcripts). Use when the "
                        "user asks what they said before, to recall an earlier "
                        "request, or references something from a previous day."),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Keywords to search for"},
                "limit": {"type": "integer", "description": "Max results (default 10)"}
            },
            "required": ["query"]
        }
    },
    {
        "name": "search_chatgpt_history",
        "description": "Search past ChatGPT conversations by keyword. Read-only.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Keyword to search for"},
                "limit": {"type": "integer", "description": "Max conversations (default 10)"}
            },
            "required": ["query"]
        }
    },
    {
        "name": "open_chatgpt_conversation",
        "description": "Open a past ChatGPT conversation by title fragment and read it back. Read-only.",
        "input_schema": {
            "type": "object",
            "properties": {
                "title_contains": {"type": "string", "description": "Part of the conversation title"}
            },
            "required": ["title_contains"]
        }
    },
    {
        "name": "ask_chatgpt",
        "description": (
            "Type a prompt into the ChatGPT website in a real browser. Only sends it "
            "when submit is true; otherwise the text is left in the box unsent."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "prompt": {
                    "type": "string",
                    "description": "The prompt text to type into ChatGPT"
                },
                "submit": {
                    "type": "boolean",
                    "description": "Send the prompt. Only true when the user explicitly asked to send."
                }
            },
            "required": ["prompt"]
        }
    },
    {
        "name": "manus_status",
        "description": (
            "Check whether the Manus AI delegation backend is configured (user's "
            "Manus cookies present locally). Read-only. Returns setup instructions "
            "if not configured, or a ready state if it is."
        ),
        "input_schema": {
            "type": "object",
            "properties": {}
        }
    },
    {
        "name": "browser_status",
        "description": "Check whether the automation browser is open and signed in to ChatGPT.",
        "input_schema": {
            "type": "object",
            "properties": {}
        }
    },
    {
        "name": "play_music",
        "description": (
            "Play music by searching YouTube Music (music.youtube.com) in a Brave "
            "window and clicking the first song result. Use for any request to play "
            "a song, artist or genre."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Song, artist or genre to play"}
            },
            "required": ["query"]
        }
    },
    {
        "name": "stop_music",
        "description": "Stop the music that play_music started.",
        "input_schema": {
            "type": "object",
            "properties": {}
        }
    },
    {
        "name": "get_credentials",
        "description": (
            "Look up the saved username and password for a project from the vault's "
            "credential notes. Returns the username and a masked password. Read-only."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "project": {"type": "string", "description": "Project or note name, e.g. iRIMS-V"}
            },
            "required": ["project"]
        }
    },
    {
        "name": "launch_project",
        "description": (
            "Start a project: run its dev server, wait for it to come up, open it in "
            "Brave and sign in with the saved credentials when the project has them."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Project name from projects.json"}
            },
            "required": ["name"]
        }
    },
    {
        "name": "compose_report",
        "description": (
            "Write an accomplishment report on a topic as a Word document, using past "
            "ChatGPT conversations about it as the source material."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "topic": {"type": "string", "description": "What the report is about"},
                "output_path": {"type": "string", "description": "Where to save the .docx (optional)"},
                "verbatim": {
                    "type": "boolean",
                    "description": ("True to copy the conversations word for word instead of "
                                    "summarising them. Use when the user asks to quote, copy, "
                                    "paste or keep the exact wording.")
                }
            },
            "required": ["topic"]
        }
    },
    {
        "name": "delegate",
        "description": (
            "Unified delegation — the ONE primitive for routing tasks. Auto-detects "
            "the best backend (music / desktop / web / chatgpt / manus run instantly "
            "via local tools; everything else goes to hermes, the full agent with the "
            "machine toolkit). Hermes path includes a confirm gate for destructive "
            "tasks and optional memory grounding (profile, session context, vault "
            "citation). Use this INSTEAD of delegate_to_hermes / "
            "delegate_to_hermes_grounded for all new tool calls."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "task": {"type": "string", "description": "The self-contained task to perform"},
                "backend": {"type": "string", "description": "Force backend: hermes, music, desktop, web, chatgpt, manus (or null for auto-detect)"},
                "confirm": {"type": "boolean", "description": "Set true ONLY to run a previously-confirmed destructive task"},
                "grounded": {"type": "boolean", "description": "Inject JARVIS memory context (default true). Set false for raw Hermes."},
                "timeout": {"type": "integer", "description": "Max seconds (15-600, default 300)"},
                "max_turns": {"type": "integer", "description": "Max agent iterations (1-30, default 15)"}
            },
            "required": ["task"]
        }
    }
]


def _truthy(v) -> bool:
    """Strict opt-in. Models emit "true"/"True"/1 as often as real booleans, but
    anything unrecognised must fall back to NOT sending."""
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return v == 1
    if isinstance(v, str):
        return v.strip().lower() in ("true", "yes", "1")
    return False


def _ask_chatgpt(**kw) -> str:
    import browser_agent
    submit = _truthy(kw.get("submit", False))
    # Audit line: every send decision the model made is recoverable from the log.
    print(f"[TOOL] ask_chatgpt submit={submit} (raw={kw.get('submit')!r}) "
          f"prompt={kw['prompt'][:80]!r}", flush=True)
    return browser_agent.ask_chatgpt(prompt=kw["prompt"], submit=submit)


def _ask_ai(**kw) -> str:
    """Write a prompt into a web AI (chatgpt/gemini/claude/copilot) or the
    Copilot desktop app. Sends ONLY on explicit consent."""
    import browser_agent
    submit = _truthy(kw.get("submit", False))
    site = (kw.get("site") or "chatgpt").strip()
    prompt = kw.get("prompt") or ""
    print(f"[TOOL] ask_ai site={site} submit={submit} "
          f"prompt={prompt[:80]!r}", flush=True)
    if site.lower() in {"copilot desktop", "copilotapp"}:
        return _ask_copilot_desktop(prompt, submit)
    return browser_agent.ask_web_ai(prompt=prompt, site=site, submit=submit)


def _ask_copilot_desktop(prompt: str, submit: bool) -> str:
    """Drive the installed Microsoft Copilot app via computer control:
    open it, type the prompt; send only on consent."""
    import subprocess as _sp, time as _time
    exe = r"C:\Program Files (x86)\Microsoft\EdgeCore\151.0.4129.101\copilotapp.exe"
    if not os.path.exists(exe):
        return "[Error] The Copilot desktop app is not at its expected path."
    try:
        _sp.Popen([exe])
        _time.sleep(4)
        import pyautogui
        pyautogui.typewrite(prompt, interval=0.02)
        if not submit:
            return (f"Typed into the Copilot app and left it unsent: {prompt}. "
                    "Say the word and I'll send it.")
        pyautogui.press("enter")
        _time.sleep(8)
        return f"Sent to the Copilot desktop app: {prompt}. Its reply is on screen."
    except Exception as e:
        return f"[Error] Driving the Copilot app failed: {e}"


# Tool dispatcher
TOOL_MAP = {
    "read_file": lambda **kw: read_file(kw["path"]),
    "write_file": lambda **kw: write_file(kw["path"], kw["content"],
                                          _truthy(kw.get("overwrite", False))),
    "write_to_notepad": lambda **kw: write_to_notepad(kw["content"], kw.get("filename", "")),
    "list_directory": lambda **kw: list_directory(kw.get("path", ".")),
    "search_chatgpt_history": lambda **kw: __import__("browser_agent").search_chatgpt_history(
        kw["query"], int(kw.get("limit", 10) or 10)),
    "open_chatgpt_conversation": lambda **kw: __import__("browser_agent").open_chatgpt_conversation(
        kw["title_contains"]),
    "search_web": lambda **kw: search_web(kw["query"]),
    "open_site": lambda **kw: open_site(kw["name"], kw.get("url")),
    "rescan_sites": lambda **kw: rescan_sites(),
    "list_sites": lambda **kw: __import__("web_registry").list_sites(),
    "add_site": lambda **kw: __import__("web_registry").add_manual_site(
        kw["name"], kw["url"], kw.get("aliases")),
    "search_sessions": lambda **kw: search_sessions(
        kw["query"], int(kw.get("limit", 10) or 10)),
    "open_application": lambda **kw: open_application(
        kw["app"], kw.get("action"), kw.get("query")),
    "ask_chatgpt": _ask_chatgpt,
    "ask_ai": _ask_ai,
    "manus_status": lambda **kw: __import__("manus_agent").manus_status(),
    "browser_status": lambda **kw: __import__("browser_agent").browser_status(),
    "play_music": lambda **kw: (
        __import__("music_agent").set_progress_cb(getattr(_tools_tls, "cb", None)),
        __import__("music_agent").play_music(kw["query"])
    )[-1],
    "stop_music": lambda **kw: (
        __import__("music_agent").set_progress_cb(getattr(_tools_tls, "cb", None)),
        __import__("music_agent").stop_music()
    )[-1],
    "play_spotify": lambda **kw: play_spotify(kw["query"]),
    "stop_spotify": lambda **kw: stop_spotify(),
    "get_credentials": _get_credentials_tool,
    "launch_project": lambda **kw: __import__("project_agent").launch_project(kw["name"]),
    "compose_report": lambda **kw: __import__("report_agent").compose_report(
        kw["topic"], kw.get("output_path"), _truthy(kw.get("verbatim", False))),
    "rescan_applications": lambda **kw: rescan_applications(),
    "close_application": lambda **kw: close_application(kw["app"]),
    "delegate_task": lambda **kw: delegate_task(kw["task"], kw.get("agent", "claude")),
    "delegate_to_hermes": lambda **kw: delegate_to_hermes(
        kw["task"], int(kw.get("timeout", 300) or 300),
        int(kw.get("max_turns", 6) or 6), _truthy(kw.get("confirm", False))),
    "delegate_to_hermes_grounded": lambda **kw: delegate_to_hermes_grounded(
        kw["task"], int(kw.get("timeout", 300) or 300),
        int(kw.get("max_turns", 6) or 6), _truthy(kw.get("confirm", False))),
    "delegate": lambda **kw: delegate(
        kw["task"], kw.get("backend"),
        _truthy(kw.get("confirm", False)),
        _truthy(kw.get("grounded", True)),
        _truthy(kw.get("background", False)),
        kw.get("on_done"),
        int(kw.get("timeout", 300) or 300),
        int(kw.get("max_turns", 15) or 15)),
}


def execute_tool(name: str, arguments: dict) -> str:
    """Execute a tool by name with arguments."""
    if name not in TOOL_MAP:
        return f"[Error] Unknown tool: {name}"
    # Audit line: makes a hung or misrouted tool call visible in the server log.
    print(f"[TOOL] {name} args={json.dumps(arguments, default=str)[:200]}", flush=True)
    start = time.monotonic()
    confirmed = _truthy(arguments.get("confirm", False))
    try:
        result = TOOL_MAP[name](**arguments)
    except Exception as e:
        result = f"[Tool Error] {name}: {str(e)}"
        print(f"[TOOL] {name} raised {type(e).__name__} after "
              f"{time.monotonic() - start:.1f}s", flush=True)
        try:
            import audit
            audit.log_call(name, arguments, time.monotonic() - start, result,
                           confirmed=confirmed)
        except Exception:
            pass
        return result
    print(f"[TOOL] {name} done in {time.monotonic() - start:.1f}s", flush=True)
    try:
        import audit
        audit.log_call(name, arguments, time.monotonic() - start, result,
                       confirmed=confirmed)
    except Exception:
        pass
    return result
