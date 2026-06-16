#!/usr/bin/env bash
# Bootstrap the Confluence Toolkit on macOS / Linux.
# Creates a user-level .venv — no global installs, no admin rights required.
# Run once from the portable-confluence-toolkit/ directory (or the repo root).

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo "=== Confluence Toolkit Setup ==="

# ── 1. Create virtual environment ───────────────────────────────────────────
if [ ! -d ".venv" ]; then
    echo "Creating .venv..."
    python3 -m venv .venv
else
    echo ".venv already exists — skipping creation."
fi

# ── 2. Upgrade pip inside the venv ──────────────────────────────────────────
# --no-user overrides any 'user = true' in the global pip config, which is
# incompatible with venv installs and causes an immediate failure.
echo "Upgrading pip..."
.venv/bin/python3 -m pip install --upgrade pip --quiet --no-user

# ── 3. Install dependencies ──────────────────────────────────────────────────
echo "Installing requirements..."
.venv/bin/pip install -r requirements.txt --quiet --no-user

# ── 4. Create .env from template if not present ──────────────────────────────
if [ ! -f ".env" ]; then
    if [ -f ".env.example" ]; then
        cp .env.example .env
        echo ""
        echo "Created .env from .env.example."
        echo "  → Edit .env and set CONFLUENCE_API_TOKEN before running any scripts."
    else
        echo "Warning: .env.example not found. Create .env manually."
    fi
else
    echo ".env already exists — skipping."
fi

# ── 5. Smoke test (optional) ─────────────────────────────────────────────────
echo ""
echo "=== Setup complete ==="
echo ""
echo "To verify your credentials, run a download of a known page:"
echo "  .venv/bin/python3 scripts/download_confluence.py <PAGE_ID> --env-file .env --output-dir workspace"
echo ""
echo "If you see page content in workspace/, your auth is working."
