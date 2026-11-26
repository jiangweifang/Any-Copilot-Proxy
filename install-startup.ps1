param([string]$Python, [switch]$Headless)
$ErrorActionPreference = 'Stop'

function Get-PythonCandidates {
    if ($Python) { $Python }
    if ($env:CONDA_PREFIX) { [System.IO.Path]::Combine($env:CONDA_PREFIX, 'python.exe') }

    $condaCommands = @($env:CONDA_EXE, $env:_CONDA_EXE)
    $condaCommands += @(Get-Command conda -CommandType Application -ErrorAction SilentlyContinue |
        Select-Object -ExpandProperty Source)
    foreach ($command in $condaCommands) {
        if (-not $command) { continue }
        $directory = Split-Path -Parent $command
        if (-not $directory) { continue }
        if ((Split-Path -Leaf $directory) -in @('Scripts', 'condabin')) {
            $directory = Split-Path -Parent $directory
        }
        [System.IO.Path]::Combine($directory, 'python.exe')
    }

    $environments = Join-Path $HOME '.conda\environments.txt'
    if (Test-Path -LiteralPath $environments -PathType Leaf) {
        foreach ($prefix in (Get-Content -LiteralPath $environments)) {
            if ($prefix.Trim()) { [System.IO.Path]::Combine($prefix.Trim(), 'python.exe') }
        }
    }
    $locations = @($HOME, $env:LOCALAPPDATA, $env:ProgramData, 'C:\ProgramData')
    foreach ($location in $locations) {
        if (-not $location) { continue }
        foreach ($distribution in @('miniconda3', 'anaconda3', 'miniforge3', 'mambaforge')) {
            [System.IO.Path]::Combine($location, "$distribution\python.exe")
        }
    }
    Get-Command python,python3 -CommandType Application -ErrorAction SilentlyContinue |
        Select-Object -ExpandProperty Source
}

function Find-PythonInterpreter {
    $seen = @{}
    foreach ($candidate in (Get-PythonCandidates)) {
        if ($seen.ContainsKey($candidate)) { continue }
        $seen[$candidate] = $true
        if ($candidate -like '*\WindowsApps\*' -or
            -not (Test-Path -LiteralPath $candidate -PathType Leaf)) { continue }
        try {
            & $candidate -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' 2>$null
        } catch {
            Write-Warning "Cannot check Python interpreter '$candidate': $_"
            continue
        }
        if ($LASTEXITCODE -eq 0) { return $candidate }
    }
}
$interpreter = Find-PythonInterpreter
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
