$ErrorActionPreference = 'Stop'
$python = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) { throw 'The project Python environment is missing. Use a Python environment with this project installed to run copilot-proxy uninstall --root <project-directory>.' }
& $python -m copilot_proxy.cli uninstall --root $PSScriptRoot
exit $LASTEXITCODE
