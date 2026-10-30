# Packs everything the Oracle Cloud server needs into .\dist (never commit or share that folder):
#   leadvera.tar.gz  - the code
#   leadvera.dump    - a copy of the local database (leads, settings, saved Claude token / SMTP)
#   server.env       - the server's .env: same SECRET_KEY as .env.local so the saved secrets still decrypt
# Usage:  powershell -ExecutionPolicy Bypass -File deploy\make-bundle.ps1 -Domain leads.example.com
param([Parameter(Mandatory = $true)][string]$Domain, [switch]$CodeOnly)
$ErrorActionPreference = "Stop"
Set-Location -Path (Split-Path $PSScriptRoot -Parent)
New-Item -ItemType Directory -Force dist | Out-Null

Write-Host "Packing code..." -ForegroundColor Cyan
tar -czf dist/leadvera.tar.gz --exclude=__pycache__ --exclude=.pytest_cache `
    app deploy docs tests Dockerfile docker-compose.yml requirements.txt requirements-dev.txt `
    README.md MASTER_PROMPT.md LOCAL_SETUP.md .dockerignore .env.example
if ($LASTEXITCODE -ne 0) { throw "tar failed" }
if ($CodeOnly) { Write-Host "Done: dist\leadvera.tar.gz" -ForegroundColor Green; exit 0 }

Write-Host "Exporting the local database..." -ForegroundColor Cyan
$compose = @("compose", "-f", "docker-compose.local.yml", "--env-file", ".env.local")
docker @compose exec -T db pg_dump -U afportal -d afportal -Fc -f /tmp/leadvera.dump
if ($LASTEXITCODE -ne 0) { throw "pg_dump failed - is the local portal running? (start-local.bat)" }
docker @compose cp db:/tmp/leadvera.dump dist/leadvera.dump | Out-Null
docker @compose exec -T db rm -f /tmp/leadvera.dump

Write-Host "Writing server settings..." -ForegroundColor Cyan
$local = @{}
Get-Content .env.local | Where-Object { $_ -match "^[A-Z_]+=" } | ForEach-Object {
    $k, $v = $_ -split "=", 2; $local[$k] = $v
}
$chars = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789".ToCharArray()
$bytes = New-Object byte[] 32
[System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
$pgPassword = -join ($bytes | ForEach-Object { $chars[$_ % $chars.Length] })
$lines = @(
    "DOMAIN=$Domain",
    "BASE_URL=https://$Domain",
    "SECRET_KEY=$($local['SECRET_KEY'])",
    "POSTGRES_PASSWORD=$pgPassword",
    "ADMIN_EMAIL=$($local['ADMIN_EMAIL'])",
    "ADMIN_PASSWORD=$($local['ADMIN_PASSWORD'])",
    "ANTHROPIC_API_KEY=",
    "GEMINI_API_KEY="
)
# LF line endings and no BOM: Docker on Linux would otherwise read a stray \r into every value.
[System.IO.File]::WriteAllText((Join-Path (Get-Location) "dist\server.env"), ($lines -join "`n") + "`n",
    (New-Object System.Text.UTF8Encoding $false))

Write-Host ""
Write-Host "Ready in .\dist : leadvera.tar.gz, leadvera.dump, server.env (keep server.env private)" -ForegroundColor Green
