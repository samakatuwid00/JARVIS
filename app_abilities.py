"""app_abilities.py - what JARVIS can do with each registered app.

Phase 1 of the "JARVIS Apps Intelligence" design (2026-09-11). Every app gets
abilities from these sources, cheapest first:
  1. universal     - open, close, switch to
  2. kind template - by app kind: browser, media, office, editor, files, terminal
  3. profile       - popular apps with extra, specific abilities (Spotify, ...)
  4. deep scan     - the real buttons and boxes of the app's open window
The AI sorting pass (sort_apps) decides each app's kind and marks background
noise (services, updaters, OEM helpers) so the panel can hide it.

Abilities live on the registry entry as "abilities" (a user field that rescans
keep). Rebuilding keeps the user's choices on each ability (enabled, level).

    {"id": "spotify.play_pause", "name": "Play or pause", "say": [...],
     "needs": {}, "level": "safe" | "ask" | "never", "how": [ops...],
     "verify": "...", "source": "universal|template|profile|scan",
     "enabled": true}

Nothing here routes voice commands yet: that is the understand-first router
(phase 4). Abilities are shown, switched, and tested from the Apps panel.
"""

import json
import os
import re
import subprocess
import threading
import time
import urllib.parse

import rules_engine

KINDS = ("browser", "media", "office", "editor", "files", "terminal", "chat", "game",
         "utility", "system", "noise", "cli")
LEVELS = ("safe", "ask", "never")
SORT_BATCH = 40          # apps per AI sorting call
MAX_SCANNED = 40         # abilities kept from one deep scan
SCAN_WAIT = 15           # seconds to wait for a launched app's window
MAX_CONTROLS = 1500      # UI elements read per window (big apps have thousands)
FIND_WAIT = 3            # seconds a click/type ability waits for its control
VOLUME_STEPS = 50        # volume-key presses from 100% to 0% (2% each)
SCAN_SOON_DELAY = 6      # seconds after JARVIS opens an app before reading it
RESCAN_AFTER = 86400     # one opportunistic deep scan per app per day

_LOCK = threading.Lock()

# Clicks that change or send something ask first.
_CONSEQUENTIAL_RE = re.compile(
    r"delete|remove|send|post|buy|pay|purchase|order|checkout|submit|install|"
    r"uninstall|format|reset|sign ?out|log ?out|shut ?down|restart|discard|erase", re.I)
# Title-bar buttons every window has; not abilities of the app.
_WINDOW_CHROME = {"minimize", "maximize", "restore", "close", "system", "system menu",
                  "restore down", "application", "more"}
# Keys an ability may press: navigation and common edit shortcuts only.
_ABILITY_KEYS_RE = re.compile(
    r"^(?:\{(?:ENTER|TAB|ESC|SPACE|UP|DOWN|LEFT|RIGHT|HOME|END|PGUP|PGDN|F5)\}"
    r"|\^[acfklnstvxz])+$", re.I)
_URI_SCHEMES = ("spotify:", "ms-settings:")
_MEDIA_KEYS = {"play_pause": 0xB3, "next": 0xB0, "previous": 0xB1,
               "volume_up": 0xAF, "volume_down": 0xAE, "mute": 0xAD}
# pywinauto reads these as modifiers/groups; typed text must send them literally.
_TYPE_SPECIALS_RE = re.compile(r"([+^%~(){}\[\]])")
_NOISE_NAME_RE = re.compile(
    r"(update|updater|uninst|setup|installer|helper|service|crash|report|elevat|"
    r"daemon|agent|broker|repair|diag|telemetry|redist|runtime|notif|host$)", re.I)
# Start Menu shortcuts to documents, not programs ("Python 3.14 Manuals").
_DOC_NAME_RE = re.compile(
    r"\b(manuals?|help|documentation|docs|readme|release notes|what is new|what's new|"
    r"license|website|homepage|home page|faq)\b", re.I)
# Console programs a person opens from the Start Menu: shells and interpreters.
_SHELL_NAME_RE = re.compile(r"command prompt|powershell|python|node|terminal|shell|\bcmd\b", re.I)

# Registry category (machine_capabilities._categorize) -> kind, when no AI answers.
_CATEGORY_KIND = {"browser": "browser", "media": "media", "office": "office",
                  "edit": "editor", "comm": "chat", "util": "utility"}


# ------------------------------------------------------------------ helpers --

def _slug(text):
    return "_".join(re.findall(r"[a-z0-9]+", (text or "").lower())) or "x"


# Names people use for apps whose program file says something else.
DISPLAY_NAMES = {"winword": "Word", "excel": "Excel", "powerpnt": "PowerPoint",
                 "msedge": "Edge", "code": "VS Code", "explorer": "File Explorer",
                 "cmd": "Command Prompt", "powershell": "PowerShell", "vlc": "VLC",
                 "obs64": "OBS Studio", "ms-settings:": "Settings"}
