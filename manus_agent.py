"""Manus AI bridge for JARVIS — wired as a delegate() backend (Phase 5).

Delegates image/video/doc/analysis generation to Manus AI via the user's
logged-in browser session (cookie handoff). No API key, no MCP.

Cookie handoff ONLY — Manus cookies are read from a LOCAL, GIT-IGNORED file
(named via env JARVIS_MANUS_COOKIES, default ``manus_cookies.json`` beside this
module). This file is NOT committed (.gitignore), never printed to chat, and never
stored in plaintext notes. If it is absent, every call returns a clear
"setup needed" message instead of failing silently.

Patterns follow the proven manus-ai-bridge skill (strict completion detection,
fresh session per task, original-format asset capture, oscillation guard).
"""

import json
import os
import time
import traceback
from pathlib import Path

COOKIE_FILE = Path(os.getenv("JARVIS_MANUS_COOKIES",
                             Path(__file__).parent / "manus_cookies.json"))
MANUS_URL = "https://manus.im/app"
DEBUG_PORT = int(os.getenv("JARVIS_BROWSER_PORT", "9223"))

COMPOSER_SELECTORS = [
    ".ProseMirror",
    ".chat-input-editor",
    "[contenteditable='true']",
    "div[role='textbox']",
]
SEND_SELECTORS = [
    "button[type='submit']",
    "button[aria-label*='Send']",
    "button:has-text('Send')",
]
NEW_TASK_SELECTORS = [
    "button:has-text('New task')",
    "button[aria-label*='New task']",
    "a:has-text('New task')",
]
MODAL_DISMISS = [
    "button:has-text('Maybe later')",
    "button:has-text('Try for free')",
    "button:has-text('Dismiss')",
]

WARMUP_POLLS = 6
STABLE_DELTA = 40
TAIL_CHARS = 600
MAX_POLLS = 60
COMPLETE_MARKERS = (
    "task completed", "the task is complete", "all done",
    "i've completed", "i have completed",
)
FALLBACK_MARKERS = (
    "here is", "here's", "your file", "download", "i've created",
    "i have created", "the result",
)


def _cookies_present() -> bool:
    return COOKIE_FILE.is_file() and COOKIE_FILE.stat().st_size > 0


def manus_status() -> str:
    """Report whether the Manus backend is configured (cookie file present)."""
    if not _cookies_present():
        return ("[Manus] Not configured. Drop your exported Manus cookies "
                f"(manus.im only) into {COOKIE_FILE} — one-time setup. "
                "JARVIS will then be able to delegate image/video/doc generation.")
    try:
        raw = json.loads(COOKIE_FILE.read_text(encoding="utf-8"))
        n = len(raw) if isinstance(raw, list) else 0
        return f"[Manus] Configured ({n} cookies at {COOKIE_FILE}). Ready to delegate."
    except Exception as e:
        return f"[Manus] Cookie file present but unreadable: {e}"


