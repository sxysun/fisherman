Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$Repo = Split-Path -Parent $PSScriptRoot
Set-Location $Repo

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
  Write-Host "uv is not installed. Install it from https://docs.astral.sh/uv/ and re-run this script."
  exit 1
}

Write-Host "==> Installing Fisherman with alpha desktop extras"
uv sync --extra desktop

Write-Host ""
Write-Host "==> Checking alpha desktop dependencies"
uv run fisherman desktop-alpha-doctor

Write-Host ""
Write-Host "Optional Windows packages commonly needed for better alpha behavior:"
Write-Host "  winget install UB-Mannheim.TesseractOCR"
Write-Host ""
Write-Host "Run:"
Write-Host "  uv run fisherman desktop-alpha-report --output-dir fisherman-alpha-report"
Write-Host "  uv run fisherman desktop-alpha-smoke --output fisherman-alpha-smoke.jpg"
Write-Host "  uv run fisherman start"
Write-Host "  uv run fisherman desktop-alpha"