# Programs that are part of Windows itself: closing means closing a window,
# never ending the process (ending explorer.exe takes the taskbar with it).
SHELL_APPS = {"explorer"}


def _display(key, entry):
    name = ((entry or {}).get("display_name") or DISPLAY_NAMES.get(key)
            or (entry or {}).get("name") or key)
    return name[:1].upper() + name[1:]


def _ab(key, local_id, name, say, how, verify, level="safe", needs=None, source="template"):
    return {"id": f"{key}.{local_id}", "name": name, "say": say, "needs": needs or {},
            "level": level, "how": how, "verify": verify, "source": source, "enabled": True}


# ---------------------------------------------------------------- abilities --

def _universal(key, name, kind):
    close_level = "ask" if kind in ("office", "editor") else "safe"  # unsaved work
    return [
        _ab(key, "open", f"Open {name}", [f"open {name}", f"launch {name}"],
            [{"op": "launch"}], "its window appears", source="universal"),
        _ab(key, "close", f"Close {name}", [f"close {name}", f"quit {name}"],
            [{"op": "close"}], "its window is gone", level=close_level, source="universal"),
        _ab(key, "switch", f"Switch to {name}", [f"switch to {name}", f"go to {name}"],
            [{"op": "focus"}], "its window is in front", source="universal"),
    ]


def _template(key, name, kind):
    t = {
        "browser": [
            _ab(key, "open_site", "Open a website", [f"open {{site}} in {name}"],
                [{"op": "open_url", "url": "{site}"}], "the site loads", needs={"site": "website"}),
            _ab(key, "search_web", "Search the web", [f"search {{query}} in {name}"],
                [{"op": "open_url", "url": "https://www.google.com/search?q={query}"}],
                "results page opens", needs={"query": "text"}),
            _ab(key, "new_tab", "New tab", [f"new tab in {name}"],
                [{"op": "focus"}, {"op": "key", "keys": "^t"}], "a new tab opens"),
            _ab(key, "play_page", "Play the video on the page", ["play it", "play the video"],
                [{"op": "page_click", "target": "play"}], "the video is playing", level="ask"),
        ],
        "media": [
            _ab(key, "play_pause", "Play or pause", ["pause", "resume", "play"],
                [{"op": "media", "key": "play_pause"}], "playback toggles"),
            _ab(key, "next", "Next track", ["next", "skip"], [{"op": "media", "key": "next"}],
                "the track changes"),
            _ab(key, "previous", "Previous track", ["previous", "go back a track"],
                [{"op": "media", "key": "previous"}], "the track changes"),
            _ab(key, "volume_up", "Volume up", ["louder", "volume up"],
                [{"op": "media", "key": "volume_up"}], "volume rises"),
            _ab(key, "volume_down", "Volume down", ["quieter", "volume down"],
                [{"op": "media", "key": "volume_down"}], "volume drops"),
            _ab(key, "set_volume", "Set the system volume", ["set the volume to {percent}",
                                                              "make it {percent} percent"],
                [{"op": "volume_set", "percent": "{percent}"}], "the volume is at that level",
                needs={"percent": "number"}),
        ],
        "office": [
            _ab(key, "new_document", "New document", [f"new document in {name}"],
                [{"op": "launch"}, {"op": "key", "keys": "^n"}], "a blank document opens"),
            _ab(key, "find", "Find in the document", [f"find in {name}"],
                [{"op": "focus"}, {"op": "key", "keys": "^f"}], "the find box opens"),
            _ab(key, "save", "Save", [f"save in {name}"],
                [{"op": "focus"}, {"op": "key", "keys": "^s"}], "the document saves", level="ask"),
        ],
        "editor": [
            _ab(key, "open_folder", "Open a folder or project", [f"open {{path}} in {name}"],
                [{"op": "launch_with", "arg": "{path}"}], "the folder opens",
                needs={"path": "folder"}),
            _ab(key, "new_file", "New file", [f"new file in {name}"],
                [{"op": "focus"}, {"op": "key", "keys": "^n"}], "a new file opens"),
            _ab(key, "find", "Find", [f"find in {name}"],
                [{"op": "focus"}, {"op": "key", "keys": "^f"}], "the find box opens"),
            _ab(key, "save", "Save", [f"save in {name}"],
                [{"op": "focus"}, {"op": "key", "keys": "^s"}], "the file saves", level="ask"),
        ],
        "files": [
            _ab(key, "open_folder", "Open a folder", ["open {path}", "show {path}"],
                [{"op": "open_path", "path": "{path}"}], "the folder opens",
                needs={"path": "folder"}),
            _ab(key, "open_downloads", "Open Downloads", ["open downloads", "show my downloads"],
                [{"op": "open_path", "path": "~/Downloads"}], "Downloads opens"),
            _ab(key, "open_documents", "Open Documents", ["open documents"],
                [{"op": "open_path", "path": "~/Documents"}], "Documents opens"),
        ],
        "terminal": [
            _ab(key, "run_command", "Run a command", ["run {command}"], [], "",
                level="never", needs={"command": "text"}),
        ],
    }
    return t.get(kind, [])


