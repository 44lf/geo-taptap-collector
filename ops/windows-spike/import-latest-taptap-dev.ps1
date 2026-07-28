$ErrorActionPreference = "Stop"
trap {
    [Console]::Error.WriteLine("")
    [Console]::Error.WriteLine("FATAL: " + $_.Exception.Message)
    [Console]::Error.WriteLine($_.InvocationInfo.PositionMessage)
    exit 1
}

$collectorRoot = [IO.Path]::GetFullPath(
    (Join-Path $PSScriptRoot "..\..")
)
$geoRoot = Join-Path (Split-Path -Parent $collectorRoot) "geo"
$python = Join-Path $env:ProgramData "GeoTapTap\python312\python.exe"
if (-not (Test-Path -LiteralPath $python)) {
    throw "DEV importer Python 3.12 was not found."
}
if (-not (Test-Path -LiteralPath (Join-Path $geoRoot "server"))) {
    throw "Sibling GEO repository was not found: $geoRoot"
}

function Get-Container {
    param([string]$Pattern)
    $line = docker ps --format "{{.ID}}|{{.Image}}|{{.Names}}" |
        Where-Object { $_ -match $Pattern } |
        Select-Object -First 1
    if (-not $line) {
        throw "Required Docker container was not found: $Pattern"
    }
    return ($line -split "\|", 2)[0]
}

function Get-ContainerEnvironment {
    param([string]$ContainerId)
    return @(
        docker inspect `
            --format "{{range .Config.Env}}{{println .}}{{end}}" `
            $ContainerId
    )
}

function Get-EnvironmentValue {
    param([string[]]$Environment, [string[]]$Names)
    foreach ($name in $Names) {
        $prefix = "$name="
        $line = $Environment |
            Where-Object { $_.StartsWith($prefix) } |
            Select-Object -First 1
        if ($line) {
            return $line.Substring($prefix.Length)
        }
    }
    return $null
}

Write-Host "[1/5] Reading DEV-local Docker credentials..."
$mysqlId = Get-Container "(?i)mysql"
$minioId = Get-Container "(?i)minio"
$mysqlEnvironment = Get-ContainerEnvironment $mysqlId
$minioEnvironment = Get-ContainerEnvironment $minioId
$mysqlPassword = Get-EnvironmentValue `
    $mysqlEnvironment `
    @("MYSQL_ROOT_PASSWORD")
$minioAccessKey = Get-EnvironmentValue `
    $minioEnvironment `
    @("MINIO_ROOT_USER", "MINIO_ACCESS_KEY")
$minioSecretKey = Get-EnvironmentValue `
    $minioEnvironment `
    @("MINIO_ROOT_PASSWORD", "MINIO_SECRET_KEY")
if (-not $mysqlPassword -or -not $minioAccessKey -or -not $minioSecretKey) {
    throw "DEV-local Docker credentials could not be resolved."
}

Write-Host "[2/5] Completing the offline runtime dependencies..."
$wheelhouse = Join-Path $PSScriptRoot "taptap-import-wheelhouse"
& $python -m pip install `
    --disable-pip-version-check `
    --no-index `
    --find-links $wheelhouse `
    "pypinyin==0.54.0" | Out-Null
if ($LASTEXITCODE -ne 0) {
    throw "The offline pypinyin dependency could not be installed."
}

Write-Host "[3/5] Configuring the isolated importer process..."
$env:PYTHONPATH = $geoRoot
$env:GEO_DATA_DIR = Join-Path $env:ProgramData "GeoTapTap\data"
$env:GEO_DB_HOST = "127.0.0.1"
$env:GEO_DB_PORT = "3307"
$env:GEO_DB_USER = "root"
$env:GEO_DB_PASS = $mysqlPassword
$env:GEO_DB_NAME = "geo_dev"
$env:GEO_MINIO_ENDPOINT = "127.0.0.1:9000"
$env:GEO_MINIO_ACCESS_KEY = $minioAccessKey
$env:GEO_MINIO_SECRET_KEY = $minioSecretKey
$env:GEO_MINIO_SECURE = "false"
$env:GEO_TAPTAP_INBOX_BUCKET = "geo-taptap-inbox"

Write-Host "[4/5] Validating, dry-running, importing, and verifying..."
& $python -m server.scripts.consume_taptap_inbox_spike
if ($LASTEXITCODE -ne 0) {
    throw "DEV inbox import failed."
}

Write-Host "[5/5] Done."
Write-Host ""
Write-Host "END-TO-END DEV CHANNEL PASSED"
