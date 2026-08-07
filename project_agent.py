"""Project launcher for JARVIS - starts a dev server and opens it in Brave.

Same worker shape as music_agent.BraveMusicWorker and for the same reason:
Playwright objects are thread-affine and its sync API refuses to run inside an
asyncio loop, while jarvis_web calls brain.think() straight from the WebSocket
handler. One dedicated thread owns Brave for the life of the process and every
call is marshalled onto it through a queue.

Deliberately a SEPARATE worker and profile from music_agent's and from
browser_agent's: music must not lose its tab because a project was launched, and
the ChatGPT session belongs to Chrome. Each owns its own profile directory so
none of them fight over a profile lock.

Which projects exist is read from projects.json beside this file; a template is
written on first use. Credentials, when a project has them, come from the vault
through tools.get_credentials and are strictly read - nothing here writes to the
vault, and the password is never logged or returned.
"""

import json
import os
import subprocess
import time
import urllib.error
import urllib.request
import webbrowser
import queue
import threading
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
REGISTRY_PATH = Path(os.getenv("JARVIS_PROJECTS_FILE", HERE / "projects.json"))
# Dev servers are long-lived and chatty; their output goes to a file so a voice
# turn is never blocked by a full pipe buffer.
LOG_PATH = HERE / "projects.log"

BRAVE_EXE = os.getenv(
    "JARVIS_BRAVE_EXE",
    r"C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe",
)
PROFILE_DIR = Path(os.getenv("JARVIS_PROJECT_PROFILE",
                             Path.home() / ".jarvis-project-profile"))

# How long to wait for a freshly started dev server to answer on its URL.
STARTUP_TIMEOUT = 60
# Login is best-effort by design: if the page has no form we say so and stop
# rather than sitting on a selector until the voice turn dies.
LOGIN_FIELD_TIMEOUT = 4000

USERNAME_SELECTORS = [
    "input[type=email]",
    "input[name=email]",
    "input[id*=email]",
    "input[name=username]",
    "input[id*=username]",
    "input[name*=user]",
]
PASSWORD_SELECTORS = [
    "input[type=password]",
    "input[name=password]",
    "input[id*=password]",
]
SUBMIT_SELECTORS = [
    "button[type=submit]",
    'button:has-text("Login")',
    'button:has-text("Log in")',
    'button:has-text("Sign in")',
    'button:has-text("Sign In")',
    "input[type=submit]",
]

# Written on first use so the user has something real to edit rather than an
# empty file. Paths that do not exist yet are reported, not guessed at.
DEFAULT_REGISTRY = {
    "my-app": {
        "path": "C:/Users/deped/Documents/Portfolio/my-app",
        "dev_cmd": "npm run dev",
        "url": "http://localhost:5173",
    },
    "lrmis-main": {
        "path": "C:/Users/deped/Documents/lrmis-main",
        "dev_cmd": "",
        "url": "",
    },
    "sticky-brain": {
        "path": "C:/Users/deped/Documents/sticky-brain",
        "dev_cmd": "",
        "url": "",
    },
    "irimsv": {
        "path": "",
        "dev_cmd": "npm run dev",
        "url": "http://localhost:5173",
        "vault_secret": "iRIMS-V",
        "note": "Set path to the live Vite repo once you know where it lives.",
    },
}


class BraveProjectWorker:
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
            return f"[Error] Project browser call timed out after {timeout}s"
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
                viewport={"width": 1400, "height": 900},
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


_worker = BraveProjectWorker()


def _first_visible(page, selectors, timeout=LOGIN_FIELD_TIMEOUT):
    """Return the first selector that actually resolves to a visible element."""
    for sel in selectors:
        try:
            el = page.locator(sel).first
            el.wait_for(state="visible", timeout=max(700, timeout // len(selectors)))
            return el
        except Exception:
            continue
    return None


def load_registry() -> dict:
    """Read projects.json, writing the template first if the file is missing."""
    if not REGISTRY_PATH.exists():
        REGISTRY_PATH.write_text(json.dumps(DEFAULT_REGISTRY, indent=2) + "\n",
                                 encoding="utf-8")
    return json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))


def _resolve(name: str, registry: dict):
    """Match a spoken project name against the registry keys, loosely."""
    needle = name.lower().strip()
    for key in registry:
        if key.lower() == needle:
            return key
    # Spoken names lose hyphens ("sticky brain"), so compare on letters alone.
    flat = "".join(ch for ch in needle if ch.isalnum())
    for key in registry:
        keyflat = "".join(ch for ch in key.lower() if ch.isalnum())
        if keyflat == flat or flat in keyflat or keyflat in flat:
            return key
    return None


def _url_alive(url: str, timeout: float = 2.0) -> bool:
    """True if anything at all answers on the URL. A 404 still means it is up."""
    try:
        urllib.request.urlopen(url, timeout=timeout).close()
        return True
    except urllib.error.HTTPError:
        return True
    except Exception:
        return False


def _wait_for_url(url: str, timeout: int = STARTUP_TIMEOUT) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _url_alive(url):
            return True
        time.sleep(1.0)
    return False


