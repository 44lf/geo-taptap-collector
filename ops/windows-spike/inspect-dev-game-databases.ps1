$ErrorActionPreference = "Stop"

$python = Join-Path $env:ProgramData "GeoTapTap\python312\python.exe"
if (-not (Test-Path -LiteralPath $python)) {
    throw "DEV importer Python 3.12 was not found."
}

$container = docker ps --format "{{.ID}}|{{.Image}}|{{.Names}}" |
    Where-Object { $_ -match "(?i)mysql" } |
    Select-Object -First 1
if (-not $container) {
    throw "No running MySQL container was found."
}
$containerId = ($container -split "\|", 2)[0]
$containerEnvironment = docker inspect --format "{{range .Config.Env}}{{println .}}{{end}}" $containerId
$passwordLine = $containerEnvironment |
    Where-Object { $_.StartsWith("MYSQL_ROOT_PASSWORD=") } |
    Select-Object -First 1
if (-not $passwordLine) {
    throw "MYSQL_ROOT_PASSWORD was not found."
}
$rootPassword = $passwordLine.Substring("MYSQL_ROOT_PASSWORD=".Length)

$probe = @'
import json
import os

import pymysql

connection = pymysql.connect(
    host="127.0.0.1",
    port=3307,
    user="root",
    password=os.environ["TAPTAP_DEV_MYSQL_ROOT_PASSWORD"],
    charset="utf8mb4",
)
try:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT table_schema
            FROM information_schema.tables
            WHERE table_name = 'games'
              AND table_schema NOT IN ('mysql', 'information_schema', 'performance_schema', 'sys')
            ORDER BY table_schema
            """
        )
        schemas = [row[0] for row in cursor.fetchall()]
        results = []
        for schema in schemas:
            escaped = schema.replace("`", "``")
            cursor.execute(
                f"""
                SELECT
                    COUNT(*) AS total,
                    SUM(CASE WHEN is_active = 1 THEN 1 ELSE 0 END) AS active
                FROM `{escaped}`.`games`
                """
            )
            total, active = cursor.fetchone()
            results.append(
                {
                    "database": schema,
                    "games_total": int(total or 0),
                    "games_active": int(active or 0),
                }
            )
        print(json.dumps(results, ensure_ascii=False, indent=2))
finally:
    connection.close()
'@

$env:TAPTAP_DEV_MYSQL_ROOT_PASSWORD = $rootPassword
$encoded = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($probe))
try {
    & $python -c "import base64; exec(base64.b64decode('$encoded'))"
    if ($LASTEXITCODE -ne 0) {
        throw "Database inventory failed."
    }
}
finally {
    Remove-Item Env:\TAPTAP_DEV_MYSQL_ROOT_PASSWORD -ErrorAction SilentlyContinue
}

Write-Host ""
Write-Host "READ-ONLY DATABASE INVENTORY COMPLETE"
Read-Host "Press Enter to close"
