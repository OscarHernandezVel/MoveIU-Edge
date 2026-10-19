# Modelos de visión

Aquí va `yolov8n.onnx` (no se incluye en el repositorio: pesa 12 MB y su licencia es AGPL-3.0).

Expórtelo **una vez en su PC** (no en la Raspberry):

```powershell
python -m venv .venv-yolo
.venv-yolo\Scripts\Activate.ps1
pip install ultralytics onnx onnxslim
yolo export model=yolov8n.pt format=onnx imgsz=320
```

Copie el `yolov8n.onnx` generado a esta carpeta (`edge/models/`) y a la misma ruta en la Raspberry.
Sin el archivo, el agente funciona igual pero solo con el conteo de puertas (confianza 0).

Verificación hecha al preparar el entorno: el detector de `bus_edge` (ONNX Runtime + NMS propio) da las
mismas cajas que Ultralytics sobre su imagen de ejemplo `bus.jpg` (3 personas, puntajes 0,85 / 0,75 / 0,69
frente a 0,85 / 0,75 / 0,64), a 34 ms por imagen en una CPU x86 de servidor. En una Raspberry Pi 4 espere
varias veces más; mídalo con `python -m bus_edge check`.

**Licencia:** YOLOv8 de Ultralytics es AGPL-3.0. Para un producto comercial hace falta su licencia empresarial
o cambiar de detector (basta otra clase que implemente `Detector` en `bus_edge/vision/detector.py`).
