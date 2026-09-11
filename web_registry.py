"""web_registry.py — named site registry for JARVIS voice resolution.

Phase 8 of jarvis_rearchitecture.md. Same pattern as app_registry.json,
but for websites:

  - web_registry.json holds manual entries + auto-scanned top-N domains
  - history scan reads the machine DEFAULT browser's Chromium History DB
    (fallback: all Chromium browsers found), incrementally via a
    last_visit_time watermark
  - manual entries always shadow history entries; visits accumulate
  - resolve_site(name) gives O(1) exact hits, fuzzy on miss

Stdlib only. Safe to import from tools.py and jarvis_web.py.
"""

import json
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
import time
import difflib
from datetime import datetime
from urllib.parse import urlparse

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
REGISTRY_PATH = os.path.join(BASE_DIR, "web_registry.json")

TOP_DOMAINS = int(os.getenv("JARVIS_WEB_TOP_N", "30"))
TOP_URLS_PER_DOMAIN = 5
MIN_VISITS = 2                      # junk gate at domain level
STALE_SECONDS = 7 * 24 * 3600       # refresh policy C: 7-day staleness window

# Chrome epoch: microseconds since 1601-01-01 UTC.
_CHROME_EPOCH_DELTA = 11644473600   # seconds between 1601 and 1970


# --------------------------------------------------------------------------
# Registry I/O
# --------------------------------------------------------------------------

def load_registry() -> dict:
    try:
        with open(REGISTRY_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"generated": None, "sites": {}, "last_scan": None,
                "watermark_chrome_us": 0}


def save_registry(reg: dict) -> None:
    reg["generated"] = datetime.now().isoformat(timespec="seconds")
    tmp = REGISTRY_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(reg, f, indent=2)
    os.replace(tmp, REGISTRY_PATH)


def is_stale(reg: dict = None) -> bool:
    """One stat call when a registry already exists on disk."""
    reg = reg or load_registry()
    ts = reg.get("last_scan")
    if not ts:
        return True
    try:
        age = datetime.now().timestamp() - datetime.fromisoformat(ts).timestamp()
        return age > STALE_SECONDS
    except Exception:
        return True


# --------------------------------------------------------------------------
# Default-browser detection (Windows UserChoice ProgId -> History path)
# --------------------------------------------------------------------------

_PROGID_HISTORY = {
    "chromehtml": r"%LOCALAPPDATA%\Google\Chrome\User Data",
    "bravebhtml": r"%LOCALAPPDATA%\BraveSoftware\Brave-Browser\User Data",
    "msedgehtm":  r"%LOCALAPPDATA%\Microsoft\Edge\User Data",
    "operastable": r"%LOCALAPPDATA%\Opera Software\Opera Stable",
}

_CHROMIUM_USER_DATA_FALLBACK = [
    r"%LOCALAPPDATA%\Google\Chrome\User Data",
    r"%LOCALAPPDATA%\Microsoft\Edge\User Data",
    r"%LOCALAPPDATA%\BraveSoftware\Brave-Browser\User Data",
]


def detect_default_browser_history_path() -> str | None:
    """Path to the DEFAULT browser's History file, or None."""
    progid = ""
    try:
        out = subprocess.run(
            ["reg", "query",
             r"HKCU\Software\Microsoft\Windows\Shell\Associations"
             r"\UrlAssociations\https\UserChoice"],
            capture_output=True, text=True, timeout=5).stdout
        m = re.search(r"ProgId\s+REG_SZ\s+(\S+)", out)
        if m:
            progid = m.group(1).lower()
    except Exception:
        pass

    base = _PROGID_HISTORY.get(progid)
    candidates = [base] if base else _CHROMIUM_USER_DATA_FALLBACK
    for cand in candidates:
        root = os.path.expandvars(cand)
        # Prefer Default profile, else any profile that has a History file.
        primary = os.path.join(root, "Default", "History")
        if os.path.exists(primary):
            return primary
        try:
            for entry in os.listdir(root):
                hp = os.path.join(root, entry, "History")
                if entry != "System Profile" and os.path.exists(hp):
                    return hp
        except Exception:
            continue
    return None


