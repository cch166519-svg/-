$ErrorActionPreference = 'Stop'
$bundled = Join-Path $env:USERPROFILE '.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
if (Test-Path -LiteralPath $bundled) { $runtime = $bundled } else { $runtime = (Get-Command python -ErrorAction Stop).Source }
Set-Location -LiteralPath $PSScriptRoot
& $runtime (Join-Path $PSScriptRoot 'app.py')
