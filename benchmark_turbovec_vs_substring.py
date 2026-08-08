#!/usr/bin/env python3
"""Final side-by-side benchmark: production substring vs sandbox turbovec semantic search."""
import time, sys
from pathlib import Path

JARVIS = Path(r"C:\Users\deped\Documents\jarvis-demo")
SANDBOX = Path(r"C:\Users\deped\Documents\Second Brain Test Sandbox")
PROD = Path(r"C:\Users\deped\Documents\Second Brain")

sys.path.insert(0, str(JARVIS))

# Fresh import to avoid stale cache state
if "tools" in sys.modules:
    del sys.modules["tools"]
import tools

queries = [
    "nexusRAG",
    "frontend js pattern",
    "Laravel quality gate shipping",
    "portfolio terminal site",
    "guest speaker AI teaching",
    "iRIMS-V pagination",
    "JARVIS voice assistant",
]

print("=" * 70)
print("FINAL BENCHMARK: Production Substring vs Sandbox Turbovec Semantic")
print("=" * 70)

# === PRODUCTION: Substring search ===
print("\n### PRODUCTION VAULT (substring search) ###\n")
tools._turbovec_cache = None
tools._turbovec_cache_vault = None
tools.VAULT_ROOT = PROD

total_prod = 0
for q in queries:
    t0 = time.monotonic()
    result = tools.search_vault(q, limit=5)
    t = time.monotonic() - t0
    total_prod += t
    status = "FOUND" if "Found" in result else "none"
    print(f"  '{q}' — {t:.3f}s ({status})")

print(f"\n  Total time: {total_prod:.3f}s for {len(queries)} queries")
print(f"  Average:    {total_prod/len(queries):.3f}s per query")

# === SANDBOX: Semantic search with cache ===
print("\n### SANDBOX VAULT (turbovec semantic search, with cache) ###\n")
tools._turbovec_cache = None
tools._turbovec_cache_vault = None
tools.VAULT_ROOT = SANDBOX

# Warmup call (includes model load + index build)
t0 = time.monotonic()
warmup = tools.search_vault_semantic(queries[0], limit=5)
t_warmup = time.monotonic() - t0
is_semantic = "Semantic search" in warmup
print(f"  Warmup (includes model load + index rebuild): {t_warmup:.2f}s ({'SEMANTIC' if is_semantic else 'FALLBACK'})")

# Cached calls (pure vector search only)
total_sandbox = 0
for q in queries:
    t0 = time.monotonic()
    result = tools.search_vault_semantic(q, limit=5)
    t = time.monotonic() - t0
    total_sandbox += t
    status = "SEMANTIC" if "Semantic search" in result else "FALLBACK"
    print(f"  '{q}' — {t:.4f}s ({status})")

# Average excluding warmup
total_no_warmup = total_sandbox  # includes one extra warmup query
avg_cached = total_sandbox / len(queries)

print(f"\n  Total (including warmup query): {t_warmup + total_sandbox:.2f}s")
print(f"  Average per query (cached, post-warmup): {avg_cached:.4f}s")

# === Summary ===
print("\n" + "=" * 70)
print("RESULTS SUMMARY")
print("=" * 70)
print(f"  Production (substring):     {total_prod/len(queries):.3f}s avg/query")
print(f"  Sandbox (turbovec cached):  {avg_cached:.4f}s avg/query")
print(f"  Speedup:                    {total_prod/len(queries) / avg_cached:.0f}x faster")
print(f"  First-call overhead:        {t_warmup:.1f}s (one-time model + index load)")
print("=" * 70)

# Verify production is still clean
print("\n### PRODUCTION VAULT INTEGRITY CHECK ###\n")
for fname in ["turbovec_index.tv", "turbovec_embeddings.npy", "turbovec_notes.txt"]:
    p = PROD / fname
    if p.exists():
        print(f"  ⚠️  LEAKED: {fname} ({p.stat().st_size} bytes)")
    else:
        print(f"  ✅ {fname} — not present (production clean)")
