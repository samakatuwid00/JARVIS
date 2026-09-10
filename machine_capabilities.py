"""JARVIS Capability Manifest (Pillar B).

Scans the host for installed software / apps / tools and writes a
machine-readable manifest used to resolve "use X" requests. Hermes (the
harness) owns this: JARVIS delegates "scan installed software" to Hermes,
which runs this and reports back. The manifest is LOCAL-ONLY and GITIGNORED
(it leaks the software inventory — must never be committed to the OSS repo).

Portable across OSes:
- Windows: PATH `where`, Start-Menu `.lnk` targets, `Program Files`,
  `AppData\\Local\\Programs`, uninstall registry keys.
- Linux: `which`, `.desktop` files, flatpak/snap.
- macOS: `/Applications`, `brew list`.

Each entry:
  { "name": str, "bin": str|null (CLI invocation or .lnk/exe path),
    "version": str|null, "category": str, "kind": "cli"|"gui",
    "confidence": "high"|"medium"|"low" }

The manifest is consumed by Hermes when a task names a specific app: it looks
up the entry, then either shells out (cli) or drives the GUI (computer_use).
"""
import os
import re
import json
import shutil
import subprocess
import platform

MANIFEST_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "capabilities.json")
REGISTRY_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "app_registry.json")

_CATEGORIES = {
    "edit": ["code", "vscode", "notepad", "sublime", "vim", "neovim", "atom", "cursor"],
    "dev": ["python", "node", "npm", "git", "docker", "go", "rust", "cargo", "php", "ruby", "java"],
    "media": ["vlc", "ffmpeg", "blender", "obs", "spotify", "audacity", "gimp", "photoshop", "premiere"],
    "browser": ["chrome", "brave", "edge", "firefox", "opera", "safari"],
    "office": ["word", "excel", "powerpoint", "libreoffice", "outlook"],
    "comm": ["discord", "telegram", "slack", "whatsapp", "teams", "zoom"],
    "util": ["7zip", "winrar", "powershell", "terminal", "cmd", "explorer"],
}


def _categorize(name: str) -> str:
    n = name.lower()
    for cat, keys in _CATEGORIES.items():
        if any(k in n for k in keys):
            return cat
    return "other"


def _find_cli(name: str):
    """Return a CLI invocation if the name is on PATH, else None."""
    bin_name = name if os.path.isabs(name) else name
    found = shutil.which(bin_name)
    if found:
        return found
    # try common exe/cmd variants on Windows
    if platform.system() == "Windows":
        for ext in (".exe", ".cmd", ".bat", ".ps1"):
            if shutil.which(bin_name + ext):
                return shutil.which(bin_name + ext)
    return None


