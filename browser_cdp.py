"""browser_cdp.py - drive the user's own Chromium browser (Brave, Chrome, Edge)
through its local debugging port, for rule steps that must click inside a
page (e.g. press play).

The user chose this on 2026-09-11: JARVIS starts the browser with a debugging
port that Chromium binds to 127.0.0.1, so programs on this PC can control the
browser but other devices cannot. A browser already running without the port
cannot be attached to; restarting it is the caller's decision (rules_steps
asks the user first). Firefox speaks a different protocol and is not covered.
"""

import json
import os
import re
import subprocess
import time
import urllib.parse
import urllib.request

import rules_engine

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# One fixed port per browser, away from 9222/9223 (JARVIS debug Chrome).
PORTS = {"brave": 9331, "chrome": 9332, "msedge": 9333}
_LABELS = {"brave": "Brave", "chrome": "Chrome", "msedge": "Edge"}
_EXE_NAMES = {"brave": "brave.exe", "chrome": "chrome.exe", "msedge": "msedge.exe"}
_ATTACH_WAIT = 12      # seconds for a launched browser to open its port
_CLOSE_WAIT = 10       # seconds for a graceful close before relaunch
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# Finds what to click: the main player (largest visible video/iframe, not a
# captcha or trailer) for play-like targets, else the visible button/link
# whose text, aria-label or title holds every word of the target.
_LOCATE_JS = r"""
(target) => {
  const t = (target || '').toLowerCase();
  const visible = el => {
    const r = el.getBoundingClientRect(), s = getComputedStyle(el);
    return r.width > 30 && r.height > 20 && s.visibility !== 'hidden' && s.display !== 'none';
  };
  const describe = el => ((el.getAttribute('aria-label') || '') + ' ' + (el.title || '') + ' ' +
                          (el.innerText || '') + ' ' + (el.value || '')).toLowerCase();
  let el = null;
  if (/play|player|video|watch|movie|stream/.test(t)) {
    const skip = /captcha|trailer|\bads?\b|doubleclick|googlesyndication/i;
    const media = [...document.querySelectorAll('video, iframe')].filter(visible)
      .filter(e => !skip.test((e.id || '') + ' ' + (e.title || '') + ' ' + (e.src || '')));
    const area = e => { const r = e.getBoundingClientRect(); return r.width * r.height; };
    media.sort((a, b) => area(b) - area(a));
    el = media[0] || null;
  }
  if (!el) {
    const words = t.split(/\s+/).filter(w => w.length > 2);
    const cands = [...document.querySelectorAll(
      'button, a, [role=button], input[type=submit], input[type=button], [aria-label], [title]')].filter(visible);
    el = cands.find(e => words.length && words.every(w => describe(e).includes(w))) || null;
  }
  if (!el) return null;
  el.scrollIntoView({block: 'center', inline: 'center'});
  const r = el.getBoundingClientRect();
  return {x: r.left + r.width / 2, y: r.top + r.height / 2, tag: el.tagName.toLowerCase(),
          src: el.src || '',
          label: (el.getAttribute('aria-label') || el.title || el.innerText || el.src || '').slice(0, 80)};
}
"""
# Inside a player frame: start playback through the player's own API. Needs
# userGesture, which a click from outside the frame did not always grant
# (JW Player on hollymoviehd.cc ignored two center clicks, 2026-09-11).
_PLAY_JS = r"""
(async () => {
  try {
    if (window.jwplayer && jwplayer().play) { jwplayer().play(); return 'jwplayer'; }
    if (window.videojs && videojs.getPlayers) {
      const p = Object.values(videojs.getPlayers())[0];
      if (p) { await p.play(); return 'videojs'; }
    }
    const v = [...document.querySelectorAll('video')]
      .sort((a, b) => b.clientWidth * b.clientHeight - a.clientWidth * a.clientHeight)[0];
    if (v) { await v.play(); return 'video'; }
  } catch (e) { return 'error: ' + e.message; }
  return '';
})()
"""
_PLAYING_JS = "[...document.querySelectorAll('video')].some(v => !v.paused && v.currentTime > 0)"
_PLAY_TARGET_RE = re.compile(r"play|watch|stream", re.I)
_PLAY_WAIT = 6  # seconds to see the video actually running


def supported(browser):
    return browser in PORTS


def label(browser):
    return _LABELS.get(browser, browser or "the browser")


def _exe(browser):
    try:
        with open(os.path.join(BASE_DIR, "app_registry.json"), encoding="utf-8") as f:
            exe = (json.load(f).get("apps", {}).get(browser) or {}).get("bin")
        return exe if exe and os.path.isfile(exe) else None
    except Exception:
        return None


def _http(browser, path, method="GET", timeout=2):
    req = urllib.request.Request(f"http://127.0.0.1:{PORTS[browser]}{path}", method=method)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8") or "null")


def is_attached(browser):
    """Is the browser running with JARVIS's debugging port open?"""
    if not supported(browser):
        return False
    try:
        return bool(_http(browser, "/json/version", timeout=1))
    except Exception:
        return False


def is_running(browser):
    """Is any instance of the browser running (with or without the port)?"""
    name = _EXE_NAMES.get(browser)
    if not name:
        return False
    out = subprocess.run(["tasklist", "/FI", f"IMAGENAME eq {name}", "/NH"],
                         capture_output=True, text=True, creationflags=_NO_WINDOW).stdout
    return name.lower() in out.lower()


def launch(browser, url=None):
    """Start the browser with the debugging port. True once the port answers."""
    exe = _exe(browser)
    if not exe or not supported(browser):
        return False
    target = url if rules_engine.is_web_url(url) else "about:blank"
    subprocess.Popen([exe, f"--remote-debugging-port={PORTS[browser]}", target],
                     close_fds=True, creationflags=getattr(subprocess, "DETACHED_PROCESS", 0))
    deadline = time.time() + _ATTACH_WAIT
    while time.time() < deadline:
        if is_attached(browser):
            return True
        time.sleep(0.4)
    return False


