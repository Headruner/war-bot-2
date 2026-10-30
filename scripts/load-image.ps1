# Joins image\evedm-image-0.3.0-amd64.tar.gz.part-* (if split), checks SHA-256 and loads it into Docker Desktop.
$ErrorActionPreference = "Stop"
$dir = Join-Path $PSScriptRoot "..\image"
$file = Join-Path $dir "evedm-image-0.3.0-amd64.tar.gz"
if (-not (Test-Path $file)) {
    $out = [System.IO.File]::Create($file)
    Get-ChildItem "$file.part-*" | Sort-Object Name | ForEach-Object {
        $bytes = [System.IO.File]::ReadAllBytes($_.FullName); $out.Write($bytes, 0, $bytes.Length)
    }
    $out.Close()
}
$expected = (Get-Content "$file.sha256").Split(" ")[0]
$actual = (Get-FileHash $file -Algorithm SHA256).Hash.ToLower()
if ($expected -ne $actual) { throw "checksum mismatch: $actual" }
docker load -i $file
