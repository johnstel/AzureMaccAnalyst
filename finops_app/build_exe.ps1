# ─────────────────────────────────────────────────────────────────────
# build_exe.ps1 — Build the Azure MACC Analyst desktop executable
# ─────────────────────────────────────────────────────────────────────
# Run from the finops_app/ directory:
#   powershell -ExecutionPolicy Bypass -File build_exe.ps1
#
# Output:
#   dist/AzureMaccAnalyst/          — folder with .exe + dependencies
#   dist/AzureMaccAnalyst.zip       — ready-to-share ZIP
# ─────────────────────────────────────────────────────────────────────
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

Write-Host "=== Azure MACC Analyst — Build ===" -ForegroundColor Cyan

# ── 1. Virtual-env ──────────────────────────────────────────────────
Write-Host "[1/5] Creating virtual environment (if missing)..." -ForegroundColor Yellow
if (-not (Test-Path ".venv")) {
    python -m venv .venv
}
& .venv\Scripts\Activate.ps1

# ── 2. Dependencies ────────────────────────────────────────────────
Write-Host "[2/5] Installing dependencies..." -ForegroundColor Yellow
pip install --quiet --upgrade pip
pip install --quiet -r requirements.txt
pip install --quiet pyinstaller

# ── 3. Clean previous build ────────────────────────────────────────
Write-Host "[3/5] Cleaning previous build artifacts..." -ForegroundColor Yellow
if (Test-Path "build") { Remove-Item -Recurse -Force "build" }
if (Test-Path "dist")  { Remove-Item -Recurse -Force "dist" }

# ── 4. PyInstaller ─────────────────────────────────────────────────
Write-Host "[4/5] Running PyInstaller (this may take a few minutes)..." -ForegroundColor Yellow
pyinstaller --noconfirm --clean AzureMaccAnalyst.spec

# Verify output
$exePath = "dist\AzureMaccAnalyst\AzureMaccAnalyst.exe"
if (-not (Test-Path $exePath)) {
    Write-Host "ERROR: Build failed — $exePath not found." -ForegroundColor Red
    exit 1
}
Write-Host "Build succeeded: $exePath" -ForegroundColor Green

# ── 5. Create distribution ZIP ─────────────────────────────────────
Write-Host "[5/5] Creating ZIP archive..." -ForegroundColor Yellow
$zipPath = "dist\AzureMaccAnalyst.zip"
if (Test-Path $zipPath) { Remove-Item $zipPath }
Compress-Archive -Path "dist\AzureMaccAnalyst" -DestinationPath $zipPath
$sizeMB = [math]::Round((Get-Item $zipPath).Length / 1MB, 1)
Write-Host ""
Write-Host "Distribution ready: $zipPath ($sizeMB MB)" -ForegroundColor Green
Write-Host ""
Write-Host "Share $zipPath with your finance team." -ForegroundColor Cyan
Write-Host "They extract the ZIP and double-click AzureMaccAnalyst.exe." -ForegroundColor Cyan
