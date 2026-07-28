$ErrorActionPreference = "Stop"

$python = Get-Command python.exe -ErrorAction SilentlyContinue
if (-not $python) {
    $python = Get-Command py.exe -ErrorAction SilentlyContinue
}
if (-not $python) {
    throw "Python was not found."
}

$checkScript = @'
import importlib
import json
import sys

modules = [
    "sqlalchemy",
    "pymysql",
    "pydantic",
    "pydantic_settings",
    "minio",
    "certifi",
    "urllib3",
]
status = {}
for name in modules:
    try:
        module = importlib.import_module(name)
        status[name] = getattr(module, "__version__", "installed")
    except Exception as exc:
        status[name] = "MISSING: " + type(exc).__name__

print("Python:", sys.version.replace("\n", " "))
print(json.dumps(status, indent=2))
missing = [name for name, value in status.items() if value.startswith("MISSING:")]
print("")
print("Missing:", ", ".join(missing) if missing else "none")
raise SystemExit(1 if missing else 0)
'@

$encoded = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($checkScript))
& $python.Source -c "import base64; exec(base64.b64decode('$encoded'))"
$exitCode = $LASTEXITCODE

Write-Host ""
if ($exitCode -eq 0) {
    Write-Host "DEPENDENCY CHECK PASSED"
}
else {
    Write-Host "DEPENDENCIES ARE MISSING - take a screenshot."
}
Read-Host "Press Enter to close"
exit $exitCode
