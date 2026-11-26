import json
import os
from pathlib import Path
import shutil
import subprocess
import unittest


@unittest.skipUnless(os.name == "nt" and shutil.which("powershell"), "Windows PowerShell required")
class InstallerTests(unittest.TestCase):
    def run_powershell(self, script):
        installer = Path(__file__).resolve().parents[1] / "install-startup.ps1"
        setup = r"""
$ErrorActionPreference = 'Stop'
$tokens = $null
$errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile(
    $env:INSTALLER_SCRIPT, [ref]$tokens, [ref]$errors)
if ($errors.Count) { throw ($errors | Out-String) }
foreach ($definition in $ast.FindAll({
    param($node)
    $node -is [System.Management.Automation.Language.FunctionDefinitionAst]
}, $false)) {
    Invoke-Expression $definition.Extent.Text
}
$Python = $null
$env:CONDA_PREFIX = ''
$env:CONDA_EXE = ''
$env:_CONDA_EXE = ''
function Get-Command {
    param($Name, $CommandType, $ErrorAction)
    if ($Name -contains 'python') {
        [pscustomobject]@{ Source = 'C:\System Python\python.exe' }
    }
}
function Test-Path {
    param($LiteralPath, $PathType)
    $LiteralPath -eq (Join-Path $HOME '.conda\environments.txt')
}
function Get-Content {
    param($LiteralPath)
    '  D:\Custom Conda\miniconda3  '
    ''
    'D:\Custom Conda\miniconda3\envs\newer'
    'Z:\Removed Conda'
}
"""
        result = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", setup + script],
            env={**os.environ, "INSTALLER_SCRIPT": str(installer)},
            capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_custom_conda_records_precede_system_python_and_preserve_spaces(self):
        candidates = self.run_powershell(
            "ConvertTo-Json -InputObject @(Get-PythonCandidates) -Compress"
        )
        self.assertEqual(candidates[0], r"D:\Custom Conda\miniconda3\python.exe")
        self.assertEqual(candidates[1], r"D:\Custom Conda\miniconda3\envs\newer\python.exe")
        self.assertEqual(candidates[2], r"Z:\Removed Conda\python.exe")
        self.assertEqual(candidates[-1], r"C:\System Python\python.exe")

    def test_explicit_active_and_conda_command_paths_have_priority(self):
        candidates = self.run_powershell(r"""
$Python = 'D:\Explicit\python.exe'
$env:CONDA_PREFIX = 'D:\Active'
$env:CONDA_EXE = 'E:\Conda\Scripts\conda.exe'
$env:_CONDA_EXE = 'F:\Conda\condabin\conda.bat'
ConvertTo-Json -InputObject @(Get-PythonCandidates) -Compress
""")
        self.assertEqual(candidates[:4], [
            r"D:\Explicit\python.exe", r"D:\Active\python.exe",
            r"E:\Conda\python.exe", r"F:\Conda\python.exe",
        ])

    def test_stale_old_and_duplicate_candidates_fall_back_to_supported_python(self):
        interpreter = self.run_powershell(r"""
function Get-PythonCandidates {
    'C:\Users\test\AppData\Local\Microsoft\WindowsApps\python.exe'
    'D:\Missing\python.exe'
    'Old-Python'
    'old-python'
    'Supported-Python'
}
function Test-Path {
    param($LiteralPath, $PathType)
    $LiteralPath -ne 'D:\Missing\python.exe'
}
$script:checks = @()
function Old-Python {
    $script:checks += 'old'
    $global:LASTEXITCODE = 1
}
function Supported-Python {
    $script:checks += 'supported'
    $global:LASTEXITCODE = 0
}
$selected = Find-PythonInterpreter
ConvertTo-Json -InputObject @{ interpreter = $selected; checks = $script:checks } -Compress
""")
        self.assertEqual(interpreter["interpreter"], "Supported-Python")
        self.assertEqual(interpreter["checks"], ["old", "supported"])
