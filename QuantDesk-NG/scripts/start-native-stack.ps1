param(
    [int]$DatabasePort = 3310,
    [switch]$NoPause
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$runtimeRoot = Join-Path $projectRoot ".runtime"
$serverRoot = Join-Path $runtimeRoot "mariadb-12.3.2-winx64"
$archive = Join-Path (Split-Path -Parent $projectRoot) "mariadb-12.3.2-winx64.zip"
$dataRoot = Join-Path $runtimeRoot "mariadb-data"
$rootSecretPath = Join-Path $runtimeRoot "mariadb-root-password.txt"
$logDir = Join-Path $projectRoot "logs"
$cli = Join-Path $projectRoot ".venv\Scripts\quantdesk-ng.exe"
$alembic = Join-Path $projectRoot ".venv\Scripts\alembic.exe"

function Read-DotEnvValue {
    param([string]$Path, [string]$Name, [string]$DefaultValue)

    $line = Get-Content -LiteralPath $Path | Where-Object {
        $_ -match "^\s*$([regex]::Escape($Name))\s*="
    } | Select-Object -Last 1
    if (-not $line) { return $DefaultValue }
    $value = ($line -split "=", 2)[1].Trim().Trim('"').Trim("'")
    if ([string]::IsNullOrWhiteSpace($value)) { return $DefaultValue }
    return $value
}

function Test-NativeDatabase {
    param([string]$Admin, [string]$RootPassword)

    & $Admin --protocol=TCP --host=127.0.0.1 "--port=$DatabasePort" "-uroot" "-p$RootPassword" --skip-ssl --connect-timeout=2 ping --silent 2>$null
    return $LASTEXITCODE -eq 0
}

try {
    $envFile = Join-Path $projectRoot ".env"
    if (-not (Test-Path -LiteralPath $envFile)) { throw "Missing $envFile" }
    if (-not (Test-Path -LiteralPath $cli)) { throw "Missing $cli" }
    if (-not (Test-Path -LiteralPath $alembic)) { throw "Missing $alembic" }

    New-Item -ItemType Directory -Path $runtimeRoot, $logDir -Force | Out-Null
    if (-not (Test-Path -LiteralPath $serverRoot)) {
        if (-not (Test-Path -LiteralPath $archive)) { throw "Missing portable MariaDB archive: $archive" }
        Expand-Archive -LiteralPath $archive -DestinationPath $runtimeRoot -Force
    }

    $installDb = Join-Path $serverRoot "bin\mariadb-install-db.exe"
    $server = Join-Path $serverRoot "bin\mariadbd.exe"
    $admin = Join-Path $serverRoot "bin\mariadb-admin.exe"
    $mysql = Join-Path $serverRoot "bin\mariadb.exe"
    if (-not (Test-Path -LiteralPath $installDb)) { throw "Portable MariaDB is incomplete." }

    if (-not (Test-Path -LiteralPath $rootSecretPath)) {
        [guid]::NewGuid().ToString("N") | Set-Content -LiteralPath $rootSecretPath -Encoding ascii -NoNewline
    }
    $rootPassword = (Get-Content -LiteralPath $rootSecretPath -Raw).Trim()

    if (-not (Test-Path -LiteralPath $dataRoot)) {
        Write-Host "[1/4] Initializing isolated native MariaDB..."
        & $installDb "--datadir=$dataRoot" "--password=$rootPassword" "--port=$DatabasePort" --silent
        if ($LASTEXITCODE -ne 0) { throw "MariaDB initialization failed." }
    }

    $databaseAlreadyRunning = Test-NativeDatabase -Admin $admin -RootPassword $rootPassword
    $occupied = Get-NetTCPConnection -State Listen -LocalPort $DatabasePort -ErrorAction SilentlyContinue
    if ($occupied -and -not $databaseAlreadyRunning) {
        throw "Port $DatabasePort is already in use by another service; choose another -DatabasePort."
    }

    Write-Host "[2/4] Starting isolated native MariaDB on 127.0.0.1:$DatabasePort..."
    $nativeLog = Join-Path $logDir "native-mariadb.err.log"
    if (-not $databaseAlreadyRunning) {
        Start-Process -FilePath $server -ArgumentList @(
            "--basedir=$serverRoot", "--datadir=$dataRoot", "--port=$DatabasePort",
            "--bind-address=127.0.0.1", "--skip-ssl", "--log-error=$nativeLog"
        ) -WorkingDirectory $serverRoot -WindowStyle Hidden | Out-Null
        $ready = $false
        for ($attempt = 0; $attempt -lt 30; $attempt++) {
            Start-Sleep -Seconds 1
            if (Test-NativeDatabase -Admin $admin -RootPassword $rootPassword) { $ready = $true; break }
        }
        if (-not $ready) { throw "Native MariaDB did not become ready. Check $nativeLog" }
    }

    $databaseName = Read-DotEnvValue -Path $envFile -Name "DB_NAME" -DefaultValue "quantdesk_ng"
    $databaseUser = Read-DotEnvValue -Path $envFile -Name "DB_USER" -DefaultValue "quantdesk_ng"
    $databasePassword = Read-DotEnvValue -Path $envFile -Name "DB_PASSWORD" -DefaultValue ""
    if ([string]::IsNullOrWhiteSpace($databasePassword)) { throw "DB_PASSWORD is missing from .env" }
    if ($databaseName -notmatch "^[A-Za-z0-9_]+$") { throw "DB_NAME may only contain letters, digits, and underscores." }
    $quotedDatabase = $databaseName
    $quotedUser = $databaseUser.Replace("'", "''")
    $quotedPassword = $databasePassword.Replace("'", "''")
    $bootstrapSql = @"
CREATE DATABASE IF NOT EXISTS $quotedDatabase CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE USER IF NOT EXISTS '$quotedUser'@'127.0.0.1' IDENTIFIED BY '$quotedPassword';
ALTER USER '$quotedUser'@'127.0.0.1' IDENTIFIED BY '$quotedPassword';
GRANT ALL PRIVILEGES ON $quotedDatabase.* TO '$quotedUser'@'127.0.0.1';
FLUSH PRIVILEGES;
"@
    & $mysql --protocol=TCP --host=127.0.0.1 "--port=$DatabasePort" "-uroot" "-p$rootPassword" --skip-ssl -e $bootstrapSql
    if ($LASTEXITCODE -ne 0) { throw "Native MariaDB user/database bootstrap failed." }

    $env:DB_HOST = "127.0.0.1"
    $env:DB_PORT = "$DatabasePort"
    Write-Host "[3/4] Applying migrations to isolated native MariaDB..."
    & $alembic upgrade head
    if ($LASTEXITCODE -ne 0) { throw "Database migration failed." }

    Write-Host "[4/4] Starting QuantDesk services..."
    $launcher = Start-Process -FilePath "powershell.exe" -ArgumentList @(
        "-NoLogo", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
        (Join-Path $PSScriptRoot "start-local.ps1"), "-NoPause"
    ) -WorkingDirectory $projectRoot -Wait -PassThru
    if ($launcher.ExitCode -ne 0) { throw "QuantDesk service startup failed." }
    Write-Host "Native stack is ready: http://127.0.0.1:8300/"
}
catch {
    Write-Host "NATIVE STARTUP FAILED: $($_.Exception.Message)" -ForegroundColor Red
    exit 1
}

if (-not $NoPause) { Read-Host "Press Enter to close this launcher" }