# Popular apps: their kind, and abilities no template can guess.
PROFILES = {
    "spotify": ("media", lambda key, name: [
        _ab(key, "play_liked", "Play liked songs", ["play my liked songs"],
            [{"op": "uri", "uri": "spotify:collection:tracks"}], "Liked Songs opens",
            source="profile"),
        _ab(key, "search", "Search Spotify", ["search {query} on spotify"],
            [{"op": "uri", "uri": "spotify:search:{query}"}], "search results show",
            needs={"query": "text"}, source="profile"),
    ]),
    "brave": ("browser", None), "chrome": ("browser", None), "msedge": ("browser", None),
    "firefox": ("browser", None), "explorer": ("files", None),
    "code": ("editor", None), "notepad": ("editor", None), "obsidian": ("editor", None),
    "winword": ("office", None), "excel": ("office", None), "powerpnt": ("office", None),
    "vlc": ("media", None), "cmd": ("terminal", None), "powershell": ("terminal", None),
    "terminal": ("terminal", None),
    "ms-settings:": ("system", lambda key, name: [
        _ab(key, "open_display", "Display settings", ["open display settings"],
            [{"op": "uri", "uri": "ms-settings:display"}], "Display settings open",
            source="profile"),
        _ab(key, "open_sound", "Sound settings", ["open sound settings"],
            [{"op": "uri", "uri": "ms-settings:sound"}], "Sound settings open", source="profile"),
    ]),
}


def build_abilities(key, entry, kind):
    """Universal + kind template + profile abilities for one app."""
    name = _display(key, entry)
    profile = PROFILES.get(key)
    out = _universal(key, name, kind) + _template(key, name, kind)
    if profile and profile[1]:
        out += profile[1](key, name)
    return out


def merge_abilities(fresh, old):
    """The fresh set, keeping the user's choices (enabled, level) on abilities
    that already existed, plus earlier deep-scan and user abilities."""
    old_by_id = {a.get("id"): a for a in old or [] if isinstance(a, dict)}
    out = []
    for a in fresh:
        prev = old_by_id.pop(a["id"], None)
        if prev:
            a = dict(a, enabled=prev.get("enabled", True), level=prev.get("level", a["level"]))
        out.append(a)
    out += [a for a in old_by_id.values() if a.get("source") in ("scan", "user")]
    return out


# ------------------------------------------------------------------ sorting --

def heuristic_kind(key, entry):
    """App kind without AI: profile, then noise patterns, then scan category."""
    if key in PROFILES:
        return PROFILES[key][0]
    try:
        import curate
        if curate.is_noise(key):
            return "noise"
    except Exception:
        pass
    if _NOISE_NAME_RE.search(key) or _NOISE_NAME_RE.search(os.path.basename(entry.get("bin") or "")):
        return "noise"
    return _CATEGORY_KIND.get(entry.get("category"), "utility")


def _sort_prompt(batch):
    lines = "\n".join(f"{k} | {os.path.basename(e.get('bin') or '')} | "
                      f"{os.path.dirname(e.get('bin') or '')[-60:]}" for k, e in batch)
    return ("Sort these Windows programs for a voice assistant that opens and controls apps.\n"
            "Kinds: browser, media, office, editor, files, terminal, chat, game, utility, "
            "system (settings, drivers, OEM tools a person rarely opens by voice), "
            "noise (background service, updater, uninstaller, crash reporter, helper, "
            "installer - never opened by a person).\n"
            f"Programs (key | program file | folder):\n{lines}\n"
            'Reply with ONLY one JSON object mapping each key to its kind, e.g. {"spotify": "media"}. '
            "/no_think")


def sort_apps(apps, llm=None, progress=None):
    """{key: kind} for every app. Profiles are known; the rest go to the AI in
    batches, and anything the AI can't answer falls back to heuristic_kind."""
    say = progress or (lambda stage, pct: None)
    kinds = {k: PROFILES[k][0] for k in apps if k in PROFILES}
    rest = [(k, e) for k, e in apps.items() if k not in kinds]
    if llm is None:
        import rules_ai
        llm = rules_ai._ask_llm
    for i in range(0, len(rest), SORT_BATCH):
        batch = rest[i:i + SORT_BATCH]
        say(f"Sorting apps {i + 1}-{i + len(batch)} of {len(rest)}...",
            10 + 70 * i / max(len(rest), 1))
        raw, _ = llm(_sort_prompt(batch))
        for k, e in batch:
            kind = str((raw or {}).get(k) or "").lower()
            kinds[k] = kind if kind in KINDS else heuristic_kind(k, e)
    return kinds