def _scan_windows():
    out = {}
    # 1) PATH binaries
    for name in ("git", "python", "python3", "node", "npm", "ffmpeg", "docker",
                 "code", "code.cmd", "powershell", "winget", "choco", "go", "php",
                 "cargo", "rustc", "java", "ruby", "7z", "vlc", "blender", "obs"):
        binp = _find_cli(name)
        if binp:
            key = name.replace(".cmd", "").replace(".exe", "")
            out[key] = {"bin": binp, "kind": "cli", "category": _categorize(key),
                        "confidence": "high", "version": _try_version(key, binp)}
    # 2) Program Files + AppData Local Programs (GUI apps)
    roots = []
    for env in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"):
        p = os.environ.get(env)
        if p:
            roots.append(p)
    roots.append(os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs"))
    seen = set()
    for root in roots:
        if not os.path.isdir(root):
            continue
        for dirpath, dirs, files in os.walk(root):
            # stop descending too deep
            if dirpath.count(os.sep) - root.count(os.sep) > 3:
                dirs[:] = []
                continue
            for fn in files:
                low = fn.lower()
                if low.endswith(".exe") and "uninstall" not in low:
                    base = fn[:-4]
                    key = base.lower()
                    if key in seen or key in out:
                        continue
                    seen.add(key)
                    out[key] = {"bin": os.path.join(dirpath, fn), "kind": "gui",
                                "category": _categorize(base), "confidence": "medium",
                                "version": None}
    # 3) Start Menu .lnk targets
    start = os.path.join(os.environ.get("APPDATA", ""),
                         "Microsoft", "Windows", "Start Menu", "Programs")
    if os.path.isdir(start):
        for dirpath, _d, files in os.walk(start):
            for fn in files:
                if fn.lower().endswith(".lnk"):
                    target = _resolve_lnk(os.path.join(dirpath, fn))
                    base = fn[:-4]
                    key = base.lower()
                    if key in out or key in seen:
                        continue
                    seen.add(key)
                    out[key] = {"bin": target or os.path.join(dirpath, fn),
                                "kind": "gui", "category": _categorize(base),
                                "confidence": "low", "version": None}
    # 4) Registry uninstall keys (discovers Store apps, MSI installs, etc.)
    try:
        for k, v in _scan_registry().items():
            if k not in out:
                out[k] = v
    except Exception:
        pass
    # 5) winget-installed apps (Microsoft Store, etc.)
    try:
        for k, v in _scan_winget().items():
            if k not in out:
                out[k] = v
    except Exception:
        pass
    return out


def _resolve_lnk(path: str):
    """Resolve a Windows .lnk shortcut to its target path via COM."""
    try:
        import win32com.client
        shell = win32com.client.Dispatch("WScript.Shell")
        shortcut = shell.CreateShortCut(path)
        return shortcut.Targetpath
    except Exception:
        return None


def _is_launchable(path) -> bool:
    """Phase 14b: launch-worthiness check for a resolved bin path.

    A bin is launchable iff it is an EXISTING .exe or .lnk file. Everything
    else the old scanner wrote (directories, .txt/.chm/.html docs, MSI
    'file.exe,0' icon strings, %TEMP% installer leftovers) is junk that makes
    open_application open Notepad/Explorer or throw instead of the app.
    """
    if not path or not isinstance(path, str):
        return False
    p = path.split(",")[0].strip()          # strip DisplayIcon ',0' suffixes
    low = p.lower()
    if not low.endswith((".exe", ".lnk")):
        return False
    if "package cache" in low:
        return False
    temp = os.environ.get("TEMP", "").lower()
    if temp and low.startswith(temp):
        return False                         # installer leftovers in %TEMP%
    try:
        return os.path.isfile(p)
    except OSError:
        return False


def _scan_registry():
    """Scan Windows uninstall registry keys for installed apps."""
    import winreg
    out = {}
    roots = [
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
    ]
    for hkey, subkey in roots:
        try:
            key = winreg.OpenKey(hkey, subkey)
        except OSError:
            continue
        i = 0
        while True:
            try:
                sk = winreg.EnumKey(key, i)
                i += 1
                try:
                    app_key = winreg.OpenKey(key, sk)
                    try:
                        name = winreg.QueryValueEx(app_key, "DisplayName")[0]
                    except Exception:
                        continue
                    loc = None
                    try:
                        loc = winreg.QueryValueEx(app_key, "InstallLocation")[0]
                    except Exception:
                        pass
                    exe = None
                    try:
                        exe = winreg.QueryValueEx(app_key, "DisplayIcon")[0]
                    except Exception:
                        pass
                    if not name:
                        continue
                    key_name = name.lower().strip()
                    entry = {"kind": "gui", "category": _categorize(name),
                             "confidence": "medium", "version": None}
                    if exe and exe.lower().endswith(".ico"):
                        exe = None
                    # Phase 14b: only write launchable bins. Prefer a real exe
                    # in InstallLocation; never fall back to the bare directory.
                    if loc and os.path.isdir(loc):
                        exes = [f for f in os.listdir(loc)
                                if f.lower().endswith(".exe") and "uninstall" not in f.lower()]
                        if exes:
                            entry["bin"] = os.path.join(loc, exes[0])
                        else:
                            continue          # directory with no exe = not an app
                    elif exe:
                        cand = exe.split(",")[0].strip()   # strip ',0' icon refs
                        if not _is_launchable(cand):
                            continue
                        entry["bin"] = cand
                    else:
                        continue
                    out[key_name] = entry
                except Exception:
                    continue
            except OSError:
                break
        try:
            winreg.CloseKey(key)
        except Exception:
            pass
    return out


def _scan_winget():
    """Scan winget-installed apps (Microsoft Store, etc.)."""
    try:
        r = subprocess.run(
            ["winget", "list", "--accept-source-agreements", "--disable-interactivity"],
            capture_output=True, text=True, timeout=30, encoding="utf-8", errors="replace")
        if r.returncode != 0:
            return {}
    except Exception:
        return {}
    out = {}
    for line in r.stdout.splitlines()[3:]:
        parts = [p.strip() for p in line.split("  ") if p.strip()]
        if len(parts) >= 2:
            name = parts[0]
            key = name.lower()
            out[key] = {"kind": "gui", "category": _categorize(name),
                        "confidence": "medium",
                        "version": parts[1] if len(parts) > 1 else None,
                        "bin": None}
    return out


def _scan_linux():
    out = {}
    for name in ("git", "python3", "python", "node", "npm", "ffmpeg", "docker",
                 "code", "go", "cargo", "php", "java", "ruby", "code-oss"):
        binp = _find_cli(name)
        if binp:
            out[name] = {"bin": binp, "kind": "cli", "category": _categorize(name),
                         "confidence": "high", "version": _try_version(name, binp)}
    # .desktop apps
    for d in ("/usr/share/applications", os.path.expanduser("~/.local/share/applications")):
        if os.path.isdir(d):
            for fn in os.listdir(d):
                if fn.endswith(".desktop"):
                    base = fn[:-8].lower()
                    out[base] = {"bin": None, "kind": "gui",
                                 "category": _categorize(base),
                                 "confidence": "low", "version": None}
    # flatpak / snap
    for mgr, key in (("flatpak", "flatpak"), ("snap", "snap")):
        if shutil.which(mgr):
            out[key] = {"bin": mgr, "kind": "cli", "category": "dev",
                        "confidence": "high", "version": None}
    return out


def _scan_macos():
    out = {}
    apps_dir = "/Applications"
    if os.path.isdir(apps_dir):
        for fn in os.listdir(apps_dir):
            if fn.endswith(".app"):
                base = fn[:-4].lower()
                out[base] = {"bin": os.path.join(apps_dir, fn), "kind": "gui",
                             "category": _categorize(base), "confidence": "medium",
                             "version": None}
    if shutil.which("brew"):
        out["brew"] = {"bin": "brew", "kind": "cli", "category": "dev",
                       "confidence": "high", "version": None}
    return out


def _try_version(name: str, binp: str) -> str | None:
    if not binp:
        return None
    for flag in ("--version", "version", "-v"):
        try:
            r = subprocess.run([binp, flag], capture_output=True, text=True,
                               timeout=5, encoding="utf-8", errors="replace")
            txt = (r.stdout or r.stderr).strip()
            if txt:
                # first line, truncated
                return txt.splitlines()[0][:60]
        except Exception:
            continue
    return None


def scan() -> dict:
    """Return a normalized manifest dict {name: entry}."""
    sysname = platform.system()
    if sysname == "Windows":
        raw = _scan_windows()
    elif sysname == "Linux":
        raw = _scan_linux()
    elif sysname == "Darwin":
        raw = _scan_macos()
    else:
        raw = {}
    manifest = {}
    for name, entry in raw.items():
        entry["name"] = name
        manifest[name] = entry
    return manifest


def write_manifest() -> str:
    """Scan and write capabilities.json. Returns a short human summary."""
    manifest = scan()
    payload = {
        "generated": __import__("datetime").datetime.now().isoformat(timespec="seconds"),
        "os": platform.system(),
        "count": len(manifest),
        "apps": manifest,
    }
    with open(MANIFEST_PATH, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
    return f"Scanned {len(manifest)} capabilities -> {MANIFEST_PATH}"


def write_registry() -> str:
    """Full scan → app_registry.json. Returns summary string."""
    manifest = {k: v for k, v in scan().items()
                if _is_launchable(v.get("bin"))}   # Phase 14b junk filter
    # Merge: existing capabilities.json entries override (more reliable),
    # but only launchable ones — old junk must not resurrect.
    if os.path.exists(MANIFEST_PATH):
        try:
            with open(MANIFEST_PATH, encoding="utf-8") as f:
                old = json.load(f).get("apps", {})
            for k, v in old.items():
                if k not in manifest and _is_launchable(v.get("bin")):
                    manifest[k] = v
                elif k in manifest and v.get("confidence") == "high" \
                        and _is_launchable(v.get("bin")):
                    manifest[k] = v
        except Exception:
            pass
    payload = {
        "generated": __import__("datetime").datetime.now().isoformat(timespec="seconds"),
        "os": platform.system(),
        "count": len(manifest),
        "apps": manifest,
    }
    with open(REGISTRY_PATH, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
    return f"Scanned {len(manifest)} apps -> {REGISTRY_PATH}"


def load_registry():
    """Load app_registry.json. Returns dict or None."""
    if not os.path.exists(REGISTRY_PATH):
        return None
    try:
        with open(REGISTRY_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def find_replacement(name: str) -> dict | None:
    """Try to find a working exe for a broken app by re-scanning."""
    for scanned in [_scan_windows, _scan_linux, _scan_macos]:
        try:
            raw = scanned()
        except Exception:
            continue
        key = name.lower()
        for k, v in raw.items():
            if key in k or k in key:
                binp = v.get("bin")
                if binp and os.path.exists(binp):
                    return {"name": k, "bin": binp, "repaired": True}
    return None


def resolve(name: str) -> dict | None:
    """Look up an app by fuzzy name (substring, case-insensitive)."""
    if not os.path.exists(MANIFEST_PATH):
        return None
    try:
        with open(MANIFEST_PATH, encoding="utf-8") as f:
            data = json.load(f)
    except OSError:
        return None
    apps = data.get("apps", {})
    n = name.lower().strip()
    # exact, then startswith, then substring
    for key, entry in apps.items():
        if key == n:
            return entry
    for key, entry in apps.items():
        if key.startswith(n):
            return entry
    for key, entry in apps.items():
        if n in key:
            return entry
    return None


def resolve_candidates(name: str, limit: int = 6):
    """Like resolve(), but reports AMBIGUITY instead of hiding it.

    resolve() returns the first match at each tier and discards the rest, so
    "open ch" silently picks one of the 46 manifest keys containing "ch" — the
    caller cannot tell a certain match from a coin flip. This returns the tier
    that matched and every key at that tier, so the caller can act when the
    match is unambiguous and ask when it is not.

    Returns (tier, [(key, entry), ...]) with tier in
    "exact" | "prefix" | "substring" | None.
    """
    if not os.path.exists(MANIFEST_PATH):
        return (None, [])
    try:
        with open(MANIFEST_PATH, encoding="utf-8") as f:
            data = json.load(f)
    except OSError:
        return (None, [])
    apps = data.get("apps", {})
    n = (name or "").lower().strip()
    if not n:
        return (None, [])

    exact = [(k, v) for k, v in apps.items() if k == n]
    if exact:
        return ("exact", exact[:limit])
    prefix = [(k, v) for k, v in apps.items() if k.startswith(n)]
    if prefix:
        # A single prefix hit is as good as exact; several is a real choice.
        return ("prefix", sorted(prefix, key=lambda kv: len(kv[0]))[:limit])
    substr = [(k, v) for k, v in apps.items() if n in k]
    if substr:
        return ("substring", sorted(substr, key=lambda kv: len(kv[0]))[:limit])
    return (None, [])


if __name__ == "__main__":
    print(write_manifest())
    m = resolve("vlc")
    print("resolve vlc:", m)
