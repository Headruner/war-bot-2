# Pushes the code to GitHub, rebuilds and restarts EVE Distance Meter in Docker Desktop, then checks it is healthy.
# Run from anywhere (PowerShell):
#   powershell -ExecutionPolicy Bypass -File C:\Users\<you>\source\repos\evedm\scripts\update.ps1
# Options: -NoPush (only rebuild and restart)   -Branch <name> (remote branch, default feat/accounts-wars)
param([switch]$NoPush, [string]$Branch = "feat/accounts-wars")
$ErrorActionPreference = "Continue"   # native tools write progress to stderr; exit codes are checked instead
Set-Location (Join-Path $PSScriptRoot "..")

function Step($text) { Write-Host ""; Write-Host "== $text" -ForegroundColor Cyan }
function Fail($text) { Write-Host $text -ForegroundColor Red; exit 1 }

docker version --format "{{.Server.Os}}" > $null 2>&1
if ($LASTEXITCODE -ne 0) { Fail "Docker Desktop is not running (or not in Linux containers mode). Start it and run this again." }
if (-not (Test-Path ".env")) { Fail ".env is missing: copy .env.example to .env and fill it in (see docs\wiki\07-Run-on-Windows.md)." }

if (-not $NoPush) {
    Step "git push origin HEAD:$Branch"
    git push -u origin "HEAD:$Branch"
    if ($LASTEXITCODE -ne 0) { Fail "git push failed (sign in to GitHub in this terminal, or run with -NoPush)." }
}

Step "docker compose up -d --build"
docker compose up -d --build
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