# ----------------------------------------------------------------- registry --

def _registry_path():
    import machine_capabilities
    return machine_capabilities.REGISTRY_PATH


def load_apps():
    path = _registry_path()
    if not os.path.exists(path):
        return {"apps": {}}
    with open(path, encoding="utf-8") as f:
        data = json.load(f) or {}
    data.setdefault("apps", {})
    return data


def save_apps(data):
    path = _registry_path()
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


def _apply_kind(entry, kind):
    """Record the kind; noise is hidden unless the user already chose."""
    entry["app_kind"] = kind
    if kind == "noise" and "hidden" not in entry:
        entry["hidden"] = True


def generate(keys=None, llm=None, progress=None):
    """Create predefined abilities for the registered apps (or `keys`) without
    launching anything. Returns a short summary."""
    say = progress or (lambda stage, pct: None)
    with _LOCK:
        data = load_apps()
        apps = data["apps"]
        if keys is None:
            import curate
            keys = [k for k, e in apps.items()
                    if isinstance(e, dict) and not e.get("hidden") and curate.is_registered(k)]
        targets = {k: apps[k] for k in keys if isinstance(apps.get(k), dict)}
        need_sort = {k: e for k, e in targets.items() if not e.get("app_kind")}
        kinds = sort_apps(need_sort, llm=llm, progress=say) if need_sort else {}
        say("Creating predefined rules...", 85)
        made = 0
        for k, e in targets.items():
            kind = kinds.get(k) or e.get("app_kind") or heuristic_kind(k, e)
            _apply_kind(e, kind)
            if kind == "noise":
                continue
            e["abilities"] = merge_abilities(build_abilities(k, e, kind), e.get("abilities"))
            made += len(e["abilities"])
        save_apps(data)
    return {"apps": len(targets), "abilities": made}


def launch_class(key, entry, start_menu):
    """What a scanned program is to a person:
      "app"     - in the Start Menu (or a known app): pinned
      "program" - opens a window but has no Start Menu entry: listed, not pinned
      "cli"     - a console program (coreutils, pip scripts, JDK tools): hidden
      "doc"     - a shortcut to a manual or help file: hidden"""
    import curate
    import machine_capabilities as mc
    if _DOC_NAME_RE.search(key):
        return "doc"
    path = mc.norm_path(entry.get("bin"))
    # Store apps (Snipping Tool) have no .lnk to find: the seed list names them.
    if key in PROFILES or key in curate.SEED_REGISTERED or path.endswith(".lnk"):
        return "app"
    if mc.exe_subsystem(path) == "console":
        # Start Menu shortcuts also run console tools ("Start PostgreSQL
        # server" is pg_ctl.exe); only shells people open count as apps.
        return "app" if path in start_menu and _SHELL_NAME_RE.search(key) else "cli"
    return "app" if path and path in start_menu else "program"


def _users(entry):
    """True when the user owns the whole entry: added it or wrote rules for it.
    (Pinning or hiding by hand is tracked per flag by pinned_by / hidden_by.)"""
    return bool(entry.get("added_by_user") or entry.get("compiled_rules"))


def _friendliness(key, entry):
    """Sort key for the name kept among duplicates: the user's entry, a known
    app, never a helper ("setup" and "epson scan 2" share setup.exe), then
    names without versions or "(user)", then the shortest."""
    helper = entry.get("app_kind") in ("noise", "system") or bool(_NOISE_NAME_RE.search(key))
    return (not _users(entry), key not in PROFILES, helper, bool(re.search(r"\d|\(", key)),
            len(key), key)


def _duplicates(apps, classes):
    """{key: kept_key} for entries that point at the same program as a
    friendlier entry ("google chrome" and "chrome", "opencode 1.18.30")."""
    import machine_capabilities as mc
    by_path = {}
    for k, cls in classes.items():
        path = mc.norm_path(apps[k].get("bin"))
        if cls in ("app", "program") and path:
            by_path.setdefault(path, []).append(k)
    out = {}
    for keys in by_path.values():
        keys.sort(key=lambda k: _friendliness(k, apps[k]))
        out.update({k: keys[0] for k in keys[1:] if not _users(apps[k])})
    return out


def _place(entry, cls, dup_of):
    """Hide and pin one scanned entry, leaving what the user decided alone."""
    kind = entry.get("app_kind")
    if not _users(entry) and entry.get("hidden_by") != "user":
        entry["hidden"] = cls in ("cli", "doc") or kind == "noise" or bool(dup_of)
        entry["hidden_by"] = "scan"
        if dup_of:
            entry["duplicate_of"] = dup_of
        else:
            entry.pop("duplicate_of", None)
    if not _users(entry) and entry.get("pinned_by") != "user":
        # Windowed programs outside the Start Menu and system tools (drivers,
        # OEM utilities) stay under "All detected": pinning floods the list.
        entry["registered"] = cls == "app" and not entry.get("hidden") \
            and kind not in ("noise", "system", "cli")
        entry["pinned_by"] = "scan"


