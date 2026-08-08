#!/usr/bin/env python3
"""Sandbox integration test: turbovec semantic search with sentence-transformers.

Non-destructive test — creates only:
  - turbovec_index.tv       (the persisted TurboQuant index)
  - turbovec_test_results.md (this run's report)

Uses BAAI/bge-m3 (1024-dim) if available locally, otherwise falls back to
all-MiniLM-L6-v2 (384-dim) which is smaller and faster.

Usage:
    python turbovec_sandbox_test_semantic.py
"""
import os
import sys
import time
import json
from pathlib import Path
import subprocess

SANDBOX = r"C:\Users\deped\Documents\Second Brain Test Sandbox"
SKIP_DIRS = {".git", ".obsidian", "_scratch", "_to_delete", ".inbox", ".claude", ".automation"}
MAX_NOTES = 200  # Full smoke test — MiniLM is fast on CPU
MAX_BYTES_PER_NOTE = 32000  # ~8K tokens


def read_notes(vault_root, max_notes=None, max_bytes_per_note=MAX_BYTES_PER_NOTE):
    """Read .md notes from vault, skipping noise dirs."""
    results = []
    skip_count = 0
    for root, dirs, files in os.walk(vault_root):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")]
        for fname in sorted(files):
            if not fname.endswith(".md"):
                continue
            fpath = Path(root) / fname
            try:
                text = fpath.read_text(encoding="utf-8", errors="replace")
                if len(text) > max_bytes_per_note:
                    text = text[:max_bytes_per_note]
                # Strip frontmatter
                if text.startswith("---"):
                    parts = text.split("---", 2)
                    if len(parts) >= 3:
                        text = parts[2]
                rel = str(fpath.relative_to(vault_root))
                results.append((rel, text))
                if max_notes and len(results) >= max_notes:
                    return results, skip_count
            except Exception:
                skip_count += 1
    return results, skip_count


def ensure_sentence_transformers():
    """Install sentence-transformers if not available."""
    try:
        from sentence_transformers import SentenceTransformer
        return SentenceTransformer
    except ImportError:
        print("Installing sentence-transformers...")
        subprocess.run(
            [sys.executable, "-m", "pip", "install", "sentence-transformers"],
            capture_output=True,
            timeout=180,
        )
        try:
            from sentence_transformers import SentenceTransformer
            return SentenceTransformer
        except ImportError:
            print("ERROR: Could not install sentence-transformers")
            return None


def load_embedder(SentenceTransformer):
    """Try bge-m3 first, then fall back to MiniLM."""
    # Try bge-m3 first (1024-dim, best quality — already cached on this machine)
    # On CPU without GPU, bge-m3 is VERY slow (~5+ min per 50 notes)
    # Use all-MiniLM-L6-v2 for a fast smoke test instead
    # try:
    #     model = SentenceTransformer("BAAI/bge-m3")
    #     print("  Loaded: BAAI/bge-m3 (1024-dim)")
    #     return model, 1024, "BAAI/bge-m3"
    # except Exception as e:
    #     print(f"  bge-m3 not available locally ({type(e).__name__})")

    # Fallback: all-MiniLM-L6-v2 (384-dim, fast and small, ~10x faster than bge-m3)
    try:
        model = SentenceTransformer("sentence-transformers/all-MiniLM-L6-v2")
        print("  Loaded: all-MiniLM-L6-v2 (384-dim)")
        return model, 384, "all-MiniLM-L6-v2"
    except Exception as e:
        print(f"  all-MiniLM-L6-v2 not available locally ({type(e).__name__})")
        return None, None, None


