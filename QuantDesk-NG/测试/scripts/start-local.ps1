param(
    [switch]$NoPause
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$cli = Join-Path $projectRoot ".venv\Scripts\quantdesk-ng.exe"
$alembic = Join-Path $projectRoot ".venv\Scripts\alembic.exe"
$envFile = Join-Path $projectRoot ".env"
$logDir = Join-Path $projectRoot "logs"
$exitCode = 0

function Read-DotEnvValue {
    param(
        [string]$Path,
        [string]$Name,
        [string]$DefaultValue
    )

    if (-not (Test-Path -LiteralPath $Path)) {
        return $DefaultValue
    }

    $line = Get-Content -LiteralPath $Path | Where-Object {
        $_ -match "^\s*$([regex]::Escape($Name))\s*="
    } | Select-Object -Last 1

    if (-not $line) {
        return $DefaultValue
    }

    $value = ($line -split "=", 2)[1].Trim().Trim('"').Trim("'")
    if ([string]::IsNullOrWhiteSpace($value)) {
        return $DefaultValue
    }
    return $value
}

function Test-ApiHealth {
    param([string]$Uri)

    try {
        $health = Invoke-RestMethod -Uri $Uri -Method Get -TimeoutSec 2
        return ($health.status -eq "ok" -and $health.version -eq "0.2.0")
    }
    catch {
        return $false
    }
}

function Get-WorkerStatusText {
    $text = (& $cli worker-status 2>&1 | Out-String)
    if ($LASTEXITCODE -ne 0) {
        throw "Unable to read worker leases.`n$text"
    }
    return $text
}

function Test-WorkerActive {
    param(
        [string]$StatusText,
        [string]$Role
    )

    $escapedRole = [regex]::Escape("quantdesk-ng:$Role")
    if (-not ($StatusText -match "(?m)'worker_key': '$escapedRole'.*'status': 'active'")) {
        return $false
    }

    # A lease can remain active for its TTL after a process was terminated.
    # Do not treat that stale lease as a running worker; verify the exact
    # QuantDesk command line as well so the launcher can recover automatically.
    $processPattern = [regex]::Escape("worker --role $Role")
    $process = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
        Where-Object {
            $_.CommandLine -and
            $_.CommandLine -match "QuantDesk-NG" -and
            $_.CommandLine -match $processPattern
        } |
        Select-Object -First 1
    return ($null -ne $process)
}

function Show-LogTail {
    param([string]$Path)

    if (Test-Path -LiteralPath $Path) {
        Write-Host "`nLast lines from $Path" -ForegroundColor Yellow
        Get-Content -LiteralPath $Path -Tail 20
    }
}

try {
    Write-Host "=========================================="
    Write-Host " QuantDesk NG 0.2.0 - Local Launcher"
    Write-Host "=========================================="

    if (-not (Test-Path -LiteralPath $cli)) {
        throw "Missing $cli. Create the virtual environment and install the project first."
    }
    if (-not (Test-Path -LiteralPath $alembic)) {
        throw "Missing $alembic. Install the project dependencies first."
    }
    if (-not (Test-Path -LiteralPath $envFile)) {
        throw "Missing $envFile. Copy .env.example to .env and configure it first."
    }

    New-Item -ItemType Directory -Path $logDir -Force | Out-Null
    $migrationStdout = Join-Path $logDir "migration.out.log"
    $migrationStderr = Join-Path $logDir "migration.err.log"

    Write-Host "[1/3] Applying database migrations..."
    $migrationProcess = Start-Process -FilePath $alembic `
        -ArgumentList @("upgrade", "head") `
        -WorkingDirectory $projectRoot `
        -WindowStyle Hidden `
        -RedirectStandardOutput $migrationStdout `
        -RedirectStandardError $migrationStderr `
        -Wait `
        -PassThru
    if ($migrationProcess.ExitCode -ne 0) {
        Show-LogTail -Path $migrationStdout
        Show-LogTail -Path $migrationStderr
        throw "Database migration failed."
    }
    Write-Host "      Database is ready." -ForegroundColor Green

    Write-Host "[2/3] Checking background workers..."
    $workerStatus = Get-WorkerStatusText
    foreach ($role in @("market", "news", "paper", "intelligence")) {
        if (Test-WorkerActive -StatusText $workerStatus -Role $role) {
            Write-Host "      $role worker is already running."
            continue
        }

        $stdout = Join-Path $logDir "$role-worker.out.log"
        $stderr = Join-Path $logDir "$role-worker.err.log"
        Start-Process -FilePath $cli `
            -ArgumentList @("worker", "--role", $role) `
            -WorkingDirectory $projectRoot `
            -WindowStyle Hidden `
            -RedirectStandardOutput $stdout `
            -RedirectStandardError $stderr | Out-Null
        Write-Host "      Started $role worker."
    }

    Start-Sleep -Seconds 2
    $workerStatus = Get-WorkerStatusText
    $missingWorkers = @()
    foreach ($role in @("market", "news", "paper", "intelligence")) {
        if (-not (Test-WorkerActive -StatusText $workerStatus -Role $role)) {
            $missingWorkers += $role
            Show-LogTail -Path (Join-Path $logDir "$role-worker.err.log")
        }
    }
    if ($missingWorkers.Count -gt 0) {
        throw "Workers did not become active: $($missingWorkers -join ', ')."
    }
    Write-Host "      All workers are active." -ForegroundColor Green

    $portText = Read-DotEnvValue -Path $envFile -Name "APP_PORT" -DefaultValue "8300"
    $port = 0
    if (-not [int]::TryParse($portText, [ref]$port)) {
        throw "APP_PORT in .env is not a valid port: $portText"
    }
    $healthUri = "http://127.0.0.1:$port/api/v2/health"
    $appUri = "http://127.0.0.1:$port/"

    Write-Host "[3/3] Checking the API..."
    if (Test-ApiHealth -Uri $healthUri) {
        Write-Host "      API is already running."
    }
    else {
        $apiStdout = Join-Path $logDir "api.out.log"
        $apiStderr = Join-Path $logDir "api.err.log"
        Start-Process -FilePath $cli `
            -ArgumentList @("serve") `
            -WorkingDirectory $projectRoot `
            -WindowStyle Hidden `
            -RedirectStandardOutput $apiStdout `
            -RedirectStandardError $apiStderr | Out-Null

        $apiReady = $false
        for ($attempt = 0; $attempt -lt 20; $attempt++) {
            Start-Sleep -Seconds 1
            if (Test-ApiHealth -Uri $healthUri) {
                $apiReady = $true
                break
            }
        }
        if (-not $apiReady) {
            Show-LogTail -Path $apiStderr
            throw "API did not become healthy within 20 seconds."
        }
        Write-Host "      API started successfully." -ForegroundColor Green
    }

    Write-Host ""
    Write-Host "QuantDesk NG is ready: $appUri" -ForegroundColor Green
    Write-Host "Logs: $logDir"
    Write-Host "Running start.bat again is safe; active services will be reused."
}
catch {
    $exitCode = 1
    Write-Host ""
    Write-Host "STARTUP FAILED: $($_.Exception.Message)" -ForegroundColor Red
    Write-Host "Logs: $logDir"
}

if (-not $NoPause) {
    Write-Host ""
    Read-Host "Press Enter to close this launcher"
}

exit $exitCode
