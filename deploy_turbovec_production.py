#!/usr/bin/env python3
"""Deploy turbovec vector index to the PRODUCTION Second Brain vault.

This script walks ALL notes in the production vault, embeds them with
all-MiniLM-L6-v2 (384-dim), builds a 2-bit TurboQuantIndex, and writes
only NEW artifact files — no existing .md notes are modified.

Usage:
    deploy_turbovec_production.py [--dry-run]

Artistic output:
    turbovec_index.tv           — persisted TurboQuant index (2-bit quantized)
    turbovec_embeddings.npy     — raw float32 embeddings (for index rebuild)
    turbovec_meta.json          — metadata (dim, model, n_notes)
    .automation/turbovec_notes.txt — note paths (for search result lookup)

NON-DESTRUCTIVE: only creates new files. Existing .md notes are never modified.
"""
import os
import sys
import time
import json
import struct
from pathlib import Path

# ── Configuration ─────────────────────────────────────────────────────────────
PRODUCTION_VAULT = Path(r"C:\Users\deped\Documents\Second Brain")
SANDBOX = Path(r"C:\Users\deped\Documents\Second Brain Test Sandbox")

# Re-use the venv that already has turbovec + sentence-transformers installed
VENV_PYTHON = r"C:\Users\deped\AppData\Local\hermes\hermes-agent\venv\Scripts\python.exe"

# Skip dirs — same rules as the sandbox script
SKIP_DIRS = {".git", ".obsidian", "_scratch", "_to_delete"}
MAX_BYTES_PER_NOTE = 32000  # ~8K tokens


def read_notes(vault_root):
    """Yield (rel_path, text) for every eligible .md note."""
    results = []
    skipped = 0
    for root, dirs, files in os.walk(vault_root):
        # Filter skip dirs in-place
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")]
        for fname in sorted(files):
            if not fname.endswith(".md"):
                continue
            fpath = Path(root) / fname
            try:
                text = fpath.read_text(encoding="utf-8", errors="replace")
                if len(text) > MAX_BYTES_PER_NOTE:
                    text = text[:MAX_BYTES_PER_NOTE]
                # Strip YAML frontmatter
                if text.startswith("---"):
                    parts = text.split("---", 2)
                    if len(parts) >= 3:
                        text = parts[2].lstrip()
                rel = fpath.relative_to(vault_root).as_posix()
                results.append((rel, text))
            except Exception:
                skipped += 1
                continue
    return results, skipped


