param(
    [switch]$Demo,
    [ValidateRange(1, 65535)][int]$Port = 8000,
    [string]$BindAddress = "127.0.0.1"
)

$ErrorActionPreference = "Stop"
Push-Location -LiteralPath $PSScriptRoot
try {
    if (-not $env:GATEWAY_API_KEY) {
        throw 'Set $env:GATEWAY_API_KEY before starting the gateway.'
    }
    if ($Demo) {
        $env:GATEWAY_ACCOUNTS = Join-Path $PSScriptRoot "accounts.demo.json"
    } elseif (-not $env:GATEWAY_ACCOUNTS) {
        $env:GATEWAY_ACCOUNTS = Join-Path $PSScriptRoot "accounts.json"
    }
    if (Test-Path -LiteralPath (Join-Path $PSScriptRoot ".vendor/fastapi")) {
        $dependencyPath = Join-Path $PSScriptRoot ".vendor"
        $env:PYTHONPATH = if ($env:PYTHONPATH) {
            $dependencyPath + [IO.Path]::PathSeparator + $env:PYTHONPATH
        } else { $dependencyPath }
        $pythonPath = "python"
    } elseif (Test-Path -LiteralPath (Join-Path $PSScriptRoot ".venv/Scripts/python.exe")) {
        $pythonPath = Join-Path $PSScriptRoot ".venv/Scripts/python.exe"
    } else {
        $pythonPath = "python"
    }
    & $pythonPath -m uvicorn main:app --host $BindAddress --port $Port --workers 1 --no-access-log
    if ($LASTEXITCODE -ne 0) {
        throw "Gateway exited with code $LASTEXITCODE"
    }
} finally {
    Pop-Location
}
