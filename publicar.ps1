# Publica o protótipo na Vercel (projeto dataforge-prototipo) e confere o site público.
# Uso, no PowerShell:  cd C:\Users\maygo\dev\hackathon-sjp ; .\publicar.ps1
$ErrorActionPreference = "Stop"
$raiz = $PSScriptRoot
$node = "C:\Users\maygo\.cache\codex-runtimes\codex-primary-runtime\dependencies\node\bin"
if (-not (Get-Command node -ErrorAction SilentlyContinue)) { $env:Path = "$node;$env:Path" }

Set-Location $raiz
Write-Host "1/3 Rodando os testes..." -ForegroundColor Cyan
& .\.venv\Scripts\python.exe -m unittest discover -s tests 2>&1 | Select-Object -Last 3
if ($LASTEXITCODE -ne 0) { throw "Testes falharam: não publicar." }

Write-Host "2/3 Montando o pacote e publicando..." -ForegroundColor Cyan
& .\.venv\Scripts\python.exe tools\prepare_vercel_demo.py
Set-Location "$raiz\.tools\vercel-demo"
& "..\vercel-cli\node_modules\.bin\vercel.CMD" deploy --prod --yes
Set-Location $raiz

Write-Host "3/3 Conferindo o site público..." -ForegroundColor Cyan
Start-Sleep -Seconds 8
$base = "https://dataforge-prototipo.vercel.app"
$falhou = $false
foreach ($p in "/login", "/manifest.webmanifest", "/sw.js", "/api/push/chave", "/notificacoes/sino") {
    try { $r = Invoke-WebRequest -UseBasicParsing "$base$p" -TimeoutSec 30; $s = [int]$r.StatusCode } catch { $s = [int]$_.Exception.Response.StatusCode }
    $ok = ($s -eq 200)
    if (-not $ok) { $falhou = $true }
    Write-Host ("{0,-24} {1}" -f $p, $s) -ForegroundColor ($(if ($ok) { "Green" } else { "Red" }))
}
if ($falhou) { Write-Host "Atenção: alguma verificação falhou. Cole a saída aqui para eu investigar." -ForegroundColor Yellow }
else { Write-Host "Publicado e verificado: $base" -ForegroundColor Green }