def full_scan(llm=None, progress=None):
    """Find every launchable program (merging, never dropping user data), keep
    console tools, help shortcuts and duplicates out of sight, sort the new
    apps, and pin the Start Menu ones. Abilities are a separate step."""
    say = progress or (lambda stage, pct: None)
    import machine_capabilities
    say("Scanning the computer for apps...", 5)
    machine_capabilities.write_registry()
    say("Checking which programs are apps...", 8)
    start_menu = machine_capabilities.start_menu_targets()
    with _LOCK:
        data = load_apps()
        apps = data["apps"]
        classes = {k: launch_class(k, e, start_menu) for k, e in apps.items()
                   if isinstance(e, dict)}
        import curate
        for k, cls in classes.items():
            if _users(apps[k]):
                continue
            if cls == "cli":
                apps[k]["app_kind"] = "cli"
            elif curate.is_noise(k):         # OEM families (asus*, epson*, ...)
                apps[k]["app_kind"] = "noise"
        new = {k: apps[k] for k, cls in classes.items()
               if cls in ("app", "program") and not apps[k].get("app_kind")}
        kinds = sort_apps(new, llm=llm, progress=say) if new else {}
        for k, kind in kinds.items():
            apps[k]["app_kind"] = kind
        dupes = _duplicates(apps, classes)
        was_pinned = {k for k in classes if apps[k].get("registered")}
        for k, cls in classes.items():
            _place(apps[k], cls, dupes.get(k))
        pinned = {k for k in classes if apps[k].get("registered") and not apps[k].get("hidden")}
        save_apps(data)
    return {"found": len(classes), "sorted": len(kinds), "pinned": len(pinned),
            "newly_registered": len(pinned - was_pinned),
            "hidden": sum(1 for k in classes if apps[k].get("hidden")),
            "cli": sum(1 for c in classes.values() if c == "cli"),
            "duplicates": len(dupes)}


def add_app(query, llm=None, progress=None, scan=True):
    """Register one app by program path or name, sort it, create its abilities,
    and deep-scan it (opening it once if it isn't running)."""
    say = progress or (lambda stage, pct: None)
    import machine_capabilities as mc
    query = (query or "").strip().strip('"')
    say(f"Finding {query}...", 10)
    # A program the user points at is theirs to add, wherever it lives (the
    # blind scan's %TEMP% filter is for installer leftovers, not for this).
    if os.path.isfile(query) and query.lower().endswith((".exe", ".lnk")):
        key = _slug(os.path.splitext(os.path.basename(query))[0]).replace("_", "")
        found = {"name": key, "bin": query, "kind": "gui", "category": "other"}
    else:
        tier, hits = mc.resolve_candidates(query)
        if not hits:
            # Not in the software index (a fresh machine, or a new install):
            # look at the computer itself before giving up.
            say(f"Searching this computer for {query}...", 20)
            q = query.lower()
            scanned = {k: e for k, e in mc.scan().items()
                       if q in k.lower() and mc._is_launchable(e.get("bin"))}
            exact = {k: e for k, e in scanned.items() if k.lower() == q}
            hits = list((exact or scanned).items())
            tier = "exact" if exact else "substring"
        if not hits:
            return {"error": f"I couldn't find an app called \"{query}\". Try its program "
                             "path, like C:\\Program Files\\App\\app.exe."}
        if tier != "exact" and len(hits) > 1:
            return {"error": "More than one app matches: " + ", ".join(k for k, _ in hits[:6])
                             + ". Type the exact name."}
        key, found = hits[0]
    with _LOCK:
        data = load_apps()
        entry = data["apps"].setdefault(key, {})
        for f, v in found.items():
            entry.setdefault(f, v)
        entry.update(added_by_user=True, registered=True, enabled=True, hidden=False,
                     pinned_by="user", hidden_by="user")
        entry.pop("duplicate_of", None)
        say("Working out what it is...", 30)
        kind = sort_apps({key: entry}, llm=llm).get(key) or heuristic_kind(key, entry)
        entry["app_kind"] = "utility" if kind == "noise" else kind
        entry["abilities"] = merge_abilities(build_abilities(key, entry, entry["app_kind"]),
                                             entry.get("abilities"))
        save_apps(data)
    summary = {"key": key, "kind": entry["app_kind"], "abilities": len(entry["abilities"])}
    if scan:
        say(f"Opening {_display(key, entry)} to learn its buttons...", 55)
        summary["scan"] = scan_app(key, launch=True, progress=say)
    return summary


# ---------------------------------------------------------------- deep scan --