def restart(browser, url=None):
    """Close the browser gracefully (it restores tabs if the user enabled
    'continue where you left off') and start it again with the port."""
    name = _EXE_NAMES.get(browser)
    if name:
        subprocess.run(["taskkill", "/IM", name], capture_output=True, creationflags=_NO_WINDOW)
        deadline = time.time() + _CLOSE_WAIT
        while time.time() < deadline and is_running(browser):
            time.sleep(0.4)
    return launch(browser, url)


def open_tab(browser, url):
    """Open url in a new tab of an attached browser. Returns the target or None."""
    if not rules_engine.is_web_url(url):
        return None
    try:
        return _http(browser, "/json/new?" + urllib.parse.quote(url, safe=":/?&=%#+,;@"),
                     method="PUT", timeout=5)
    except Exception:
        return None


def _page_target(browser, url_hint=None):
    """The tab to act on: one whose url contains the hint, else the first page."""
    try:
        pages = [t for t in _http(browser, "/json/list") if t.get("type") == "page"]
    except Exception:
        return None
    if url_hint:
        host = rules_engine.bare_host(url_hint)
        for t in pages:
            if url_hint.rstrip("/") in t.get("url", "") or \
                    (host and rules_engine.same_site(t.get("url", ""), host)):
                return t
    return pages[0] if pages else None


class _Page:
    """Minimal CDP session on one tab (request/response by id, events ignored)."""

    def __init__(self, ws_url):
        from websockets.sync.client import connect
        self._ws = connect(ws_url, max_size=None, open_timeout=5)
        self._id = 0

    def send(self, method, params=None, timeout=10):
        self._id += 1
        mid = self._id
        self._ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
        deadline = time.time() + timeout
        while time.time() < deadline:
            msg = json.loads(self._ws.recv(timeout=max(0.1, deadline - time.time())))
            if msg.get("id") == mid:
                return msg.get("result") or {}
        raise TimeoutError(method)

    def evaluate(self, expression, gesture=False):
        res = self.send("Runtime.evaluate", {"expression": expression, "returnByValue": True,
                                             "awaitPromise": True, "userGesture": gesture})
        return (res.get("result") or {}).get("value")

    def click_at(self, x, y):
        for kind in ("mouseMoved", "mousePressed", "mouseReleased"):
            self.send("Input.dispatchMouseEvent", {"type": kind, "x": x, "y": y,
                                                   "button": "left", "clickCount": 1})

    def close(self):
        try:
            self._ws.close()
        except Exception:
            pass


def click(browser, target, url_hint=None, settle=3.0, tries=4):
    """Click `target` ("play", "Server 2", "Accept") in the tab showing
    url_hint. Waits for the page to settle; retries while it is still loading."""
    tab = _page_target(browser, url_hint)
    if not tab or not tab.get("webSocketDebuggerUrl"):
        return f"[Error] I couldn't find the page in {label(browser)}."
    page = _Page(tab["webSocketDebuggerUrl"])
    try:
        page.send("Page.bringToFront")
        time.sleep(settle)
        spot = None
        for _ in range(tries):
            spot = page.evaluate(f"({_LOCATE_JS})({json.dumps(target)})")
            if spot:
                break
            time.sleep(1.5)
        if not spot:
            return f"[Error] I couldn't find \"{target}\" on the page."
        time.sleep(0.4)  # let scrollIntoView finish before clicking its center
        page.click_at(spot["x"], spot["y"])
        if spot["tag"] in ("iframe", "video") and _PLAY_TARGET_RE.search(target or ""):
            return _confirm_playing(browser, tab, spot)
        what = "the player" if spot["tag"] in ("iframe", "video") else f"\"{spot['label'] or target}\""
        return f"Clicked {what} in {label(browser)}."
    except Exception as e:
        return f"[Error] Clicking in {label(browser)} failed: {type(e).__name__}"
    finally:
        page.close()


def _media_sessions(browser, tab, spot):
    """Debugger urls where the video lives: the tab itself, plus the player
    frame (a separate target when it is on another site)."""
    urls = [tab["webSocketDebuggerUrl"]]
    host = rules_engine.bare_host(spot.get("src") or "")
    if spot["tag"] == "iframe" and host:
        for t in _http(browser, "/json/list"):
            if t.get("type") == "iframe" and rules_engine.same_site(t.get("url", ""), host) \
                    and t.get("webSocketDebuggerUrl"):
                urls.append(t["webSocketDebuggerUrl"])
    return urls


def _on_each(urls, expression, gesture=False):
    """Evaluate in every session; the first truthy value, else None."""
    for ws in urls:
        page = _Page(ws)
        try:
            value = page.evaluate(expression, gesture=gesture)
            if value:
                return value
        except Exception:
            continue
        finally:
            page.close()
    return None


def _playing_within(urls, seconds):
    deadline = time.time() + seconds
    while time.time() < deadline:
        if _on_each(urls, _PLAYING_JS):
            return True
        time.sleep(0.8)
    return False


def _confirm_playing(browser, tab, spot):
    """After the click: is a video really running? If not, start it through
    the player's own API inside its frame, and check again."""
    urls = _media_sessions(browser, tab, spot)
    if not _playing_within(urls, _PLAY_WAIT / 2):
        _on_each(urls[1:] or urls, _PLAY_JS, gesture=True)
        if not _playing_within(urls, _PLAY_WAIT):
            return f"[Error] I pressed play in {label(browser)}, but the video didn't start."
    return f"Pressed play in {label(browser)}. It's playing."
