# System Tools - File ops, shell commands, web search
import os
import subprocess
import json
import platform
import re
import time
import datetime
import webbrowser
import urllib.parse
import urllib.request
from pathlib import Path


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


def write_file(path: str, content: str, overwrite: bool = False) -> str:
    """Write content to a file. Refuses to clobber an existing file unless told to.

    JARVIS is driven by speech, and a misheard filename should not silently
    destroy an existing file, so replacing one is an explicit opt-in.
    """
    try:
        p = Path(path).expanduser()
        if p.exists() and not overwrite:
            size = p.stat().st_size
            return (f"[Blocked] {p.name} already exists ({size:,} bytes) and I did not "
                    "overwrite it. Confirm you want it replaced and I will.")
        p.parent.mkdir(parents=True, exist_ok=True)
        existed = p.exists()
        p.write_text(content, encoding="utf-8")
        verb = "Replaced" if existed else "Written to"
        return f"{verb} {path} ({len(content)} chars)"
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


def open_application(app: str) -> str:
    """Open an application."""
    system = platform.system()
    try:
        if system == "Windows":
            os.startfile(app)
        elif system == "Darwin":
            subprocess.run(["open", "-a", app])
        else:
            subprocess.run(["xdg-open", app])
        return f"Opened {app}"
    except Exception as e:
        return f"[Error] Could not open {app}: {str(e)}"


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
        "name": "run_shell",
        "description": "Execute a shell command on the system. Use for running programs, checking status, system operations.",
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "The shell command to execute"
                }
            },
            "required": ["command"]
        }
    },
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
        "name": "open_application",
        "description": "Open an application by name.",
        "input_schema": {
            "type": "object",
            "properties": {
                "app": {
                    "type": "string",
                    "description": "Application name or path"
                }
            },
            "required": ["app"]
        }
    },
    {
        "name": "get_weather",
        "description": "Get current weather for a city.",
        "input_schema": {
            "type": "object",
            "properties": {
                "city": {
                    "type": "string",
                    "description": "City name (default: Manila)"
                }
            }
        }
    },
    {
        "name": "get_system_info",
        "description": "Get system information (OS, Python version, etc).",
        "input_schema": {
            "type": "object",
            "properties": {}
        }
    },
    {
        "name": "search_files",
        "description": "Search inside files recursively for text; returns matching files and line numbers.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Text to search for"},
                "path": {"type": "string", "description": "Folder to search (default: current directory)"},
                "max_results": {"type": "integer", "description": "Maximum files to report (default 20)"}
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
        "name": "browser_status",
        "description": "Check whether the automation browser is open and signed in to ChatGPT.",
        "input_schema": {
            "type": "object",
            "properties": {}
        }
    },
    {
        "name": "search_vault",
        "description": (
            "Search the user's Second Brain Obsidian vault for text and return the "
            "matching notes with the line that matched. Strictly read-only."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Text to search the vault for"},
                "limit": {"type": "integer", "description": "Maximum notes to return (default 10)"}
            },
            "required": ["query"]
        }
    },
    {
        "name": "read_vault_note",
        "description": (
            "Read one Second Brain note in full, found by part of its filename. "
            "Strictly read-only."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Part of the note's filename"}
            },
            "required": ["name"]
        }
    },
    {
        "name": "open_file",
        "description": (
            "Find a file by part of its name under Documents, Desktop, Downloads, "
            "the Second Brain vault and Portfolio, and open it in its default app."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Part of the file's name"}
            },
            "required": ["name"]
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
                "output_path": {"type": "string", "description": "Where to save the .docx (optional)"}
            },
            "required": ["topic"]
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


# Tool dispatcher
TOOL_MAP = {
    "run_shell": lambda **kw: run_shell(kw["command"]),
    "read_file": lambda **kw: read_file(kw["path"]),
    "write_file": lambda **kw: write_file(kw["path"], kw["content"],
                                          _truthy(kw.get("overwrite", False))),
    "list_directory": lambda **kw: list_directory(kw.get("path", ".")),
    "search_files": lambda **kw: search_files(kw["query"], kw.get("path", "."),
                                              int(kw.get("max_results", 20) or 20)),
    "search_chatgpt_history": lambda **kw: __import__("browser_agent").search_chatgpt_history(
        kw["query"], int(kw.get("limit", 10) or 10)),
    "open_chatgpt_conversation": lambda **kw: __import__("browser_agent").open_chatgpt_conversation(
        kw["title_contains"]),
    "search_web": lambda **kw: search_web(kw["query"]),
    "open_application": lambda **kw: open_application(kw["app"]),
    "get_weather": lambda **kw: get_weather(kw.get("city", "Manila")),
    "get_system_info": lambda **kw: json.dumps(get_system_info(), indent=2),
    "ask_chatgpt": _ask_chatgpt,
    "browser_status": lambda **kw: __import__("browser_agent").browser_status(),
    "search_vault": lambda **kw: search_vault(kw["query"], int(kw.get("limit", 10) or 10)),
    "read_vault_note": lambda **kw: read_vault_note(kw["name"]),
    "open_file": lambda **kw: open_file(kw["name"]),
    "play_music": lambda **kw: __import__("music_agent").play_music(kw["query"]),
    "stop_music": lambda **kw: __import__("music_agent").stop_music(),
    "get_credentials": _get_credentials_tool,
    "launch_project": lambda **kw: __import__("project_agent").launch_project(kw["name"]),
    "compose_report": lambda **kw: __import__("report_agent").compose_report(
        kw["topic"], kw.get("output_path")),
}


def execute_tool(name: str, arguments: dict) -> str:
    """Execute a tool by name with arguments."""
    if name not in TOOL_MAP:
        return f"[Error] Unknown tool: {name}"
    # Audit line: makes a hung or misrouted tool call visible in the server log.
    print(f"[TOOL] {name} args={json.dumps(arguments, default=str)[:200]}", flush=True)
    start = time.monotonic()
    try:
        result = TOOL_MAP[name](**arguments)
    except Exception as e:
        print(f"[TOOL] {name} raised {type(e).__name__} after "
              f"{time.monotonic() - start:.1f}s", flush=True)
        return f"[Tool Error] {name}: {str(e)}"
    print(f"[TOOL] {name} done in {time.monotonic() - start:.1f}s", flush=True)
    return result
