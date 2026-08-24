"""Music playback for JARVIS - drives YouTube Music inside Brave via Playwright.

Same shape as browser_agent._BrowserWorker and for the same reason: Playwright
objects are thread-affine and its sync API refuses to run inside an asyncio
loop, while jarvis_web calls brain.think() straight from the WebSocket handler.
So one dedicated worker thread owns the Brave instance for the life of the
process and every call is marshalled onto it through a queue.

Deliberately a SEPARATE worker from browser_agent's: that one owns Chrome and
the ChatGPT session, and music must not steal or close that tab. Brave is
launched headed with its own profile directory so playback is visible and
audible, and so it never fights Chrome for a profile lock.

If Playwright cannot drive Brave at all (missing binary, launch failure), the
tools fall back to opening the YouTube Music search URL in Brave via the OS and say
so plainly rather than pretending playback started.
"""

import difflib
import os
import queue
import re
import subprocess
import threading
import traceback
import urllib.parse
import webbrowser
from pathlib import Path

BRAVE_EXE = os.getenv(
    "JARVIS_BRAVE_EXE",
    r"C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe",
)
# Dedicated profile: Brave's default profile is locked while Brave runs, and
# pointing at it would let JARVIS act as the user's everyday logged-in browser.
PROFILE_DIR = Path(os.getenv("JARVIS_MUSIC_PROFILE",
                             Path.home() / ".jarvis-brave-profile"))
YOUTUBE_SEARCH = "https://music.youtube.com/search?q={}"

# YouTube Music markup, NOT youtube.com: music.youtube.com renders ytmusic-*
# components and has no ytd-video-renderer / a#video-title anywhere, so the
# youtube.com selectors this list used to hold matched zero elements and every
# play_music call fell through to _fallback_open. Verified against a live
# search page: the row title link is the reliable play target. Ordered most to
# least specific; try each and report if all miss.
RESULT_SELECTORS = [
    "ytmusic-responsive-list-item-renderer .title-column a",
    "ytmusic-responsive-list-item-renderer yt-formatted-string.title a",
    "ytmusic-responsive-list-item-renderer",
    "a[href*='watch?v=']",
]
CONSENT_SELECTORS = [
    "button[aria-label*='Accept all']",
    "button[aria-label*='Reject all']",
    "form[action*='consent'] button",
    "ytmusic-you-there-renderer button",
]
# Clicked only after playback has been verified NOT to have started: these are
# toggles, so hitting one while audio is already running would pause it.
PLAY_BUTTON_SELECTORS = [
    "ytmusic-player-bar #play-pause-button",
    "ytmusic-player-bar button.play-pause-button",
    "ytmusic-player-bar tp-yt-paper-icon-button#play-pause-button",
    "#play-pause-button",
    "ytmusic-player button[aria-label*='Play']",
    "button[aria-label*='Play']",
    ".ytp-play-button",
]
# Read the track that is ACTUALLY playing. The tab title is tried first (a
# /watch page titles itself "Song - Artist - YouTube Music"); these player-bar
# nodes are the fallback when it is unreadable. Most specific first.
PLAYING_TITLE_SELECTORS = [
    "ytmusic-player-bar .title",
    "ytmusic-player-bar yt-formatted-string.title",
    "ytmusic-player-bar .content-info-wrapper yt-formatted-string",
]
_TITLE_STOPWORDS = {"the", "a", "an", "and", "or", "for", "in", "on", "of", "to", "by",
                    "with", "my", "your", "me"}

# Release metadata a genuine track title carries that says nothing about WHICH
# song it is. Ignored on the title side of the video-vs-song test so
# "Bohemian Rhapsody (Official Video Remastered)" is not scored as junk-heavy.
_TITLE_NOISE = {"official", "video", "audio", "lyric", "lyrics", "remaster",
                "remastered", "version", "edit", "mix", "radio", "live",
                "album", "full", "feat", "ft", "hd", "hq", "mv", "topic",
                "visualizer", "extended", "soundtrack"}

