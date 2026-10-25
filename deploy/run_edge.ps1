# Arranca el agente en el portátil y lo vuelve a arrancar si se cae (equivale a bus-edge.service en Linux).
#   powershell -ExecutionPolicy Bypass -File deploy\run_edge.ps1
# Ctrl+C lo detiene. Mientras corre, el agente impide que Windows suspenda el equipo (la pantalla sí puede apagarse).
$Edge = Split-Path -Parent $PSScriptRoot
Set-Location $Edge
$env:PYTHONPATH = Join-Path $Edge "src"
$python = Join-Path $Edge ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) { throw "Falta .venv: ejecute primero deploy\setup_windows.ps1" }

while ($true) {
  & $python -m bus_edge --env edge.env run
  $code = $LASTEXITCODE
  if ($code -eq 0) { break }                       # salida normal (Ctrl+C)
  if ($code -eq 2) { Write-Host "Revise edge.env (configuración incompleta)." -ForegroundColor Yellow; break }
  Write-Host "El agente terminó con el código $code; se reinicia en 5 s." -ForegroundColor Yellow
  Start-Sleep -Seconds 5
}
