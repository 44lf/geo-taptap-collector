$ErrorActionPreference = "Stop"

$transferPath = Join-Path $PSScriptRoot "geo-taptap-uploader-credentials.transfer.txt"
$mcSourcePaths = @(
    (Join-Path $PSScriptRoot "mc.exe"),
    (Join-Path $PSScriptRoot "mc-legacy.exe")
)

if (-not (Test-Path -LiteralPath $transferPath)) {
    throw "Transfer credential file not found. Run export-uploader-credentials.cmd on DEV first."
}

$values = @{}
foreach ($line in Get-Content -LiteralPath $transferPath) {
    $separatorIndex = $line.IndexOf("=")
    if ($separatorIndex -lt 1) {
        continue
    }
    $key = $line.Substring(0, $separatorIndex).Trim()
    $value = $line.Substring($separatorIndex + 1).Trim()
    $values[$key] = $value
}

foreach ($requiredKey in @("Endpoint", "Bucket", "AccessKey", "SecretKey")) {
    if (-not $values.ContainsKey($requiredKey) -or -not $values[$requiredKey]) {
        throw "The credential file is missing: $requiredKey"
    }
}

$installDirectory = Join-Path $env:ProgramData "GeoTapTap"
$installedCredentialPath = Join-Path $installDirectory "minio-uploader.env"
$temporaryDirectory = Join-Path ([IO.Path]::GetTempPath()) ("geo-taptap-upload-test-" + [guid]::NewGuid())
New-Item -ItemType Directory -Path $temporaryDirectory | Out-Null
$mcConfigPath = Join-Path $temporaryDirectory "mc-config"
$probePath = Join-Path $temporaryDirectory "channel-probe.json"

try {
    Write-Host "[1/4] Selecting a MinIO client compatible with this Windows server..."
    $mcPath = $null
    foreach ($candidate in $mcSourcePaths) {
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
        catch {
            Write-Host "      Trying the compatibility fallback..."
        }
    }
    if (-not $mcPath) {
        throw "No compatible MinIO client can run on this Windows server."
    }

    Write-Host "[2/4] Installing the restricted uploader credentials..."
    New-Item -ItemType Directory -Path $installDirectory -Force | Out-Null
    Copy-Item -LiteralPath $transferPath -Destination $installedCredentialPath -Force
    & icacls.exe $installDirectory /inheritance:r /grant:r "*S-1-5-18:(OI)(CI)F" "*S-1-5-32-544:(OI)(CI)F" | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "Unable to restrict the credential directory permissions."
    }

    Write-Host "[3/4] Uploading a harmless channel probe..."
    & $mcPath --config-dir $mcConfigPath alias set local $values.Endpoint $values.AccessKey $values.SecretKey | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "Unable to authenticate to MinIO with the restricted uploader."
    }

    $timestamp = Get-Date -Format "yyyyMMdd-HHmmss"
    $objectKey = "incoming/spike/channel-probe-$timestamp.json"
    @{
        probe = "geo-taptap-channel"
        collector_host = $env:COMPUTERNAME
        created_at = (Get-Date).ToUniversalTime().ToString("o")
    } | ConvertTo-Json | Set-Content -LiteralPath $probePath -Encoding UTF8

    & $mcPath --config-dir $mcConfigPath cp $probePath "local/$($values.Bucket)/$objectKey"
    if ($LASTEXITCODE -ne 0) {
        throw "Probe upload failed."
    }

    Write-Host "[4/4] Removing the temporary shared credential copy..."
    Remove-Item -LiteralPath $transferPath -Force

    Write-Host ""
    Write-Host "UPLOAD TEST PASSED"
    Write-Host "Object: $($values.Bucket)/$objectKey"
    Write-Host "Credentials installed at: $installedCredentialPath"
}
finally {
    $resolvedTemporaryDirectory = [IO.Path]::GetFullPath($temporaryDirectory)
    $resolvedTempRoot = [IO.Path]::GetFullPath([IO.Path]::GetTempPath())
    if ($resolvedTemporaryDirectory.StartsWith($resolvedTempRoot) -and
        (Split-Path -Leaf $resolvedTemporaryDirectory).StartsWith("geo-taptap-upload-test-")) {
        Remove-Item -LiteralPath $resolvedTemporaryDirectory -Recurse -Force -ErrorAction SilentlyContinue
    }
}

Read-Host "Press Enter to close"
