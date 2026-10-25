# Prepara el portátil con Windows como equipo de a bordo de la maqueta (equivale a setup_pi.sh).
#
# Uso, en PowerShell desde la carpeta edge\:
#   powershell -ExecutionPolicy Bypass -File deploy\setup_windows.ps1            # instala (puede repetirlo)
#   powershell -ExecutionPolicy Bypass -File deploy\setup_windows.ps1 -Startup   # además arranca el agente al iniciar sesión
#
# Qué hace: entorno virtual .venv con las dependencias, edge.env a partir del ejemplo (si no existe), carpeta data\
# y, con -Startup, una tarea programada "MoveIU-Edge" que ejecuta deploy\run_edge.ps1 al iniciar sesión.
# No cambia la configuración de energía: eso se hace a mano (vea docs\hardware.md, sección 4).
param([switch]$Startup)
$ErrorActionPreference = "Stop"
$Edge = Split-Path -Parent $PSScriptRoot
Set-Location $Edge

function Say($text) { Write-Host "==> $text" -ForegroundColor Cyan }

Say "Python 3.11 o más reciente"
$python = (Get-Command python -ErrorAction SilentlyContinue).Source
if (-not $python) { throw "No se encontró Python. Instale Python 3.11+ (python.org o Microsoft Store) y vuelva a ejecutar." }
& $python -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)"
if ($LASTEXITCODE -ne 0) { throw "Se necesita Python 3.11 o más reciente." }

Say "Entorno virtual .venv"
if (-not (Test-Path ".venv\Scripts\python.exe")) { & $python -m venv .venv }
$venv = Join-Path $Edge ".venv\Scripts\python.exe"

Say "Dependencias (requirements.txt)"
& $venv -m pip install -q -r requirements.txt
if ($LASTEXITCODE -ne 0) {
  # Algunos equipos (antivirus o proxy que inspecciona HTTPS) solo confían en el almacén de certificados de Windows.
  Say "Reintentando con los certificados de Windows (truststore)"
  & $venv -m pip install -q --use-feature=truststore -r requirements.txt
  if ($LASTEXITCODE -ne 0) { throw "No se pudieron instalar las dependencias." }
}

Say "Configuración edge.env y carpeta data"
if (-not (Test-Path "edge.env")) {
  Copy-Item "deploy\edge.env.example" "edge.env"
  Write-Host "    edge.env creado: complete EDGE_SERVER_URL y EDGE_INGEST_KEY antes de arrancar." -ForegroundColor Yellow
}
New-Item -ItemType Directory -Force "data" | Out-Null

if ($Startup) {
  Say "Tarea programada MoveIU-Edge (al iniciar sesión)"
  $action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Minimized -File `"$Edge\deploy\run_edge.ps1`"" `
    -WorkingDirectory $Edge
  $trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
  $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew
  Register-ScheduledTask -TaskName "MoveIU-Edge" -Action $action -Trigger $trigger -Settings $settings -Force | Out-Null
  Write-Host "    Para quitarla: Unregister-ScheduledTask -TaskName MoveIU-Edge -Confirm:`$false"
}

Say "Listo. Pruebe el hardware y luego arranque el agente:"
Write-Host "    .venv\Scripts\python.exe -m bus_edge check   (con `$env:PYTHONPATH = 'src')"
Write-Host "    powershell -ExecutionPolicy Bypass -File deploy\run_edge.ps1"
