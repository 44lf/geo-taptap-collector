$ErrorActionPreference = "Stop"

$collectorRoot = [IO.Path]::GetFullPath(
    (Join-Path $PSScriptRoot "..\..")
)
$geoRoot = Join-Path (Split-Path -Parent $collectorRoot) "geo"
$wheelhouse = Join-Path $PSScriptRoot "taptap-import-wheelhouse"
$pythonInstaller = Join-Path $PSScriptRoot "python-3.12.10-amd64.exe"
$installRoot = Join-Path $env:ProgramData "GeoTapTap"
$pythonRoot = Join-Path $installRoot "python312"
$runtimePython = Join-Path $pythonRoot "python.exe"
$oldVenvPath = Join-Path $installRoot "venv"

if (-not (Test-Path -LiteralPath $wheelhouse)) {
    throw "Offline wheelhouse was not found: $wheelhouse"
}
if (-not (Test-Path -LiteralPath $pythonInstaller)) {
    throw "Python 3.12 offline installer was not found: $pythonInstaller"
}
if (-not (Test-Path -LiteralPath (Join-Path $geoRoot "server"))) {
    throw "Sibling GEO repository was not found: $geoRoot"
}

Write-Host "[1/4] Installing the isolated Python 3.12 runtime..."
New-Item -ItemType Directory -Path $installRoot -Force | Out-Null
if (-not (Test-Path -LiteralPath $runtimePython)) {
    $installerArguments = @(
        "/quiet",
        "InstallAllUsers=0",
        "TargetDir=$pythonRoot",
        "AssociateFiles=0",
        "Shortcuts=0",
        "Include_launcher=0",
        "PrependPath=0",
        "Include_doc=0",
        "Include_test=0",
        "Include_pip=1"
    )
    $process = Start-Process `
        -FilePath $pythonInstaller `
        -ArgumentList $installerArguments `
        -Wait `
        -PassThru `
        -WindowStyle Hidden
    if ($process.ExitCode -ne 0 -or -not (Test-Path -LiteralPath $runtimePython)) {
        throw "Python 3.12 installation failed. Exit code: $($process.ExitCode)"
    }
}

Write-Host "[2/4] Verifying the Python runtime version..."
$versionText = & $runtimePython -c "import sys; print('.'.join(map(str, sys.version_info[:3])))"
if ($LASTEXITCODE -ne 0 -or -not $versionText.StartsWith("3.12.")) {
    throw "Expected Python 3.12, found: $versionText"
}
Write-Host "      Python $versionText"

Write-Host "[3/4] Installing dependencies from the offline shared wheelhouse..."
& $runtimePython -m pip install `
    --disable-pip-version-check `
    --no-index `
    --find-links $wheelhouse `
    "SQLAlchemy==2.0.45" `
    "pymysql==1.1.1" `
    "pypinyin==0.54.0" `
    "pydantic==2.13.3" `
    "pydantic-settings==2.14.2" `
    "minio==7.2.20"
if ($LASTEXITCODE -ne 0) {
    throw "Offline dependency installation failed."
}

Write-Host "[4/4] Verifying the environment and GEO importer imports..."
$env:PYTHONPATH = $geoRoot
$env:GEO_DATA_DIR = Join-Path $installRoot "data"
$verification = @'
import sqlalchemy
import pymysql
import pypinyin
import pydantic
import pydantic_settings
import minio
from server.scripts import remote_game_bundle_import
print("GEO importer dependencies and module import: OK")
'@
$encoded = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($verification))
& $runtimePython -c "import base64; exec(base64.b64decode('$encoded'))"
if ($LASTEXITCODE -ne 0) {
    throw "The isolated environment verification failed."
}

$resolvedOldVenv = [IO.Path]::GetFullPath($oldVenvPath)
$resolvedInstallRoot = [IO.Path]::GetFullPath($installRoot)
if ((Test-Path -LiteralPath $resolvedOldVenv) -and
    $resolvedOldVenv.StartsWith($resolvedInstallRoot) -and
    (Split-Path -Leaf $resolvedOldVenv) -eq "venv") {
    Remove-Item -LiteralPath $resolvedOldVenv -Recurse -Force
}

Write-Host ""
Write-Host "DEV IMPORT ENVIRONMENT READY"
Write-Host "Python: $runtimePython"
Read-Host "Press Enter to close"
