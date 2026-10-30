# Start Leadvera on this computer (Windows) with Docker Desktop.
# Run by double-clicking start-local.bat, or:  powershell -ExecutionPolicy Bypass -File start-local.ps1
$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

function Fail($msg) { Write-Host $msg -ForegroundColor Red; Read-Host "Press Enter to close"; exit 1 }

if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    Fail "Docker Desktop is not installed. Install it from https://www.docker.com/products/docker-desktop/ and re-run."
}
docker info *> $null
if ($LASTEXITCODE -ne 0) {
    Fail "Docker Desktop is not running. Start it, wait until it shows 'Engine running', then re-run."
}

function New-Secret([int]$length) {
    $chars = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789".ToCharArray()
    $bytes = New-Object byte[] $length
    [System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
    -join ($bytes | ForEach-Object { $chars[$_ % $chars.Length] })
}

if (-not (Test-Path ".env.local")) {
    Write-Host "First-time setup - creating .env.local" -ForegroundColor Cyan
    $adminEmail = Read-Host "Admin email (your login)"
    while ($true) {
        $sec = Read-Host "Admin password (12+ chars, upper + lower case + a digit, no `$ sign)" -AsSecureString
        $pw = [Runtime.InteropServices.Marshal]::PtrToStringAuto([Runtime.InteropServices.Marshal]::SecureStringToBSTR($sec))
        if ($pw.Length -ge 12 -and $pw -cmatch "[A-Z]" -and $pw -cmatch "[a-z]" -and $pw -match "[0-9]" -and -not $pw.Contains('$')) { break }
        Write-Host "  Password does not meet the rules, try again." -ForegroundColor Yellow
    }
    $apiKey = Read-Host "Anthropic API key (leave EMPTY if you will use your Claude Pro/Max plan - press Enter)"
    $port = Read-Host "Port for the portal [8000]"
    if ([string]::IsNullOrWhiteSpace($port)) { $port = "8000" }
    $lines = @(
        "SECRET_KEY=$(New-Secret 64)",
        "POSTGRES_PASSWORD=$(New-Secret 32)",
        "ADMIN_EMAIL=$adminEmail",
        "ADMIN_PASSWORD=$pw",
        "ANTHROPIC_API_KEY=$apiKey",
        "PORT=$port"
    )
    # UTF-8 without BOM so Docker Compose reads the first key correctly
    [System.IO.File]::WriteAllLines((Join-Path $PSScriptRoot ".env.local"), $lines, (New-Object System.Text.UTF8Encoding $false))
    Write-Host "Saved .env.local (keep it private - it holds your secrets)." -ForegroundColor Green
}

$port = ((Get-Content ".env.local" | Where-Object { $_ -match "^PORT=" }) -replace "^PORT=", "")
if ([string]::IsNullOrWhiteSpace($port)) { $port = "8000" }

Write-Host "Starting the database..." -ForegroundColor Cyan
docker compose -f docker-compose.local.yml --env-file .env.local up -d --wait db
if ($LASTEXITCODE -ne 0) { Fail "The database did not start - see the messages above." }

# An existing pgdata volume keeps the password it was created with; make it match .env.local.
# The SQL goes through stdin so the password never appears on a command line.
$pgPassword = ((Get-Content ".env.local" | Where-Object { $_ -match "^POSTGRES_PASSWORD=" }) -replace "^POSTGRES_PASSWORD=", "")
"ALTER USER afportal WITH PASSWORD '$($pgPassword.Replace("'", "''"))';" |
    docker compose -f docker-compose.local.yml --env-file .env.local exec -T db psql -v ON_ERROR_STOP=1 -q -U afportal -d afportal
if ($LASTEXITCODE -ne 0) { Fail "Could not sync the database password - see the messages above." }

Write-Host "Building and starting containers (first run downloads ~300 MB, takes a few minutes)..." -ForegroundColor Cyan
docker compose -f docker-compose.local.yml --env-file .env.local up -d --build
if ($LASTEXITCODE -ne 0) { Fail "docker compose failed - see the messages above." }

Write-Host -NoNewline "Waiting for the portal"
for ($i = 0; $i -lt 90; $i++) {
    try {
        $r = Invoke-WebRequest -Uri "http://localhost:$port/healthz" -UseBasicParsing -TimeoutSec 3
        if ($r.StatusCode -eq 200) {
            Write-Host ""
            Write-Host "Portal is running: http://localhost:$port" -ForegroundColor Green
            Start-Process "http://localhost:$port"
            Write-Host "Logs:  docker compose -f docker-compose.local.yml --env-file .env.local logs -f worker"
            Write-Host "Stop:  double-click stop-local.bat"
            Write-Host "Use your Claude Pro/Max plan for research: double-click connect-claude.bat, then paste the token in Settings" -ForegroundColor Yellow
            Read-Host "Press Enter to close this window (the portal keeps running)"
            exit 0
        }
    } catch { }
    Write-Host -NoNewline "."
    Start-Sleep -Seconds 2
}
Write-Host ""
docker compose -f docker-compose.local.yml --env-file .env.local logs --tail 40 web worker
Fail "The portal did not answer in 3 minutes - see the logs above."
