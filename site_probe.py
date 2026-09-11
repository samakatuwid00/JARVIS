"""site_probe.py - load real pages in a hidden Chrome to check a rule works.

Used at rule setup (rules_sim) to:
  1. read a site's own search form -> its real search address, and
  2. load a sample search and judge the page: ok / not_found / no_results /
     blocked / unclear, with the evidence (page title, hits).

Uses the installed Chrome in headless mode with a persistent JARVIS verifier
profile, separate from the user's own Brave/Chrome profiles. Keeping the
profile keeps a guarded site's bot-check clearance (Cloudflare), so later
checks take ~2-3s; the first visit to such a site can take up to ~30s while
the check clears. Measured 2026-09-11 on hollymoviehd.cc.
"""

import html
import json
import os
import re
import subprocess
import threading
import urllib.parse

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PROFILE_DIR = os.path.join(os.getenv("LOCALAPPDATA") or os.path.expanduser("~"),
                           "JARVIS", "verifier-chrome")
# host -> search address a real page check verified (see remember_search).
CACHE_PATH = os.path.join(os.path.dirname(PROFILE_DIR), "site_search.json")
_CHROME_PATHS = (
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
)
# One headless Chrome per profile at a time: a second instance on the same
# user-data-dir would fail to lock it.
_LOCK = threading.Lock()

_CHALLENGE_RE = re.compile(
    r"<title>\s*(just a moment|attention required|checking your browser|"
    r"verify you are human|access denied|security check)", re.I)
_NOT_FOUND_RE = re.compile(r"\b(page not found|404|not found|no longer available)\b", re.I)
_NO_RESULTS_RE = re.compile(
    r"\b(no results|nothing found|0 results|did not match any|no matches|"
    r"sorry, but nothing matched|no posts found)\b", re.I)
_FORM_RE = re.compile(r"<form\b([^>]*)>(.*?)</form>", re.I | re.S)
_INPUT_RE = re.compile(r"<input\b([^>]*)>", re.I)
_ATTR_RE = re.compile(r"([\w:-]+)\s*=\s*(\"[^\"]*\"|'[^']*'|[^\s>]+)")
# Field names sites use for the search words, most specific first.
_SEARCH_FIELDS = ("s", "q", "query", "search_query", "search", "keyword", "keywords",
                  "k", "term", "searchword", "text", "wd")


def chrome_path():
    """The installed Chrome, from the app registry first."""
    try:
        with open(os.path.join(BASE_DIR, "app_registry.json"), encoding="utf-8") as f:
            exe = (json.load(f).get("apps", {}).get("chrome") or {}).get("bin")
        if exe and os.path.isfile(exe):
            return exe
    except Exception:
        pass
    return next((p for p in _CHROME_PATHS if os.path.isfile(p)), None)


def fetch_dom(url, budget_ms=10000, timeout=60):
    """The page's DOM after scripts ran, or "" on failure."""
    exe = chrome_path()
    # Web pages only: the url sits in Chrome's argv, where "--anything" would
    # be read as a switch.
    if not exe or not re.match(r"^https?://[^\s\"'<>]+$", url or "", re.I):
        return ""
    cmd = [exe, "--headless=new", "--disable-gpu", "--no-first-run",
           "--no-default-browser-check", "--mute-audio",
           f"--user-data-dir={PROFILE_DIR}", f"--virtual-time-budget={budget_ms}",
           "--dump-dom", url]
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    with _LOCK:
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                    stderr=subprocess.DEVNULL, creationflags=flags)
        except OSError:
            return ""
        try:
            out, _ = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            # A hung Chrome keeps the profile locked for every later check:
            # take down its whole process tree, not just the parent.
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                           capture_output=True, creationflags=flags)
            try:
                proc.communicate(timeout=5)
            except Exception:
                pass
            return ""
    return (out or b"").decode("utf-8", errors="replace")


def title_of(dom):
    m = re.search(r"<title[^>]*>(.*?)</title>", dom or "", re.I | re.S)
    return html.unescape(re.sub(r"\s+", " ", m.group(1))).strip() if m else ""


def visible_text(dom):
    """Page text without scripts, styles and form fields (a search box echoes
    the query, which must not count as a result)."""
    t = re.sub(r"<(script|style|noscript|template)\b.*?</\1>", " ", dom or "", flags=re.I | re.S)
    t = re.sub(r"<(input|textarea)\b[^>]*>", " ", t, flags=re.I)
    # alt/title attributes carry result names on poster grids: keep their text
    t = re.sub(r"\b(?:alt|title)\s*=\s*\"([^\"]*)\"", r" \1 ", t, flags=re.I)
    t = re.sub(r"<[^>]+>", " ", t)
    return re.sub(r"\s+", " ", html.unescape(t))


def load(url):
    """{"url", "dom", "title", "blocked"}; retries once with a long budget when
    a bot check is showing, since it clears itself on a first visit."""
    # Normal loads take 3-7s; the bot-check clearance below gets a long budget.
    dom = fetch_dom(url, budget_ms=8000, timeout=12)
    if not dom:
        # headless Chrome occasionally hangs on a page it loads fine the next
        # time (1 in 3 on hollymoviehd.cc): one retry before judging.
        dom = fetch_dom(url, budget_ms=8000, timeout=15)
    if _CHALLENGE_RE.search(dom or ""):
        dom = fetch_dom(url, budget_ms=30000, timeout=90)
    return {"url": url, "dom": dom, "title": title_of(dom),
            "blocked": bool(_CHALLENGE_RE.search(dom or "")) or not dom}


