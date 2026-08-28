#!/usr/bin/env bash
# scripts/pi_try.sh — INTERACTIVE Pi playground (for hands-on testing).
# Runs Pi's interactive TUI inside Docker, routed through 9Router (no API key
# needed). The container IS the security boundary — only the mounted scratch
# dir is visible to Pi. MUST be run from your own terminal (needs a real TTY);
# Hermes's own terminal can't drive an interactive TUI.
#
# Usage:
#   bash scripts/pi_try.sh            # uses jarvis-demo/pi_playground
#   bash scripts/pi_try.sh /path/to/scratch   # use your own scratch dir
#
# Tip: drop files into the scratch dir first (or let Pi create them), then
# ask Pi to read/edit them. Type /exit or Ctrl-C to leave.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PLAYGROUND="${1:-$SCRIPT_DIR/pi_playground}"
mkdir -p "$PLAYGROUND"
MODELS_JSON="$SCRIPT_DIR/pi_models.json"

echo "[pi-try] launching interactive Pi (sandboxed, via 9Router) in: $PLAYGROUND" >&2

exec docker run -it --rm \
  -v "${PLAYGROUND}:/work:rw" \
  -v "${MODELS_JSON}:/root/.pi/agent/models.json:ro" \
  -w /work \
  --memory=2g --cpus=2 \
  "jarvis-pi:0.84.3" \
  --model "ag/claude-sonnet-4-6" \
  --approve \
  --no-session
