param([switch]$Headless)
$ErrorActionPreference = 'Stop'
$python = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) { throw 'Run install-startup.bat first.' }
if ($Headless) {
    & $python -m copilot_proxy.cli serve --root $PSScriptRoot
    exit $LASTEXITCODE
}
$pythonw = Join-Path $PSScriptRoot '.venv\Scripts\pythonw.exe'
Start-Process -FilePath $pythonw -ArgumentList @('-m', 'copilot_proxy.desktop', '--root', "`"$PSScriptRoot`"") -WorkingDirectory $PSScriptRoot
