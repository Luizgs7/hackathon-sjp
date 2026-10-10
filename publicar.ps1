# Publica o protótipo na Vercel (projeto dataforge-prototipo) e confere o site público.
# Uso, no PowerShell:  cd C:\Users\maygo\dev\hackathon-sjp ; powershell -ExecutionPolicy Bypass -File .\publicar.ps1
$raiz = $PSScriptRoot
$node = "C:\Users\maygo\.cache\codex-runtimes\codex-primary-runtime\dependencies\node\bin"
if (-not (Get-Command node -ErrorAction SilentlyContinue)) { $env:Path = "$node;$env:Path" }
Set-Location $raiz

Write-Host "1/3 Rodando os testes (leva ~20 s)..." -ForegroundColor Cyan
$saida = Join-Path $env:TEMP "dataforge_testes.txt"
# Os logs dos testes saem no canal de erro; o redirecionamento por cmd evita que o PowerShell os trate como falha.
cmd /c ".\.venv\Scripts\python.exe -m unittest discover -s tests > `"$saida`" 2>&1"
$codigo = $LASTEXITCODE
Get-Content $saida | Select-Object -Last 4
if ($codigo -ne 0) { Write-Host "Testes falharam: nada foi publicado." -ForegroundColor Red; exit 1 }

Write-Host "2/3 Montando o pacote e publicando..." -ForegroundColor Cyan
& .\.venv\Scripts\python.exe tools\prepare_vercel_demo.py
if ($LASTEXITCODE -ne 0) { Write-Host "Falha ao montar o pacote." -ForegroundColor Red; exit 1 }
Set-Location "$raiz\.tools\vercel-demo"
& "..\vercel-cli\node_modules\.bin\vercel.CMD" deploy --prod --yes
$deploy = $LASTEXITCODE
Set-Location $raiz
if ($deploy -ne 0) { Write-Host "O deploy retornou erro. Cole a saída acima aqui." -ForegroundColor Red; exit 1 }

Write-Host "3/3 Conferindo o site público..." -ForegroundColor Cyan
Start-Sleep -Seconds 8
$base = "https://dataforge-prototipo.vercel.app"
$falhou = $false
foreach ($p in "/login", "/manifest.webmanifest", "/sw.js", "/api/push/chave", "/notificacoes/sino") {
    $s = 0
    try { $r = Invoke-WebRequest -UseBasicParsing "$base$p" -TimeoutSec 30; $s = [int]$r.StatusCode }
    catch { if ($_.Exception.Response) { $s = [int]$_.Exception.Response.StatusCode } }
    $ok = ($s -eq 200)
    if (-not $ok) { $falhou = $true }
    $cor = if ($ok) { "Green" } else { "Red" }
    Write-Host ("{0,-24} {1}" -f $p, $s) -ForegroundColor $cor
}
if ($falhou) { Write-Host "Atenção: alguma verificação falhou. Cole a saída aqui para eu investigar." -ForegroundColor Yellow }
else { Write-Host "Publicado e verificado: $base" -ForegroundColor Green }
