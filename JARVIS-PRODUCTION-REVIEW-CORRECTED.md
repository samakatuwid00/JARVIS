## 3. App Features — FINDINGS

### What works
- `app_registry.json` auto-generates with 854 detected apps, including bin paths, categories, confidence scores, and versions. This is impressive discovery work.
- `tools.py` has `install_app()`, `uninstall_app()`, `open_application()`, `app_lookup()` — real app lifecycle management.
- `rules_engine.py` + `rules_compiler.py` provide per-app rule enforcement (hard/soft, launch/close scoped).
- `apps_panel.html` is a polished web UI for managing apps and rules — dark theme, app cards, rule editors, toggle/install/open buttons. Looks production-quality visually.
- `rules_engine.py` supports action-scoped rules (pre_play/pre_launch block launch but not close; pre_close blocks close but not launch) — thoughtful design preventing false blocks.
- **`tools.py` already wires `rules_engine` into `open_application()`** (lines 3199-3211): imports `evaluate_app_rules`, calls it before launch, blocks if hard rule fires, surfaces soft notices. Claude Code review initially reported this as dead code — it is actually connected. The rules feature works at runtime.

### What's broken / missing
- **install_app() is scaffolding, not implemented.** Reading the install_app section of tools.py (lines 3289-3400) shows it's a shell — the actual installation logic for arbitrary apps is not implemented. It can install from known package managers but has no generic "install any app from URL/repo" capability.
- **"reads apps and creates scripts to operate them" is aspirational.** There's no code that reads an installed app's capabilities and generates operation scripts. The app registry stores what's installed but doesn't introspect what each app CAN do. Voice commands like "maximize Spotify" would need JARVIS to know Spotify's CLI/media key interface — this mapping doesn't exist programmatically.
- **App capability listing is manual, not automatic.** `apps_panel.html` shows app cards but the "what I can do" list per app would need to be hand-authored or generated from some capability manifest. No such manifest exists in the codebase.
- **`app_registry.json` is user-specific.** Paths like `C:\Program Files\Git\mingw64\bin\git.EXE` and `C:\Users\deped\AppData\Local\hermes\...` are YOUR machine. For open-source, this must be regenerated per-install or made relative.
- **`capabilities.json` is stale.** Generated 2026-08-22, app_registry.json is from 2026-08-28. Two competing registries with different timestamps — confusing which is authoritative.

### Production-readiness verdict: PARTIAL (corrected)
Rules engine is wired and functional. App discovery and launch work. But install automation is scaffolding, app capability scripting is aspirational, and the registry is machine-specific. Students can launch apps by voice but can't install new ones programmatically or ask "what can this app do."