# A title is treated as "a video ABOUT the song" - not the song - when it
# carries at least this many content words the query never asked for AND the
# query explains less than this fraction of its content words. Tuned on the
# live false accept: "The most INSANE Bohemian Rhapsody Flashmob you will ever
# see!!" scores 7 unmatched / 0.22 coverage, while the tightest true match
# ("Deep Focus - Calm Instrumental Mix") scores 2 unmatched / 0.50 coverage.
_VIDEO_JUNK_WORDS = 4
_VIDEO_COVERAGE_MIN = 0.35

# Whole-string floor for titles that share no meaningful text with the query.
# The tightest true match ("lofi hip hop radio - beats to relax/study to" vs
# "lofi study beats") sits at 0.414; unrelated titles land under 0.2.
_TITLE_SIMILARITY_MIN = 0.35

# Kick playback off inside the page. Returning a dict (never throwing) matters:
# an exception here would surface as a worker error and be mistaken for a
# transport failure instead of "autoplay was blocked".
_JS_START_PLAY = """
() => {
  try {
    const v = document.querySelector('video');
    if (!v) return {state: 'novideo'};
    window.__jarvisPlayError = '';
    try { v.muted = false; } catch (e) {}
    try { if (!v.volume) v.volume = 1; } catch (e) {}
    try {
      const p = v.play();
      if (p && typeof p.catch === 'function') {
        p.catch(err => {
          window.__jarvisPlayError = String((err && (err.name || err.message)) || err);
        });
      }
    } catch (e) {
      return {state: 'threw', error: String((e && (e.name || e.message)) || e)};
    }
    return {state: 'called'};
  } catch (e) {
    return {state: 'error', error: String(e)};
  }
}
"""

# Read-only probe used for polling; must never mutate player state.
_JS_PROBE = """
() => {
  try {
    const v = document.querySelector('video');
    if (!v) return {video: false};
    return {
      video: true,
      paused: v.paused,
      muted: v.muted,
      volume: v.volume,
      currentTime: v.currentTime,
      readyState: v.readyState,
      ended: v.ended,
      error: window.__jarvisPlayError || ''
    };
  } catch (e) {
    return {video: false, error: String(e)};
  }
}
"""


def _is_closed_error(e):
    # A closed/dead browser, context, or page. Playwright reports these in
    # several ways; match the class and the common message strings.
    if getattr(type(e), "__name__", "") == "TargetClosedError":
        return True
    msg = " ".join(str(e).split()).lower()
    return any(k in msg for k in ("target closed", "target page",
                                  "context or browser has been closed",
                                  "browser has been closed", "page closed",
                                  "connection closed"))


def _is_dead_browser_message(text: str) -> bool:
    """Same test as _is_closed_error, but against the [Error] string call() returns.

    The worker turns exceptions into text before play_music ever sees them, so
    the recovery decision has to be made on the message.
    """
    low = " ".join((text or "").split()).lower()
    return any(k in low for k in ("targetclosederror", "target closed", "target page",
                                  "context or browser has been closed",
                                  "browser has been closed", "page closed",
                                  "connection closed", "browser closed",
                                  "call timed out", "browser.newpage",
                                  "browsercontext.new_page"))


def _kill_profile_brave():
    """Kill Brave processes launched against JARVIS's OWN music profile.

    Scoped by --user-data-dir deliberately: the user's everyday Brave must never
    be killed by a music retry. A half-dead Brave that still holds the profile
    is what makes launch_persistent_context fail on the rebuild, so it has to go
    before a relaunch can succeed.
    """
    marker = str(PROFILE_DIR).replace("'", "''")
    ps = ("Get-CimInstance Win32_Process -Filter \"Name='brave.exe'\" | "
          "Where-Object { $_.CommandLine -like '*" + marker + "*' } | "
          "ForEach-Object { Stop-Process -Id $_.ProcessId -Force "
          "-ErrorAction SilentlyContinue }")
    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
            timeout=25, capture_output=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except Exception:
        pass


def _clear_singleton_locks():
    """Remove the profile lock files a crashed Chromium leaves behind.

    Only safe once no Brave owns the profile, so this is always called straight
    after _kill_profile_brave().
    """
    for name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
        p = PROFILE_DIR / name
        try:
            if p.is_symlink() or p.exists():
                p.unlink()
        except Exception:
            pass


# Sentinel job: tells the worker thread to tear its Playwright handles down and
# exit. Playwright objects are thread-affine, so only that thread may close them.
_STOP = object()


