# ELECTRON_PLAN.md — JARVIS as an installed desktop app with a tray icon

Status: planned, not started (2026-09-12). Discussed in chat on 2026-09-12;
this file is the first written version.

## Goal

JARVIS starts at login, lives in the system tray, and can be called from
anywhere with a global hotkey or the wake word, without opening a browser.
It installs like a normal Windows program (Start menu, desktop shortcut,
uninstaller). It only has to run on this PC.

## Not in scope

- Running on other PCs (packaging option B below). Needs the backend
  bundled with PyInstaller, data moved out of the repo folder and the
  hard-coded paths fixed. Later, if ever.
- Controlling the phone itself (parked; see PHONE_PLAN.md "Not in scope").
- Rewriting the HUD. Electron loads the existing page from the backend.

## Shape

- **Electron main process**: tray, global hotkey, one window, starting and
  stopping the backend. Lives in a new `desktop/` folder; nothing else in
  the repo moves.
- **Backend**: the existing `jarvis_web.py`, started by Electron from a
  dedicated venv, still on `127.0.0.1:8001`.
- **Window**: loads `http://127.0.0.1:8001/`, hidden to the tray when
  closed, shown by the hotkey.

Packaging option **A** (this plan): the installed app runs the backend
from the repo's venv. Code edits take effect on the next restart; a broken
edit also breaks the app, so commit before restarting.

## Already done (from the phone work)

- Server binds `127.0.0.1` by default (`JARVIS_HOST`), commit `980eeb2`.
- Host allowlist on every HTTP request and `/ws`, plus the `/ws` Origin
  check. A DNS-rebinding page or any other website can no longer drive the
  tools (`980eeb2`).
- `/ws` tells local clients from remote ones (`WS_REMOTE`); the server-side
  wake word only opens a capture on local screens. The Electron window
  connects to `127.0.0.1`, so it counts as local.
- `requirements.txt` lists `fastapi` and `uvicorn` (`a56dff6`).
- Windows Firewall: Python's inbound Allow rules on Public networks are
  disabled.
- The HUD already has a "no browser wake word" path (phones): skip
  `startWakeListener()`, capture through the same Whisper command path.

Machine: Node 26.3 and npm 11.19 installed; Python 3.14.6 (default),
3.13 and uv's 3.11.15 available; 166 GB free.

## Caveats that shape the design

