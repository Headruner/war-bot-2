# Exports the real notification bodies stored by the running Docker container to data\notification-samples.jsonl
# (git-ignored), for scripts\check_notification_samples.py and tests\unit\test_real_notifications.py.
# The file holds raw ESI text with character and structure IDs: keep it private, do not commit or share it.
# Run from anywhere (PowerShell):
#   powershell -NoProfile -ExecutionPolicy Bypass -File C:\Users\<you>\source\repos\evedm\scripts\export-notification-samples.ps1
# Options: -Types <a,b,...> (notification types, default all)   -Days <n> (look back, default 90)
#          -Limit <n> (newest n per type, default all)   -Service <name> (compose service, default evedm)
param([string]$Types = "", [double]$Days = 90, [int]$Limit = 0, [string]$Service = "evedm")
$ErrorActionPreference = "Continue"
Set-Location (Join-Path $PSScriptRoot "..")

$cmd = @("compose", "exec", "-T", $Service, "evedm", "notifications", "samples", "--days", "$Days", "--limit", "$Limit")
if ($Types) { $cmd += @("--types", $Types) }
New-Item -ItemType Directory -Force -Path "data" | Out-Null
$out = Join-Path (Get-Location) "data\notification-samples.jsonl"
# UTF-8 without BOM, one JSON object per line
$lines = & docker @cmd
if ($LASTEXITCODE -ne 0) { Write-Host "export failed: is the container running (docker compose ps)?" -ForegroundColor Red; exit 1 }
[System.IO.File]::WriteAllLines($out, [string[]]@($lines), (New-Object System.Text.UTF8Encoding($false)))
Write-Host "wrote $(@($lines).Count) notifications to $out" -ForegroundColor Green
Write-Host "check them:  .venv\Scripts\python scripts\check_notification_samples.py"