class BraveMusicWorker:
    """Single thread owning the Brave Playwright context; all calls funnel through it."""

    def __init__(self):
        self._jobs = queue.Queue()
        self._thread = None
        self._lock = threading.Lock()
        self._pw = None
        self._ctx = None
        self._page = None
        # Bumped by _hard_reset. A worker thread whose generation is stale must
        # not touch the shared handles again — otherwise a thread that was
        # wedged past the reset could wake up and rebuild a context underneath
        # its replacement, leaving two Braves and one of them unreachable.
        self._gen = 0

    def call(self, fn, timeout=180):
        """Run fn(page) on the worker thread, relaunching Brave if it died.

        Two levels of recovery, because they fail for different reasons. The
        in-thread retry in _loop covers a context/page that closed while the
        Brave PROCESS is still alive. When the process itself is gone, that
        retry cannot help: the Playwright driver and its sync dispatcher belong
        to the worker thread and the dead Brave may still hold the profile lock,
        so the whole worker is retired and rebuilt here instead.
        """
        out = self._call_once(fn, timeout)
        if isinstance(out, str) and out.startswith("[Error]") and _is_dead_browser_message(out):
            print(f"[music] Brave looks dead ({out[:120]}); relaunching...", flush=True)
            self._hard_reset()
            out = self._call_once(fn, timeout)
        return out

    def _call_once(self, fn, timeout):
        with self._lock:
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._loop, args=(self._jobs,),
                                                daemon=True)
                self._thread.start()
            jobs = self._jobs
        reply = queue.Queue(maxsize=1)
        jobs.put((fn, reply))
        try:
            ok, value = reply.get(timeout=timeout)
        except queue.Empty:
            return f"[Error] Music browser call timed out after {timeout}s"
        if not ok:
            return f"[Error] {value}"
        return value

    def _hard_reset(self):
        """Retire the worker thread and relaunch Brave from nothing.

        The thread is given its own queue generation so a wedged old thread can
        never steal a job from the new one, and it does its own teardown because
        Playwright handles may only be touched by the thread that made them.
        """
        with self._lock:
            thread, jobs = self._thread, self._jobs
            self._thread = None
            self._jobs = queue.Queue()

        if thread is not None and thread.is_alive():
            reply = queue.Queue(maxsize=1)
            jobs.put((_STOP, reply))
            try:
                reply.get(timeout=20)
            except queue.Empty:
                pass  # Wedged in a dead driver call; it starves on the old queue.
            thread.join(timeout=10)

        # Drop the handles even if the thread never got to: they point at a
        # browser that no longer exists.
        self._ctx = self._page = self._pw = None
        _kill_profile_brave()
        _clear_singleton_locks()

    def _loop(self, jobs):
        while True:
            fn, reply = jobs.get()
            if fn is _STOP:
                try:
                    self._teardown()
                except Exception:
                    pass
                if reply is not None:
                    reply.put((True, "stopped"))
                return
            try:
                page = self._ensure_page()
                reply.put((True, fn(page)))
            except Exception as e:
                if _is_closed_error(e):
                    # The Brave context/page died (closed or zombie). Drop it and
                    # re-run the SAME job once on a rebuilt context so playback
                    # actually starts instead of silently falling back to a URL open.
                    try:
                        self._teardown()
                        page = self._ensure_page()
                        reply.put((True, fn(page)))
                    except Exception as e2:
                        traceback.print_exc()
                        reply.put((False, f"{type(e2).__name__}: {e2}"))
                    continue
                traceback.print_exc()
                reply.put((False, f"{type(e).__name__}: {e}"))

    def _teardown(self):
        """Drop every Playwright handle, closing whatever still closes."""
        if self._ctx is not None:
            try:
                self._ctx.close()
            except Exception:
                pass
        if self._pw is not None:
            try:
                self._pw.stop()
            except Exception:
                pass
        self._ctx = self._page = self._pw = None

    def _ctx_dead(self) -> bool:
        """True when _ctx is missing or a stale handle to a dead Brave.

        This worker is a module-level singleton, so _pw/_ctx outlive a Brave
        crash, a manual window close and anything else that kills the browser
        process. The handles stay non-None and only blow up on first use, which
        is how new_page() came to raise TargetClosedError on every play_music.
        """
        if self._ctx is None:
            return True
        checker = getattr(self._ctx, "is_closed", None)
        if checker is None:
            # Older Playwright without the predicate: the retry in
            # _ensure_page is the safety net rather than a wrong guess here.
            return False
        try:
            return bool(checker())
        except Exception:
            return True

    def _build_page(self):
        """Create whatever handles are missing and return a usable page."""
        from playwright.sync_api import sync_playwright

        if self._pw is None:
            self._pw = sync_playwright().start()

        if self._ctx is None:
            PROFILE_DIR.mkdir(parents=True, exist_ok=True)
            self._ctx = self._pw.chromium.launch_persistent_context(
                user_data_dir=str(PROFILE_DIR),
                executable_path=BRAVE_EXE,
                headless=False,
                args=["--disable-blink-features=AutomationControlled"],
                viewport={"width": 1280, "height": 800},
            )

        pages = [p for p in self._ctx.pages if not p.is_closed()]
        self._page = pages[0] if pages else self._ctx.new_page()
        return self._page

    def _ensure_page(self):
        # A closed context is not a cache hit: forget it before anything reuses
        # it, otherwise new_page() below throws TargetClosedError.
        if self._ctx_dead():
            self._ctx = None
            self._page = None

        if self._page is not None:
            try:
                if not self._page.is_closed():
                    return self._page
            except Exception:
                pass
            self._page = None

        if not Path(BRAVE_EXE).exists():
            raise RuntimeError(f"Brave was not found at {BRAVE_EXE}")

        try:
            return self._build_page()
        except Exception:
            # is_closed() can still miss a half-dead browser, so treat any
            # failure to build as stale state: tear everything down and rebuild
            # from scratch exactly once. A second failure propagates to _loop so
            # the tool reports an honest [Error] instead of a silent no-op.
            traceback.print_exc()
            self._teardown()
            # A Brave that crashed with the profile still checked out makes the
            # relaunch fail the same way forever; clear the corpse first.
            _kill_profile_brave()
            _clear_singleton_locks()
            return self._build_page()

    def is_open(self) -> bool:
        try:
            return self._page is not None and not self._page.is_closed()
        except Exception:
            return False

    def shutdown(self):
        # Through _hard_reset, not _teardown directly: Playwright handles may
        # only be closed by the thread that created them, and close_music() is
        # called from the WebSocket/tool thread. The direct call raised there,
        # got swallowed, and left an orphan Brave running with JARVIS no longer
        # holding a handle to it.
        self._hard_reset()