def all_chromium_history_paths() -> list[str]:
    """Fallback: every Chromium History file we can find."""
    found = []
    for cand in _CHROMIUM_USER_DATA_FALLBACK:
        root = os.path.expandvars(cand)
        primary = os.path.join(root, "Default", "History")
        if os.path.exists(primary):
            found.append(primary)
    return found


# --------------------------------------------------------------------------
# Junk filter
# --------------------------------------------------------------------------

_IP_RE = re.compile(r"^https?://\d+\.\d+\.\d+\.\d+")

_JUNK_HOSTS = {
    "localhost", "127.0.0.1", "::1",
    "fonts.googleapis.com", "fonts.gstatic.com", "clients.google.com",
    "ssl.gstatic.com", "www.google-analytics.com", "analytics.google.com",
    "doubleclick.net", "googleads.g.doubleclick.net",
    "api.segment.io", "cdn.jsdelivr.net", "unpkg.com",
}


def _host_of(url: str) -> str | None:
    try:
        host = urlparse(url).netloc.lower().split(":")[0]
    except Exception:
        return None
    if not host or not url.startswith("http"):
        return None
    if _IP_RE.match(url):
        return None
    if host in _JUNK_HOSTS or host.endswith(".local"):
        return None
    return host


# Second-level labels under a country code: "google.com.ph", "bbc.co.uk".
_CC_SECOND = {"com", "co", "org", "net", "gov", "edu", "ac"}


def _host_parts(host: str):
    """(labels before the registrable domain, the registrable domain's labels)."""
    parts = [p for p in (host or "").lower().split(".") if p]
    n = 3 if len(parts) >= 3 and len(parts[-1]) == 2 and parts[-2] in _CC_SECOND else 2
    return parts[:-n], parts[-n:]


def _root_host(host: str) -> str:
    """'music.youtube.com' -> 'youtube.com'."""
    return ".".join(_host_parts(host)[1])


def _domain_name(host: str) -> str:
    """The speakable name: 'www.youtube.com' -> 'youtube'.

    A subdomain keeps its own name: 'music.youtube.com' -> 'youtube music',
    'accounts.google.com.ph' -> 'google accounts'. Before 2026-09-11 both
    became 'youtube' (and '.com.ph' became 'com'), so whichever subdomain
    was visited most took the domain's name and 'open youtube' opened
    YouTube Music."""
    subs, root = _host_parts(host)
    if not root:
        return (host or "").lower()
    subs = [s for s in subs if s not in ("www", "m", "mobile")]
    return f"{root[0]} {subs[-1]}" if subs else root[0]


def normalize_sites(sites: dict) -> list:
    """Name every history entry by its host (see _domain_name) and, where no
    entry holds a domain's bare name, give it as an alias to that domain's
    most-visited entry ('open deepseek' still opens chat.deepseek.com).
    Manual entries are never touched. Returns [(old_key, new_key)] renames."""
    renames = []
    for key in list(sites):
        site = sites[key]
        if site.get("source") != "history" or not site.get("host"):
            continue
        want = new = _domain_name(site["host"])
        n = 2
        while new != key and new in sites:
            new = f"{want}{n}"
            n += 1
        if new != key:
            sites[new] = sites.pop(key)
            renames.append((key, new))
    owners = {}
    for key, site in sites.items():
        if site.get("source") == "history" and " " in key:
            bare = key.split(" ")[0]
            if bare not in sites and (bare not in owners or
                                      (site.get("visits") or 0) > (sites[owners[bare]].get("visits") or 0)):
                owners[bare] = key
    for bare, key in owners.items():
        aliases = sites[key].setdefault("aliases", [])
        if bare not in aliases:
            aliases.append(bare)
    return renames


# --------------------------------------------------------------------------
# History scanning (incremental)
# --------------------------------------------------------------------------

