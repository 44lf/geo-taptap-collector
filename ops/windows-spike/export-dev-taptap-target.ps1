$ErrorActionPreference = "Stop"

$collectorRoot = [IO.Path]::GetFullPath(
    (Join-Path $PSScriptRoot "..\..")
)
$geoRoot = Join-Path (Split-Path -Parent $collectorRoot) "geo"
$python = Join-Path $env:ProgramData "GeoTapTap\python312\python.exe"
$outputPath = Join-Path $PSScriptRoot "taptap-dev-targets.transfer.json"

if (-not (Test-Path -LiteralPath $python)) {
    throw "DEV importer Python 3.12 was not found. Run install-dev-import-environment.cmd first."
}
if (-not (Test-Path -LiteralPath (Join-Path $geoRoot "server"))) {
    throw "Sibling GEO repository was not found: $geoRoot"
}

Write-Host "[1/3] Reading the DEV MySQL container configuration..."
$container = docker ps --format "{{.ID}}|{{.Image}}|{{.Names}}" |
    Where-Object { $_ -match "(?i)mysql" } |
    Select-Object -First 1
if (-not $container) {
    throw "No running MySQL container was found."
}
$containerId = ($container -split "\|", 2)[0]
$containerEnvironment = docker inspect --format "{{range .Config.Env}}{{println .}}{{end}}" $containerId

function Get-ContainerEnvironmentValue {
    param([string[]]$Environment, [string[]]$Names)
    foreach ($name in $Names) {
        $prefix = "$name="
        $line = $Environment | Where-Object { $_.StartsWith($prefix) } | Select-Object -First 1
        if ($line) {
            return $line.Substring($prefix.Length)
        }
    }
    return $null
}

$dbUser = "root"
$dbPassword = Get-ContainerEnvironmentValue `
    -Environment $containerEnvironment `
    -Names @("MYSQL_ROOT_PASSWORD")
if (-not $dbPassword) {
    throw "MySQL credentials were not found in the container environment."
}

Write-Host "[2/3] Exporting one active game from geo_dev (read-only)..."
$env:PYTHONPATH = $geoRoot
$env:GEO_DATA_DIR = Join-Path $env:ProgramData "GeoTapTap\data"
$env:GEO_DB_HOST = "127.0.0.1"
$env:GEO_DB_PORT = "3307"
$env:GEO_DB_USER = $dbUser
$env:GEO_DB_PASS = $dbPassword
$env:GEO_DB_NAME = "geo_dev"

& $python -m server.scripts.export_game_backfill_targets `
    $outputPath `
    --limit 1 `
    --max-screenshots 2 `
    --delay-seconds 5
if ($LASTEXITCODE -ne 0) {
    throw "DEV target export failed."
}

Write-Host "[3/3] Done."
Write-Host ""
Write-Host "DEV TARGET EXPORTED"
Write-Host "Switch to 172.25.25.102 for the real collector run."
Write-Host "No database password was written to the shared drive."
Read-Host "Press Enter to close"
