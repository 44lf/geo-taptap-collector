param(
    [Parameter(Mandatory = $true)]
    [string]$Manifest,
    [string]$OutputRoot = "",
    [string]$FixtureRoot = ""
)

$ErrorActionPreference = "Stop"
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$ScriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
if ([string]::IsNullOrWhiteSpace($OutputRoot)) {
    $OutputRoot = Join-Path $ScriptRoot "output"
}
$CollectorVersion = "1.0.0"
$StopStatusCodes = @(403, 405, 429)
$Utf8Bom = New-Object System.Text.UTF8Encoding($true)
$Utf8NoBom = New-Object System.Text.UTF8Encoding($false)

function Read-JsonFile {
    param([string]$Path)
    $text = [System.IO.File]::ReadAllText($Path, [System.Text.Encoding]::UTF8)
    return ($text | ConvertFrom-Json)
}

function Write-JsonFile {
    param([string]$Path, $Value)
    $parent = Split-Path -Parent $Path
    if (-not [string]::IsNullOrWhiteSpace($parent)) {
        New-Item -ItemType Directory -Path $parent -Force | Out-Null
    }
    $json = $Value | ConvertTo-Json -Depth 20
    [System.IO.File]::WriteAllText($Path, $json, $Utf8Bom)
}

function Get-Sha256 {
    param([string]$Path)
    $stream = [System.IO.File]::OpenRead($Path)
    $sha = [System.Security.Cryptography.SHA256]::Create()
    try {
        $hash = $sha.ComputeHash($stream)
        return (($hash | ForEach-Object { $_.ToString("x2") }) -join "")
    }
    finally {
        $sha.Dispose()
        $stream.Dispose()
    }
}

function Get-StatusFromException {
    param($Exception)
    try {
        if ($null -ne $Exception.Response) {
            return [int]$Exception.Response.StatusCode
        }
    }
    catch {}
    return 0
}

function New-Xua {
    return (
        "V=1&PN=WebApp&LANG=zh_CN&VN_CODE=102&LOC=CN&PLT=PC&DS=Android" +
        "&UID=$([guid]::NewGuid())&OS=Windows&OSV=10&DT=PC"
    )
}

function Add-Event {
    param(
        [System.Collections.ArrayList]$Events,
        [string]$Stage,
        [int]$Status,
        [long]$ElapsedMs,
        [string]$ErrorText = ""
    )
    $entry = [ordered]@{
        stage = $Stage
        ok = ($Status -ge 200 -and $Status -lt 300)
        http_status = $Status
        elapsed_ms = $ElapsedMs
    }
    if (-not [string]::IsNullOrWhiteSpace($ErrorText)) {
        $entry.error = $ErrorText
    }
    [void]$Events.Add([pscustomobject]$entry)
}

function Invoke-JsonRequest {
    param(
        [string]$Stage,
        [string]$Uri,
        [Microsoft.PowerShell.Commands.WebRequestSession]$Session,
        [System.Collections.ArrayList]$Events,
        [string]$Method = "Get",
        [hashtable]$Headers = @{},
        [byte[]]$Body = $null,
        [string]$ContentType = $null
    )
    $started = Get-Date
    try {
        $parameters = @{
            Uri = $Uri
            Method = $Method
            WebSession = $Session
            Headers = $Headers
            TimeoutSec = 20
            UseBasicParsing = $true
            UserAgent = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) GEO-authorized-collector/1.0"
        }
        if ($null -ne $Body) {
            $parameters.Body = $Body
        }
        if ($null -ne $ContentType) {
            $parameters.ContentType = $ContentType
        }
        $response = Invoke-WebRequest @parameters
        $elapsed = [math]::Round(((Get-Date) - $started).TotalMilliseconds)
        Add-Event $Events $Stage ([int]$response.StatusCode) $elapsed
        return ($response.Content | ConvertFrom-Json)
    }
    catch {
        $elapsed = [math]::Round(((Get-Date) - $started).TotalMilliseconds)
        $status = Get-StatusFromException $_.Exception
        Add-Event $Events $Stage $status $elapsed $_.Exception.Message
        throw
    }
}

