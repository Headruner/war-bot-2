# Pulls the published EVE Distance Meter image (GHCR) and restarts it in Docker Desktop, then checks it is healthy.
# Nothing is built from the source code unless -Build is given.
# Run from anywhere (PowerShell):
#   powershell -ExecutionPolicy Bypass -File C:\Users\<you>\source\repos\evedm\scripts\update.ps1
# Options: -Build (build the image from this checkout instead of pulling)
#          -Push (git push the checkout to origin first)   -Branch <name> (remote branch for -Push, default main)
#          -NoPush (kept for old command lines; pushing is off by default now)
# The image is $env:EVEDM_IMAGE, else EVEDM_IMAGE from .env, else ghcr.io/headruner/war-bot-2:latest.
param([switch]$Build, [switch]$Push, [switch]$NoPush, [string]$Branch = "main")
$ErrorActionPreference = "Continue"   # native tools write progress to stderr; exit codes are checked instead
Set-Location (Join-Path $PSScriptRoot "..")

function Step($text) { Write-Host ""; Write-Host "== $text" -ForegroundColor Cyan }
function Fail($text) { Write-Host $text -ForegroundColor Red; exit 1 }

docker version --format "{{.Server.Os}}" > $null 2>&1
if ($LASTEXITCODE -ne 0) { Fail "Docker Desktop is not running (or not in Linux containers mode). Start it and run this again." }
if (-not (Test-Path ".env")) { Fail ".env is missing: copy .env.example to .env and fill it in (see Run on Windows in README.md)." }

if ($Push -and -not $NoPush) {
    Step "git push origin HEAD:$Branch"
    git push -u origin "HEAD:$Branch"
    if ($LASTEXITCODE -ne 0) { Fail "git push failed (sign in to GitHub in this terminal, or run without -Push)." }
}

if ($Build) {
    Step "docker compose up -d --build   (image built from this checkout)"
    docker compose up -d --build
} else {
    if (-not $env:EVEDM_IMAGE -and -not (Select-String -Path ".env" -Pattern '^\s*EVEDM_IMAGE\s*=' -Quiet)) {
        $env:EVEDM_IMAGE = "ghcr.io/headruner/war-bot-2:latest"
    }
    $img = if ($env:EVEDM_IMAGE) { $env:EVEDM_IMAGE } else { "EVEDM_IMAGE from .env" }
    Step "docker compose pull   ($img)"
    docker compose pull evedm
    if ($LASTEXITCODE -ne 0) { Fail "Pull failed. A private package needs 'docker login ghcr.io' (token with read:packages), or make the package public." }
    Step "docker compose up -d --no-build"
    docker compose up -d --no-build
}
if ($LASTEXITCODE -ne 0) { Fail "docker compose failed: see the output above." }

Step "waiting for the container to become healthy"
$deadline = (Get-Date).AddMinutes(3)
$status = ""
do {
    Start-Sleep -Seconds 3
    $status = (docker inspect -f "{{.State.Health.Status}}" evedm 2> $null | Out-String).Trim()
    Write-Host "  $status"
} until ($status -eq "healthy" -or (Get-Date) -gt $deadline)
if ($status -ne "healthy") {
    docker compose logs --tail 40 evedm
    Fail "The container is '$status' after 3 minutes: see the log above."
}

Step "version and health"
$mapping = (docker compose port evedm 8080 | Out-String).Trim()   # e.g. 0.0.0.0:8081
$port = $mapping.Split(":")[-1]
try {
    $health = Invoke-RestMethod "http://localhost:$port/api/health" -TimeoutSec 10
    Write-Host ("  evedm {0}, status {1}, mode {2}, database {3}" -f $health.version, $health.status, $health.mode, $health.db.journal_mode)
} catch {
    Fail "The app does not answer on http://localhost:${port}: $_"
}
Write-Host ""
Write-Host "Done. Open http://localhost:$port" -ForegroundColor Green
