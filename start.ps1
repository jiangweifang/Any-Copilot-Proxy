$ErrorActionPreference = 'Stop'
$env:HOST = '127.0.0.1'

Set-Location -LiteralPath $PSScriptRoot -ErrorAction Stop
$node = (Get-Command node -ErrorAction Stop).Source
foreach ($port in @(4140, 4141)) {
    if (Get-NetTCPConnection -State Listen -LocalPort $port -ErrorAction SilentlyContinue) {
        throw "Port $port is already in use; no existing processes were changed."
    }
}

$bridge = $null
$adapter = $null
try {
    $bridgePath = Join-Path $PSScriptRoot 'node_modules\copilot-bridge\dist\main.js'
    $bridge = Start-Process -FilePath $node -ArgumentList @("`"$bridgePath`"", 'start', '--host', '127.0.0.1', '--port', '4141', '--rate-limit', '3', '--wait') -WorkingDirectory $PSScriptRoot -NoNewWindow -PassThru -RedirectStandardOutput (Join-Path $PSScriptRoot 'proxy.log') -RedirectStandardError (Join-Path $PSScriptRoot 'proxy-error.log')
    $adapterPath = Join-Path $PSScriptRoot 'adapter.mjs'
    $adapter = Start-Process -FilePath $node -ArgumentList "`"$adapterPath`"" -WorkingDirectory $PSScriptRoot -NoNewWindow -PassThru -RedirectStandardOutput (Join-Path $PSScriptRoot 'adapter.log') -RedirectStandardError (Join-Path $PSScriptRoot 'adapter-error.log')

    while (-not $bridge.HasExited -and -not $adapter.HasExited) {
        Start-Sleep -Seconds 1
    }
    throw "Copilot service stopped unexpectedly. Check proxy-error.log and adapter-error.log."
}
finally {
    foreach ($child in @($adapter, $bridge)) {
        if ($null -ne $child -and -not $child.HasExited) {
            Stop-Process -Id $child.Id -ErrorAction Continue
        }
    }
}
