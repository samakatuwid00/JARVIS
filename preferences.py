"""preferences.py - defaults JARVIS learns from use, saved only after you agree.

Phase 3 of the JARVIS Apps Intelligence design. observe(topic, choice) counts
the same choice used for a topic ("movie search" -> hollymoviehd.cc in Brave).
After STREAK uses in a row it returns a question, once per topic; confirm()
saves the default and decline() stops asking. Saved defaults are what the
understand-first router uses when a command leaves the site or app open
("search a movie" with no site named).

Stored in memory/preferences.json (gitignored: private, never committed).
"""

import json
import os
import threading
import time

STREAK = 3
_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "memory", "preferences.json")
_LOCK = threading.Lock()


def _load():
    try:
        with open(_PATH, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(data):
    os.makedirs(os.path.dirname(_PATH), exist_ok=True)
    tmp = _PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp, _PATH)


def _describe(choice):
    parts = [choice.get("site"), f"in {choice['browser'].title()}" if choice.get("browser") else None,
             choice.get("app")]
    return " ".join(p for p in parts if p) or json.dumps(choice)


def observe(topic, choice):
    """Count one use. Returns the question to ask when the same choice has been
    used STREAK times in a row and this topic was never asked about."""
    key = json.dumps(choice, sort_keys=True)
    with _LOCK:
        data = _load()
        t = data.setdefault(topic, {"streak_choice": None, "streak": 0, "asked": False,
                                    "default": None})
        t["streak"] = t["streak"] + 1 if t["streak_choice"] == key else 1
        t["streak_choice"] = key
        ask = t["streak"] >= STREAK and not t["asked"] and t["default"] is None
        if ask:
            t["asked"] = True
        _save(data)
    if not ask:
        return None
    return f"You always use {_describe(choice)} for {topic}. Make that the default?"


def confirm(topic):
    """Save the streak's choice as the default. Returns it (or None)."""
    with _LOCK:
        data = _load()
        t = data.get(topic)
        if not t or not t.get("streak_choice"):
            return None
        t["default"] = json.loads(t["streak_choice"])
        t["saved_at"] = time.strftime("%Y-%m-%d %H:%M")
        _save(data)
        return t["default"]


def decline(topic):
    with _LOCK:
        data = _load()
        if topic in data:
            data[topic]["asked"] = True
            _save(data)


def default(topic):
    return (_load().get(topic) or {}).get("default")


def all_defaults():
    return {t: v["default"] for t, v in _load().items() if v.get("default")}


def forget(topic):
    """Remove a saved default; JARVIS may ask again after a new streak."""
    with _LOCK:
        data = _load()
        if data.pop(topic, None) is not None:
            _save(data)
            return True
    return False