def _scan_history_file(history_path: str, watermark_us: int) -> dict:
    """{domain_key: {url, name, visits_delta, top:{url:visits}, last_seen_us}}"""
    tmp = os.path.join(tempfile.gettempdir(), f"jarvis_hist_{os.getpid()}_{int(time.time())}")
    shutil.copy2(history_path, tmp)
    try:
        con = sqlite3.connect(tmp)
        try:
            rows = con.execute(
                "SELECT url, visit_count, last_visit_time FROM urls "
                "WHERE last_visit_time > ?", (watermark_us,)).fetchall()
        finally:
            con.close()
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass

    domains = {}
    for url, count, last_seen in rows:
        host = _host_of(url)
        if not host or count < 1:
            continue
        d = domains.setdefault(host, {
            "name": _domain_name(host), "visits": 0, "top": {}, "last": 0})
        d["visits"] += count
        d["top"][url] = d["top"].get(url, 0) + count
        d["last"] = max(d["last"], last_seen or 0)
    return domains


def scan_sites(force_full: bool = False) -> dict:
    """Incremental scan -> merged registry dict. Never loses manual entries."""
    reg = load_registry()
    watermark = 0 if force_full else int(reg.get("watermark_chrome_us") or 0)

    path = detect_default_browser_history_path()
    sources = [path] if path else all_chromium_history_paths()
    if not sources:
        reg["last_scan"] = datetime.now().isoformat(timespec="seconds")
        save_registry(reg)
        return reg

    max_last = watermark
    merged = {}
    for hp in sources:
        for host, d in _scan_history_file(hp, watermark).items():
            m = merged.setdefault(host, {"visits": 0, "top": {}, "last": 0,
                                         "name": d["name"]})
            m["visits"] += d["visits"]
            m["last"] = max(m["last"], d["last"])
            for u, c in d["top"].items():
                m["top"][u] = m["top"].get(u, 0) + c
            max_last = max(max_last, d["last"])

    sites = reg.setdefault("sites", {})
    normalize_sites(sites)

    # 1) update existing HISTORY-sourced entries (accumulate)
    # 2) promote scanned domains that are not yet present
    ranked = sorted(merged.items(), key=lambda kv: kv[1]["visits"], reverse=True)
    existing_hosts = {s.get("host") for s in sites.values() if s.get("source") == "history"}

    for host, m in ranked:
        key = next((k for k, v in sites.items()
                    if v.get("source") == "history" and v.get("host") == host), None)
        if key is None:
            if len([s for s in sites.values() if s.get("source") == "history"]) >= TOP_DOMAINS \
               and host not in existing_hosts:
                continue  # registry full, this domain didn't rank
            key = m["name"]
            suffix = 2
            # Any entry already under this name (manual, or another host)
            # keeps it: overwriting one lost that site's visits and URLs.
            while key in sites:
                key = f"{m['name']}{suffix}"; suffix += 1
            sites[key] = {"url": f"https://{host}", "host": host,
                          "aliases": [], "source": "history", "visits": 0,
                          "top_urls": []}
        entry = sites[key]
        entry["visits"] = int(entry.get("visits") or 0) + m["visits"]
        # merge top URLs
        agg = {u.get("url"): int(u.get("visits") or 0) for u in entry.get("top_urls", [])}
        for u, c in m["top"].items():
            agg[u] = agg.get(u, 0) + c
        entry["top_urls"] = [{"url": u, "visits": c} for u, c in
                             sorted(agg.items(), key=lambda kv: kv[1], reverse=True)
                             [:TOP_URLS_PER_DOMAIN]]

    # prune history entries that fell below the junk gate entirely
    for k in [k for k, v in sites.items()
              if v.get("source") == "history"
              and (v.get("visits") or 0) * 0 >= MIN_VISITS]:  # never prune accumulated
        pass  # accumulation means counts only grow; pruning handled by ranking cap above

    normalize_sites(sites)
    reg["watermark_chrome_us"] = max_last
    reg["last_scan"] = datetime.now().isoformat(timespec="seconds")
    save_registry(reg)
    return reg


# --------------------------------------------------------------------------
# Manual entries
# --------------------------------------------------------------------------

