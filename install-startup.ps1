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
    try { & $candidate -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' 2>$null } catch { continue }
    if ($LASTEXITCODE -eq 0) { $interpreter = $candidate; break }
}
if (-not $interpreter) {
    Write-Host '未找到 Python 3.11 或更高版本。' -ForegroundColor Red
    Write-Host '请任选一种方式解决：'
    Write-Host '  1. 安装 Python 3.11+（https://www.python.org/downloads/），安装时勾选 "Add python.exe to PATH"；'
    Write-Host '  2. 先激活 Conda 环境（conda activate <环境名>）后再运行；'
    Write-Host '  3. 手动指定解释器：.\install-startup.ps1 -Python "C:\完整路径\python.exe"'
    exit 1
}
$arguments = @((Join-Path $PSScriptRoot 'bootstrap.py'))
if ($Headless) { $arguments += '--headless' }
& $interpreter @arguments
exit $LASTEXITCODE