def _process_exe(pid):
    """Full program path of a process, lowercased ("" when unreadable).
    Windows API directly, so no psutil dependency."""
    try:
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.windll.kernel32
        handle = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return ""
        try:
            buf = ctypes.create_unicode_buffer(1024)
            size = wintypes.DWORD(len(buf))
            if k32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
                return buf.value.lower()
            return ""
        finally:
            k32.CloseHandle(handle)
    except Exception:
        return ""


def find_app_window(entry, timeout=1.0):
    """The app's top-level window, matched by its program file (Word's window
    says "Document1 - Word", not "winword"), else by name in the title."""
    try:
        from pywinauto import Desktop
    except Exception:
        return None
    exe = os.path.basename((entry.get("bin") or "").split(",")[0]).lower()
    name = (entry.get("display_name") or entry.get("name") or "").lower()
    deadline = time.time() + timeout
    while True:
        try:
            wins = [w for w in Desktop(backend="uia").windows() if w.is_visible()]
        except Exception:
            wins = []
        for w in wins:
            if exe and exe.endswith(".exe") and \
                    os.path.basename(_process_exe(w.element_info.process_id)) == exe:
                return w
        for w in wins:
            if name and name in (w.window_text() or "").lower():
                return w
        if time.time() >= deadline:
            return None
        time.sleep(0.5)


def _label(info):
    """A control's name as the deep scan stores it: whitespace collapsed."""
    return " ".join((info.name or "").split())


def _find_control(window, title, types=None):
    """The window's first control named `title` (of one of `types`), waiting
    up to FIND_WAIT seconds for it to appear. Desktop() windows are
    UIAWrapper, which has no child_window(), so this searches descendants."""
    want = (title or "").lower()
    deadline = time.time() + FIND_WAIT
    while True:
        try:
            controls = window.descendants()[:MAX_CONTROLS]
        except Exception:
            controls = []
        for c in controls:
            info = c.element_info
            if _label(info).lower() == want and (not types or info.control_type in types):
                return c
        if time.time() >= deadline:
            return None
        time.sleep(0.5)


def abilities_from_window(key, name, window):
    """Click and type abilities from a window's real controls."""
    seen, out = set(), []
    clicks = ("Button", "MenuItem", "TabItem", "Hyperlink", "SplitButton")
    try:
        controls = window.descendants()[:MAX_CONTROLS]
    except Exception:
        return []
    for c in controls:
        info = c.element_info
        label = _label(info)
        kind = info.control_type
        if not (2 <= len(label) <= 40) or label.lower() in _WINDOW_CHROME or label.isdigit():
            continue
        if kind in clicks and ("click", label.lower()) not in seen:
            seen.add(("click", label.lower()))
            out.append(_ab(key, "click_" + _slug(label), f"Click \u201c{label}\u201d",
                           [f"click {label.lower()} in {name.lower()}"],
                           [{"op": "focus"}, {"op": "click", "target": label}],
                           f"\u201c{label}\u201d responds",
                           level="ask" if _CONSEQUENTIAL_RE.search(label) else "safe",
                           source="scan"))
        elif kind in ("Edit", "ComboBox") and ("type", label.lower()) not in seen:
            seen.add(("type", label.lower()))
            out.append(_ab(key, "type_" + _slug(label), f"Type into \u201c{label}\u201d",
                           [f"type {{text}} into {label.lower()}"],
                           [{"op": "focus"}, {"op": "type", "target": label, "text": "{text}"}],
                           "the text appears", needs={"text": "text"}, source="scan"))
        if len(out) >= MAX_SCANNED:
            break
    return out


def scan_app(key, launch=False, progress=None):
    """Deep-scan one app's window and store what it can do. Opens the app only
    when launch=True, and closes it again if the scan opened it."""
    say = progress or (lambda stage, pct: None)
    import tools
    data = load_apps()
    entry = data["apps"].get(key)
    if not isinstance(entry, dict):
        return {"error": f"{key} isn't in JARVIS Apps."}
    name = _display(key, entry)
    window = find_app_window(entry)
    opened = False
    if window is None and launch:
        tools.open_application(key)
        opened = True
        window = find_app_window(entry, timeout=SCAN_WAIT)
    if window is None:
        return {"error": f"{name} isn't open, so there was nothing to read."}
    say(f"Reading {name}'s buttons and boxes...", 75)
    scanned = abilities_from_window(key, name, window)
    if opened:
        # Close the window the scan opened - never end the program: other
        # windows of it may be the user's (File Explorer is the taskbar).
        try:
            window.close()
        except Exception:
            pass
    with _LOCK:
        data = load_apps()
        entry = data["apps"].get(key) or {}
        kept = [a for a in entry.get("abilities") or [] if a.get("source") != "scan"]
        entry["abilities"] = merge_abilities(kept + scanned, entry.get("abilities"))
        entry["deep_scan"] = {"at": time.strftime("%Y-%m-%d %H:%M"), "found": len(scanned),
                              "version": entry.get("version")}
        save_apps(data)
    return {"found": len(scanned), "opened": opened}