function Save-NetworkImage {
    param(
        [string]$Stage,
        [string]$Uri,
        [string]$Path,
        [System.Collections.ArrayList]$Events
    )
    $started = Get-Date
    $response = $null
    $fileStream = $null
    try {
        $request = [System.Net.HttpWebRequest]::Create($Uri)
        $request.Method = "GET"
        $request.Timeout = 20000
        $request.ReadWriteTimeout = 20000
        $request.AllowAutoRedirect = $true
        $request.UserAgent = (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) GEO-authorized-collector/1.0"
        )
        $request.Accept = "image/*"
        $response = [System.Net.HttpWebResponse]$request.GetResponse()
        $fileStream = [System.IO.File]::Create($Path)
        $response.GetResponseStream().CopyTo($fileStream)
        $fileStream.Flush()
        $elapsed = [math]::Round(((Get-Date) - $started).TotalMilliseconds)
        Add-Event $Events $Stage ([int]$response.StatusCode) $elapsed
        return [string]$response.ContentType
    }
    catch {
        $elapsed = [math]::Round(((Get-Date) - $started).TotalMilliseconds)
        $status = Get-StatusFromException $_.Exception
        Add-Event $Events $Stage $status $elapsed $_.Exception.Message
        throw
    }
    finally {
        if ($null -ne $fileStream) {
            $fileStream.Dispose()
        }
        if ($null -ne $response) {
            $response.Dispose()
        }
    }
}

function Get-ExactBrand {
    param($SearchJson, [string]$TargetName)
    foreach ($group in @($SearchJson.data.list)) {
        foreach ($item in @($group.list)) {
            if ([string]$item.type -ne "brand") {
                continue
            }
            $candidate = $item.brand.app
            if ([string]::Equals(
                [string]$candidate.title,
                $TargetName,
                [System.StringComparison]::Ordinal
            )) {
                return $candidate
            }
        }
    }
    return $null
}

function Get-FirstUrl {
    param($Object)
    if ($null -eq $Object) {
        return $null
    }
    foreach ($field in @("original_url", "url")) {
        $value = [string]$Object.$field
        if (-not [string]::IsNullOrWhiteSpace($value)) {
            return $value
        }
    }
    return $null
}

function Get-MediaExtension {
    param([string]$MediaType)
    if ($MediaType -match "^image/png") {
        return ".png"
    }
    if ($MediaType -match "^image/jpeg") {
        return ".jpg"
    }
    throw "Unsupported image content type: $MediaType"
}

function Get-FixtureMediaType {
    param([string]$Path)
    $bytes = [System.IO.File]::ReadAllBytes($Path)
    if ($bytes.Length -ge 8 -and
        $bytes[0] -eq 0x89 -and
        $bytes[1] -eq 0x50 -and
        $bytes[2] -eq 0x4E -and
        $bytes[3] -eq 0x47) {
        return "image/png"
    }
    if ($bytes.Length -ge 3 -and
        $bytes[0] -eq 0xFF -and
        $bytes[1] -eq 0xD8 -and
        $bytes[2] -eq 0xFF) {
        return "image/jpeg"
    }
    throw "Fixture image has unsupported signature: $Path"
}