def _attrs(tag_attrs):
    return {k.lower(): v.strip("\"'") for k, v in _ATTR_RE.findall(tag_attrs or "")}


def search_template_from_dom(dom, page_url):
    """The site's own search address ('https://site/?s={query}') from a GET
    search form on the page, or None."""
    best = None
    for form_attrs, body in _FORM_RE.findall(dom or ""):
        fa = _attrs(form_attrs)
        if fa.get("method", "get").lower() != "get":
            continue
        fields, fixed = [], []
        for input_attrs in _INPUT_RE.findall(body):
            ia = _attrs(input_attrs)
            name = ia.get("name")
            if not name:
                continue
            kind = ia.get("type", "text").lower()
            if kind == "hidden":
                fixed.append((name, html.unescape(ia.get("value", ""))))
            elif kind in ("text", "search", ""):
                fields.append(name)
        field = next((f for f in _SEARCH_FIELDS if f in fields), None)
        if not field:
            continue
        action = urllib.parse.urljoin(page_url, html.unescape(fa.get("action") or page_url))
        base = action.split("#", 1)[0]
        query = urllib.parse.urlencode(fixed)
        sep = "&" if "?" in base else "?"
        template = f"{base}{sep}{query + '&' if query else ''}{field}={{query}}"
        score = _SEARCH_FIELDS.index(field) - (5 if "search" in form_attrs.lower() else 0)
        if best is None or score < best[0]:
            best = (score, template, field)
    if not best:
        return None
    return {"search_url": best[1], "field": best[2]}


def _host(url):
    h = urllib.parse.urlsplit(url if "://" in url else "https://" + url).netloc.lower()
    return h[4:] if h.startswith("www.") else h


def _read_cache():
    try:
        with open(CACHE_PATH, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def remember_search(site, search_url):
    """Save a search address a real page check verified, so the next rule
    for the same site skips reading its homepage."""
    data = _read_cache()
    data[_host(site)] = search_url
    try:
        os.makedirs(os.path.dirname(CACHE_PATH), exist_ok=True)
        tmp = CACHE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=1)
        os.replace(tmp, CACHE_PATH)
    except OSError:
        pass


def discover_search(site_url):
    """The site's search address: a previously verified one, else read from
    the search form on its homepage.

    Returns {"search_url", "field", "title"[, "cached"]} | {"blocked": True} | None.
    """
    known = _read_cache().get(_host(site_url))
    if known:
        return {"search_url": known, "field": "", "title": "", "cached": True}
    page = load(site_url)
    if page["blocked"]:
        return {"blocked": True, "title": page["title"]}
    found = search_template_from_dom(page["dom"], site_url)
    if found:
        found["title"] = page["title"]
    return found


def judge_search(dom, title, sample):
    """(status, evidence) for a loaded search-results page."""
    if _CHALLENGE_RE.search(dom or "") or not dom:
        return "blocked", f"the site's bot check stopped the page ({title or 'no page'})"
    if _NOT_FOUND_RE.search(title or ""):
        return "not_found", f"page title \"{title}\""
    text = visible_text(dom)
    hits = len(re.findall(re.escape(sample), text, re.I)) if sample else 0
    in_title = bool(sample) and sample.lower() in (title or "").lower()
    if hits >= 2 or (in_title and hits >= 1):
        return "ok", f"page title \"{title}\", \"{sample}\" appears {hits} times"
    if _NO_RESULTS_RE.search(text[:20000]):
        return "no_results", f"page title \"{title}\" says nothing was found"
    return "unclear", f"page title \"{title}\", \"{sample}\" appears {hits} times"


def probe_search(search_url, sample):
    """Load search_url for sample and judge it. {"status", "evidence", "url", "title"}."""
    url = search_url.replace("{query}", urllib.parse.quote_plus(sample))
    page = load(url)
    status, evidence = judge_search(page["dom"], page["title"], sample)
    return {"status": status, "evidence": evidence, "url": url, "title": page["title"]}


_PLAY_TARGET_RE = re.compile(r"play|player|video|watch|movie|stream", re.I)
_MEDIA_RE = re.compile(r"<(iframe|video)\b([^>]*)>", re.I)
_NOT_PLAYER_RE = re.compile(r"captcha|trailer|javascript:false|doubleclick|googlesyndication", re.I)


def click_target_in_dom(dom, target):
    """Is there something on the page that browser_cdp.click would hit for
    `target`? A player (iframe/video that is not a captcha or trailer) for
    play-like targets, else visible text containing every word of the target."""
    if _PLAY_TARGET_RE.search(target or ""):
        for _, attrs in _MEDIA_RE.findall(dom or ""):
            if not _NOT_PLAYER_RE.search(attrs) and 'display: none' not in attrs.lower():
                return True
    words = [w for w in re.findall(r"[a-z0-9]+", (target or "").lower()) if len(w) > 2]
    text = visible_text(dom).lower()
    return bool(words) and all(w in text for w in words)


def probe_open(url):
    """Does the page load (not a 404, not a bot check)?"""
    page = load(url)
    if page["blocked"]:
        return {"status": "blocked", "url": url, "title": page["title"],
                "evidence": "the site's bot check stopped the page"}
    if _NOT_FOUND_RE.search(page["title"] or ""):
        return {"status": "not_found", "url": url, "title": page["title"],
                "evidence": f"page title \"{page['title']}\""}
    return {"status": "ok", "url": url, "title": page["title"],
            "evidence": f"page title \"{page['title']}\""}
