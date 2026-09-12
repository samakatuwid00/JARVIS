"""context_assembler.py — Phase 4: Context-assembly layer.

Before JARVIS spawns any real agent (hermes, opencode, cli), it pulls only
relevant facts from its own memory, packages them as a self-contained brief
formatted for that delegate's interface, and logs the brief.

Design:
  - pull minimal relevant facts: profile (tier2) + recent window (tier3) + vault semantic (tier1) + app context
  - format per delegate: hermes gets full grounding + cite rule; opencode gets repo-focused; gemini/chatgpt get ideation-focused
  - log brief to audit.jsonl (truncated) and return full brief string
  - delegates can request missing piece via 'clarify' skill (future); for now they say "missing X"

Stdlib only; fails open (returns empty brief on error).
"""

import os
import re
import json
from pathlib import Path

_PROFILE_PATH = Path(__file__).parent / "jarvis-profile.md"
_VAULT_HINT = "Second Brain vault at C:/Users/deped/Documents/Second Brain (semantic search via turbovec; read/write wiki/evergreen)."

def _load_profile() -> str:
    try:
        return _PROFILE_PATH.read_text(encoding="utf-8").strip()
    except Exception:
        return "(profile not found)"

def _recent_summary(n=6) -> str:
    try:
        from session_store import recent_summary as rs
        return rs(n)
    except Exception:
        return ""

def _app_context(task: str) -> str:
    try:
        from tools import _app_context as ac
        return ac(task)
    except Exception:
        return ""

def _vault_context(task: str, max_hits=3, delegate: str = "hermes") -> str:
    """Pull vault facts relevant to task. Delegate-specific truncation."""
    # For file/opencode delegates, prioritize file-related notes; for ideation, broader.
    try:
        # Use semantic search if available, else keyword
        result = ""
        try:
            from tools import execute_tool as _et
            # need to avoid recursion via delegate loop; call search directly
            import tools as t
            # Try semantic first
            result = t.search_vault_semantic(task, limit=max_hits) if hasattr(t, "search_vault_semantic") else t.search_vault(task, limit=max_hits)
        except Exception:
            pass
        if result and "[Error" not in result[:20] and "no results" not in result.lower() and "No vault" not in result:
            txt = result.strip()
            # per-delegate budget: hermes gets 2400, opencode gets 1800, cli gets 1200
            budget = {"hermes": 2400, "opencode": 1800, "gemini-cli": 1200, "claude-cli": 1200, "codex-cli": 1200}.get(delegate, 1800)
            if len(txt) > budget:
                txt = txt[:budget] + "\n...(truncated)"
            return f"[VAULT CONTEXT]\n{txt}\n[END VAULT]\n"
    except Exception:
        pass
    return ""

# CLI agents are named by their cli_agents.json key; the brief styles below
# use the -cli names, so Claude Code was getting Hermes's brief.
_CLI_BRIEF_NAMES = {"claude": "claude-cli", "gemini": "gemini-cli", "codex": "codex-cli"}


def assemble_brief(task: str, delegate: str = "hermes") -> str:
    """Build per-delegate brief and log it. Returns brief prefix to prepend to task."""
    delegate = _CLI_BRIEF_NAMES.get(delegate, delegate)
    profile = _load_profile()
    recent = _recent_summary(6)
    appctx = _app_context(task)
    vault = _vault_context(task, max_hits=3, delegate=delegate)
    # recall-shaped queries also pull past session matches
    facts = ""
    try:
        import semantic_memory as smod
        block = smod.context_block(10)
        if block:
            facts = block + "\n"
    except Exception:
        pass
    recall = ""
    if re.search(r"\b(remember|last time|did i|what did i|previously|before|history)\b", task, re.I):
        try:
            import session_index as si
            si.rebuild()
            hits = si.search_sessions(task, 3)
            if hits:
                recall = "[PAST SESSION MATCHES]\n" + si.format_hits(hits) + "\n\n"
        except Exception:
            pass

    # Per-delegate formatting
    if delegate in ("opencode", "claude-cli", "codex-cli"):
        header = (
            f"CONTEXT for CODE delegate ({delegate}):\n"
            f"[PROFILE]\n{profile}\n\n"
            f"[RECENT]\n{recent}\n\n"
            f"{facts}"
            f"{recall}{vault}"
            f"[APP CONTEXT]\n{appctx}\n"
            "Instructions: Focus on codebase; read repo files before editing; cite files touched; verify build.\n\n"
        )
    elif delegate in ("gemini-cli", "chatgpt"):
        header = (
            f"CONTEXT for IDEATION delegate ({delegate}):\n"
            f"[PROFILE]\n{profile}\n\n"
            f"[RECENT]\n{recent}\n\n"
            f"{facts}"
            f"{recall}{vault}"
            "Instructions: Provide ideas, alternatives, citations; do not mutate files unless asked.\n\n"
        )
    else:  # hermes floor
        header = (
            "CONTEXT (JARVIS persistent memory):\n"
            f"[PROFILE]\n{profile}\n\n"
            f"[RECENT SESSION]\n{recent}\n\n"
            f"{facts}"
            f"{recall}{vault}"
            f"[KNOWLEDGE BASE] {_VAULT_HINT}\n"
            "Ground factual answers in Second Brain vault and cite source note. If vault has nothing, say so. Respect [PROFILE] preferences.\n"
        )
        if appctx:
            header += appctx + "\n"
    header += "TASK:\n"
    # Log brief (truncated) to audit trail
    try:
        import audit
        # Use tools audit helper if available, else direct
        from tools import _audit_log
        _audit_log(f"context:{delegate}", task[:200], "brief_assembled", result=header[:1000], extra={"delegate": delegate, "vault_len": len(vault), "appctx_len": len(appctx)})
    except Exception:
        pass
    return header

def delegate_needs_clarification(reply: str) -> bool:
    """True if delegate output indicates missing context and wants clarification."""
    if not reply:
        return False
    t = reply.lower()
    return any(k in t for k in ("missing context", "need more info", "clarify", "which file", "which project", "not enough information"))