_worker = BraveMusicWorker()
# Set once the Brave context exists, so only the first play_music pays launch cost.
_warmed = False

# Module-level progress callback — set by tools.py before calling play_music,
# so the job() running on the BraveWorker thread can emit mid-task status.
_progress_cb = None


def set_progress_cb(cb):
    global _progress_cb
    _progress_cb = cb


def _emit(msg):
    """Emit a progress update if a callback is set."""
    if _progress_cb:
        try:
            _progress_cb(msg)
        except Exception:
            pass


def _first_visible(page, selectors, timeout=10000):
    """Return the first element that is actually visible, across all selectors.

    Scans every match rather than only .first: on a YouTube Music search page a
    selector routinely resolves to more offscreen matches than visible ones
    (the title link matches 14 nodes but only 6 render, and the first visible
    one sits at index 2). Taking .first therefore latched onto a hidden node and
    timed out waiting for it to become visible, which is why correct selectors
    alone were not enough to make playback work.
    """
    per_sel = max(1000, timeout // max(1, len(selectors)))
    for sel in selectors:
        try:
            loc = page.locator(sel)
            # Wait for the selector to attach at all before scanning matches.
            loc.first.wait_for(state="attached", timeout=per_sel)
        except Exception:
            continue
        try:
            for i in range(min(loc.count(), 25)):
                node = loc.nth(i)
                try:
                    if node.is_visible():
                        return node
                except Exception:
                    continue
        except Exception:
            continue
    return None


def _dismiss_consent(page):
    """YouTube Music sometimes interposes a cookie wall on a fresh profile."""
    el = _first_visible(page, CONSENT_SELECTORS, timeout=3000)
    if el is not None:
        try:
            el.click(timeout=2000)
            page.wait_for_timeout(1200)
        except Exception:
            pass


def _collect_candidates(page, timeout=15000):
    """Visible, plausibly-playable result rows, most specific selector first.

    The loose a[href*='watch?v='] tail of RESULT_SELECTORS also matches album,
    playlist and channel links, which load a page that never autoplays; those
    are filtered here so a bad match cannot masquerade as a started track.
    Returns dicts so a candidate can be re-resolved and described after a
    navigation invalidated the original handle.
    """
    per_sel = max(1000, timeout // max(1, len(RESULT_SELECTORS)))
    out, seen = [], set()
    for sel in RESULT_SELECTORS:
        try:
            loc = page.locator(sel)
            loc.first.wait_for(state="attached", timeout=per_sel)
        except Exception:
            continue
        try:
            count = min(loc.count(), 25)
        except Exception:
            continue
        for i in range(count):
            node = loc.nth(i)
            try:
                if not node.is_visible():
                    continue
                href = node.get_attribute("href") or ""
                text = " ".join((node.inner_text() or "").split())
            except Exception:
                continue
            if href and "watch" not in href:
                # browse/, channel/, playlist?list= ... never a single track.
                continue
            key = (href, text[:80])
            if not text and not href:
                continue
            if key in seen:
                continue
            seen.add(key)
            out.append({"selector": sel, "index": i, "href": href, "text": text})
    return out


def _resolve(page, cand):
    return page.locator(cand["selector"]).nth(cand["index"])


def _probe(page):
    try:
        state = page.evaluate(_JS_PROBE)
    except Exception as e:
        return {"video": False, "error": f"{type(e).__name__}: {e}"}
    return state if isinstance(state, dict) else {"video": False}


def _start_and_verify(page, wait_ms=5000):
    """Force playback, then poll until the video is genuinely advancing.

    Returns (playing: bool, reason: str). "Playing" requires paused === false
    AND currentTime > 0 AND currentTime moving between two samples - a loaded
    but stalled player reports paused === false while sitting at 0.
    """
    try:
        page.wait_for_selector("video", timeout=8000)
    except Exception:
        return False, "no video element appeared"

    try:
        started = page.evaluate(_JS_START_PLAY)
    except Exception as e:
        started = {"state": "error", "error": f"{type(e).__name__}: {e}"}
    if isinstance(started, dict) and started.get("state") == "novideo":
        return False, "no video element in the page"

    prev = None
    last = {}
    deadline = max(1, wait_ms // 250)
    for _ in range(deadline):
        last = _probe(page)
        if not last.get("video"):
            page.wait_for_timeout(250)
            continue
        current = last.get("currentTime") or 0
        if not last.get("paused") and current > 0 and prev is not None and current > prev + 0.05:
            return True, "advancing"
        prev = current
        page.wait_for_timeout(250)

    if not last.get("video"):
        return False, "no video element in the page"
    err = (last.get("error") or "").strip()
    if err:
        return False, f"play() rejected ({err})"
    if last.get("paused"):
        return False, "video paused"
    return False, "video not advancing (currentTime stuck at %.2f)" % (last.get("currentTime") or 0)


def _click_play_button(page):
    """Click the player's own play control. Only safe when NOT already playing."""
    for sel in PLAY_BUTTON_SELECTORS:
        try:
            loc = page.locator(sel)
            count = min(loc.count(), 5)
        except Exception:
            continue
        for i in range(count):
            node = loc.nth(i)
            try:
                if not node.is_visible():
                    continue
                label = (node.get_attribute("aria-label") or
                         node.get_attribute("title") or "").lower()
            except Exception:
                continue
            # "Pause", "Play next", "Play previous" would do the wrong thing.
            if "pause" in label or "next" in label or "previous" in label:
                continue
            try:
                node.click(timeout=3000)
                return True
            except Exception:
                continue
    return False


def _clean_tab_title(raw: str) -> str:
    """"Song - Artist - YouTube Music" -> "Song". "" when nothing usable."""
    t = " ".join((raw or "").split())
    if not t:
        return ""
    low = t.lower()
    if low.endswith("- youtube music"):
        t = t[:-len("- youtube music")].strip(" -")
    elif low in ("youtube music", "music"):
        return ""
    parts = [p.strip() for p in t.split(" - ") if p.strip()]
    if len(parts) >= 2:
        # Last segment is the artist; a song whose own name contains " - "
        # keeps everything before it rather than being truncated to its head.
        return " - ".join(parts[:-1])
    return parts[0] if parts else ""


def _strip_ytm_suffix(raw: str) -> str:
    """Drop only the " - YouTube Music" tail; every other segment is kept.

    The artist and any extra dash segments stay because the matcher needs them:
    a real YTM tab title like "Deep Focus - Calm Instrumental Mix" is all song
    name, and dropping its tail would hide half the query's tokens.
    """
    t = " ".join((raw or "").split())
    if not t:
        return ""
    low = t.lower()
    if low.endswith("- youtube music"):
        t = t[:-len("- youtube music")].strip(" -")
    elif low in ("youtube music", "music"):
        return ""
    return t


def _playing_titles(page):
    """(display, match) for the track actually playing, read off the live page.

    `display` is the short song-only string for TTS; `match` is the full title
    minus the " - YouTube Music" suffix, for _title_matches. Both are "" when no
    source is readable - callers must treat that as "cannot tell", never as a
    mismatch, so a selector miss does not reject a track that is playing right.
    """
    # YouTube Music is an SPA: the tab title is rewritten a beat after the URL
    # becomes /watch, so poll briefly instead of sampling once.
    for attempt in range(4):
        try:
            raw = page.title()
        except Exception:
            raw = ""
        title = _clean_tab_title(raw)
        if title:
            print(f"[music] now playing (page.title): {title}", flush=True)
            return title, _strip_ytm_suffix(raw) or title
        if attempt < 3:
            try:
                page.wait_for_timeout(500)
            except Exception:
                break

    for sel in PLAYING_TITLE_SELECTORS:
        try:
            text = " ".join((page.locator(sel).first.inner_text(timeout=2000) or "").split())
        except Exception:
            continue
        if text:
            # Not a tab title - no suffix to strip, so it is its own match text.
            print(f"[music] now playing ({sel}): {text}", flush=True)
            return text, text
    print("[music] could not read the now-playing title", flush=True)
    return "", ""


def _playing_title(page) -> str:
    """The cleaned, display-length track title, or "" when unreadable."""
    return _playing_titles(page)[0]


def _title_similarity(a: str, b: str) -> float:
    """Whole-string closeness of two titles, 0..1. stdlib difflib only.

    Callers pass already-normalised text; the lower/strip here is a safety net
    so a raw string never scores differently just for casing or padding.
    """
    return difflib.SequenceMatcher(None, (a or "").strip().lower(),
                                   (b or "").strip().lower()).ratio()


def _video_about_song(t: str, q: str) -> bool:
    """Does `t` read as a video that merely MENTIONS the song `q`?

    Both args must already be normalised (lowercase, punctuation stripped).

    A real track title is almost entirely song, artist and release metadata, so
    the query plus `_TITLE_NOISE` explains nearly all of its content words. A
    video title carries a pile of words the query never asked for
    ("most insane ... flashmob you will ever see"), and it is rejected even
    though the query appears in it verbatim - which is exactly the presence
    evidence every rule below accepts on.

    Both conditions are required. Junk count alone would reject a long genuine
    title; low coverage alone would reject an artist-only query like
    "metallica" against "Nothing Else Matters - Remastered - Metallica".
    """
    qwords = set(q.split())
    sig = [w for w in t.split()
           if len(w) >= 3 and w not in _TITLE_STOPWORDS and w not in _TITLE_NOISE]
    if not sig:
        return False
    unmatched = [w for w in sig if w not in qwords]
    coverage = (len(sig) - len(unmatched)) / len(sig)
    return len(unmatched) >= _VIDEO_JUNK_WORDS and coverage < _VIDEO_COVERAGE_MIN


def _title_matches(title: str, query: str) -> bool:
    """Best-effort: does `title` name the song `query` asked for? stdlib only.

    `title` is expected to be the FULL title (artist and all), not the cleaned
    display string - the artist segment often carries query tokens.

    Ordered by how much evidence each rule needs, strongest first. The
    edit-distance rule is last and single-token only because near-miss titles
    ("Two Goals" for "Two Ghosts") score ~0.77 against the real thing, so on
    multi-word queries a ratio test says yes to the wrong song.
    """
    def norm(s):
        return " ".join(re.sub(r"[^\w\s]", " ", (s or "").lower()).split())

    t, q = norm(title), norm(query)
    if not t or not q:
        return False
    # Title inside query: the query named the song plus an artist the bare
    # title omits. A subset can never be a video padded out around the song, so
    # this direction runs ahead of the video gate.
    if t in q:
        return True

    # Everything past here accepts on the query being PRESENT in the title, and
    # presence is exactly what a video about the song also has. Gate first.
    if _video_about_song(t, q):
        return False

    # The query names only the song of a "Song (feat. X)" title.
    if q in t:
        return True

    # Nothing meaningful in common: no presence rule below should get a vote.
    if _title_similarity(t, q) < _TITLE_SIMILARITY_MIN:
        return False

    qtokens = q.split()
    sig = [w for w in qtokens if len(w) >= 3 and w not in _TITLE_STOPWORDS]
    if not sig:
        sig = [q]

    # The longest content word is the query's identity - "ghosts", not "two".
    # Present means match; absent just falls through to the weaker rules.
    longest = max(sig, key=len)
    if len(longest) >= 4 and longest in t:
        return True

    hits = sum(1 for w in qtokens if w in t)
    need = (2 * len(qtokens) + 2) // 3  # ceil(2/3 * n)
    if len(qtokens) > 1:
        need = max(need, 2)
    if hits >= need:
        return True

    if len(qtokens) == 1:
        return difflib.SequenceMatcher(None, t, q).ratio() >= 0.75
    return False


def _fallback_open(url: str, why: str) -> str:
    """Last resort: hand the URL to Brave through the OS, and say that we did."""
    try:
        if Path(BRAVE_EXE).exists():
            subprocess.Popen([BRAVE_EXE, url])
        else:
            webbrowser.open(url)
        return (f"I could not drive Brave directly ({why}), so I opened the YouTube Music "
                "search in a normal Brave window instead. Press play there.")
    except Exception as e:
        return f"[Error] Could not start music: {why}; opening Brave also failed: {e}"


def play_music(query: str, navigate_only: bool = False) -> str:
    """Search YouTube Music for `query` in Brave and play the first song result.

    Opens https://music.youtube.com/search?q=<query>, waits for the
    first song result and clicks it. The page handle is kept on the worker so
    stop_music() can stop playback later. Falls back to launching Brave with the
    search URL if Playwright cannot drive Brave.

    Playback is verified, never assumed: after the click the <video> element is
    polled until it is unpaused with an advancing currentTime. If it never
    starts (autoplay blocked, non-playable match, stalled player) this says so
    instead of reporting a success the user cannot hear.

    navigate_only=True loads the results page without clicking - used by the
    smoke test so a verification run does not start blasting audio.
    """
    global _warmed
    url = YOUTUBE_SEARCH.format(urllib.parse.quote_plus(query))
    _emit("On it, sir.")
    _emit(f"Searching YouTube Music for {query}...")

    def job(page):
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(1500)
        _dismiss_consent(page)

        candidates = _collect_candidates(page, timeout=15000)
        if not candidates:
            return (f"[Error] Loaded the YouTube Music results for '{query}' but found no "
                    "song result to click. The page layout likely changed.")
        _emit(f"Found {len(candidates)} results, picking the best match...")
        if navigate_only:
            title = candidates[0]["text"]
            return f"Loaded YouTube Music results for '{query}'. First result: {title[:120]}"

        why = "unknown"
        last_actual, tried = "", 0
        for attempt in range(min(3, len(candidates))):
            if attempt:
                # Previous candidate led somewhere unplayable; reload the
                # results page so the row handles resolve against fresh markup.
                try:
                    page.goto(url, wait_until="domcontentloaded", timeout=60000)
                    page.wait_for_timeout(1500)
                    _dismiss_consent(page)
                    candidates = _collect_candidates(page, timeout=15000) or candidates
                except Exception as e:
                    why = f"could not reload results: {type(e).__name__}"
                    break
                if attempt >= len(candidates):
                    break

            cand = candidates[attempt]
            tried += 1
            _emit(f"Starting playback of {cand['text'][:50]}...")
            try:
                _resolve(page, cand).click(timeout=8000)
            except Exception as e:
                why = f"could not click result: {type(e).__name__}"
                continue
            page.wait_for_timeout(2500)

            # A track click lands on /watch; an album or playlist row does not.
            try:
                current_url = page.url or ""
            except Exception:
                current_url = ""
            has_video = _probe(page).get("video", False)
            if "watch" not in current_url and not has_video:
                why = f"'{cand['text'][:60]}' did not open a player"
                continue

            playing, why = _start_and_verify(page)
            if not playing and _click_play_button(page):
                page.wait_for_timeout(1200)
                playing, why = _start_and_verify(page, wait_ms=6000)
            if playing:
                try:
                    page.bring_to_front()
                except Exception:
                    pass
                # Playing is not the same as playing the right thing: read the
                # live track title and only claim what is actually sounding.
                # Match on the full title, speak the short one: the artist and
                # trailing segments carry query tokens the display string drops.
                actual, match_title = _playing_titles(page)
                if not actual:
                    # Phase 12a: playback IS running but the title could not be
                    # read. Never claim a song name — the old fallback string
                    # ("Playing '<query>'") impersonated a verified result and
                    # once announced the literal words of the voice command.
                    return (f"Playback started for '{query}' on YouTube Music in Brave, "
                            f"but I couldn't read what's playing — please check it's "
                            f"the right song.")
                if _title_matches(match_title, query):
                    return f"Playing '{actual}' on YouTube Music in Brave."
                last_actual = actual
                why = f"'{actual}' does not match '{query}'"
                continue

        if last_actual:
            # Audio IS running, just the wrong track - no _fallback_open, which
            # would open a second window on top of it. Report it honestly and
            # let the user say "next" or name the song.
            return (f"[MatchError] Started '{last_actual}' for '{query}' but it does not "
                    f"match. I tried the first {tried} results.")

        return (f"[Error] Loaded '{query}' on YouTube Music but playback did not start "
                f"({why}). You may need to press play in the Brave window.")

    # Cold start - launching headed Brave against a fresh profile - can eat most
    # of a play_music budget on its own and used to blow the 150s deadline,
    # silently degrading the very first request to the no-playback fallback.
    if not _warmed:
        _worker.call(lambda page: "ready", timeout=240)
        _warmed = True

    out = _worker.call(job, timeout=240)
    if isinstance(out, str) and out.startswith("[Error]"):
        return _fallback_open(url, out[len("[Error]"):].strip())
    # Phase 12a: one retry with navigate_only on a MatchError — the first
    # click landed on a wrong/unverifiable track; reloading the results page
    # and picking fresh often finds the right one. Only once, only when we
    # have not already retried (navigate_only calls never recurse).
    if isinstance(out, str) and out.startswith("[MatchError]") and not navigate_only:
        _emit("That doesn't look like the right song — trying once more...")
        retry = play_music(query, navigate_only=False)
        return f"{retry} (second attempt after a wrong first match)"
    return out


def stop_music() -> str:
    """Stop playback by pausing the video and navigating the tab to about:blank."""
    if not _worker.is_open():
        return "Nothing is playing - the music window is not open."

    def job(page):
        # Pause first: navigating away alone can leave audio running for a beat.
        try:
            page.evaluate("document.querySelectorAll('video').forEach(v => v.pause())")
        except Exception:
            pass
        try:
            page.goto("about:blank", wait_until="domcontentloaded", timeout=15000)
        except Exception:
            pass
        return "Music stopped."

    return _worker.call(job, timeout=60)


def close_music() -> str:
    """Close the Brave music window entirely."""
    global _warmed
    _worker.shutdown()
    # The next play_music launches Brave cold again, so it must be allowed the
    # long warm-up budget rather than assuming a live context.
    _warmed = False
    return "Music browser closed."


if __name__ == "__main__":
    # python music_agent.py  ->  loads a search page without starting playback.
    print("Brave:   ", BRAVE_EXE)
    print("Profile: ", PROFILE_DIR)
    print(play_music("lofi hip hop", navigate_only=True))
    print(stop_music())
