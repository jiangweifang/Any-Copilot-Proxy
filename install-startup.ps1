param([string]$Python, [switch]$Headless)
$ErrorActionPreference = 'Stop'
$candidates = @()
if ($Python) { $candidates += $Python }
$candidates += @(Get-Command python,python3 -CommandType Application -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Source)
if ($env:CONDA_PREFIX) { $candidates += Join-Path $env:CONDA_PREFIX 'python.exe' }
$candidates += @((Join-Path $HOME 'miniconda3\python.exe'), 'C:\ProgramData\miniconda3\python.exe',
    (Join-Path $HOME 'anaconda3\python.exe'), 'C:\ProgramData\anaconda3\python.exe')
$interpreter = $null
foreach ($candidate in $candidates) {
    if ($candidate -like '*\WindowsApps\*' -or -not (Test-Path -LiteralPath $candidate)) { continue }
    $result = & $candidate -c 'import sys; print("COPILOT_PYTHON_OK" if sys.version_info >= (3, 11) else "OLD")'
    if ($LASTEXITCODE -eq 0 -and $result -eq 'COPILOT_PYTHON_OK') { $interpreter = $candidate; break }
}
if (-not $interpreter) { throw 'Python 3.11+ was not found. Activate Conda, or run .\install-startup.ps1 -Python <full-path-to-python.exe>.' }
$arguments = @((Join-Path $PSScriptRoot 'bootstrap.py'))
if ($Headless) { $arguments += '--headless' }
& $interpreter @arguments
exit $LASTEXITCODE