1. **Browser wake word does not work in Electron.** `webkitSpeechRecognition`
   exists but fails with `network` (Google's key ships only with Chrome).
   The HUD treats `network` as harmless and restarts immediately
   (`jarvis_hud_v3.html`, `rec.onerror` / `rec.onend`), which is a silent
   restart loop. The server's Whisper `WakeEngine` becomes the wake word.
2. **Live transcript hint goes away.** In Chrome the HUD sends the browser's
   live text as a `hint` with each clip and the server keeps the better of
   hint and Whisper. In Electron it is Whisper alone; expect accuracy to
   shift somewhat (not measured).
3. **Wake word costs CPU all day.** Whisper `tiny.en` on 2 threads, next to
   Ollama on a CPU-only machine. Measure in phase 1.
4. **Two local HUDs double up.** Server wakes go to every local client. An
   Electron window plus a Chrome tab on `127.0.0.1` would both open a
   capture. Phase 1 makes the desktop window the only wake target when it
   is connected.
5. **Leftover backend.** The backend starts its own children (Playwright,
   Hermes warm session, CLI agents). If Electron dies, `jarvis_web.py` can
   keep port 8001 and the mic. Kill the whole tree on quit; on start, kill
   a stale backend recorded in a pid file.
6. **Port.** 8001 comes from `.env` because the Honcho memory stack owns
   8000. If 8001 is already served by a JARVIS started by hand, attach to
   it instead of starting a second one; if something else holds it, say so
   in the tray.
7. **`watch_and_restart.py` must not run alongside the app.** It kills and
   restarts `jarvis_web.py` on file changes, fighting Electron over the
   process and the port.
8. **Startup is slow.** Whisper loads at import and the port only opens once
   everything is loaded, so "GET / answers" means ready. Show "Starting…" in
   the tray until then.
9. **Sleep, resume, device changes.** The Python mic stream and the page's
   `getUserMedia` can die or stick to an old device. Recover on
   `powerMonitor` resume; offer "Restart backend" in the tray.
10. **Hotkey conflicts.** Ctrl+Space and Alt+Space are often taken (IME,
    PowerToys Run). `globalShortcut.register` returns false quietly when a
    combo is taken; check it and tell the user.
11. **Microphone always on** from login: the Windows mic indicator stays lit;
    "Let desktop apps access your microphone" must be on.
12. **Unsigned installer.** SmartScreen shows "Windows protected your PC"
    (More info, Run anyway). An app that auto-starts, registers hotkeys and
    keeps the mic open can trip antivirus heuristics.
13. **Size.** Electron adds roughly 100–200 MB RAM and an 80–100 MB
    installer on top of Python, Whisper and Ollama (estimates).
14. **Electron hardening.** `contextIsolation: true`, `nodeIntegration:
    false`, `sandbox: true`; allow only the `media` permission for
    `http://127.0.0.1:8001`; block in-window navigation elsewhere and open
    external links in the default browser.
15. **Phone keeps working.** `tailscale serve` proxies to `127.0.0.1:8001`
    whoever started the backend, so the desktop app makes the phone more
    reliable (backend up from login).

## Phase 0 — Environment

- [ ] Create `.venv` on Python 3.14 (`py -3.14 -m venv .venv`) and install
      `requirements.txt`. JARVIS already runs on the global 3.14.6
      (faster-whisper loads, 246 tests pass), so the version is proven; the
      venv is for isolation, so the app's packages cannot be broken by other
      projects' `pip install`s.
- [ ] Run `pytest` and `jarvis_web.py` from the venv. Anything the global
      install had but `requirements.txt` misses shows up here (fastapi and
      uvicorn were the first two).

Done when: `.venv\Scripts\python jarvis_web.py` serves the HUD on 8001 and
the test suite passes inside the venv.

## Phase 1 — HUD knows it is inside the desktop app

- [ ] `IS_DESKTOP` in the HUD (Electron's user agent contains `Electron/`):
      skip `startWakeListener()` like phones do; the server `WakeEngine` is
      the wake word.
- [ ] HUD announces itself on connect (`{"type": "hello", "client":
      "desktop"}`); while a desktop client is connected, server wakes go
      only to it (caveat 4).
- [ ] Preload bridge `window.jarvisDesktop.onHotkey(fn)`: the hotkey opens a
      command capture exactly like the phone's TALK tap.
- [ ] Drop the "Activate audio" unlock in the desktop app (Electron can
      allow autoplay without a gesture).

Done when: in Electron there is no `[wake]` restart loop in the console;
saying "jarvis" at the PC opens a capture through the server wake; with a
Chrome tab also open, only the desktop window reacts.

## Phase 2 — Electron shell (`desktop/`)

- [ ] `package.json`, `main.js`, `preload.js`; Electron pinned.
- [ ] Single instance (`app.requestSingleInstanceLock()`); a second launch
      focuses the first.
- [ ] Backend supervisor: spawn `.venv\Scripts\python.exe jarvis_web.py`
      with the repo as cwd; stdout/stderr to `logs/desktop-backend.log`;
      pid file; wait for `GET /` (timeout ~180 s); attach instead of spawn
      when 8001 already serves JARVIS (caveat 6).
- [ ] Window: loads the HUD, closes to the tray, `backgroundThrottling:
      false`, hardening from caveat 14.
- [ ] Tray: Show, Restart backend, Open logs, Quit. Tooltip shows
      Starting / Ready / Backend stopped.
- [ ] Global hotkey (see decisions) shows the window and opens a capture;
      failure to register is reported.
- [ ] Quit kills the backend tree (`taskkill /PID <pid> /T /F`).

Done when: tray icon appears; hotkey shows the window and opens a capture;
Quit leaves nothing listening on 8001; killing Electron from Task Manager
and relaunching recovers (stale backend cleaned up).

## Phase 3 — Staying healthy

- [ ] `powerMonitor` resume: reload the window; restart the backend if the
      mic stream is dead.
- [ ] Startup checks surfaced in the tray: Ollama (`:11434`), Chrome
      debugging port (`:9223`), mic permission.
- [ ] Note in README: do not run `watch_and_restart.py` with the app.

Done when: sleep and wake the laptop; JARVIS answers the wake word
afterwards without a manual restart.

## Phase 4 — Installer

- [ ] `electron-builder` NSIS, per-user install (`%LOCALAPPDATA%\Programs`),
      Start menu entry, desktop shortcut, uninstaller.
- [ ] Launch at login (`app.setLoginItemSettings`), toggle in the tray.
- [ ] The app finds the repo and venv from a small config file written at
      install time (option A).

Done when: install, reboot, JARVIS is in the tray after login and the phone
can reach it; uninstall removes the app and its login entry.

## Later: option B (other PCs)

Bundle the backend with PyInstaller; move `memory/`, `sessions/`,
`jarvis-profile.md`, `delegate_registry.json` to `%APPDATA%\JARVIS`; fix the
hard-coded `C:\Users\deped` paths (8 in `tools.py`, 3 in
`project_agent.py`); download the 338 MB of Whisper models on first run.

## Decisions needed

1. Hotkey. Suggest Ctrl+Alt+J (Ctrl+Space / Alt+Space are often taken).
2. Window style: normal window, or a frameless always-on-top HUD panel?
3. At login: tray only (window hidden until the hotkey or wake word), or
   show the window?
4. Chrome tab alongside the app: allowed (desktop window wins wakes,
   phase 1), or discouraged?

## Open questions

- Is `requirements.txt` complete? A clean venv will tell (phase 0).
- Whisper wake word accuracy and CPU as the only wake path (phase 1).
- Does Electron remember the mic permission across launches with the
  permission handler, or prompt each time?
