# Renders docs/c4/*.puml to docs/diagrams/*.svg and *.png (Windows), one file at a time,
# and prints PlantUML's own message for any file that fails.
# Uses $env:PLANTUML_JAR (java -jar) when set, otherwise the plantuml command.
# Run:  powershell -NoProfile -ExecutionPolicy Bypass -File scripts\render-diagrams.ps1
$ErrorActionPreference = 'Continue'
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
$out = Join-Path $root 'docs\diagrams'
New-Item -ItemType Directory -Force -Path $out | Out-Null
# 1) a valid $env:PLANTUML_JAR, 2) the plantuml command, 3) download the jar once to %LOCALAPPDATA%\plantuml
$jar = $null
if ($env:PLANTUML_JAR -and (Test-Path $env:PLANTUML_JAR)) {
    $jar = $env:PLANTUML_JAR
} elseif ($env:PLANTUML_JAR) {
    Write-Host "PLANTUML_JAR points to a missing file ($env:PLANTUML_JAR); ignoring it." -ForegroundColor Yellow
}
if (-not $jar -and -not (Get-Command plantuml -ErrorAction SilentlyContinue)) {
    $dir = Join-Path $env:LOCALAPPDATA 'plantuml'
    $jar = Join-Path $dir 'plantuml-1.2024.7.jar'
    if (-not (Test-Path $jar)) {
        New-Item -ItemType Directory -Force -Path $dir | Out-Null
        Write-Host "Downloading plantuml-1.2024.7.jar to $dir ..."
        Invoke-WebRequest -UseBasicParsing -OutFile $jar -Uri 'https://github.com/plantuml/plantuml/releases/download/v1.2024.7/plantuml-1.2024.7.jar'
    }
}
if (-not (Get-Command java -ErrorAction SilentlyContinue) -and $jar) { throw 'Java is not on PATH (winget install EclipseAdoptium.Temurin.21.JRE).' }
if ($jar) { $cmd = 'java'; $pre = @('-Djava.awt.headless=true', '-jar', $jar) } else { $cmd = 'plantuml'; $pre = @() }
# Graphviz (dot) is needed for most diagrams; without it fall back to PlantUML's built-in Smetana layout.
if (-not (Get-Command dot -ErrorAction SilentlyContinue) -and -not $env:GRAPHVIZ_DOT) {
    Write-Host 'Graphviz (dot) not found: using -Playout=smetana (layout may differ). Install: winget install Graphviz.Graphviz' -ForegroundColor Yellow
    $pre += '-Playout=smetana'
}
Write-Host "Using: $cmd $($pre -join ' ')"
& $cmd @pre -version 2>&1 | Select-Object -First 4 | ForEach-Object { Write-Host $_ }
$failed = @()
foreach ($f in Get-ChildItem (Join-Path $root 'docs\c4') -Filter *.puml) {
    foreach ($fmt in '-tsvg', '-tpng') {
        $log = & $cmd @pre $fmt -o $out $f.FullName 2>&1
        if ($LASTEXITCODE -ne 0) {
            $failed += "$($f.Name) $fmt"
            Write-Host "FAILED $($f.Name) $fmt (exit $LASTEXITCODE)" -ForegroundColor Red
            $log | ForEach-Object { Write-Host "    $_" }
        }
    }
}
if ($failed.Count) { Write-Host "`n$($failed.Count) render(s) failed" -ForegroundColor Red; exit 1 }
Get-ChildItem $out | Select-Object -ExpandProperty Name
