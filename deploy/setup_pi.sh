#!/usr/bin/env bash
# Prepara una Raspberry Pi 4/5 (Raspberry Pi OS Bookworm 64 bits) para la maqueta del bus.
#
#   cd ~/core-service-py/edge
#   bash deploy/setup_pi.sh            # instala todo; vuelva a ejecutarlo sin miedo (es idempotente)
#   bash deploy/setup_pi.sh --service  # además deja el agente arrancando solo al encender
#
# Qué hace: paquetes del sistema, entorno virtual de Python, grupos dialout/video, regla udev para
# /dev/arduino, UART para el GPS NEO-6M (opcional), carpetas de datos y, con --service, la unidad systemd.
set -euo pipefail

EDGE_DIR="$(cd "$(dirname "$0")/.." && pwd)"
USER_NAME="$(id -un)"
WITH_SERVICE=false
[[ "${1:-}" == "--service" ]] && WITH_SERVICE=true

say() { printf '\n==> %s\n' "$*"; }

if [[ "$(uname -m)" != "aarch64" ]]; then
  echo "Aviso: este script está pensado para Raspberry Pi OS de 64 bits (aarch64); aquí es $(uname -m)."
fi

say "Paquetes del sistema"
sudo apt-get update -q
sudo apt-get install -y -q python3-venv python3-dev libgl1 libglib2.0-0 v4l-utils sqlite3

say "Entorno virtual de Python en $EDGE_DIR/.venv"
python3 -m venv "$EDGE_DIR/.venv"
"$EDGE_DIR/.venv/bin/pip" install -q --upgrade pip
"$EDGE_DIR/.venv/bin/pip" install -q -r "$EDGE_DIR/requirements.txt"

say "Permisos: puerto serie (dialout) y cámaras (video) para $USER_NAME"
sudo usermod -aG dialout,video "$USER_NAME"

say "Nombre fijo /dev/arduino"
sudo cp "$EDGE_DIR/deploy/99-bus-edge.rules" /etc/udev/rules.d/99-bus-edge.rules
sudo udevadm control --reload
sudo udevadm trigger

say "UART para el GPS NEO-6M en GPIO14/15 (opcional; la consola serie se desactiva)"
if command -v raspi-config >/dev/null 2>&1; then
  sudo raspi-config nonint do_serial_hw 0     # habilita el puerto serie
  sudo raspi-config nonint do_serial_cons 1   # sin consola de login en el puerto serie
fi

say "Carpetas y configuración"
mkdir -p "$EDGE_DIR/data" "$EDGE_DIR/models" "$EDGE_DIR/maqueta"
if [[ ! -f "$EDGE_DIR/edge.env" ]]; then
  cp "$EDGE_DIR/deploy/edge.env.example" "$EDGE_DIR/edge.env"
  chmod 600 "$EDGE_DIR/edge.env"
  echo "Creado edge.env: ponga EDGE_SERVER_URL y EDGE_INGEST_KEY antes de arrancar."
fi
if [[ ! -f "$EDGE_DIR/maqueta/bus_2101_layout.json" ]]; then
  (cd "$EDGE_DIR" && PYTHONPATH=src .venv/bin/python -m bus_edge layout)
fi
[[ -f "$EDGE_DIR/models/yolov8n.onnx" ]] || echo "Falta models/yolov8n.onnx (ver models/README.md). Sin él solo cuentan las puertas."

if $WITH_SERVICE; then
  say "Servicio systemd bus-edge"
  sed -e "s#^User=.*#User=$USER_NAME#" -e "s#^Group=.*#Group=$USER_NAME#" \
      -e "s#/home/pi/core-service-py/edge#$EDGE_DIR#g" \
      "$EDGE_DIR/deploy/bus-edge.service" | sudo tee /etc/systemd/system/bus-edge.service >/dev/null
  sudo systemctl daemon-reload
  sudo systemctl enable bus-edge
  echo "Listo. Arranque con: sudo systemctl start bus-edge   (mensajes: journalctl -u bus-edge -f)"
fi

say "Hecho. Cierre sesión y vuelva a entrar (o reinicie) para que los grupos dialout/video apliquen."
echo "Prueba del hardware:   cd $EDGE_DIR && PYTHONPATH=src .venv/bin/python -m bus_edge check"
