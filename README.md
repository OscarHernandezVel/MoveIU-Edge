# edge — Hardware a bordo de la maqueta del bus

Lo que va **dentro del bus**: el contador de puertas (Arduino UNO + dos FC-51), las dos cámaras con el
detector de personas (Raspberry Pi), la posición (GPS NEO-6M o recorrido simulado de la ruta real) y el
envío de una lectura cada 3 segundos al servicio central. Implementa el Módulo 4 del documento v7 en su
versión de maqueta ("Componentes Tecnológicos Prototipo").

```
FC-51 delantero ─┐                         ┌─ webcam frontal (filas 1-5)
FC-51 trasero ───┤ Arduino UNO ──USB──► Raspberry Pi ◄──USB── webcam trasera (filas 6-11)
LEDs + pulsador ─┘  cuenta pasos          │  detecta personas, las ubica en su silla o posición de pie,
                                          │  guarda en SQLite y envía cada 3 s
                                          └──Wi-Fi──► servicio central (FastAPI)
```

**Privacidad (réplica 3D).** Cada lectura puede incluir `cabin`: la lista de posiciones ocupadas
(`"S04-L1"`, `"P05"`, hasta 80). Es lo único que sale de las cámaras hacia el servicio: las imágenes se procesan en
la Raspberry y se descartan. El servicio la entrega en `GET /api/v1/buses/{id}/cabin` y el gemelo 3D
(`web/public/arquitectura/index.html`) dibuja muñecos genéricos en esas posiciones. Solo se envía si alguna
cámara vio la cabina.

Ninguna imagen sale de la Raspberry ni se guarda en disco: solo viajan números (PB-N07).

| Documento | Contenido |
|---|---|
| [`firmware/door_counter/`](firmware/door_counter/) | Programa del Arduino (`door_counter.ino`) |

## 1. Empezar en su PC, sin hardware