# "Deep scan all": one app at a time in the background, cancellable, and
# resumable (apps already scanned are skipped).
_SCAN_ALL = {"running": False, "cancel": False, "done": 0, "total": 0, "current": "",
             "scanned": 0, "failed": 0}


def scan_all_status():
    return dict(_SCAN_ALL)


def cancel_scan_all():
    _SCAN_ALL["cancel"] = True


def _scan_all_targets():
    import curate
    apps = load_apps()["apps"]
    return [k for k, e in apps.items()
            if isinstance(e, dict) and not e.get("hidden") and not e.get("deep_scan")
            and e.get("app_kind") not in ("noise", "system", "cli") and curate.is_registered(k)]


_SCAN_ALL_LOCK = threading.Lock()


def claim_scan_all():
    """Take the one "Deep scan all" slot. False when a scan already holds it:
    two quick clicks must never launch apps from two scans at once."""
    with _SCAN_ALL_LOCK:
        if _SCAN_ALL["running"]:
            return False
        _SCAN_ALL.update(running=True, cancel=False, done=0, total=0, current="",
                         scanned=0, failed=0)
        return True


def deep_scan_all(progress=None, claimed=False):
    """Open, read and close every registered app not scanned yet. Returns a
    summary; progress(stage, pct) reports each app. claimed=True when the
    caller already took the slot with claim_scan_all()."""
    say = progress or (lambda stage, pct: None)
    if not claimed and not claim_scan_all():
        return {"error": "A deep scan of all apps is already running."}
    targets = _scan_all_targets()
    _SCAN_ALL["total"] = len(targets)
    try:
        for i, key in enumerate(targets):
            if _SCAN_ALL["cancel"]:
                break
            _SCAN_ALL["current"] = key
            say(f"Scanning {key} ({i + 1} of {len(targets)})...", 100 * i / max(len(targets), 1))
            try:
                out = scan_app(key, launch=True)
            except Exception as e:
                out = {"error": type(e).__name__}
            _SCAN_ALL["scanned" if "error" not in out else "failed"] += 1
            _SCAN_ALL["done"] = i + 1
    finally:
        cancelled = _SCAN_ALL["cancel"]
        _SCAN_ALL.update(running=False, current="")
    return {"total": len(targets), "scanned": _SCAN_ALL["scanned"],
            "failed": _SCAN_ALL["failed"], "cancelled": cancelled,
            "left": len(targets) - _SCAN_ALL["done"]}


def scan_soon(app):
    """After JARVIS opens an app: read its window in the background once it is
    up (under a second when open). At most once a day per app."""
    def work():
        time.sleep(SCAN_SOON_DELAY)
        try:
            apps = load_apps()["apps"]
            key = app if app in apps else next(
                (k for k, e in apps.items() if isinstance(e, dict)
                 and (e.get("name") or "").lower() == (app or "").lower()), None)
            last = ((apps.get(key) or {}).get("deep_scan") or {}).get("at")
            if key and (not last or time.time() - time.mktime(
                    time.strptime(last, "%Y-%m-%d %H:%M")) > RESCAN_AFTER):
                scan_app(key, launch=False)
        except Exception as e:
            print(f"[abilities] background scan of {app} failed: {type(e).__name__}", flush=True)
    threading.Thread(target=work, daemon=True).start()


# ------------------------------------------------------------------ running --

def _fill(template, args):
    """Replace {name} with the user's value; missing values stay detectable."""
    return re.sub(r"\{(\w+)\}", lambda m: str(args.get(m.group(1), m.group(0))), template or "")


def _press_media(name):
    import ctypes
    vk = _MEDIA_KEYS[name]
    ctypes.windll.user32.keybd_event(vk, 0, 0, 0)
    ctypes.windll.user32.keybd_event(vk, 0, 2, 0)


def _window_or_open(key, entry):
    import tools
    window = find_app_window(entry)
    if window is None:
        tools.open_application(key)
        window = find_app_window(entry, timeout=SCAN_WAIT)
    return window