def main():
    dry_run = "--dry-run" in sys.argv

    print("=" * 60)
    print("TURBOVEC PRODUCTION DEPLOYMENT")
    print("=" * 60)
    print(f"Vault: {PRODUCTION_VAULT}")
    print(f"Dry run: {dry_run}")
    print()

    # Step 1: Check prerequisites
    try:
        import turbovec as tv
    except ImportError:
        print("ERROR: turbovec not installed. Run:")
        print(f'  "{VENV_PYTHON}" -m pip install turbovec')
        sys.exit(1)

    try:
        import numpy as np
        from sentence_transformers import SentenceTransformer
    except ImportError:
        print("ERROR: sentence-transformers or numpy not installed. Run:")
        print(f'  "{VENV_PYTHON}" -m pip install sentence-transformers numpy')
        sys.exit(1)

    # Step 2: Verify vault exists
    if not PRODUCTION_VAULT.exists():
        print(f"ERROR: Production vault not found at {PRODUCTION_VAULT}")
        sys.exit(1)

    # Count notes before
    notes_before = sum(
        1 for r, d, fs in os.walk(str(PRODUCTION_VAULT))
        for f in fs if f.endswith(".md")
    )
    print(f"Production notes (before): {notes_before}")
    print()

    # Step 3: Read notes
    print("[1/4] Reading notes from production vault...")
    t0 = time.monotonic()
    notes, skipped = read_notes(PRODUCTION_VAULT)
    t_read = time.monotonic() - t0
    print(f"  Notes read: {len(notes)}  (skipped {skipped})  [{t_read:.1f}s]")
    if not notes:
        print("  ERROR: No notes found!")
        sys.exit(1)
    print(f"  Example: {notes[0][0]}")
    print()

    # Step 4: Embed notes
    print("[2/4] Embedding notes with all-MiniLM-L6-v2 (384-dim)...")
    model_name = "sentence-transformers/all-MiniLM-L6-v2"
    t0 = time.monotonic()
    model = SentenceTransformer(model_name)
    dim = 384
    texts = [content for _, content in notes]
    embeddings = model.encode(texts, show_progress_bar=True, normalize_embeddings=True)
    embeddings = embeddings.astype(np.float32)
    t_embed = time.monotonic() - t0
    print(f"  Embeddings shape: {embeddings.shape}  [{t_embed:.1f}s]")
    print()

    # Step 5: Build TurboQuantIndex
    print("[3/4] Building TurboQuantIndex (2-bit)...")
    t0 = time.monotonic()
    index = tv.TurboQuantIndex(dim)
    try:
        index.prepare()
    except TypeError:
        pass
    index.add(embeddings)
    t_build = time.monotonic() - t0
    print(f"  Built in {t_build:.2f}s")
    print()

    # Step 6: Write artifacts
    print("[4/4] Writing artifacts...")
    if dry_run:
        print("  [DRY RUN] Skipping file writes")
    else:
        # turbovec_index.tv
        index_path = PRODUCTION_VAULT / "turbovec_index.tv"
        index.write(str(index_path))
        idx_bytes = index_path.stat().st_size
        print(f"  {index_path.name}: {idx_bytes:,} bytes")

        # turbovec_embeddings.npy
        emb_path = PRODUCTION_VAULT / "turbovec_embeddings.npy"
        np.save(str(emb_path), embeddings)
        emb_bytes = emb_path.stat().st_size
        print(f"  {emb_path.name}: {emb_bytes:,} bytes")

        # turbovec_meta.json
        meta_path = PRODUCTION_VAULT / "turbovec_meta.json"
        meta = {"dim": dim, "model": model_name, "n_notes": len(notes)}
        meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")
        print(f"  {meta_path.name}: {meta_path.stat().st_size} bytes")

        # .automation/turbovec_notes.txt (primary location)
        # Also write to vault root as fallback for code that checks there
        auto_dir = PRODUCTION_VAULT / ".automation"
        auto_dir.mkdir(parents=True, exist_ok=True)
        notes_path = auto_dir / "turbovec_notes.txt"
        notes_path.write_text(
            "\n".join(rel for rel, _ in notes) + "\n",
            encoding="utf-8",
        )
        print(f"  .automation/{notes_path.name}: {notes_path.stat().st_size} bytes")
        # Also write copy at vault root
        root_notes_path = PRODUCTION_VAULT / "turbovec_notes.txt"
        root_notes_path.write_text(
            "\n".join(rel for rel, _ in notes) + "\n",
            encoding="utf-8",
        )
        print(f"  {root_notes_path.name}: {root_notes_path.stat().st_size} bytes")
    print()

    # Verify
    notes_after = sum(
        1 for r, d, fs in os.walk(str(PRODUCTION_VAULT))
        for f in fs if f.endswith(".md")
    )
    raw_bytes = embeddings.nbytes
    ratio = raw_bytes / idx_bytes if not dry_run else 0

    print("=" * 60)
    print("DEPLOYMENT COMPLETE")
    print("=" * 60)
    print(f"  Notes before:  {notes_before}")
    print(f"  Notes after:   {notes_after} (Δ={notes_after - notes_before})")
    print(f"  Index file:    {idx_bytes / 1_048_576:.2f} MB ({ratio:.2f}x compression)" if not dry_run else "  (dry run)")
    print(f"  Embeddings:    {emb_bytes / 1_048_576:.2f} MB raw float32" if not dry_run else "  (dry run)")
    print(f"  Build time:    {t_build:.2f}s")
    print(f"  Embed time:    {t_embed:.1f}s")
    print(f"  Model:         {model_name} ({dim}-dim)")
    if dry_run:
        print("  Status:        [DRY RUN — no files written]")
    else:
        print("  Status:        ✅ Artifacts deployed to production vault")
    print("=" * 60)

    if notes_after != notes_before:
        print(f"\n⚠️  WARNING: Note count changed by {notes_after - notes_before}!")
        print("  This should not happen — check for accidental file creation/deletion.")
        sys.exit(1)


if __name__ == "__main__":
    main()
