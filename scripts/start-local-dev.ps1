[CmdletBinding()]
param(
    [switch]$Foreground
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$stateFile = Join-Path $root '.run\gptcheckplugin-local-dev.json'
$logDir = Join-Path $root '.run'
$envFile = Join-Path $root '.env'
if (-not (Test-Path -LiteralPath $envFile)) { throw "Missing $envFile" }

# The other local launchers run in this PowerShell process when invoked by the
# operations wrapper. Clear only their overlapping variables; Python then
# loads this project's own `.env` with its native dotenv parser.
foreach ($name in @(
    'DATABASE_URL', 'HTTP_ADDR', 'APP_ENCRYPTION_KEY_BASE64', 'PICKUP_KEY_PEPPER_BASE64',
    'ADMIN_API_TOKEN', 'AUTO_MIGRATE', 'VITE_BACKEND_ORIGIN', 'SERVER_PORT',
    'EXTERNAL_RESOURCE_MANAGER_ADDR', 'EXTERNAL_RESOURCE_MANAGER_WEB_DIR',
    'DAILY_CASHBACK_BASE_URL', 'DAILY_CASHBACK_BROWSER_URL',
    'EXTERNAL_RESOURCE_MANAGER_SERVICE_TOKEN_FILE', 'EXTERNAL_RESOURCE_MANAGER_FRAME_ANCESTORS',
    'HOST', 'PORT', 'STATIC_DIR', 'SUB2API_ADMIN_API_KEY_FILE',
    'DAILY_CASHBACK_DATABASE_URL', 'DAILY_CASHBACK_DATABASE_URL_FILE',
    'DAILY_CASHBACK_FRAME_ANCESTORS', 'DATABASE_PATH', 'SUB2API_BASE_URL'
)) {
    Remove-Item -Path "Env:$name" -ErrorAction SilentlyContinue
}
$venvBin = Join-Path $root '.venv\Scripts'
if (Test-Path -LiteralPath (Join-Path $venvBin 'python.exe')) {
    $env:Path = "$venvBin;$env:Path"
}

if ($null -ne (Get-NetTCPConnection -State Listen -LocalPort 5173 -ErrorAction SilentlyContinue)) {
    throw 'Port 5173 is already in use. Resolve the conflict explicitly; this launcher never selects another port.'
}

if ($Foreground) {
    Push-Location $root
    try { & npm.cmd run dev } finally { Pop-Location }
    exit $LASTEXITCODE
}

New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$stdout = Join-Path $logDir 'gptcheckplugin-local-dev.log'
$stderr = Join-Path $logDir 'gptcheckplugin-local-dev.err.log'
$process = Start-Process -FilePath 'npm.cmd' -ArgumentList @('run', 'dev') -WorkingDirectory $root -RedirectStandardOutput $stdout -RedirectStandardError $stderr -WindowStyle Hidden -PassThru
@{ pid = $process.Id; startedAt = (Get-Date).ToString('o') } |
    ConvertTo-Json | Set-Content -LiteralPath $stateFile -Encoding ASCII
Write-Output 'GptCheckPlugin local development started.'
Write-Output 'URL:   http://127.0.0.1:5173'
Write-Output "State: $stateFile"
