# Checks and guidance for Windows. Not an installer — it prints the command and
# lets you run it yourself. Nothing is installed without your say-so.
$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")

Write-Host "Video to LLM - Windows setup check"
Write-Host ""

$missing = @()

if (-not (Get-Command ffmpeg -ErrorAction SilentlyContinue)) {
    $missing += "FFmpeg:  winget install Gyan.FFmpeg"
}
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    $missing += "uv:      winget install astral-sh.uv"
}

if ($missing.Count -gt 0) {
    Write-Host "Missing. Run:"
    foreach ($item in $missing) { Write-Host "  $item" }
    Write-Host ""
    Write-Host "Then open a new terminal (so PATH updates) and run this again."
    exit 1
}

Write-Host "Installing this project's dependencies with uv..."
uv sync

Write-Host ""
Write-Host "Checking..."
uv run video-to-llm doctor
