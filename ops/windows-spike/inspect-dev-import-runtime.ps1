$ErrorActionPreference = "Continue"

Write-Host "=== DEV IMPORT RUNTIME CHECK ==="
Write-Host ""

Write-Host "[1/4] Host"
Write-Host "Computer: $env:COMPUTERNAME"
Write-Host "Windows: $([Environment]::OSVersion.VersionString)"
Write-Host ""

Write-Host "[2/4] Python on host"
$python = Get-Command python.exe -ErrorAction SilentlyContinue
$py = Get-Command py.exe -ErrorAction SilentlyContinue
if ($python) {
    & $python.Source --version
    Write-Host "Path: $($python.Source)"
}
elseif ($py) {
    & $py.Source --version
    Write-Host "Path: $($py.Source)"
}
else {
    Write-Host "Python: NOT FOUND"
}
Write-Host ""

Write-Host "[3/4] Running Docker containers"
docker ps --format "table {{.Names}}\t{{.Image}}\t{{.Ports}}\t{{.Status}}"
if ($LASTEXITCODE -ne 0) {
    Write-Host "Unable to list Docker containers."
}
Write-Host ""

Write-Host "[4/4] Required DEV ports"
foreach ($port in @(9000, 3307)) {
    $result = Test-NetConnection -ComputerName 127.0.0.1 -Port $port -WarningAction SilentlyContinue
    Write-Host ("127.0.0.1:{0} = {1}" -f $port, $result.TcpTestSucceeded)
}

Write-Host ""
Write-Host "CHECK COMPLETE - take a screenshot of this window."
Read-Host "Press Enter to close"
