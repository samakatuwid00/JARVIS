# CYGNUS_UI_PLAN.md — the Cygnus look for the HUD, widget, phone and tray

Status: all eight steps done 2026-09-12 (commits below). Still open: the
wake word (owner to decide), and checks on the real phone and in the real
desktop window by hand. Design:
https://claude.ai/code/artifact/d25f561a-e279-4ef6-ae2f-8e38049b29a8 (version 2).

## Decisions (owner, 2026-09-12)

- The program is called **Cygnus**. The wake word stays **"Jarvis"** until the
  owner decides otherwise (it lives in `wake_engine.py` `WAKE_WORDS` and the
  HUD's `WAKE_WORDS`); on-screen hints keep saying "Jarvis". Python module and
  repo names do not change.
- **Black-hole mark** everywhere: tray icon (a small still cut), the mini orb
  (animated), the top bar, the home-screen icons, and the centre of the full
  HUD, where it **replaces the old animated orb** (`hud_artifact.html`).
- **Widget 560 px tall** (was 720).
- **Phone uses the widget design** (chat bar with mic, no TALK circle).

## Steps (one commit each, checked at 380x560 widget, 400x860 phone, 1400x900 desktop)

1. **Name and mark assets.** Cygnus in the title, top-bar wordmark, input hint,
   manifest, tray tooltip, window title, first-run notification and starting
   page. Black-hole tray icons (16/32) and home-screen icons (192/512).
2. **Black hole replaces the old orb.** The `#hud` iframe stops loading
   `/hud.html` (the element stays so the old overlay code, which already
   tolerates an empty frame, keeps working). A black-hole stage fills the
   centre; the mini orb becomes the animated black hole with state tempos.
3. **Mic inside the chat bar.** Replaces the TALK circle everywhere: tap to
   talk, tap to stop, Send while there is text, available while thinking.
   Ctrl+Alt+J and the wake word unchanged.
4. **Top bar in two groups.** Brand block (mark, wordmark, status) is the only
   window drag handle; Panels and Apps in one group, full HUD and mini orb in
   the window group. Fixes the Apps × that the drag strip swallowed.
5. **Trimmed transcript.** Last exchange only, with "Show all N" / "Show less";
   in the widget the expanded transcript takes the panel.
6. **Panels as tabs** below 1100 px: Tasks, Music, Voice, System, Model, one
   at a time, in a sheet under the top bar.
7. **Apps inside the widget.** Opens under the top bar with back and close;
   in `apps_panel.html` the four action buttons fold into a ⋯ menu when narrow.
8. **Widget height 560** in `desktop/main.js`.

## Done

| Step | Commit | Checked with |
|---|---|---|
| 1 Name and mark assets | `bf7fd0e` | title, wordmark, manifest; desktop app attaches by `id="jv-shell"`, not the title |
| 2 Black hole replaces the old orb | `9c3d6a6` | no `/hud.html` request; centred at 1400x900; mini orb renders alone |
| 3 Mic in the chat bar | `cc417a4` | tap, stop, Send (a typed turn got its reply), hotkey; widget, phone, 1400 |
| 4 Top bar in two groups | `716291d` | drag region only on the brand block; Apps opens and closes in the widget |
| 5 Trimmed transcript | `1230e7d` | 2 lines trimmed, all 8 expanded; black hole clear of the card |
| 6 Panels as tabs | `94ecb98` | each tab shows its sections; sections back in columns at 1400 |
| 7 Apps inside the widget | (this commit) | docked under the top bar; ‹, ×, Escape close; ⋯ menu presses the real buttons |
| 8 Widget 560 px | `846499c` | real window 383x560 |

Two cascade bugs of the same kind were caught on the way (step 5 spacer,
step 7 ⋯ button): a narrow-screen rule placed before the base rule it
overrides loses the tie on source order; both now use a more specific
selector.

## Not changing

- Backend behaviour, wake word, voice, the phone's Tailscale setup.
- The full HUD's side panels (they stay as columns at 1100 px and up).
