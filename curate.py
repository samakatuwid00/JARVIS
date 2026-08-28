"""curate.py — Phase 8 opt-in curation layer for the App Registry.

The app_registry.json currently holds a full blind scan of ~853 executables.
Phase 8 makes the registry OPT-IN: only apps the user explicitly registers
(and enables) are primary routing candidates for the new intent router.
Everything else stays in the registry as "detected but not registered" so the
panel can show it and the legacy matcher can still fall back to it.

This module is the single source of truth for the opt-in boundary:
- is_registered(app)  -> user explicitly opted this app in
- is_enabled(app)     -> registered AND not disabled
- registered_match(requested) -> best registered+enabled match, or None
"""
import json
import os
import re

from machine_capabilities import REGISTRY_PATH, load_registry

# Seed opt-in set: the apps the plan says to register first. These start as
# registered+enabled; everything else is detected-but-not-registered until the
# user opts in via the panel or mark_registered().
SEED_REGISTERED = {
    "spotify", "chrome", "msedge", "brave", "explorer",
    "code", "winword", "excel", "powerpnt", "notepad",
}

# Apps that must NEVER be auto-registered (junk / system / installer helpers).
NEVER_REGISTER = {
    "aslogdumptool2", "asusadobepromotion", "asusfeatureservice",
    "docker-agent", "docker-ai", "docker-debug", "officeclicktorun",
    "mavinject32", "appvcleaner", "appvshnotify", "integratedoffice",
}


def _apps():
    reg = load_registry() or {}
    return reg.get("apps", {})


def is_registered(key):
    """True if the app entry carries an explicit registered/opt-in flag."""
    entry = _apps().get(key)
    if not isinstance(entry, dict):
        return False
    # Explicit flag wins. Absent flag -> fall back to seed set membership.
    if "registered" in entry:
        return bool(entry.get("registered"))
    return key in SEED_REGISTERED and key not in NEVER_REGISTER


def is_enabled(key):
    entry = _apps().get(key)
    if not isinstance(entry, dict):
        return False
    if not is_registered(key):
        return False
    return entry.get("enabled", True) is not False


def mark_registered(key, registered=True, enabled=True):
    """Opt an app in (or out). Writes the explicit flag to the registry."""
    if not load_registry():
        return False
    data = json.load(open(REGISTRY_PATH, encoding="utf-8"))
    apps = data.setdefault("apps", {})
    if key not in apps:
        return False
    apps[key]["registered"] = bool(registered)
    if registered:
        apps[key]["enabled"] = bool(enabled)
    tmp = REGISTRY_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp, REGISTRY_PATH)
    return True


def registered_apps():
    return [k for k in _apps() if is_registered(k)]


def detected_but_unregistered():
    return [k for k in _apps()
            if k not in NEVER_REGISTER and not is_registered(k)]


def _norm(s):
    return re.sub(r"[^a-z0-9]+", "", (s or "").lower())


def registered_match(requested):
    """Phase 8 new-router pre-check: prefer a registered+enabled app.

    Returns the matched key (str) or None. Tries, in order:
      1. exact key match among registered+enabled
      2. registered+enabled key contained in the normalized request
      3. request word equals a registered+enabled key
    Does NOT fall back to the blind scan — callers fall through to the legacy
    matcher when this returns None (parallel old+new, per plan Phase 8.3).
    """
    req = _norm(requested)
    if not req:
        return None
    enabled = [k for k in registered_apps() if is_enabled(k)]
    for k in enabled:
        nk = _norm(k)
        if nk == req or nk in req.split() or nk in req:
            return k
    # also try display names
    apps = _apps()
    for k in enabled:
        name = _norm(apps.get(k, {}).get("name", ""))
        if name and (name == req or name in req.split()):
            return k
    return None


def migrate_to_optin():
    """Phase 8.6: make the registry opt-in.

    Sets an explicit `registered` flag on every app: True for the seed set
    (and any app already flagged), False for everything else. Idempotent.
    Does NOT delete detected apps — they remain as fallback candidates.
    Returns (registered_count, detected_count).
    """
    if not load_registry():
        return (0, 0)
    data = json.load(open(REGISTRY_PATH, encoding="utf-8"))
    apps = data.setdefault("apps", {})
    reg_count = 0
    det_count = 0
    for key, entry in apps.items():
        if not isinstance(entry, dict):
            continue
        if "registered" in entry:
            if entry["registered"]:
                reg_count += 1
            else:
                det_count += 1
            continue
        if key in SEED_REGISTERED and key not in NEVER_REGISTER:
            entry["registered"] = True
            entry.setdefault("enabled", True)
            reg_count += 1
        else:
            entry["registered"] = False
            det_count += 1
    tmp = REGISTRY_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp, REGISTRY_PATH)
    return (reg_count, det_count)


if __name__ == "__main__":
    rc, dc = migrate_to_optin()
    print(f"migrate_to_optin -> registered={rc}, detected={dc}")
    print("sample registered:", registered_apps()[:12])