def _start_dev_server(name: str, cmd: str, cwd: Path) -> str:
    """Spawn the dev command detached, with its output appended to projects.log."""
    log = open(LOG_PATH, "a", encoding="utf-8", errors="replace")
    log.write(f"\n=== {name}: {cmd} in {cwd} at {time.strftime('%Y-%m-%d %H:%M:%S')} ===\n")
    log.flush()
    subprocess.Popen(cmd, shell=True, cwd=str(cwd), stdout=log,
                     stderr=subprocess.STDOUT)
    return f"started '{cmd}' (output in {LOG_PATH.name})"


def _open_and_login(url: str, creds) -> str:
    """Open the URL in Brave and, if we have credentials, try a generic login.

    Best-effort throughout: a missing form, a missing field or a missing button
    each end the attempt with a plain report instead of a wait. The password is
    typed into the page and never returned or printed.
    """

    def job(page):
        page.goto(url, wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(1500)
        try:
            page.bring_to_front()
        except Exception:
            pass

        if not creds:
            return f"opened {url} in Brave"

        pw_field = _first_visible(page, PASSWORD_SELECTORS)
        if pw_field is None:
            return (f"opened {url} in Brave but could not auto-login - no password "
                    "field appeared, so it may already be signed in or the login "
                    "page is elsewhere")

        user_field = _first_visible(page, USERNAME_SELECTORS, timeout=2500)
        filled = []
        if user_field is not None and creds.get("username"):
            user_field.fill(creds["username"])
            filled.append("username")
        if creds.get("password"):
            pw_field.fill(creds["password"])
            filled.append("password")
        if not filled:
            return f"opened {url} in Brave but the credential note had nothing to fill in"

        submit = _first_visible(page, SUBMIT_SELECTORS, timeout=2500)
        if submit is None:
            return (f"opened {url} in Brave and filled the {' and '.join(filled)}, "
                    "but found no submit button - press enter there yourself")
        submit.click()
        page.wait_for_timeout(3000)
        return f"opened {url} in Brave and submitted the login as {creds.get('username')}"

    return _worker.call(job, timeout=140)


def _open_plainly(url: str, why: str) -> str:
    """Last resort: hand the URL to Brave through the OS, and say that we did."""
    try:
        if Path(BRAVE_EXE).exists():
            subprocess.Popen([BRAVE_EXE, url])
        else:
            webbrowser.open(url)
        return f"opened {url} in a normal Brave window ({why})"
    except Exception as e:
        return f"could not open {url}: {why}; launching Brave also failed: {e}"


def launch_project(name: str) -> str:
    """Start a project's dev server, wait for it, open it in Brave, maybe log in.

    Everything is wrapped so a broken registry entry, a dead server or a browser
    failure comes back as a sentence rather than taking the turn down with it.
    """
    try:
        registry = load_registry()
    except Exception as e:
        return f"[Error] Could not read {REGISTRY_PATH.name}: {e}"

    try:
        key = _resolve(name, registry)
        if key is None:
            listed = ", ".join(registry) or "none"
            return f"[Error] No project called '{name}'. I know about: {listed}"

        entry = registry[key] or {}
        raw_path = (entry.get("path") or "").strip()
        dev_cmd = (entry.get("dev_cmd") or "").strip()
        url = (entry.get("url") or "").strip()
        secret = (entry.get("vault_secret") or "").strip()

        if not raw_path:
            hint = entry.get("note") or "Add its path to projects.json."
            return f"[Error] {key} has no path set yet. {hint}"
        path = Path(raw_path).expanduser()
        if not path.exists():
            return f"[Error] {key} points at {path}, which does not exist."

        steps = []

        already_up = bool(url) and _url_alive(url)
        if dev_cmd and already_up:
            steps.append(f"{url} was already answering, so I left the server alone")
        elif dev_cmd:
            steps.append(_start_dev_server(key, dev_cmd, path))
            if url:
                if _wait_for_url(url):
                    steps.append(f"{url} came up")
                else:
                    return (f"Started {key} but {url} did not answer within "
                            f"{STARTUP_TIMEOUT} seconds. Check {LOG_PATH.name}.")
        elif not url:
            return (f"Opened nothing for {key}: it has no dev command and no URL "
                    f"in {REGISTRY_PATH.name}. Its folder is {path}.")

        creds = None
        if secret:
            import tools
            got = tools.get_credentials(secret)
            if got.get("ok"):
                creds = got
                steps.append(f"credentials from the vault - {got['summary']}")
            else:
                steps.append(f"no credentials used - {got.get('summary')}")

        if url:
            opened = _open_and_login(url, creds)
            if isinstance(opened, str) and opened.startswith("[Error]"):
                opened = _open_plainly(url, opened[len("[Error]"):].strip())
            steps.append(opened)

        return f"{key}: " + "; ".join(steps) + "."
    except Exception as e:
        return f"[Error] Could not launch '{name}': {type(e).__name__}: {e}"


if __name__ == "__main__":
    # python project_agent.py  ->  shows the registry without launching anything.
    reg = load_registry()
    print("Registry:", REGISTRY_PATH)
    for k, v in reg.items():
        print(f"  {k}: path={v.get('path') or '(unset)'} "
              f"dev_cmd={v.get('dev_cmd') or '(none)'} url={v.get('url') or '(none)'} "
              f"vault_secret={v.get('vault_secret') or '(none)'}")
