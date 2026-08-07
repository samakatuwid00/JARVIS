# Task: Drive the user's daily Chrome (signed-in) instead of the separate profile

**Context:** `browser_agent.py` currently launches its OWN dedicated Chrome profile
(`~/.jarvis-chrome-profile`, port 9222). The user finds that window incognito-like and
refuses to sign in there. They want JARVIS to drive their **real, daily Chrome** (default
profile, already signed into ChatGPT) by opening a **new tab** each time — no separate
profile, no re-login.

The mechanism already exists: `_ensure_page()` FIRST tries `_attach()` (connect_over_cdp
to `http://127.0.0.1:9222`) and only launches the dedicated profile when no debug-port
Chrome is listening. The user's Chrome will be launched with `--remote-debugging-port=9222`
(via a desktop shortcut, created separately), so `_attach()` will win and drive the real
browser. **This task is a small, targeted change to `browser_agent.py` only.**

## Scope — edit ONLY `C:/Users/deped/Documents/jarvis-demo/browser_agent.py`

Do NOT touch: `jarvis_web.py`, `jarvis_visual.html`, `hud_artifact.html`, `tools.py`,
`.env`, `config.py`, `voice_engine.py`, `brain_gemini.py`. This is not a git repo — do NOT
init/commit.

## Changes

1. **`_attach()` — open a NEW tab, never reuse existing tabs.** When attaching over CDP
   to the user's Chrome, replace the `_pick_page(self._ctx)` call with
   `self._ctx.new_page()` and return that page. The user explicitly wants a fresh tab per
   ask; do not grab their current ChatGPT tab or any other open tab.

2. **Keep `_pick_page` for the launched-profile path only.** `_ensure_page()`'s
   `launch_persistent_context` branch must keep using `_pick_page` (a fresh profile has no
   useful tabs; reuse of an existing chatgpt.com tab there is still correct). Do not
   delete `_pick_page`.

3. **Update the module docstring** (top of file, lines 1-11): primary mode is now
   "attach to the user's daily Chrome via debug port 9222, open a new tab, session already
   signed in"; the dedicated profile (`~/.jarvis-chrome-profile`) is the fallback used only
   when no debug-port Chrome is running. Keep the note that JARVIS never handles
   credentials.

4. **Keep all selectors, `ask_chatgpt`, `search_chatgpt_history`,
   `open_chatgpt_conversation`, `browser_status`, `open_for_login`, `close_browser`
   behavior identical** except where the new-tab change touches them. `open_for_login`
   will now naturally open a new tab in the user's Chrome and navigate to chatgpt.com.

5. **Error messages:** the "[Error] Not signed in..." messages still apply to the
   fallback-profile case; keep them. No message rewording needed elsewhere.

## Verification (must run, paste results)

1. `python -m py_compile browser_agent.py` — must pass.
2. `python -c "import browser_agent; print('import ok')"` — must print `import ok` (this
   opens no browser; the worker thread only starts on first call).
3. Grep the file to confirm `_attach` uses `new_page()` and `_pick_page` still exists.
4. Full live attach test is NOT possible here (needs the user's Chrome running with the
   debug port) — note that limitation in your final summary; do not fake a browser test.

## Pitfalls

- `ctx.new_page()` must be called on the attached context (`self._ctx`), which is
  `browser.contexts[0]` for a normal Chrome — that already works, keep it.
- Playwright's sync API is thread-affine; all of this stays inside the `_BrowserWorker`
  thread. Do not restructure the threading.
- Do not change `DEBUG_PORT`/`PROFILE_DIR` defaults.