function New-FileEntry {
    param(
        [string]$BundleRoot,
        [string]$Path,
        [string]$MediaType,
        [string]$Role,
        [string]$SourceUrl = ""
    )
    $rootPath = [System.IO.Path]::GetFullPath($BundleRoot).TrimEnd("\")
    $fullPath = [System.IO.Path]::GetFullPath($Path)
    if (-not $fullPath.StartsWith(
        "$rootPath\",
        [System.StringComparison]::OrdinalIgnoreCase
    )) {
        throw "Output file escaped bundle root: $fullPath"
    }
    $relative = $fullPath.Substring($rootPath.Length + 1).Replace("\", "/")
    $file = Get-Item -LiteralPath $fullPath
    $entry = [ordered]@{
        path = $relative
        bytes = [long]$file.Length
        sha256 = Get-Sha256 $fullPath
        media_type = $MediaType
        role = $Role
    }
    if (-not [string]::IsNullOrWhiteSpace($SourceUrl)) {
        $entry.source_url = $SourceUrl
    }
    return [pscustomobject]$entry
}

function Read-PublicIp {
    if (-not [string]::IsNullOrWhiteSpace($FixtureRoot)) {
        foreach ($target in @($InputManifest.targets)) {
            $responsePath = Join-Path $FixtureRoot "$($target.target_game_id)\responses.json"
            if (Test-Path -LiteralPath $responsePath) {
                $responses = Read-JsonFile $responsePath
                if (-not [string]::IsNullOrWhiteSpace([string]$responses.public_ip)) {
                    return [string]$responses.public_ip
                }
            }
        }
        return $null
    }
    foreach ($uri in @("https://checkip.amazonaws.com/", "https://icanhazip.com/")) {
        try {
            $request = [System.Net.HttpWebRequest]::Create($uri)
            $request.Timeout = 20000
            $request.UserAgent = "GEO-authorized-collector/1.0"
            $response = $request.GetResponse()
            try {
                $reader = New-Object System.IO.StreamReader($response.GetResponseStream())
                try {
                    $value = $reader.ReadToEnd().Trim()
                    if (-not [string]::IsNullOrWhiteSpace($value)) {
                        return $value
                    }
                }
                finally {
                    $reader.Dispose()
                }
            }
            finally {
                $response.Dispose()
            }
        }
        catch {}
    }
    return $null
}

function Get-FixtureStageStatus {
    param($Responses, [string]$Stage)
    $value = $Responses.$Stage
    if ($null -eq $value) {
        return 200
    }
    return [int]$value
}

function Assert-FixtureStage {
    param(
        $Responses,
        [string]$Stage,
        [System.Collections.ArrayList]$Events
    )
    $status = Get-FixtureStageStatus $Responses $Stage
    Add-Event $Events $Stage $status 0
    if ($status -lt 200 -or $status -ge 300) {
        $exception = New-Object System.Exception("Fixture stage $Stage returned HTTP $status")
        $exception.Data["HttpStatus"] = $status
        throw $exception
    }
}

function Get-ExceptionStatus {
    param($Exception)
    if ($Exception.Data.Contains("HttpStatus")) {
        return [int]$Exception.Data["HttpStatus"]
    }
    return Get-StatusFromException $Exception
}

if (-not (Test-Path -LiteralPath $Manifest -PathType Leaf)) {
    Write-Error "Manifest file not found: $Manifest"
    exit 1
}

try {
    $InputManifest = Read-JsonFile $Manifest
    if ([int]$InputManifest.schema_version -ne 1) {
        throw "Manifest schema_version must be 1."
    }
    $targets = @($InputManifest.targets)
    if ($targets.Count -lt 1 -or $targets.Count -gt 20) {
        throw "Manifest targets must contain between 1 and 20 entries."
    }
    $bundleId = [string]$InputManifest.bundle_id
    if ([string]::IsNullOrWhiteSpace($bundleId) -or $bundleId -notmatch "^[A-Za-z0-9._-]+$") {
        throw "Manifest bundle_id must use only letters, digits, dot, underscore, or hyphen."
    }
    $seenIds = @{}
    foreach ($target in $targets) {
        $targetId = [int]$target.target_game_id
        if ($targetId -le 0) {
            throw "Every target_game_id must be a positive integer."
        }
        if ($seenIds.ContainsKey($targetId)) {
            throw "Duplicate target_game_id: $targetId"
        }
        $seenIds[$targetId] = $true
        if ([string]::IsNullOrWhiteSpace([string]$target.name)) {
            throw "Every target must provide a non-empty exact name."
        }
    }
    $maxScreenshots = [int]$InputManifest.max_screenshots
    if ($maxScreenshots -lt 0 -or $maxScreenshots -gt 6) {
        throw "max_screenshots must be between 0 and 6."
    }
    $delaySeconds = [int]$InputManifest.delay_seconds
    if ($delaySeconds -lt 0 -or $delaySeconds -gt 3600) {
        throw "delay_seconds must be between 0 and 3600."
    }
}
catch {
    Write-Error $_.Exception.Message
    exit 1
}

$bundleRoot = Join-Path $OutputRoot $bundleId
$checkpointPath = Join-Path $bundleRoot "checkpoint.json"
$manifestPath = Join-Path $bundleRoot "manifest.json"
$reportPath = Join-Path $bundleRoot "report.json"
New-Item -ItemType Directory -Path $bundleRoot -Force | Out-Null

$successfulIds = @()
$bundleTargets = @()
$bundleFiles = @()
if (Test-Path -LiteralPath $checkpointPath) {
    $checkpoint = Read-JsonFile $checkpointPath
    $successfulIds = @($checkpoint.successful_target_ids | ForEach-Object { [int]$_ })
}
if (Test-Path -LiteralPath $manifestPath) {
    $existingManifest = Read-JsonFile $manifestPath
    $bundleTargets = @($existingManifest.targets)
    $bundleFiles = @($existingManifest.files)
}

$reportTargets = @()
$skippedSuccess = 0
$blocked = $false
$startedAt = (Get-Date).ToUniversalTime().ToString("o")
$publicIp = Read-PublicIp
$exitCode = 0

foreach ($target in $targets) {
    $targetId = [int]$target.target_game_id
    $targetName = [string]$target.name
    if ($successfulIds -contains $targetId) {
        $skippedSuccess += 1
        continue
    }
    $events = New-Object System.Collections.ArrayList
    $targetStatus = "error"
    $targetError = ""
    try {
        $searchJson = $null
        $detailJson = $null
        $responses = $null
        $session = New-Object Microsoft.PowerShell.Commands.WebRequestSession
        if (-not [string]::IsNullOrWhiteSpace($FixtureRoot)) {
            $fixtureTarget = Join-Path $FixtureRoot $targetId
            $responses = Read-JsonFile (Join-Path $fixtureTarget "responses.json")
            Assert-FixtureStage $responses "bootstrap_xsrf" $events
            Assert-FixtureStage $responses "search_by_name" $events
            $searchJson = Read-JsonFile (Join-Path $fixtureTarget "search.json")
        }
        else {
            $tagName = ([string][char]0x5361) + ([string][char]0x724C)
            $bootstrapUrl = (
                "https://www.taptap.cn/webapiv2/app-tag/v1/by-tag" +
                "?X-UA=$([Uri]::EscapeDataString((New-Xua)))" +
                "&tag=$([Uri]::EscapeDataString($tagName))" +
                "&sort=hits&from=0&limit=1"
            )
            [void](Invoke-JsonRequest `
                -Stage "bootstrap_xsrf" `
                -Uri $bootstrapUrl `
                -Session $session `
                -Events $events `
                -Headers @{
                    Referer = "https://www.taptap.cn/categories/all"
                    "X-Requested-With" = "XMLHttpRequest"
                    Accept = "application/json, text/plain, */*"
                })
            $cookies = $session.Cookies.GetCookies([Uri]"https://www.taptap.cn/")
            $xsrf = $cookies |
                Where-Object { $_.Name -eq "XSRF-TOKEN" } |
                Select-Object -First 1
            if ($null -eq $xsrf) {
                throw "Bootstrap returned no XSRF-TOKEN cookie."
            }
            $boundary = "----WebKitFormBoundary$([guid]::NewGuid().ToString('N'))"
            $crlf = "`r`n"
            $bodyText = (
                "--$boundary$crlf" +
                "Content-Disposition: form-data; name=`"kw`"$crlf$crlf" +
                "$targetName$crlf" +
                "--$boundary$crlf" +
                "Content-Disposition: form-data; name=`"types`"$crlf$crlf" +
                "mix$crlf" +
                "--$boundary--$crlf"
            )
            $searchUrl = (
                "https://www.taptap.cn/webapiv2/search/v6/agg-search" +
                "?X-UA=$([Uri]::EscapeDataString((New-Xua)))"
            )
            $searchJson = Invoke-JsonRequest `
                -Stage "search_by_name" `
                -Uri $searchUrl `
                -Session $session `
                -Events $events `
                -Method "Post" `
                -Body $Utf8NoBom.GetBytes($bodyText) `
                -ContentType "multipart/form-data; boundary=$boundary" `
                -Headers @{
                    Referer = "https://www.taptap.cn/search/$([Uri]::EscapeDataString($targetName))"
                    Origin = "https://www.taptap.cn"
                    "X-XSRF-TOKEN" = $xsrf.Value
                    "X-Requested-With" = "XMLHttpRequest"
                    Accept = "application/json, text/plain, */*"
                }
        }

        $app = Get-ExactBrand $searchJson $targetName
        if ($null -eq $app) {
            $targetStatus = "miss"
            $targetError = "No exact brand result found."
            throw [System.Management.Automation.HaltCommandException]::new($targetError)
        }
        $sourceGameId = [string]$app.id
        if ([string]::IsNullOrWhiteSpace($sourceGameId)) {
            throw "Exact brand result contained no game id."
        }
        if (-not [string]::IsNullOrWhiteSpace($FixtureRoot)) {
            Assert-FixtureStage $responses "game_detail" $events
            $detailJson = Read-JsonFile (Join-Path $fixtureTarget "detail.json")
        }
        else {
            $detailUrl = (
                "https://www.taptap.cn/webapiv2/app/v6/detail" +
                "?X-UA=$([Uri]::EscapeDataString((New-Xua)))" +
                "&id=$([Uri]::EscapeDataString($sourceGameId))"
            )
            $detailJson = Invoke-JsonRequest `
                -Stage "game_detail" `
                -Uri $detailUrl `
                -Session $session `
                -Events $events `
                -Headers @{
                    Referer = "https://www.taptap.cn/"
                    "X-Requested-With" = "XMLHttpRequest"
                    Accept = "application/json, text/plain, */*"
                }
        }
        $detail = $detailJson.data.app
        if ($null -eq $detail) {
            throw "Detail response contained no data.app."
        }

        $targetRoot = Join-Path $bundleRoot "games\$targetId"
        $imageRoot = Join-Path $targetRoot "images"
        New-Item -ItemType Directory -Path $imageRoot -Force | Out-Null
        $targetFiles = @()
        $assetPaths = @()
        $iconUrl = Get-FirstUrl $detail.icon
        if (-not [bool]$target.icon_local -and -not [string]::IsNullOrWhiteSpace($iconUrl)) {
            if (-not [string]::IsNullOrWhiteSpace($FixtureRoot)) {
                Assert-FixtureStage $responses "download_icon" $events
                $fixtureIcon = Join-Path $fixtureTarget "icon.png"
                $iconType = Get-FixtureMediaType $fixtureIcon
                $iconPath = Join-Path $imageRoot ("icon" + (Get-MediaExtension $iconType))
                Copy-Item -LiteralPath $fixtureIcon -Destination $iconPath -Force
            }
            else {
                $iconPath = Join-Path $imageRoot "icon.download"
                $iconType = Save-NetworkImage "download_icon" $iconUrl $iconPath $events
                $finalIconPath = Join-Path $imageRoot ("icon" + (Get-MediaExtension $iconType))
                Move-Item -LiteralPath $iconPath -Destination $finalIconPath -Force
                $iconPath = $finalIconPath
            }
            $iconEntry = New-FileEntry $bundleRoot $iconPath $iconType "icon" $iconUrl
            $targetFiles += $iconEntry
            $assetPaths += $iconEntry.path
        }

        $screenshots = @($detail.screenshots)
        $shotCount = [math]::Min($maxScreenshots, $screenshots.Count)
        for ($index = 0; $index -lt $shotCount; $index++) {
            $shotUrl = Get-FirstUrl $screenshots[$index]
            if ([string]::IsNullOrWhiteSpace($shotUrl)) {
                continue
            }
            $number = $index + 1
            if (-not [string]::IsNullOrWhiteSpace($FixtureRoot)) {
                Assert-FixtureStage $responses "download_screenshot" $events
                $fixtureShot = Join-Path $fixtureTarget ("screenshot-{0:d2}.jpg" -f $number)
                $shotType = Get-FixtureMediaType $fixtureShot
                $shotPath = Join-Path $imageRoot (
                    "screenshot-{0:d2}{1}" -f $number, (Get-MediaExtension $shotType)
                )
                Copy-Item -LiteralPath $fixtureShot -Destination $shotPath -Force
            }
            else {
                $shotPath = Join-Path $imageRoot ("screenshot-{0:d2}.download" -f $number)
                $shotType = Save-NetworkImage `
                    "download_screenshot" $shotUrl $shotPath $events
                $finalShotPath = Join-Path $imageRoot (
                    "screenshot-{0:d2}{1}" -f $number, (Get-MediaExtension $shotType)
                )
                Move-Item -LiteralPath $shotPath -Destination $finalShotPath -Force
                $shotPath = $finalShotPath
            }
            $shotEntry = New-FileEntry $bundleRoot $shotPath $shotType "screenshot" $shotUrl
            $targetFiles += $shotEntry
            $assetPaths += $shotEntry.path
        }

        $tags = @($detail.tags | ForEach-Object { [string]$_.value } | Where-Object { $_ })
        $platforms = @(
            $detail.platform_info.supported_platforms |
                ForEach-Object { [string]$_.key } |
                Where-Object { $_ }
        )
        $screenshotUrls = @(
            $screenshots |
                ForEach-Object { Get-FirstUrl $_ } |
                Where-Object { $_ }
        )
        $gamePayload = [ordered]@{
            target_game_id = $targetId
            category_id = if ($null -eq $target.category_id) { $null } else {
                [int]$target.category_id
            }
            game = [ordered]@{
                source = "taptap"
                game_id = [string]$detail.id
                name = [string]$detail.title
                score = $detail.stat.rating.score
                tags = $tags
                platforms = $platforms
                comment_count = $detail.stat.review_count
                icon_url = $iconUrl
                screenshot_urls = $screenshotUrls
                android_package = [string]$detail.identifier
                description = [string]$detail.description.text
                raw = @{}
            }
        }
        $gamePath = Join-Path $targetRoot "game.json"
        Write-JsonFile $gamePath $gamePayload
        $gameEntry = New-FileEntry $bundleRoot $gamePath "application/json" "game"
        $targetFiles = @($gameEntry) + @($targetFiles)

        $bundleFiles += $targetFiles
        $bundleTargets += [pscustomobject][ordered]@{
            target_game_id = $targetId
            name = $targetName
            category_id = if ($null -eq $target.category_id) { $null } else {
                [int]$target.category_id
            }
            status = "success"
            game_file = $gameEntry.path
            asset_files = @($assetPaths)
        }
        $successfulIds += $targetId
        Write-JsonFile $checkpointPath ([ordered]@{
            bundle_id = $bundleId
            successful_target_ids = @($successfulIds)
        })
        $targetStatus = "success"
    }
    catch [System.Management.Automation.HaltCommandException] {
        if ($targetStatus -ne "miss") {
            $targetStatus = "error"
            $targetError = $_.Exception.Message
        }
    }
    catch {
        $status = Get-ExceptionStatus $_.Exception
        $targetError = $_.Exception.Message
        if ($StopStatusCodes -contains $status) {
            $targetStatus = "blocked"
            $blocked = $true
            $exitCode = 3
        }
        else {
            $targetStatus = "error"
            $exitCode = 2
        }
    }
    $reportTargets += [pscustomobject][ordered]@{
        target_game_id = $targetId
        name = $targetName
        status = $targetStatus
        error = $targetError
        events = @($events)
    }
    if ($blocked) {
        break
    }
    if ($delaySeconds -gt 0) {
        Start-Sleep -Seconds $delaySeconds
    }
}

$finishedAt = (Get-Date).ToUniversalTime().ToString("o")
$finalManifest = [ordered]@{
    schema_version = 1
    bundle_id = $bundleId
    collector_version = $CollectorVersion
    source = "taptap"
    started_at = $startedAt
    finished_at = $finishedAt
    targets = @($bundleTargets)
    files = @($bundleFiles)
}
Write-JsonFile $manifestPath $finalManifest

$report = [ordered]@{
    schema_version = 1
    bundle_id = $bundleId
    collector_version = $CollectorVersion
    host = $env:COMPUTERNAME
    public_ip = $publicIp
    started_at = $startedAt
    finished_at = $finishedAt
    blocked = $blocked
    skipped_success = $skippedSuccess
    targets = @($reportTargets)
}
Write-JsonFile $reportPath $report

Write-Output "bundle=$bundleRoot"
Write-Output "skipped_success=$skippedSuccess"
Write-Output "blocked=$blocked"
exit $exitCode
