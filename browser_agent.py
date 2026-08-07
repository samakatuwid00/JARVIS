"""Browser automation for JARVIS - drives chatgpt.com in your everyday Chrome.

Playwright objects are thread-affine and its sync API refuses to run inside an
asyncio loop, but jarvis_web calls brain.think() straight from the WebSocket
handler. So every browser call is marshalled onto one dedicated worker thread
that owns the browser for the life of the process.

Primary mode: attach over CDP to your daily Chrome on debug port 9222 (start it
from the JARVIS shortcut, which adds --remote-debugging-port) and open a NEW tab
for each session. That Chrome is already signed in to ChatGPT, so there is
nothing to log in to and your open tabs are left alone.

Fallback, only when no debug-port Chrome is listening: a dedicated persistent
profile at ~/.jarvis-chrome-profile, launched headed, where you sign in by hand
once and the session survives restarts. JARVIS never handles your credentials.
"""

import os
import queue
import threading
import traceback
from pathlib import Path

# Dedicated profile: the default Chrome profile is locked while Chrome runs,
# and pointing at it would let JARVIS act as your everyday logged-in browser.
PROFILE_DIR = Path(os.getenv("JARVIS_BROWSER_PROFILE",
                             Path.home() / ".jarvis-chrome-profile"))
CHATGPT_URL = "https://chatgpt.com/"
# Shared debug port so a second Jarvis process attaches instead of deadlocking
# on the profile directory lock. Chrome 151 on this machine silently refuses to
# bind 9222 (works on 9223); the launcher shortcut uses 9223 too.
DEBUG_PORT = int(os.getenv("JARVIS_BROWSER_PORT", "9223"))

# chatgpt.com markup moves around; try each in order and report if all miss.
COMPOSER_SELECTORS = [
    "#prompt-textarea",
    "div[contenteditable='true'][data-virtualkeyboard='true']",
    "div.ProseMirror[contenteditable='true']",
    "textarea[data-testid='prompt-textarea']",
]
SEND_SELECTORS = [
    "button[data-testid='send-button']",
    "button[aria-label*='Send']",
    "button#composer-submit-button",
]
STOP_SELECTORS = [
    "button[data-testid='stop-button']",
    "button[aria-label*='Stop']",
]
ASSISTANT_TURN = "[data-message-author-role='assistant']"
LOGIN_MARKERS = ["button[data-testid='login-button']", "a[href*='/auth/login']"]
# Conversation history: the sidebar links are the stable surface. Each one points
# at /c/<id> and its inner_text is the clean conversation title. The in-app search
# box was tried and abandoned - it does not reliably filter the sidebar, so the
# titles are read straight off these links and filtered in Python instead.
CONVO_LINK = "a[href*='/c/']"


class _BrowserWorker:
    """Single thread owning the Playwright context; all calls funnel through it."""

    def __init__(self):
        self._jobs = queue.Queue()
        self._thread = None
        self._lock = threading.Lock()
        self._pw = None
        self._ctx = None
        self._page = None
        self._browser = None
        self._owns = False

    def call(self, fn, timeout=300):
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
            return f"[Error] Browser call timed out after {timeout}s"
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

        if self._pw is None:
            self._pw = sync_playwright().start()

        # A Chrome profile can only be owned by one process. The test scripts and
        # the jarvis_web server are separate processes, so whoever launches first
        # exposes a debug port and everyone else attaches to it instead of
        # fighting over the lock.
        page = self._attach()
        if page is not None:
            return page

        PROFILE_DIR.mkdir(parents=True, exist_ok=True)
        try:
            self._ctx = self._pw.chromium.launch_persistent_context(
                user_data_dir=str(PROFILE_DIR),
                headless=False,
                channel="chrome" if _has_chrome() else None,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    f"--remote-debugging-port={DEBUG_PORT}",
                ],
                viewport={"width": 1280, "height": 900},
            )
            self._owns = True
        except Exception as e:
            # Lost a launch race, or a stale Chrome still holds the profile.
            if "already in use" not in str(e) and "existing browser session" not in str(e):
                raise
            page = self._attach()
            if page is None:
                raise RuntimeError(
                    f"The profile at {PROFILE_DIR} is held by a Chrome that is not "
                    f"exposing debug port {DEBUG_PORT}. Close that Chrome window and "
                    "try again."
                ) from e
            return page

        self._page = _pick_page(self._ctx)
        return self._page

    def _attach(self):
        """Attach to an already-running Chrome on the debug port, if one is listening.

        This is normally your daily Chrome. Always open a fresh tab rather than
        reusing one: the tabs in that window are yours, and hijacking the ChatGPT
        tab you are reading would be rude and destructive.
        """
        try:
            browser = self._pw.chromium.connect_over_cdp(
                f"http://127.0.0.1:{DEBUG_PORT}", timeout=3000
            )
        except Exception:
            return None
        self._browser = browser
        self._owns = False
        self._ctx = browser.contexts[0] if browser.contexts else browser.new_context()
        self._page = self._ctx.new_page()
        return self._page

    def shutdown(self):
        # Only tear down a browser this process actually launched; attached
        # sessions belong to someone else.
        if self._ctx and self._owns:
            try:
                self._ctx.close()
            except Exception:
                pass
        if self._browser and not self._owns:
            try:
                self._browser.close()
            except Exception:
                pass
        if self._pw:
            try:
                self._pw.stop()
            except Exception:
                pass
        self._ctx = self._page = self._pw = self._browser = None
        self._owns = False