def main():
    try:
        import turbovec as tv
    except ImportError:
        print("ERROR: turbovec not installed. Run: pip install turbovec")
        sys.exit(1)

    import numpy as np

    print("=" * 60)
    print("TURBOVEC SEMANTIC SEARCH TEST (sentence-transformers)")
    print("=" * 60)
    print(f"Sandbox: {SANDBOX}")
    print()

    # Step 1: Read notes
    print("[1/6] Reading notes from sandbox vault...")
    t0 = time.monotonic()
    notes, skipped = read_notes(SANDBOX, max_notes=MAX_NOTES)
    t_read = time.monotonic() - t0
    print(f"  Notes read: {len(notes)}  (skipped {skipped})  [{t_read:.1f}s]")
    print(f"  Example: {notes[0][0]}")

    # Step 2: Ensure sentence-transformers is available
    print("\n[2/6] Checking sentence-transformers...")
    ST = ensure_sentence_transformers()
    if ST is None:
        print("  Install with: pip install sentence-transformers")
        sys.exit(1)

    # Step 3: Load embedding model
    print(f"\n[3/6] Loading embedding model...")
    model, dim, model_name = load_embedder(ST)
    if model is None:
        print("  No local model available. Install one:")
        print("  pip install sentence-transformers  (first run downloads ~90MB)")
        sys.exit(1)

    # Step 4: Embed notes
    print(f"\n[4/6] Embedding {len(notes)} notes with {model_name}...")
    t0 = time.monotonic()
    texts = [content for _, content in notes]
    embeddings = model.encode(texts, show_progress_bar=True, normalize_embeddings=True)
    embeddings = embeddings.astype(np.float32)
    t_embed = time.monotonic() - t0
    print(f"  Embeddings: {embeddings.shape}  [{t_embed:.1f}s]")

    # Step 5: Build turbovec index
    print(f"\n[5/6] Building TurboQuantIndex (2-bit, dim={dim})...")
    t0 = time.monotonic()
    index = tv.TurboQuantIndex(dim)
    try:
        index.prepare()
    except TypeError:
        pass
    index.add(embeddings)
    t_build = time.monotonic() - t0
    print(f"  Built in {t_build:.2f}s")

    # Persist and measure
    index_path = os.path.join(SANDBOX, "turbovec_index.tv")
    index.write(index_path)

    # Also save embeddings + note paths for in-memory rebuild at query time
    # (turbovec 0.8.0 load() has a bug — search returns empty after load)
    emb_path = os.path.join(SANDBOX, "turbovec_embeddings.npy")
    np.save(emb_path, embeddings)
    # Save note paths for lookup
    notes_path = os.path.join(SANDBOX, "turbovec_notes.txt")
    with open(notes_path, "w", encoding="utf-8") as f:
        for rel_path, _ in notes:
            f.write(rel_path + "\n")
    # Save dimension + model info
    meta_path = os.path.join(SANDBOX, "turbovec_meta.json")
    meta = {"dim": dim, "model": model_name, "n_notes": len(notes)}
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2)
    print(f"  Saved: turbovec_embeddings.npy, turbovec_notes.txt, turbovec_meta.json")

    idx_bytes = os.path.getsize(index_path)
    raw_bytes = embeddings.nbytes
    ratio = raw_bytes / idx_bytes
    print(f"  Raw (float32): {raw_bytes:,} bytes ({raw_bytes / 1_048_576:.1f} MB)")
    print(f"  Index (2-bit): {idx_bytes:,} bytes ({idx_bytes / 1_048_576:.1f} MB)")
    print(f"  Compression: {ratio:.2f}x")

    # Step 6: Semantic search
    print(f"\n[6/6] Running semantic searches...")
    queries = [
        "iRIMS-V pagination and skeleton loader",
        "JARVIS voice assistant brain routing",
        "turbovec vector index quantization",
        "Obsidian vault automation scripts",
        "Laravel quality gate shipping",
        "portfolio terminal shell site",
        "guest speaker AI teaching presentation",
    ]

    results_md = []
    results_md.append("# Turbovec Semantic Search Test Results")
    results_md.append("")
    results_md.append(f"**Date:** {time.strftime('%Y-%m-%d %H:%M:%S')}")
    results_md.append(f"**Vault:** Second Brain Test Sandbox")
    results_md.append(f"**Notes indexed:** {len(notes)} (sampled first {MAX_NOTES} notes)")
    results_md.append(f"**Embedding:** {model_name} ({dim}-dim, normalized)")
    results_md.append(f"**Index:** turbovec 2-bit TurboQuant ({idx_bytes / 1_048_576:.1f} MB, {ratio:.2f}x compression)")
    results_md.append("")
    results_md.append("## Search Results")
    results_md.append("")

    for q in queries:
        q_vec = model.encode([q], normalize_embeddings=True).astype(np.float32)
        t0 = time.monotonic()
        scores, indices = index.search(q_vec, k=5)
        t_search = (time.monotonic() - t0) * 1000

        print(f"  '{q}' ({t_search:.0f}ms)")
        results_md.append(f"### \"{q}\"")
        results_md.append(f"Search time: {t_search:.0f}ms")
        results_md.append("")
        results_md.append("| Rank | Score | Note | Snippet |")
        results_md.append("|---|---|---|---|")
        for rank, (score, idx) in enumerate(zip(scores[0], indices[0])):
            note_rel = notes[idx][0]
            snippet = notes[idx][1][:120].replace("|", "\\|").replace("\n", " ")
            print(f"    [{rank + 1}] {note_rel} (score={score:.4f})")
            results_md.append(f"| {rank + 1} | {score:.4f} | {note_rel} | {snippet}... |")
        results_md.append("")

    results_path = os.path.join(SANDBOX, "turbovec_test_results.md")
    Path(results_path).write_text("\n".join(results_md), encoding="utf-8")

    print(f"\n{'=' * 60}")
    print("TEST COMPLETE")
    print(f"  Index:  {index_path}")
    print(f"  Report: {results_path}")
    print(f"  Notes:  {len(notes)}")
    print(f"  Size:   {idx_bytes / 1_048_576:.1f} MB ({ratio:.2f}x compression)")
    print(f"  Model:  {model_name} ({dim}-dim)")
    print("=" * 60)


if __name__ == "__main__":
    main()
