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

import os
import queue
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


class BraveMusicWorker:
    """Single thread owning the Brave Playwright context; all calls funnel through it."""

    def __init__(self):
        self._jobs = queue.Queue()
        self._thread = None
        self._lock = threading.Lock()
        self._pw = None
        self._ctx = None
        self._page = None

    def call(self, fn, timeout=180):
        """Run fn(page) on the worker thread and return its result."""
        with self._lock:
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._loop, daemon=True)
                self._thread.start()
        reply = queue.Queue(maxsize=1)
        self._jobs.put((fn, reply))
        try:
            ok, value = reply.get(timeout=timeout)
        except queue.Empty:
            return f"[Error] Music browser call timed out after {timeout}s"
        if not ok:
            return f"[Error] {value}"
        return value

    def _loop(self):
        while True:
            fn, reply = self._jobs.get()
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
            return self._build_page()

    def is_open(self) -> bool:
        return self._page is not None and not self._page.is_closed()

    def shutdown(self):
        self._teardown()


_worker = BraveMusicWorker()
# Set once the Brave context exists, so only the first play_music pays launch cost.
_warmed = False


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

    def job(page):
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(1500)
        _dismiss_consent(page)

        candidates = _collect_candidates(page, timeout=15000)
        if not candidates:
            return (f"[Error] Loaded the YouTube Music results for '{query}' but found no "
                    "song result to click. The page layout likely changed.")
        if navigate_only:
            title = candidates[0]["text"]
            return f"Loaded YouTube Music results for '{query}'. First result: {title[:120]}"

        why = "unknown"
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
                return f"Playing '{query}' on YouTube Music in Brave."

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
    _worker.shutdown()
    return "Music browser closed."


if __name__ == "__main__":
    # python music_agent.py  ->  loads a search page without starting playback.
    print("Brave:   ", BRAVE_EXE)
    print("Profile: ", PROFILE_DIR)
    print(play_music("lofi hip hop", navigate_only=True))
    print(stop_music())
