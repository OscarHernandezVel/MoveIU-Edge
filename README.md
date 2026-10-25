# edge — Hardware a bordo de la maqueta del bus

Lo que va **dentro del bus**: el contador de puertas (Arduino UNO + cuatro FC-51, dos por puerta), las dos cámaras con el
detector de personas, el equipo de a bordo que las lee (en la maqueta, **un portátil con Windows**), la posición
(recorrido simulado de la ruta real, o un GPS NEO-6M) y el envío de una lectura cada 3 segundos al servicio central.
Implementa el Módulo 4 del documento v7 en su versión de maqueta ("Componentes Tecnológicos Prototipo").

```
2 FC-51 delanteros ─┐                         USB 3.0 (1) ┌─ webcam frontal (filas 1-5)
2 FC-51 traseros ───┤ Arduino UNO ──3.er USB──► Portátil ◄─┤
LEDs + pulsador ────┘  sube/baja por el orden     (Windows) USB 3.0 (2) └─ webcam trasera (filas 6-11)
                                          │  detecta personas, las ubica en su silla o posición de pie,
                                          │  guarda en SQLite y envía cada 3 s (en un hilo aparte)
                                          └──Wi-Fi──► servicio central (FastAPI)
```

Una Raspberry Pi 4/5 sigue siendo una alternativa sin cambios de código (sección 3.2).

**Privacidad (réplica 3D).** Cada lectura puede incluir `cabin`: la lista de posiciones ocupadas
(`"S04-L1"`, `"P05"`, hasta 80). Es lo único que sale de las cámaras hacia el servicio: las imágenes se procesan en
el equipo de a bordo y se descartan. El servicio la entrega en `GET /api/v1/buses/{id}/cabin` y el gemelo 3D
(`web/public/arquitectura/index.html`) dibuja muñecos genéricos en esas posiciones. Solo se envía si alguna
cámara vio la cabina.

Ninguna imagen sale del equipo de a bordo ni se guarda en disco: solo viajan números (PB-N07).

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
4. Subir. En el Monitor serie (115200 baudios) verá `$BOOT,door-counter-2.0,...` y un `$HB,...` por segundo.
   Pase la mano por la puerta delantera de afuera hacia adentro (primero el FC-51 exterior, luego el interior):
   aparece `$DOOR,...,F,IN,...` y parpadea el LED verde. Al revés, `$DOOR,...,F,OUT,...` y el LED rojo; si
   tapa solo uno de los dos, `$DOOR,...,F,ABORTED,...`.

La versión 1.0 (un FC-51 por puerta) ocupaba 6,7 KB de 32 KB y 461 bytes de RAM; la 2.0 agrega dos sensores y
su lógica de dirección: verifique el tamaño con el botón **Verificar** del IDE. La lógica de filtrado y de
dirección se prueba en el PC: `g++ -std=c++11 -I firmware/door_counter firmware/tests/test_firmware_logic.cpp -o t && ./t`.

Con el Arduino conectado al PC ya puede usar el contador real: `EDGE_SERIAL_PORT=auto` (o `COM3`) y
`python -m bus_edge doors` para ver los conteos en vivo.

## 3. Preparar el equipo de a bordo

### 3.1 Portátil con Windows (el de la maqueta)