def _run_op(key, entry, op, args):
    """One step of an ability. Returns an [Error] string or ""."""
    import tools
    kind = op.get("op")
    if kind == "launch":
        out = tools.open_application(key)
        return out if str(out).startswith("[Error]") else ""
    if kind == "close":
        if key in SHELL_APPS:
            window = find_app_window(entry)
            if window is None:
                return f"[Error] No {_display(key, entry)} window is open."
            window.close()
            return ""
        out = tools.close_application(key)
        return out if str(out).startswith("[Error]") else ""
    if kind == "media":
        _press_media(op["key"])
        return ""
    if kind == "volume_set":
        digits = re.sub(r"\D", "", _fill(op.get("percent") or "", args))
        if not digits or int(digits) > 100:
            return "[Error] Say a volume from 0 to 100 percent."
        # Windows volume keys move 2% a press: all the way down, then up.
        for _ in range(VOLUME_STEPS):
            _press_media("volume_down")
        for _ in range(round(int(digits) / 2)):
            _press_media("volume_up")
        return ""
    if kind == "uri":
        uri = _fill(op["uri"], {k: urllib.parse.quote(str(v)) for k, v in args.items()})
        if not uri.startswith(_URI_SCHEMES) or "{" in uri:
            return "[Error] That link type isn't allowed."
        os.startfile(uri)
        return ""
    if kind == "open_url":
        # "{site}" is the whole address; a value inside a longer address
        # (a search query) is URL-encoded.
        whole = op["url"].startswith("{")
        url = _fill(op["url"], {k: str(v) if whole else urllib.parse.quote_plus(str(v))
                                for k, v in args.items()})
        if not url.lower().startswith(("http://", "https://")):
            url = "https://" + url
        if not rules_engine.is_web_url(url):
            return f"[Error] {url!r} isn't a web address."
        out = tools._open_url_in_browser(url, url, key, remember=False)
        return out if out.startswith("[Error]") else ""
    if kind == "open_path":
        # Folders only: os.startfile on a program file would run it.
        path = os.path.expanduser(_fill(op["path"], args))
        if not os.path.isdir(path):
            return f"[Error] {path} isn't a folder."
        os.startfile(path)
        return ""
    if kind == "launch_with":
        arg = os.path.expanduser(_fill(op["arg"], args))
        exe = (entry.get("bin") or "").split(",")[0]
        if not os.path.exists(arg) or arg.startswith("-") or not os.path.isfile(exe):
            return f"[Error] {arg} doesn't exist."
        subprocess.Popen([exe, arg], close_fds=True)
        return ""
    if kind == "page_click":
        import browser_cdp
        out = browser_cdp.click(key, op.get("target") or "play")
        return out if out.startswith("[Error]") else ""
    window = _window_or_open(key, entry)
    if window is None:
        return f"[Error] {_display(key, entry)} didn't open."
    window.set_focus()
    if kind == "focus":
        return ""
    if kind == "key":
        keys = op.get("keys") or ""
        if not _ABILITY_KEYS_RE.match(keys):
            return f"[Error] I won't press \"{keys}\"."
        window.type_keys(keys)
        return ""
    if kind == "click":
        ctrl = _find_control(window, op.get("target"))
        if ctrl is None:
            return f"[Error] I couldn't find \"{op.get('target')}\" in {_display(key, entry)}."
        ctrl.click_input()
        return ""
    if kind == "type":
        ctrl = _find_control(window, op.get("target"), types=("Edit", "ComboBox"))
        if ctrl is None:
            return f"[Error] I couldn't find the \"{op.get('target')}\" box."
        text = _TYPE_SPECIALS_RE.sub(r"{\1}", _fill(op.get("text") or "", args))
        ctrl.set_focus()
        ctrl.type_keys(text, with_spaces=True)
        return ""
    return f"[Error] I don't know how to do \"{kind}\"."


def run_ability(key, ability_id, args=None):
    """Run one ability of one app (the panel's Test). Returns what happened."""
    args = {k: v for k, v in (args or {}).items() if str(v).strip()}
    entry = load_apps()["apps"].get(key) or {}
    ability = next((a for a in entry.get("abilities") or [] if a.get("id") == ability_id), None)
    if not ability:
        return f"[Error] {ability_id} isn't one of {key}'s abilities."
    if ability.get("level") == "never" or not ability.get("how"):
        return f"[Error] \"{ability['name']}\" is set to never."
    if ability.get("enabled") is False:
        return f"[Error] \"{ability['name']}\" is switched off."
    missing = [n for n in ability.get("needs") or {} if n not in args]
    if missing:
        return f"[Error] \"{ability['name']}\" needs a {missing[0]}."
    t0 = time.time()
    for op in ability["how"]:
        err = _run_op(key, entry, op, args)
        if err:
            return err
    out = f"Done: {ability['name']}."
    try:
        import audit
        audit.log_call("abilities.run", {"app": key, "ability": ability_id}, time.time() - t0, out)
    except Exception:
        pass
    return out


def set_choice(key, ability_id, enabled=None, level=None):
    """The user's switch and consent level for one ability."""
    with _LOCK:
        data = load_apps()
        entry = data["apps"].get(key) or {}
        for a in entry.get("abilities") or []:
            if a.get("id") == ability_id:
                if enabled is not None:
                    a["enabled"] = bool(enabled)
                if level in LEVELS:
                    a["level"] = level
                save_apps(data)
                return a
    return None
