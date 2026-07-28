$ErrorActionPreference = "Stop"

$policyName = "geo-taptap-upload-only"
$uploadUser = "geo-taptap-uploader"
$bucketName = "geo-taptap-inbox"
$endpoint = "http://172.25.20.57:9000"
$policyPath = Join-Path $PSScriptRoot "geo-taptap-upload-policy.json"
$mcSourcePath = Join-Path $PSScriptRoot "mc.exe"
$mcLegacySourcePath = Join-Path $PSScriptRoot "mc-legacy.exe"

if (-not (Test-Path -LiteralPath $policyPath)) {
    throw "Policy file not found: $policyPath"
}
if (-not (Test-Path -LiteralPath $mcSourcePath) -and
    -not (Test-Path -LiteralPath $mcLegacySourcePath)) {
    throw "No MinIO Windows client was found beside this script."
}

Write-Host "[1/5] Finding the running MinIO container..."
$container = docker ps --format "{{.ID}}|{{.Image}}|{{.Names}}" |
    Where-Object { $_ -match "(?i)minio" -and $_ -notmatch "(?i)minio/mc" } |
    Select-Object -First 1

if (-not $container) {
    throw "No running MinIO container was found."
}

$containerId = ($container -split "\|", 2)[0]
Write-Host "      Found: $container"

Write-Host "[2/5] Reading MinIO administrator credentials from the container..."
$containerEnvironment = docker inspect --format "{{range .Config.Env}}{{println .}}{{end}}" $containerId

function Get-ContainerEnvironmentValue {
    param(
        [string[]]$Environment,
        [string[]]$Names
    )

    foreach ($name in $Names) {
        $prefix = "$name="
        $line = $Environment | Where-Object { $_.StartsWith($prefix) } | Select-Object -First 1
        if ($line) {
            return $line.Substring($prefix.Length)
        }
    }
    return $null
}

$rootUser = Get-ContainerEnvironmentValue `
    -Environment $containerEnvironment `
    -Names @("MINIO_ROOT_USER", "MINIO_ACCESS_KEY")
$rootPassword = Get-ContainerEnvironmentValue `
    -Environment $containerEnvironment `
    -Names @("MINIO_ROOT_PASSWORD", "MINIO_SECRET_KEY")

if (-not $rootUser -or -not $rootPassword) {
    throw "MinIO administrator credentials were not found in the container environment."
}

$alphabet = "abcdefghijkmnopqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"
$randomBytes = New-Object byte[] 32
[System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($randomBytes)
$uploadSecret = -join ($randomBytes | ForEach-Object { $alphabet[$_ % $alphabet.Length] })

Write-Host "[3/5] Creating the restricted upload policy and user..."
$temporaryDirectory = Join-Path ([IO.Path]::GetTempPath()) ("geo-taptap-minio-" + [guid]::NewGuid())
New-Item -ItemType Directory -Path $temporaryDirectory | Out-Null
$localPolicyPath = Join-Path $temporaryDirectory "policy.json"
$mcConfigPath = Join-Path $temporaryDirectory "mc-config"

try {
    Copy-Item -LiteralPath $policyPath -Destination $localPolicyPath

    $mcPath = $null
    foreach ($candidate in @($mcSourcePath, $mcLegacySourcePath)) {
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
            Write-Host "      This client is not compatible with this Windows version; trying fallback."
        }
    }

    if (-not $mcPath) {
        throw "Neither MinIO client can run on this Windows server."
    }

    & $mcPath --config-dir $mcConfigPath alias set local "http://127.0.0.1:9000" $rootUser $rootPassword
    if ($LASTEXITCODE -ne 0) {
        throw "Unable to connect to the local MinIO service."
    }

    & $mcPath --config-dir $mcConfigPath admin policy create local $policyName $localPolicyPath
    if ($LASTEXITCODE -ne 0) {
        Write-Host "      Trying the command used by older MinIO versions..."
        & $mcPath --config-dir $mcConfigPath admin policy add local $policyName $localPolicyPath
        if ($LASTEXITCODE -ne 0) {
            throw "Unable to create the upload policy."
        }
    }

    & $mcPath --config-dir $mcConfigPath admin user add local $uploadUser $uploadSecret
    if ($LASTEXITCODE -ne 0) {
        throw "Unable to create the uploader user."
    }

    & $mcPath --config-dir $mcConfigPath admin policy attach local $policyName --user $uploadUser
    if ($LASTEXITCODE -ne 0) {
        Write-Host "      Trying the command used by older MinIO versions..."
        & $mcPath --config-dir $mcConfigPath admin policy set local $policyName "user=$uploadUser"
        if ($LASTEXITCODE -ne 0) {
            throw "Unable to attach the upload policy."
        }
    }

    & $mcPath --config-dir $mcConfigPath admin user info local $uploadUser
    if ($LASTEXITCODE -ne 0) {
        throw "Unable to verify the uploader user."
    }
}
finally {
    $resolvedTemporaryDirectory = [IO.Path]::GetFullPath($temporaryDirectory)
    $resolvedTempRoot = [IO.Path]::GetFullPath([IO.Path]::GetTempPath())
    if ($resolvedTemporaryDirectory.StartsWith($resolvedTempRoot) -and
        (Split-Path -Leaf $resolvedTemporaryDirectory).StartsWith("geo-taptap-minio-")) {
        Remove-Item -LiteralPath $resolvedTemporaryDirectory -Recurse -Force -ErrorAction SilentlyContinue
    }
}

Write-Host "[4/5] Saving the uploader credentials on this DEV server..."
$credentialPath = Join-Path ([Environment]::GetFolderPath("Desktop")) "geo-taptap-uploader-credentials.txt"
@"
Endpoint=$endpoint
Bucket=$bucketName
AccessKey=$uploadUser
SecretKey=$uploadSecret
AllowedPrefixes=incoming/,ready/
"@ | Set-Content -LiteralPath $credentialPath -Encoding UTF8

Write-Host "[5/5] Done."
Write-Host ""
Write-Host "Credentials were saved to:"
Write-Host "  $credentialPath"
Write-Host ""
Write-Host "Do not send the SecretKey in chat."
Read-Host "Press Enter to close"
