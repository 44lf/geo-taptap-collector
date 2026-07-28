$ErrorActionPreference = "Stop"

$sourcePath = Join-Path ([Environment]::GetFolderPath("Desktop")) "geo-taptap-uploader-credentials.txt"
$transferPath = Join-Path $PSScriptRoot "geo-taptap-uploader-credentials.transfer.txt"

if (-not (Test-Path -LiteralPath $sourcePath)) {
    throw "Uploader credentials were not found on this DEV server desktop: $sourcePath"
}

$requiredKeys = @("Endpoint=", "Bucket=", "AccessKey=", "SecretKey=")
$content = Get-Content -LiteralPath $sourcePath -Raw
foreach ($requiredKey in $requiredKeys) {
    if (-not $content.Contains($requiredKey)) {
        throw "The credential file is missing: $requiredKey"
    }
}

Copy-Item -LiteralPath $sourcePath -Destination $transferPath -Force

Write-Host ""
Write-Host "Uploader credentials are ready for transfer."
Write-Host "Switch to the 172.25.25.102 collector server now."
Write-Host "On that server, run: install-and-test-minio-upload.cmd"
Write-Host ""
Write-Host "Do not open or send the credential file in chat."
Read-Host "Press Enter to close"
