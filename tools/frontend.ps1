param([ValidateSet('install','build','watch','catalog','serve','check')][string]$Command = 'build')
$taskRoot = Split-Path -Parent $PSScriptRoot
& (Join-Path $taskRoot '.venv/Scripts/python.exe') (Join-Path $PSScriptRoot 'frontend.py') $Command
exit $LASTEXITCODE
