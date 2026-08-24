"""guard.py — prompt-injection sanitizer for external content (Phase 11b).

Every place JARVIS reads text from OUTSIDE its own code — ChatGPT replies,
page content, search results, pasted text — passes through sanitize_web_text()
before entering model context. Instruction-shaped patterns are quarantined:
replaced with a marked placeholder so downstream models treat the block as
data, never as directions.

This is a chokepoint, not a guarantee. New external-content paths must call
this; grep for `sanitize` in review to confirm coverage.
"""

import re

# Patterns that try to speak AS the system or override behavior.
_INJECTION_PATTERNS = [
    re.compile(r"ignore\s+(all\s+)?(previous|prior|above)\s+(instructions|prompts?|rules?)", re.I),
    re.compile(r"disregard\s+(all\s+)?(previous|prior|your)\s+(instructions|prompts?|rules?)", re.I),
    re.compile(r"(you are|act as|pretend to be|from now on you(?:'| a)re)\s+(now\s+)?(an?\s+)?(unrestricted|uncensored|dan|jailbroken|different|new)", re.I),
    re.compile(r"system\s*[:\-]\s*", re.I),
    re.compile(r"</?(system|assistant|developer)\s*>", re.I),
    re.compile(r"\b(your new|new)\s+(system\s+)?(prompt|instructions)\s+is\b", re.I),
    re.compile(r"repeat\s+(your|the)\s+(system\s+)?(prompt|instructions)", re.I),
    # hidden/obfuscation tricks
    re.compile(r"\[\s*(?:SYSTEM|INST|SYS)\s*\]", re.I),
]

_QUARANTINE = "[QUARANTINED: looked like an embedded instruction — treated as data]"


def sanitize_web_text(text: str, label: str = "external") -> str:
    """Quarantine instruction-shaped spans inside external text.

    Returns text of equal informational value minus injection attempts.
    Quarantined spans are replaced inline so surrounding context survives.
    """
    if not text:
        return text
    cleaned = str(text)
    hits = 0
    for pat in _INJECTION_PATTERNS:
        cleaned, n = pat.subn(_QUARANTINE, cleaned)
        hits += n
    return cleaned


def is_suspicious(text: str) -> bool:
    """True if any injection pattern fires (for logging/routing decisions)."""
    if not text:
        return False
    return any(p.search(str(text)) for p in _INJECTION_PATTERNS)
