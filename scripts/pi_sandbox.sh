#!/usr/bin/env bash
# scripts/pi_sandbox.sh — run Pi coding-agent inside Docker, routed through 9Router.
# Usage: pi_sandbox.sh <repo_abs_path> <prompt> [timeout_sec]
# Security: Pi has NO built-in permission system. The Docker container IS the
# boundary — only the explicitly mounted repo is visible, no host perms.
#
# IMPORTANT (Windows/WSL2 Docker Desktop): the repo path MUST be under a
# Docker-bridgeable location (e.g. /c/Users/...). Paths like /tmp/... do NOT
# round-trip back to the host filesystem in this setup — writes happen inside
# the container but are invisible on the host. Keep scratch repos under
# /c/Users/... (or copy the repo into the image) to see results on the host.
set -euo pipefail

REPO="${1:?usage: pi_sandbox.sh <repo> <prompt> [timeout]}"
PROMPT="${2:?prompt required}"
TIMEOUT="${3:-600}"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# 9Router is declared as a custom OpenAI-compatible provider in pi_models.json
# (mounted into ~/.pi/agent/models.json).
MODELS_JSON="$SCRIPT_DIR/pi_models.json"
PI_MODEL="${PI_MODEL:-ag/gemini-3-flash}"

echo "[pi-sandbox] 9Router via custom provider, model=${PI_MODEL} repo=${REPO}" >&2

# Run via bash -c "cd /work && pi ..." — Pi's print-mode resolves the working
# directory correctly only when launched from the mounted repo root. Launching
# `pi` directly (without cd) caused path confusion and edits that didn't
# round-trip to the host.
exec docker run --rm \
  -v "${REPO}:/work:rw" \
  -v "${MODELS_JSON}:/root/.pi/agent/models.json:ro" \
  --memory=2g --cpus=2 \
  "jarvis-pi:0.84.3" \
  bash -c "cd /work && pi --model '${PI_MODEL}' --approve --session-dir /tmp/pi-session --no-session -p '${PROMPT}'"
