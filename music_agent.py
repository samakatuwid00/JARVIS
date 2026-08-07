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
                traceback.print_exc()
                reply.put((False, f"{type(e).__name__}: {e}"))

    def _ensure_page(self):
        from playwright.sync_api import sync_playwright

        if self._page is not None and not self._page.is_closed():
            return self._page

        if not Path(BRAVE_EXE).exists():
            raise RuntimeError(f"Brave was not found at {BRAVE_EXE}")

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

    def is_open(self) -> bool:
        return self._page is not None and not self._page.is_closed()

    def shutdown(self):
        if self._ctx:
            try:
                self._ctx.close()
            except Exception:
                pass
        if self._pw:
            try:
                self._pw.stop()
            except Exception:
                pass
        self._ctx = self._page = self._pw = None


_worker = BraveMusicWorker()


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

    navigate_only=True loads the results page without clicking - used by the
    smoke test so a verification run does not start blasting audio.
    """
    url = YOUTUBE_SEARCH.format(urllib.parse.quote_plus(query))

    def job(page):
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(1500)
        _dismiss_consent(page)

        result = _first_visible(page, RESULT_SELECTORS, timeout=15000)
        if result is None:
            return (f"[Error] Loaded the YouTube Music results for '{query}' but found no "
                    "song result to click. The page layout likely changed.")
        if navigate_only:
            title = " ".join((result.inner_text() or "").split())
            return f"Loaded YouTube Music results for '{query}'. First result: {title[:120]}"

        result.click()
        page.wait_for_timeout(3000)
        try:
            page.bring_to_front()
        except Exception:
            pass
        return f"Playing '{query}' on YouTube Music in Brave."

    out = _worker.call(job, timeout=150)
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
