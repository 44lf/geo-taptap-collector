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
$targetSource = Join-Path $PSScriptRoot "taptap-dev-targets.transfer.json"
$collectorScript = Join-Path $collectorRoot "collector.ps1"
$collectorTarget = Join-Path $collectorRoot "targets.json"
$credentialPath = Join-Path $env:ProgramData "GeoTapTap\minio-uploader.env"
$mcCandidates = @(
    (Join-Path $PSScriptRoot "mc.exe"),
    (Join-Path $PSScriptRoot "mc-legacy.exe")
)

foreach ($requiredPath in @($targetSource, $collectorScript, $credentialPath)) {
    if (-not (Test-Path -LiteralPath $requiredPath)) {
        throw "Required file was not found: $requiredPath"
    }
}

function Read-KeyValueFile {
    param([string]$Path)
    $values = @{}
    foreach ($line in Get-Content -LiteralPath $Path) {
        $separatorIndex = $line.IndexOf("=")
        if ($separatorIndex -lt 1) {
            continue
        }
        $key = $line.Substring(0, $separatorIndex).Trim()
        $value = $line.Substring($separatorIndex + 1).Trim()
        $values[$key] = $value
    }
    return $values
}

function Get-Sha256 {
    param([string]$Path)
    return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
}

$temporaryDirectory = Join-Path `
    ([IO.Path]::GetTempPath()) `
    ("geo-taptap-bundle-upload-" + [guid]::NewGuid())
New-Item -ItemType Directory -Path $temporaryDirectory | Out-Null

try {
    Write-Host "[1/6] Preparing the exported DEV target..."
    $target = Get-Content -LiteralPath $targetSource -Raw | ConvertFrom-Json
    $bundleId = [string]$target.bundle_id
    if ([string]::IsNullOrWhiteSpace($bundleId) -or
        $bundleId -notmatch "^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$") {
        throw "The exported bundle_id is missing or unsafe."
    }
    if (@($target.targets).Count -lt 1 -or @($target.targets).Count -gt 20) {
        throw "The target file must contain between 1 and 20 games."
    }
    Copy-Item -LiteralPath $targetSource -Destination $collectorTarget -Force

    Write-Host "[2/6] Running the real TapTap collector..."
    & powershell.exe `
        -NoProfile `
        -ExecutionPolicy Bypass `
        -File $collectorScript `
        -Manifest $collectorTarget
    $collectorExitCode = $LASTEXITCODE
    if ($collectorExitCode -ne 0) {
        throw "Collector stopped with exit code $collectorExitCode. No bundle was uploaded."
    }

    $bundleRoot = Join-Path (Join-Path $collectorRoot "output") $bundleId
    $manifestPath = Join-Path $bundleRoot "manifest.json"
    $reportPath = Join-Path $bundleRoot "report.json"
    foreach ($requiredOutput in @($manifestPath, $reportPath)) {
        if (-not (Test-Path -LiteralPath $requiredOutput)) {
            throw "Collector output is incomplete: $requiredOutput"
        }
    }

    Write-Host "[3/6] Creating the transport archive..."
    $archivePath = Join-Path $temporaryDirectory "$bundleId.zip"
    Add-Type -AssemblyName System.IO.Compression
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    [System.IO.Compression.ZipFile]::CreateFromDirectory(
        $bundleRoot,
        $archivePath,
        [System.IO.Compression.CompressionLevel]::Optimal,
        $false
    )
    $archiveFile = Get-Item -LiteralPath $archivePath
    $archiveSha256 = Get-Sha256 $archivePath

    Write-Host "[4/6] Selecting the compatible MinIO client..."
    $mcPath = $null
    foreach ($candidate in $mcCandidates) {
        if (-not (Test-Path -LiteralPath $candidate)) {
            continue
        }
        $candidateCopy = Join-Path $temporaryDirectory ([IO.Path]::GetFileName($candidate))
        Copy-Item -LiteralPath $candidate -Destination $candidateCopy
        try {
            & $candidateCopy --version | Out-Null
            if ($LASTEXITCODE -eq 0) {
                $mcPath = $candidateCopy
                break
            }
        }
        catch {}
    }
    if (-not $mcPath) {
        throw "No compatible MinIO client can run on this Windows server."
    }

    $credentials = Read-KeyValueFile $credentialPath
    foreach ($requiredKey in @("Endpoint", "Bucket", "AccessKey", "SecretKey")) {
        if (-not $credentials.ContainsKey($requiredKey) -or
            [string]::IsNullOrWhiteSpace($credentials[$requiredKey])) {
            throw "Installed uploader credentials are missing: $requiredKey"
        }
    }

    $mcConfigPath = Join-Path $temporaryDirectory "mc-config"
    & $mcPath `
        --config-dir $mcConfigPath `
        alias set local `
        $credentials.Endpoint `
        $credentials.AccessKey `
        $credentials.SecretKey | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "Unable to authenticate to MinIO."
    }

    Write-Host "[5/6] Uploading the bundle archive..."
    $transportId = [guid]::NewGuid().ToString("N")
    $archiveObject = "incoming/$bundleId/$transportId.zip"
    & $mcPath `
        --config-dir $mcConfigPath `
        cp $archivePath `
        "local/$($credentials.Bucket)/$archiveObject"
    if ($LASTEXITCODE -ne 0) {
        throw "Bundle archive upload failed. No ready marker was created."
    }

    Write-Host "[6/6] Publishing the atomic ready marker..."
    $readyObject = "ready/$bundleId-$transportId.json"
    $readyPath = Join-Path $temporaryDirectory "ready.json"
    $ready = [ordered]@{
        schema_version = 1
        bundle_id = $bundleId
        archive_object = $archiveObject
        archive_sha256 = $archiveSha256
        archive_bytes = [long]$archiveFile.Length
        created_at = (Get-Date).ToUniversalTime().ToString("o")
        collector_host = $env:COMPUTERNAME
    }
    $readyJson = $ready | ConvertTo-Json -Depth 5
    [IO.File]::WriteAllText(
        $readyPath,
        $readyJson,
        (New-Object Text.UTF8Encoding($false))
    )
    & $mcPath `
        --config-dir $mcConfigPath `
        cp $readyPath `
        "local/$($credentials.Bucket)/$readyObject"
    if ($LASTEXITCODE -ne 0) {
        throw "Ready marker upload failed. The archive remains unannounced in incoming."
    }

    Write-Host ""
    Write-Host "COLLECT AND UPLOAD PASSED"
    Write-Host "Bundle: $bundleId"
    Write-Host "Archive: $($credentials.Bucket)/$archiveObject"
    Write-Host "Ready: $($credentials.Bucket)/$readyObject"
    Write-Host "SHA256: $archiveSha256"
}
finally {
    $resolvedTemporaryDirectory = [IO.Path]::GetFullPath($temporaryDirectory)
    $resolvedTempRoot = [IO.Path]::GetFullPath([IO.Path]::GetTempPath())
    if ($resolvedTemporaryDirectory.StartsWith($resolvedTempRoot) -and
        (Split-Path -Leaf $resolvedTemporaryDirectory).StartsWith(
            "geo-taptap-bundle-upload-"
        )) {
        Remove-Item `
            -LiteralPath $resolvedTemporaryDirectory `
            -Recurse `
            -Force `
            -ErrorAction SilentlyContinue
    }
}
