# GraphSleuth — one-shot setup + demo run for Windows PowerShell
# Usage:  powershell -ExecutionPolicy Bypass -File .\setup.ps1
#
# Does everything end to end:
#   1. creates .venv if missing and installs dependencies
#   2. creates .env from .env.example if missing
#   3. launches Docker Desktop if the engine is not running and waits for it
#   4. brings FalkorDB up via docker compose and waits until it answers
#   5. seeds the fraud graph
#   6. runs the investigation demo and saves the report

$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

Write-Host "== GraphSleuth setup ==" -ForegroundColor Cyan

# --- 1. virtual environment + dependencies ---------------------------------
if (-not (Test-Path ".venv")) {
    Write-Host "Creating virtual environment ..."
    py -m venv .venv
}

$python = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"

Write-Host "Installing dependencies ..."
& $python -m pip install --quiet --upgrade pip
& $python -m pip install --quiet -r requirements.txt

# --- 2. config file ---------------------------------------------------------
if (-not (Test-Path ".env")) {
    Copy-Item ".env.example" ".env"
    Write-Host "Created .env (add GROQ_API_KEY — free at console.groq.com — for the real model)."
}

# --- 3. Docker engine -------------------------------------------------------
$docker = "C:\Program Files\Docker\Docker\resources\bin\docker.exe"
$desktop = "C:\Program Files\Docker\Docker\Docker Desktop.exe"

function Test-DockerEngine {
    try {
        & $docker info *> $null
        return ($LASTEXITCODE -eq 0)
    } catch {
        return $false
    }
}

if (-not (Test-DockerEngine)) {
    Write-Host "Starting Docker Desktop (accept any first-run prompts) ..."
    Start-Process -FilePath $desktop
    Write-Host "Waiting for the Docker engine (up to ~5 minutes on first start) ..."
    $ready = $false
    for ($i = 0; $i -lt 60; $i++) {
        if (Test-DockerEngine) { $ready = $true; break }
        Start-Sleep -Seconds 5
    }
    if (-not $ready) {
        Write-Host "Docker engine did not start." -ForegroundColor Red
        Write-Host "  * If Docker asks about WSL 2, let it install the kernel, or switch to"
        Write-Host "    Settings > General > uncheck 'Use the WSL 2 based engine' (Hyper-V)."
        Write-Host "  * If WSL is missing, run in an ADMIN terminal:  wsl --update"
        exit 1
    }
}
Write-Host "Docker engine is up." -ForegroundColor Green

# --- 4. FalkorDB ------------------------------------------------------------
Write-Host "Starting FalkorDB ..."
& $docker compose up -d

Write-Host "Waiting for FalkorDB to accept connections ..."
$ok = $false
for ($i = 0; $i -lt 30; $i++) {
    & $python -m graphsleuth status *> $null
    if ($LASTEXITCODE -eq 0) { $ok = $true; break }
    Start-Sleep -Seconds 2
}

if (-not $ok) {
    Write-Host "FalkorDB did not come up. Container logs:" -ForegroundColor Red
    & $docker compose logs --tail 40
    exit 1
}

# --- 5. seed ---------------------------------------------------------------
Write-Host "Seeding the fraud graph ..."
& $python -m graphsleuth seed

# --- 6. demo ---------------------------------------------------------------
Write-Host ""
Write-Host "Running the investigation demo ..." -ForegroundColor Cyan
New-Item -ItemType Directory -Force -Path "demos\out" | Out-Null
& $python -m graphsleuth demo --target ACC-9003 --save demos\out\report.md

Write-Host ""
Write-Host "Done. Report: demos\out\report.md" -ForegroundColor Green
Write-Host "Two-agent pipeline:  $python -m graphsleuth pipeline --target ACC-9003"
Write-Host "Concurrency bench:   $python -m graphsleuth bench"
Write-Host "Interactive mode:    $python -m graphsleuth chat"
