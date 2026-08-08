#!/usr/bin/env python3
"""Standalone test runner for search_vault_semantic against the Second Brain Test Sandbox.

Steps:
1. Sets JARVIS_VAULT_ROOT env var to the sandbox vault path (read by tools.py as VAULT_ROOT).
2. Resets the module-level turbovec cache (_turbovec_cache = None) to force a fresh load.
3. Runs search_vault_semantic("frontend js pattern", limit=5) with timing.
"""
import os
import sys
import time
import importlib

# ── Step 1: Set VAULT_ROOT via the env var that tools.py reads ──────────────
SANDBOX = r"C:\Users\deped\Documents\Second Brain Test Sandbox"
os.environ["JARVIS_VAULT_ROOT"] = SANDBOX

# Ensure jarvis-demo is on the path so `import tools` resolves to tools.py
VENV_PY_HAD_TO_RESTART = False
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# ── Import tools module (this reads VAULT_ROOT from env at import time) ──────
import tools

print("=" * 60)
print("search_vault_semantic — Sandbox cache-reset + timing test")
print("=" * 60)
print(f"JARVIS_VAULT_ROOT = {os.environ['JARVIS_VAULT_ROOT']}")
print(f"tools.VAULT_ROOT  = {tools.VAULT_ROOT}")
print()

# ── Step 2: Reset the module-level cache ─────────────────────────────────────
# Before reset (may be None on first import, or stale from another vault)
print(f"Cache before reset: _turbovec_cache={tools._turbovec_cache!r}")
print(f"Cache vault before reset: _turbovec_cache_vault={tools._turbovec_cache_vault!r}")

tools._turbovec_cache = None
tools._turbovec_cache_vault = None

print(f"Cache after reset:  _turbovec_cache={tools._turbovec_cache!r}")
print(f"Cache vault after reset: _turbovec_cache_vault={tools._turbovec_cache_vault!r}")
print()

# ── Step 3: Run search_vault_semantic("frontend js pattern", limit=5) ────────
QUERY = "frontend js pattern"
LIMIT = 5

print(f"Query: '{QUERY}'  (limit={LIMIT})")
print("---")

t_start = time.monotonic()
result = tools.search_vault_semantic(QUERY, limit=LIMIT)
t_elapsed = time.monotonic() - t_start

print(f"\n[search_vault_semantic returned in {t_elapsed:.4f}s = {t_elapsed*1000:.1f}ms]")
print("---")
print("RESULT:")
print(result)
print("---")

# Second call to show cache-hit timing
print("\nSecond call (cache should be warm):")
t_start2 = time.monotonic()
result2 = tools.search_vault_semantic(QUERY, limit=LIMIT)
t_elapsed2 = time.monotonic() - t_start2
print(f"[second call returned in {t_elapsed2:.4f}s = {t_elapsed2*1000:.1f}ms]")

print()
print("=" * 60)
print("TIMING SUMMARY")
print("=" * 60)
print(f"First call (cold cache — loads model + rebuilds index): {t_elapsed:.4f}s")
print(f"Second call (warm cache):                            {t_elapsed2:.4f}s")
