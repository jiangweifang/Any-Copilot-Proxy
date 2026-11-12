$ErrorActionPreference = 'Stop'
$env:HOST = '127.0.0.1'

Set-Location -LiteralPath $PSScriptRoot -ErrorAction Stop
$node = (Get-Command node -ErrorAction Stop).Source
$config = Get-Content -LiteralPath (Join-Path $PSScriptRoot 'adapter-config.json') -Raw -Encoding UTF8 | ConvertFrom-Json
$ports = @{ port = 4140; bridgePort = 4141 }
foreach ($name in @('port', 'bridgePort')) {
    if ($config.PSObject.Properties.Name -contains $name) {
        $value = $config.$name
        if (($value -isnot [int] -and $value -isnot [long] -and $value -isnot [double]) -or $value -lt 1 -or $value -gt 65535 -or [Math]::Truncate($value) -ne $value) {
            throw "Set $name to an integer between 1 and 65535 in adapter-config.json"
        }
        $ports[$name] = $value
    }
}
if ($ports.port -eq $ports.bridgePort) {
    throw 'port and bridgePort must be different in adapter-config.json'
}
foreach ($port in @($ports.port, $ports.bridgePort)) {
    if (Get-NetTCPConnection -State Listen -LocalPort $port -ErrorAction SilentlyContinue) {
        throw "Port $port is already in use; no existing processes were changed."
    }
}

$bridge = $null
$adapter = $null
try {
    $bridgePath = Join-Path $PSScriptRoot 'node_modules\copilot-bridge\dist\main.js'
    $bridge = Start-Process -FilePath $node -ArgumentList @("`"$bridgePath`"", 'start', '--host', '127.0.0.1', '--port', [string]$ports.bridgePort, '--rate-limit', '3', '--wait') -WorkingDirectory $PSScriptRoot -NoNewWindow -PassThru -RedirectStandardOutput (Join-Path $PSScriptRoot 'proxy.log') -RedirectStandardError (Join-Path $PSScriptRoot 'proxy-error.log')
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
