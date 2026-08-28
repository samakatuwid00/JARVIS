"""music_rules.py - make adapter_check strings machine-actionable for PLAYBACK.

Stdlib only (re, json, os). This is the bridge between the human-written
`adapter_check` strings that rules_compiler.py stores in app_registry.json and
the real, executable playback controls in music_agent.py.

Two kinds of playback rule exist today, and they are NOT equivalent in trust:

* "cap volume at N%"  -> REAL. music_agent.py drives the <video> element
  directly, so v.volume can be set. This is enforced, not advisory.
* "skip if track.explicit" -> BEST-EFFORT proxy. YouTube Music renders the word
  "Explicit" inside the visible track title; there is NO Spotify desktop control
  and NO metadata flag exposed here. We match on the title string only. That is
  an advisory cue, never a guarantee, and every caller must label it as such.
"""

import re
import json
import os

# Returned when no rules resolve (no registry, missing app, empty compiled_rules).
DEFAULT_MUSIC_SETTINGS = {
    "cap_volume": None,      # None => no cap; otherwise float 0.0..1.0
    "skip_explicit": False,  # best-effort title-cue proxy, advisory
    "needs_clarification": False,
}

# "pre_play: cap spotify volume at 60%" / "cap volume at 60%" / "volume 60%"
_CAP_VOLUME_RE = re.compile(
    r"cap\s+.*?volume\s+at\s+(\d+)\s*%",
    re.IGNORECASE,
)
_CAP_VOLUME_FALLBACK_RE = re.compile(r"volume\s+at\s+(\d+)\s*%", re.IGNORECASE)
# "pre_play: skip if track.explicit" / "skip explicit"
_SKIP_EXPLICIT_RE = re.compile(r"skip\b.*explicit", re.IGNORECASE)


def _title_looks_explicit(title):
    """Best-effort cue only: music.youtube.com renders Explicit in the title.

    NOT a metadata guarantee - do not present as one. Returns True when the
    visible title string carries the word "explicit".
    """
    if not title:
        return False
    return "explicit" in title.lower()


def _parse_adapter_check(check):
    """Parse one adapter_check string into machine-actionable playback settings.

    Returns {"cap_volume": Optional[float], "skip_explicit": bool}. Unknown or
    empty checks yield the no-op defaults so a missing rule never blocks playback.
    """
    result = {"cap_volume": None, "skip_explicit": False}
    if not check:
        return result
    low = check.lower()

    m = _CAP_VOLUME_RE.search(low) or _CAP_VOLUME_FALLBACK_RE.search(low)
    if m:
        pct = int(m.group(1))
        # Clamp defensively: a 0% cap would be silent, >100% is meaningless.
        pct = max(0, min(100, pct))
        result["cap_volume"] = pct / 100.0

    if _SKIP_EXPLICIT_RE.search(low):
        result["skip_explicit"] = True

    return result


def _parse_music_rules(entry):
    """Pull playback-relevant settings from a registry entry's compiled_rules.

    `entry` is apps[<app_key>] from app_registry.json. Only rules whose intent
    mentions playback (play / volume / explicit) are honoured; everything else is
    ignored so non-playback rules never leak into the audio path. When several
    caps are present the STRICTEST (lowest) wins, since that is the safer bound.
    """
    settings = dict(DEFAULT_MUSIC_SETTINGS)
    if not isinstance(entry, dict):
        return settings

    compiled = entry.get("compiled_rules") or []
    for rule in compiled:
        if not isinstance(rule, dict):
            continue
        intent = (rule.get("intent") or "").lower()
        if not any(k in intent for k in ("play", "volume", "explicit")):
            continue
        parsed = _parse_adapter_check(rule.get("adapter_check") or "")
        if parsed["cap_volume"] is not None:
            if settings["cap_volume"] is None or parsed["cap_volume"] < settings["cap_volume"]:
                settings["cap_volume"] = parsed["cap_volume"]
        if parsed["skip_explicit"]:
            settings["skip_explicit"] = True
        if rule.get("needs_clarification"):
            settings["needs_clarification"] = True
    return settings


def resolve_music_rules(registry_path=None, app_key="spotify"):
    """Load compiled rules from app_registry.json and resolve playback settings.

    registry_path defaults to app_registry.json beside this file. A missing or
    unreadable registry returns DEFAULT_MUSIC_SETTINGS rather than raising, so a
    playback rule lookup can never crash a music command.
    """
    if registry_path is None:
        registry_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                     "app_registry.json")
    try:
        with open(registry_path, "r", encoding="utf-8") as f:
            registry = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return dict(DEFAULT_MUSIC_SETTINGS)

    apps = registry.get("apps", {}) if isinstance(registry, dict) else {}
    return _parse_music_rules(apps.get(app_key))


def advisory_note(settings):
    """Human-facing caveat for the skip-explicit proxy, or "" when not in use."""
    if settings.get("skip_explicit"):
        return ("(explicit-skip is a best-effort title cue on YouTube Music, "
                "not a metadata guarantee)")
    return ""