def add_manual_site(name: str, url: str, aliases: list[str] = None) -> str:
    """Add/overwrite a manual site entry. Manual always shadows history."""
    name = name.strip().lower()
    url = url.strip()
    if not url.startswith("http"):
        url = "https://" + url
    reg = load_registry()
    entry = reg["sites"].get(name, {})
    entry.update({
        "url": url,
        "aliases": [a.strip().lower() for a in (aliases or [])],
        "source": "manual",
        "host": urlparse(url).netloc.lower(),
    })
    entry.setdefault("visits", None)
    entry.setdefault("top_urls", [])
    reg["sites"][name] = entry
    save_registry(reg)
    return f"Added site '{name}' -> {url}" + (
        f" (aliases: {', '.join(entry['aliases'])})" if entry["aliases"] else "")


# --------------------------------------------------------------------------
# Resolution index
# --------------------------------------------------------------------------

_index = {"built_from": None, "exact": {}, "names": []}


def _singular(word: str) -> str:
    return word[:-1] if word.endswith("s") and len(word) > 3 else word


def build_index(reg: dict = None) -> None:
    reg = reg or load_registry()
    # A site's own name beats an alias, and a manual entry beats history:
    # the manual "youtube" (youtube.com) must win over the "youtube" alias of
    # "youtube music", whatever order the entries are in.
    ranked = {}
    for name, site in reg.get("sites", {}).items():
        manual = site.get("source") == "manual"
        aliases = site.get("aliases", [])
        tiers = [(0, {name, _singular(name)}),
                 (1, {_singular(a) for a in aliases} | {a.lower() for a in aliases}),
                 (2, {_domain_name(site["host"])} if site.get("host") else set())]
        for tier, keys in tiers:
            for k in keys:
                rank = (tier, not manual)
                if k and (k not in ranked or rank < ranked[k][0]):
                    ranked[k] = (rank, name)
    exact = {k: name for k, (_, name) in ranked.items()}
    names = list(reg.get("sites", {}))
    _index["exact"] = exact
    _index["names"] = names
    _index["built_from"] = reg.get("generated")


def ensure_index() -> None:
    reg = load_registry()
    if _index["built_from"] != reg.get("generated"):
        build_index(reg)


def resolve_site(name: str):
    """O(1) exact/plural hit, then fuzzy. Returns registry key or None."""
    ensure_index()
    q = _singular((name or "").strip().lower())
    if q in _index["exact"]:
        return _index["exact"][q]
    close = difflib.get_close_matches(q, list(_index["exact"].keys()), n=1, cutoff=0.8)
    return _index["exact"][close[0]] if close else None


def get_site(key: str):
    reg = load_registry()
    return reg.get("sites", {}).get(key)


def list_sites(limit: int = TOP_DOMAINS) -> str:
    reg = load_registry()
    items = sorted(reg.get("sites", {}).items(),
                   key=lambda kv: -(kv[1].get("visits") or 0))
    if not items:
        return "No sites registered yet. Say 'rescan my sites' or 'add site <name> <url>'."
    lines = []
    for name, s in items[:limit]:
        tag = "manual" if s.get("source") == "manual" else "auto"
        visits = s.get("visits")
        vc = f", ~{visits} visits" if visits else ""
        al = f" (aka {', '.join(s['aliases'])})" if s.get("aliases") else ""
        lines.append(f"- {name}: {s['url']} [{tag}{vc}]{al}")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Startup refresh (policy C, non-blocking)
# --------------------------------------------------------------------------

_refreshing = False


def maybe_refresh_async() -> bool:
    """Stat-check staleness; scan on a background thread if stale.
    Returns True if a scan was kicked off. Voice resolution keeps using
    the current registry meanwhile."""
    global _refreshing
    if _refreshing:
        return False
    if not is_stale():
        return False
    import threading

    def run():
        global _refreshing
        try:
            scan_sites()
            build_index()
            print("[WEB_REGISTRY] background refresh complete", flush=True)
        except Exception as e:
            print(f"[WEB_REGISTRY] background refresh failed: {e}", flush=True)
        finally:
            _refreshing = False

    _refreshing = True
    threading.Thread(target=run, daemon=True, name="web-registry-refresh").start()
    return True
