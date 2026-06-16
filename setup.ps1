# Bootstrap the Confluence Toolkit on Windows.
# Creates a user-level .venv — no global installs, no admin rights required.
# Run once from the portable-confluence-toolkit\ directory (or the repo root).
# Usage: .\setup.ps1

param()

$ErrorActionPreference = 'Stop'
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ScriptDir

Write-Host "=== Confluence Toolkit Setup ===" -ForegroundColor Cyan

# ── 1. Locate Python ─────────────────────────────────────────────────────────
# Try py launcher first (standard on Windows), fall back to python3 / python
$python = $null
foreach ($cmd in @('py', 'python3', 'python')) {
    try {
        $ver = & $cmd --version 2>&1
        if ($ver -match 'Python 3') {
            $python = $cmd
            Write-Host "Using Python: $ver ($cmd)"
            break
        }
    } catch { }
}
if (-not $python) {
    Write-Error "Python 3 not found. Install from https://python.org and re-run."
    exit 1
}

# ── 2. Create virtual environment ────────────────────────────────────────────
if (-not (Test-Path '.venv')) {
    Write-Host "Creating .venv..."
    & $python -m venv .venv
} else {
    Write-Host ".venv already exists — skipping creation."
}

# ── 3. Upgrade pip inside the venv ───────────────────────────────────────────
Write-Host "Upgrading pip..."
& .venv\Scripts\python.exe -m pip install --upgrade pip --quiet

# ── 4. Install dependencies ───────────────────────────────────────────────────
Write-Host "Installing requirements..."
& .venv\Scripts\pip.exe install -r requirements.txt --quiet

# ── 5. Create .env from template if not present ──────────────────────────────
if (-not (Test-Path '.env')) {
    if (Test-Path '.env.example') {
        Copy-Item '.env.example' '.env'
        Write-Host ""
        Write-Host "Created .env from .env.example." -ForegroundColor Yellow
        Write-Host "  -> Edit .env and set CONFLUENCE_API_TOKEN before running any scripts." -ForegroundColor Yellow
    } else {
        Write-Host "Warning: .env.example not found. Create .env manually." -ForegroundColor Yellow
    }
} else {
    Write-Host ".env already exists — skipping."
}

# ── 6. Smoke test instructions ────────────────────────────────────────────────
Write-Host ""
Write-Host "=== Setup complete ===" -ForegroundColor Green
Write-Host ""
Write-Host "To verify your credentials, run a download of a known page:"
Write-Host "  .venv\Scripts\python.exe scripts\download_confluence.py <PAGE_ID> --env-file .env --output-dir workspace"
Write-Host ""
Write-Host "If you see page content in workspace\, your auth is working."
