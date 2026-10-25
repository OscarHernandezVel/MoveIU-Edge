# Modelos de visión

Aquí va `yolov8n.onnx` (no se incluye en el repositorio: pesa 12 MB y su licencia es AGPL-3.0).

Expórtelo **una vez**, en un entorno aparte (Ultralytics trae PyTorch, que el agente no necesita):

```powershell
python -m venv .venv-yolo
.venv-yolo\Scripts\Activate.ps1
pip install ultralytics onnx onnxslim
yolo export model=yolov8n.pt format=onnx imgsz=640
```

Copie el `yolov8n.onnx` generado a esta carpeta (`edge/models/`) del equipo de a bordo.
Sin el archivo, el agente funciona igual pero solo con el conteo de puertas (confianza 0).

**Tamaño de entrada:** un modelo exportado tiene un tamaño fijo. `imgsz=640` es el del portátil (las minifiguras
del fondo miden ~30 píxeles); en una Raspberry use `imgsz=320`. Si `EDGE_MODEL_INPUT` no coincide con el del
modelo, el agente usa el del modelo y lo avisa en el registro.

Verificación hecha al preparar el entorno: el detector de `bus_edge` (ONNX Runtime + NMS propio) da las
mismas cajas que Ultralytics sobre su imagen de ejemplo `bus.jpg` (3 personas, puntajes 0,85 / 0,75 / 0,69
frente a 0,85 / 0,75 / 0,64), a 34 ms por imagen en una CPU x86 de servidor con el modelo a 320 px. El tiempo en el
portátil (a 640 px, unas 4 veces más cómputo) no se ha medido: `python -m bus_edge check` lo informa.

**Licencia:** YOLOv8 de Ultralytics es AGPL-3.0. Para un producto comercial hace falta su licencia empresarial
o cambiar de detector (basta otra clase que implemente `Detector` en `bus_edge/vision/detector.py`).