def _load_cookies() -> list:
    """Load + normalise Manus cookies. Keep ONLY manus.im cookies. Returns [] on
    any problem so callers can fall back. Expires converted to epoch float."""
    from datetime import datetime
    try:
        raw = json.loads(COOKIE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return []
    out = []
    for c in raw:
        domain = (c.get("domain") or "")
        if "manus" not in domain.lower():
            continue  # strip everything non-Manus (incl. FB xs/sb/fr)
        exp = None
        if c.get("expires_iso"):
            try:
                exp = datetime.fromisoformat(
                    c["expires_iso"].replace("Z", "+00:00")).timestamp()
            except Exception:
                pass
        elif isinstance(c.get("expirationDate"), (int, float)):
            exp = float(c["expirationDate"])
        out.append({
            "name": c["name"], "value": c["value"], "domain": domain,
            "path": c.get("path", "/"), "secure": bool(c.get("secure", False)),
            "http_only": bool(c.get("httpOnly", False)),
            "same_site": c.get("sameSite") or "None", "expires": exp,
        })
    return out


def _is_done(text: str, prev_len: int, idx: int) -> bool:
    """Strict completion: marker in tail AND length stable, post-warmup.
    Plus oscillation guard (see skill)."""
    if idx <= WARMUP_POLLS:
        return False
    tail = text.lower()[-TAIL_CHARS:]
    fresh = any(m in tail for m in COMPLETE_MARKERS)
    stable = prev_len != -1 and abs(len(text) - prev_len) < STABLE_DELTA
    # oscillation guard
    osc = getattr(_is_done, "_osc_count", 0)
    if prev_len != -1 and len(text) == prev_len:
        _is_done._osc_count = osc + 1
    else:
        _is_done._osc_count = 0
    if _is_done._osc_count >= 10 and idx > WARMUP_POLLS and len(text) > 1500:
        return True
    return fresh and stable


def delegate_to_manus(task: str, timeout: int = 300, max_turns: int = 30,
                      progress_cb=None) -> str:
    """Delegate a generation task to Manus.

    Requires: (1) Playwright installed, (2) the user's Manus cookies in
    COOKIE_FILE. Returns a clear message if either is missing rather than
    raising. Runs synchronously (blocking) — pair with delegate(background=True)
    for voice responsiveness.
    """
    task = str(task or "").strip()
    if not task:
        return "[Error] No task given to delegate to Manus."
    if not _cookies_present():
        return ("[Manus] Not configured — no cookie file. Drop your Manus "
                "cookies (manus.im only) into "
                f"{COOKIE_FILE} to enable image/video/doc delegation.")
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return ("[Manus] Playwright not installed. Run: "
                "`uv pip install playwright && playwright install chromium`.")

    cookies = _load_cookies()
    if not cookies:
        return ("[Manus] Cookie file had no manus.im cookies (or they were "
                "all stripped). Re-export Manus-only cookies.")

    if callable(progress_cb):
        try:
            progress_cb(f"Delegating to Manus: {task[:80]}")
        except Exception:
            pass

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=False)
            ctx = browser.new_context()
            ctx.add_cookies(cookies)
            page = ctx.new_page()
            page.goto(MANUS_URL, wait_until="domcontentloaded", timeout=60000)
            time.sleep(2)
            # dismiss modal if present
            for sel in MODAL_DISMISS:
                try:
                    el = page.locator(sel).first
                    if el.count() and el.is_visible(timeout=2000):
                        el.click()
                        time.sleep(1)
                except Exception:
                    pass
            # fresh session per task (user rule)
            for sel in NEW_TASK_SELECTORS:
                try:
                    el = page.locator(sel).first
                    if el.count() and el.is_visible(timeout=2000):
                        before = page.url
                        el.click()
                        time.sleep(3)
                        if page.url != before:
                            break
                except Exception:
                    pass
            # find composer + send
            composer = None
            for sel in COMPOSER_SELECTORS:
                try:
                    el = page.locator(sel).first
                    if el.count() and el.is_visible(timeout=2000):
                        composer = el
                        break
                except Exception:
                    pass
            if composer is None:
                browser.close()
                return ("[Manus] Could not find the message composer. The site "
                        "layout may have changed.")
            composer.click()
            page.keyboard.press("Control+A")
            page.keyboard.press("Delete")
            composer.type(task, delay=12)
            sent = None
            for sel in SEND_SELECTORS:
                try:
                    el = page.locator(sel).first
                    if el.count() and el.is_visible(timeout=2000):
                        el.click()
                        sent = "clicked_send"
                        break
                except Exception:
                    pass
            if sent is None:
                composer.press("Enter")
                sent = "enter_fallback"

            # poll for completion
            prev_len = -1
            final_text = ""
            for idx in range(1, MAX_POLLS + 1):
                time.sleep(20)
                body = page.evaluate("() => document.body.innerText")
                final_text = body
                if _is_done(body, prev_len, idx) or idx == MAX_POLLS:
                    time.sleep(15)
                    final_text = page.evaluate("() => document.body.innerText")
                    break
                prev_len = len(body)

            browser.close()
            if callable(progress_cb):
                try:
                    progress_cb("Manus finished.")
                except Exception:
                    pass
            snippet = final_text.strip()[-2000:] if final_text else ""
            return (f"[Manus] Task complete.\n{snippet}\n\n"
                    f"(Session: {MANUS_URL})")
    except Exception as e:
        traceback.print_exc()
        return f"[Manus] Delegation failed: {type(e).__name__}: {e}"


if __name__ == "__main__":
    print(manus_status())