def _pick_page(ctx):
    """Reuse the tab already on ChatGPT rather than opening another one.

    Only for the launched fallback profile: that window is JARVIS's own, so if a
    tab there is already signed in, that is the tab to drive - taking pages[0]
    blindly would land on some unrelated tab and look like a signed-out browser.
    The attach path deliberately does not use this; it opens a new tab instead.
    """
    pages = [p for p in ctx.pages if not p.is_closed()]
    for p in pages:
        try:
            if "chatgpt.com" in p.url:
                return p
        except Exception:
            continue
    for p in pages:
        try:
            if p.url in ("about:blank", "") or p.url.startswith("chrome://newtab"):
                return p
        except Exception:
            continue
    return pages[0] if pages else ctx.new_page()


def _has_chrome() -> bool:
    for p in (r"C:\Program Files\Google\Chrome\Application\chrome.exe",
              r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"):
        if Path(p).exists():
            return True
    return False


_worker = _BrowserWorker()


def _first_visible(page, selectors, timeout=8000):
    """Return the first selector that actually resolves to a visible element."""
    deadline = timeout
    for sel in selectors:
        try:
            el = page.locator(sel).first
            el.wait_for(state="visible", timeout=max(1000, deadline // len(selectors)))
            return el
        except Exception:
            continue
    return None


def _logged_out(page) -> bool:
    for sel in LOGIN_MARKERS:
        try:
            if page.locator(sel).first.is_visible(timeout=1500):
                return True
        except Exception:
            continue
    return False


def ask_chatgpt(prompt: str, submit: bool = False, wait_seconds: int = 120) -> str:
    """Type a prompt into chatgpt.com. Only sends when submit is True."""

    def job(page):
        if CHATGPT_URL not in page.url:
            page.goto(CHATGPT_URL, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(1200)

        if _logged_out(page):
            return ("[Error] Not signed in to ChatGPT. A Chrome window is open - "
                    "sign in there once, then ask me again. The session is remembered.")

        composer = _first_visible(page, COMPOSER_SELECTORS)
        if composer is None:
            return ("[Error] Could not find the ChatGPT message box. The site layout "
                    "likely changed, so the selectors need updating.")

        composer.click()
        page.keyboard.press("Control+A")
        page.keyboard.press("Delete")
        composer.type(prompt, delay=12)

        if not submit:
            return (f"Typed into ChatGPT and left it unsent: {prompt}. "
                    "Say the word and I'll send it.")

        before = page.locator(ASSISTANT_TURN).count()

        send = _first_visible(page, SEND_SELECTORS, timeout=4000)
        if send is not None:
            send.click()
        else:
            page.keyboard.press("Enter")

        # Reply is complete once a new assistant turn exists and streaming stopped.
        deadline = wait_seconds * 1000
        step = 500
        waited = 0
        settled = False
        while waited < deadline:
            page.wait_for_timeout(step)
            waited += step
            if page.locator(ASSISTANT_TURN).count() <= before:
                continue
            streaming = False
            for sel in STOP_SELECTORS:
                try:
                    if page.locator(sel).first.is_visible(timeout=250):
                        streaming = True
                        break
                except Exception:
                    continue
            if not streaming:
                settled = True
                break

        turns = page.locator(ASSISTANT_TURN)
        if turns.count() <= before:
            return "[Error] Sent the prompt but ChatGPT produced no reply in time."

        answer = (turns.last.inner_text() or "").strip()
        if not settled:
            answer += " ... (still writing when I stopped listening)"
        return answer or "[Error] ChatGPT replied but the text came back empty."

    return _worker.call(job, timeout=wait_seconds + 90)


def _goto_chatgpt(page):
    if "chatgpt.com" not in page.url:
        page.goto(CHATGPT_URL, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(1200)


def search_chatgpt_history(query: str, limit: int = 10) -> str:
    """Search past ChatGPT conversation titles read off the sidebar links.

    Deliberately does not touch the site's own search box: it does not reliably
    filter the sidebar, so results came back flaky. Reading the loaded sidebar
    titles and filtering them here is boring and repeatable.
    """

    def job(page):
        _goto_chatgpt(page)
        if _logged_out(page):
            return "[Error] Not signed in to ChatGPT. Sign in to the open Chrome window first."

        titles = []
        for link in page.locator(CONVO_LINK).all():
            try:
                title = " ".join((link.inner_text() or "").split())
            except Exception:
                continue
            if title and title not in titles:
                titles.append(title)

        needle = query.lower()
        hits = [t for t in titles if needle in t.lower()][:limit]

        if not hits:
            return f"No past ChatGPT conversations matched '{query}'."
        listed = "; ".join(f"{i}. {t[:110]}" for i, t in enumerate(hits, 1))
        return f"Found {len(hits)} conversation(s) matching '{query}': {listed}"

    return _worker.call(job, timeout=150)


def open_chatgpt_conversation(title_contains: str, max_chars: int = 3000) -> str:
    """Open a past conversation from the sidebar and read its contents back."""

    def job(page):
        _goto_chatgpt(page)
        if _logged_out(page):
            return "[Error] Not signed in to ChatGPT. Sign in to the open Chrome window first."

        needle = " ".join(title_contains.split()).lower()
        target = None

        # The sidebar link's own text is the conversation title, so match on it
        # and click the link itself - no pin buttons, nothing that writes to the
        # account.
        for link in page.locator(CONVO_LINK).all():
            try:
                text = " ".join((link.inner_text() or "").split())
            except Exception:
                continue
            if text and needle in text.lower():
                target = link
                break

        # Older layout, and any row whose link text has not rendered yet.
        if target is None:
            links = page.locator("nav a[href*='/c/']")
            for i in range(min(links.count(), 60)):
                try:
                    text = (links.nth(i).inner_text() or "").strip()
                except Exception:
                    continue
                if needle in text.lower():
                    target = links.nth(i)
                    break
        if target is None:
            return (f"[Error] No conversation in the sidebar matches '{title_contains}'. "
                    "Try search_chatgpt_history first, or scroll the sidebar.")

        target.click()
        page.wait_for_timeout(2500)

        turns = page.locator("[data-message-author-role]")
        count = turns.count()
        if count == 0:
            return "[Error] Opened the conversation but no messages were readable."

        parts, total = [], 0
        for i in range(count):
            node = turns.nth(i)
            try:
                role = node.get_attribute("data-message-author-role") or "?"
                body = " ".join((node.inner_text() or "").split())
            except Exception:
                continue
            if not body:
                continue
            chunk = f"{role}: {body}"
            if total + len(chunk) > max_chars:
                parts.append("... (conversation truncated)")
                break
            parts.append(chunk)
            total += len(chunk)
        return f"Conversation at {page.url} has {count} messages. " + " | ".join(parts)

    return _worker.call(job, timeout=180)


def browser_status() -> str:
    """Report whether the automation browser is up and signed in."""

    def job(page):
        # The signed-in check only means anything on chatgpt.com itself - on a blank
        # tab the login markers are simply absent, which is not the same as signed in.
        if "chatgpt.com" not in page.url:
            return (f"Browser is open at {page.url}, not on ChatGPT, "
                    "so I cannot tell yet whether the session is signed in.")
        return f"Browser open at {page.url}. Signed in: {not _logged_out(page)}"

    return _worker.call(job, timeout=60)


def is_signed_in() -> bool:
    """True if the automation browser is signed in to ChatGPT (navigates lazily if needed)."""

    def job(page):
        if "chatgpt.com" not in page.url:
            page.goto(CHATGPT_URL, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(1200)
        return not _logged_out(page)

    return _worker.call(job, timeout=60)


def close_browser() -> str:
    _worker.shutdown()
    return "Browser closed."


def open_for_login() -> str:
    """Open the JARVIS browser on ChatGPT so you can sign in to it once."""

    def job(page):
        _goto_chatgpt(page)
        page.wait_for_timeout(1500)
        page.bring_to_front()
        return ("signed in already" if not _logged_out(page)
                else "waiting for you to sign in")

    return _worker.call(job, timeout=120)


if __name__ == "__main__":
    # python browser_agent.py  ->  opens the window and holds it open for login.
    print(f"Profile:    {PROFILE_DIR}")
    print(f"Debug port: {DEBUG_PORT}")
    print("Opening ChatGPT in the JARVIS browser...")
    print("Status:", open_for_login())
    print()
    print("Sign in to ChatGPT in that window. The session is saved to the profile")
    print("above and survives restarts, so this is a one-time step.")
    print("Leave the window open and JARVIS will drive that same tab.")
    try:
        input("Press Enter here when you are done signing in... ")
    except (EOFError, KeyboardInterrupt):
        pass
    print("Final status:", browser_status())