Las dos webcams van a los puertos USB 3.0 y el Arduino al tercer puerto, sin hub. Desde `edge\`, en PowerShell:

```powershell
powershell -ExecutionPolicy Bypass -File deploy\setup_windows.ps1   # .venv, dependencias, edge.env y data\
$env:PYTHONPATH = "src"
.venv\Scripts\python.exe -m bus_edge check     # puertos COM, Arduino, cámaras (con su número) y modelo
powershell -ExecutionPolicy Bypass -File deploy\run_edge.ps1        # arranca el agente y lo reinicia si se cae
```

* `edge.env` sale de `deploy\edge.env.example`, ya pensado para el portátil: webcams `1` y `2` (la `0` suele ser la
  integrada; confírmelo con `check`), Arduino `auto`, GPS `replay` y modelo a 640 px.
* **Cierre el Monitor serie del Arduino IDE** antes de arrancar: el agente necesita el puerto COM.
* Si `check` no ve una webcam: Configuración → Privacidad y seguridad → Cámara → permitir a las aplicaciones de
  escritorio.
* Mientras corre, el agente impide que Windows suspenda el equipo. Deje el cargador conectado y desactive la
  suspensión selectiva de USB (Opciones de energía): si no, Windows puede apagar las cámaras o el Arduino.
* Para que arranque solo al iniciar sesión: `deploy\setup_windows.ps1 -Startup` (tarea programada `MoveIU-Edge`).
* Sin cámaras todavía, ensaye con videos grabados: `EDGE_CAMERAS=front=videos\frente.mp4,rear=videos\atras.mp4`
  (se repiten en bucle).

### 3.2 Alternativa: Raspberry Pi 4/5

Raspberry Pi OS **Bookworm 64 bits** (Raspberry Pi Imager: active SSH y configure el Wi-Fi). Copie la carpeta
`core-service-py` (o solo `edge/` y `backend/demo_data/`) y ejecute:

```bash
cd ~/core-service-py/edge
bash deploy/setup_pi.sh            # paquetes, entorno Python, permisos, /dev/arduino, UART del GPS
# cierre sesión y vuelva a entrar; luego:
PYTHONPATH=src .venv/bin/python -m bus_edge check     # puertos, Arduino, cámaras y modelo
```

Para que arranque solo al encender: `bash deploy/setup_pi.sh --service` y `sudo systemctl start bus-edge`
(mensajes con `journalctl -u bus-edge -f`). En `edge.env` use `EDGE_CAMERAS=front=0,rear=1`, `EDGE_MODEL_INPUT=320`
y, sin pantalla, `opencv-python-headless` en lugar de `opencv-python`.

## 4. Modelo de visión

Exporte YOLOv8n a ONNX una vez (`imgsz=640` para el portátil) y copie `yolov8n.onnx` a `models/` (instrucciones y
licencia en [`models/README.md`](models/README.md)). Si el modelo se exportó con otro tamaño, el agente usa el del
modelo y lo avisa. Sin modelo el agente sigue enviando el conteo de puertas.

## 5. Calibrar las cámaras (RF-9.4)

Con la maqueta armada, las cámaras fijas y las figuras sentadas:

```powershell
$env:PYTHONPATH = "src"
.venv\Scripts\python.exe -m bus_edge calibrate --camera front
.venv\Scripts\python.exe -m bus_edge calibrate --camera rear
```

Cada cámara se abre con el número que tiene en `EDGE_CAMERAS` (o el de `--device`) y a 640×480, la misma imagen que
verá el agente. Haga clic en las 4 esquinas de cada silla, posición de pie y puerta, en el orden que indica la ventana. El
piso de la maqueta tiene grabado el código de cada posición (`S03-L1`, `P04`...), el mismo que pide la
herramienta. Se guarda en `maqueta/bus_2101_layout.json`, con el formato de `bus_positions` del servicio.

## 6. Qué hace el agente cada 3 segundos

1. Toma la posición (GPS o recorrido simulado). Al llegar a una terminal reinicia el contador (RNF-6.3).
2. Lee el total a bordo según las puertas y las cajas detectadas por cada cámara.
3. Ubica a cada persona en su posición: silla, posición de pie, zona de puerta o fuera de posición (sobrecupo).
4. Guarda la lectura en SQLite **antes** de enviarla (RNF-8.1). Sin red, se acumulan hasta 60 000 lecturas
   (más de 48 h) y al volver la red se envían en orden cronológico en lotes de 500 (RNF-8.2). El envío corre en un
   hilo aparte: un servidor lento (8 s de espera por lectura, 30 s por lote) no atrasa las lecturas.

## 7. Pruebas

```bash
pip install -r requirements-dev.txt
pytest                      # protocolo, contador, GPS, visión, cola SQLite, cliente HTTP, maqueta (78 pruebas)
CORE_TEST_DATABASE_URL=postgresql://... pytest    # 81: añade las pruebas contra el servicio central real
ruff check src tests maqueta docs && mypy
```

La prueba de punta a punta levanta el servicio central con uvicorn y PostgreSQL, deja al agente sin red
tres ciclos (las lecturas esperan en SQLite) y comprueba que al volver la red las cuatro lecturas llegan en
un lote y el estado vivo del bus refleja el conteo de puertas.

## 8. Límites de esta versión

* Con dos FC-51 por puerta el sentido sale del orden de los haces, en las dos puertas. Dos personas que cruzan
  pegadas sin dejar libres los haces cuentan como una; lo corrige la fusión con las cámaras (RF-6.5). La
  separación de 15–25 mm y la espera de 400 ms entre cruces **no se han probado con la maqueta real**.
* YOLOv8n viene entrenado con fotos de personas reales. Con las minifiguras de Lego puede fallar: pruebe primero
  con `check` y con la calibración a la vista; si detecta mal, hace falta el conjunto de datos propio y el
  reentrenamiento que describen RF-12.1 y RF-12.2.
* El rendimiento del detector en el portátil no se midió (falta el `.onnx`; en una CPU de servidor: 34 ms por imagen).
  `check` lo mide en cuanto el modelo esté en `models/`.
* El clasificador TinyML de puertas (RF-12.3) no cabe en el UNO; el filtro por tiempos cumple RNF-6.1.
