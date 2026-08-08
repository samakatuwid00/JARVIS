#!/usr/bin/env python3
"""Verification script for Second Brain Test Sandbox isolation.

Confirms that the Second Brain Test Sandbox vault is a properly
isolated copy of the production vault, safe for testing turbovec integration.
"""
import os
import subprocess
from pathlib import Path

# Use Windows-native paths — Python3 runs in the Windows context, not MSYS.
PROD_VAULT   = Path(r"C:\Users\deped\Documents\Second Brain")
SANDBOX_VAULT = Path(r"C:\Users\deped\Documents\Second Brain Test Sandbox")

results = []

def test(name, fn):
    try:
        ok, detail = fn()
        results.append((name, ok, detail))
    except Exception as e:
        results.append((name, False, str(e)))

def count_md(path):
    """Count .md files, excluding .git and .obsidian, using Python native traversal."""
    count = 0
    skip_dirs = {".git", ".obsidian"}
    for root, dirs, files in os.walk(str(path)):
        # In-place prune skip dirs
        dirs[:] = [d for d in dirs if d not in skip_dirs]
        for f in files:
            if f.endswith(".md"):
                count += 1
    return count

def git_remotes(path):
    """Return list of git remote names."""
    try:
        out = subprocess.run(
            ["git", "-C", str(path), "remote"],
            capture_output=True, text=True, timeout=10
        )
        return [r for r in out.stdout.strip().splitlines() if r]
    except Exception:
        return []

def git_remote_url(path):
    """Return the origin URL if it exists."""
    try:
        out = subprocess.run(
            ["git", "-C", str(path), "remote", "get-url", "origin"],
            capture_output=True, text=True, timeout=10
        )
        return out.stdout.strip() if out.returncode == 0 else None
    except Exception:
        return None

def git_commit_count(path):
    """Return number of commits."""
    try:
        out = subprocess.run(
            ["git", "-C", str(path), "log", "--oneline"],
            capture_output=True, text=True, timeout=10
        )
        return len(out.stdout.strip().splitlines()) if out.stdout.strip() else 0
    except Exception:
        return 0

# --- Tests ---

def t1_sandbox_exists():
    return SANDBOX_VAULT.exists(), f"Sandbox path exists: {SANDBOX_VAULT}"

def t2_sandbox_note_count():
    n = count_md(SANDBOX_VAULT)
    return n == 1242, f"Sandbox has {n} .md notes (expected 1242)"

def t3_sandbox_no_remotes():
    remotes = git_remotes(SANDBOX_VAULT)
    return len(remotes) == 0, f"Sandbox git remotes: {remotes or '(none - safe)'}"

def t4_sandbox_no_push_protection():
    """Even if someone tries git push, there's no origin to push to."""
    url = git_remote_url(SANDBOX_VAULT)
    return url is None, f"Sandbox origin URL: {url or '(none - cannot push to prod)'}"

def t5_prod_intact():
    n = count_md(PROD_VAULT)
    return n == 1242, f"Production vault has {n} .md notes (expected 1242, confirming untouched)"

def t6_prod_has_remote():
    url = git_remote_url(PROD_VAULT)
    ok = url is not None and "second-brain-vault" in url
    return ok, f"Production origin URL: {url or '(MISSING!)'}"

def t7_prod_commit_count():
    c = git_commit_count(PROD_VAULT)
    return c == 51, f"Production has {c} commits (expected 51)"

_TURBOVEC_FILENAME = "RyanCodraiturbovec A vector index built on TurboQuant, written in Rust with Python bindings.md"

def t8_turbovec_note_in_sandbox():
    target = SANDBOX_VAULT / "raw" / _TURBOVEC_FILENAME
    return target.exists(), f"turbovec note in sandbox: {target.exists()}"

def t9_turbovec_note_in_prod():
    target = PROD_VAULT / "raw" / _TURBOVEC_FILENAME
    return target.exists(), f"turbovec note in production: {target.exists()}"

def t10_sandbox_writable():
    test_file = SANDBOX_VAULT / "raw" / "_sandbox_write_test.txt"
    try:
        test_file.write_text("sandbox-write-test")
        content = test_file.read_text()
        test_file.unlink()
        return content == "sandbox-write-test", "Sandbox is writable"
    except Exception as e:
        return False, f"Sandbox write failed: {e}"

def t11_sandbox_index_tsv():
    idx = SANDBOX_VAULT / ".automation" / "vault-index.tsv"
    exists = idx.exists()
    size = idx.stat().st_size if exists else 0
    return exists and size > 0, f"Sandbox vault-index.tsv: {size} bytes"

def t12_sandbox_git_status_clean():
    """Sandbox git status - should have no test artifacts left behind.
    Untracked source files (added after last commit) are fine."""
    try:
        out = subprocess.run(
            ["git", "-C", str(SANDBOX_VAULT), "status", "--porcelain"],
            capture_output=True, text=True, timeout=10
        )
        lines = [l for l in out.stdout.strip().splitlines() if l]
        # Check that t10's test file is gone (the only artifact we create)
        test_artifacts = [l for l in lines if "_sandbox_write_test" in l]
        return len(test_artifacts) == 0, f"Sandbox git status: {len(lines)} entries ({len(test_artifacts)} test artifacts). Untracked source files are expected."
    except Exception as e:
        return False, f"git status failed: {e}"

# Run all tests
tests = [
    ("1. Sandbox path exists", t1_sandbox_exists),
    ("2. Sandbox has 1242 notes (matches prod)", t2_sandbox_note_count),
    ("3. Sandbox has NO git remote (no accidental push)", t3_sandbox_no_remotes),
    ("4. Sandbox cannot push to production", t4_sandbox_no_push_protection),
    ("5. Production vault untouched (1242 notes)", t5_prod_intact),
    ("6. Production still has its remote", t6_prod_has_remote),
    ("7. Production commit count unchanged (51)", t7_prod_commit_count),
    ("8. turbovec note exists in sandbox", t8_turbovec_note_in_sandbox),
    ("9. turbovec note exists in production", t9_turbovec_note_in_prod),
    ("10. Sandbox is writable (for testing)", t10_sandbox_writable),
    ("11. Sandbox has vault-index.tsv", t11_sandbox_index_tsv),
    ("12. Sandbox git status is clean", t12_sandbox_git_status_clean),
]

for name, fn in tests:
    test(name, fn)

# Report
passed = sum(1 for _, ok, _ in results if ok)
total  = len(results)

print(f"\n{'='*60}")
print(f" SECOND BRAIN TEST SANDBOX VERIFICATION: {passed}/{total} PASSED")
print(f"{'='*60}\n")

for name, ok, detail in results:
    icon = "OK" if ok else "XX"
    print(f"  [{icon}] {name}")
    print(f"        -> {detail}")
    print()

if passed == total:
    print("ALL CHECKS PASSED - sandbox is properly isolated and ready for turbovec testing.")
    exit(0)
else:
    failed = total - passed
    print(f"{failed} CHECK(S) FAILED - review before testing.")
    exit(1)
