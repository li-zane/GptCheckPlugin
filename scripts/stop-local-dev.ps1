$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$stateFile = Join-Path $root '.run\gptcheckplugin-local-dev.json'

if (-not (Test-Path -LiteralPath $stateFile)) {
    Write-Output 'No GptCheckPlugin local development state file found.'
    exit 0
}

function Stop-ProcessTree([int]$processId) {
    $children = Get-CimInstance Win32_Process -Filter "ParentProcessId=$processId" -ErrorAction SilentlyContinue
    foreach ($child in $children) { Stop-ProcessTree ([int]$child.ProcessId) }
    Stop-Process -Id $processId -Force -ErrorAction SilentlyContinue
}

$state = Get-Content -LiteralPath $stateFile -Raw | ConvertFrom-Json
if ([int]$state.pid -gt 0) { Stop-ProcessTree ([int]$state.pid) }
$listenerPids = Get-NetTCPConnection -State Listen -LocalPort 5173 -ErrorAction SilentlyContinue |
    Select-Object -ExpandProperty OwningProcess -Unique
foreach ($pidValue in $listenerPids) { Stop-ProcessTree ([int]$pidValue) }
Remove-Item -LiteralPath $stateFile -Force
Write-Output 'GptCheckPlugin local development process stopped.'