Así se prueba toda la cadena antes de tener el Arduino y las cámaras (Windows, PowerShell, desde `edge\`):

```powershell
python -m venv .venv
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.venv\Scripts\Activate.ps1
pip install -r requirements-dev.txt
copy deploy\edge.env.example edge.env        # y edítelo (ver abajo)
$env:PYTHONPATH = "src"
python -m bus_edge run
```

En `edge.env`, para esta prueba: `EDGE_SERVER_URL=http://127.0.0.1:8080`, `EDGE_INGEST_KEY` igual a la
`CORE_INGEST_KEY` con la que arrancó el servicio central, `EDGE_SERIAL_PORT=sim` (puertas simuladas),
`EDGE_CAMERAS=off` y `EDGE_GPS=replay` (el bus 2101 recorre la ruta C1 real). Cada 3 s verá una línea como:

```
[C1] puertas: 4 a bordo | sin cámaras | enviadas 1, en cola 0
```

Y el bus se moverá en el mapa (`python simulator/watch_stream.py --routes C1` en la carpeta del servicio).

## 2. Cargar el programa en el Arduino UNO

1. Instale **Arduino IDE 2** (arduino.cc). Si su UNO es un clon con chip CH340, instale también su controlador.
2. Abra `firmware/door_counter/door_counter.ino` (el IDE abre también `door_logic.h` y `protocol.h`).
3. Herramientas → Placa: **Arduino Uno**; Puerto: el COM que aparece al conectar el cable.
4. Subir. En el Monitor serie (115200 baudios) verá `$BOOT,door-counter-1.0,...` y un `$HB,...` por segundo.
   Pase la mano frente al FC-51 delantero: aparece `$DOOR,...,F,IN,...` y parpadea el LED verde.

El programa ocupa 6,7 KB de 32 KB y 461 bytes de RAM (compilado aquí con avr-gcc para ATmega328P). Su lógica
de filtrado se prueba en el PC: `g++ -std=c++11 -I firmware/door_counter firmware/tests/test_firmware_logic.cpp -o t && ./t`.

Con el Arduino conectado al PC ya puede usar el contador real: `EDGE_SERIAL_PORT=auto` (o `COM3`) y
`python -m bus_edge doors` para ver los conteos en vivo.

## 3. Preparar la Raspberry Pi

Raspberry Pi OS **Bookworm 64 bits** (Raspberry Pi Imager: active SSH y configure el Wi-Fi). Copie la carpeta
`core-service-py` (o solo `edge/` y `backend/demo_data/`) y ejecute:

```bash
cd ~/core-service-py/edge
bash deploy/setup_pi.sh            # paquetes, entorno Python, permisos, /dev/arduino, UART del GPS
# cierre sesión y vuelva a entrar; luego:
PYTHONPATH=src .venv/bin/python -m bus_edge check     # puertos, Arduino, cámaras y modelo
```

Para que arranque solo al encender: `bash deploy/setup_pi.sh --service` y `sudo systemctl start bus-edge`
(mensajes con `journalctl -u bus-edge -f`).

## 4. Modelo de visión

Exporte YOLOv8n a ONNX una vez en su PC y copie `yolov8n.onnx` a `models/` (instrucciones y licencia en
[`models/README.md`](models/README.md)). Sin modelo el agente sigue enviando el conteo de puertas.

## 5. Calibrar las cámaras (RF-9.4)

Con la maqueta armada, las cámaras fijas y las figuras sentadas:

```bash
PYTHONPATH=src .venv/bin/python -m bus_edge calibrate --camera front --device 0
PYTHONPATH=src .venv/bin/python -m bus_edge calibrate --camera rear  --device 1
```

Haga clic en las 4 esquinas de cada silla, posición de pie y puerta, en el orden que indica la ventana. El
piso de la maqueta tiene grabado el código de cada posición (`S03-L1`, `P04`...), el mismo que pide la
herramienta. Se guarda en `maqueta/bus_2101_layout.json`, con el formato de `bus_positions` del servicio.

## 6. Qué hace el agente cada 3 segundos

1. Toma la posición (GPS o recorrido simulado). Al llegar a una terminal reinicia el contador (RNF-6.3).
2. Lee el total a bordo según las puertas y las cajas detectadas por cada cámara.
3. Ubica a cada persona en su posición: silla, posición de pie, zona de puerta o fuera de posición (sobrecupo).
4. Guarda la lectura en SQLite **antes** de enviarla (RNF-8.1). Sin red, se acumulan hasta 60 000 lecturas
   (más de 48 h) y al volver la red se envían en orden cronológico en lotes de 500 (RNF-8.2).

## 7. Pruebas

```bash
pip install -r requirements-dev.txt
pytest                      # protocolo, contador, GPS, visión, cola SQLite, cliente HTTP, maqueta (78 pruebas)
CORE_TEST_DATABASE_URL=postgresql://... pytest    # 81: añade las pruebas contra el servicio central real
ruff check src tests maqueta && mypy
```

La prueba de punta a punta levanta el servicio central con uvicorn y PostgreSQL, deja al agente sin red
tres ciclos (las lecturas esperan en SQLite) y comprueba que al volver la red las cuatro lecturas llegan en
un lote y el estado vivo del bus refleja el conteo de puertas.

## 8. Límites de esta versión

* Con un solo FC-51 por puerta la dirección se asume: la puerta delantera cuenta subidas y la trasera bajadas.
  Quien baje por delante suma en vez de restar. Con dos sensores por puerta se sabría el sentido (RF-12.3).
* YOLOv8n viene entrenado con fotos de personas reales. Con las minifiguras de Lego puede fallar: pruebe primero
  con `check` y con la calibración a la vista; si detecta mal, hace falta el conjunto de datos propio y el
  reentrenamiento que describen RF-12.1 y RF-12.2.
* El rendimiento del detector en la Raspberry no se midió aquí (en una CPU de servidor: 34 ms por imagen).
* El clasificador TinyML de puertas (RF-12.3) no cabe en el UNO; el filtro por tiempos cumple RNF-6.1.
