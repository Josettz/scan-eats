# vision_service — Microservicio de visión de ScanEats

Procesa video de las cámaras (archivo o RTSP) con OpenCV + YOLOv8 + SORT/DeepSORT y **notifica eventos por HTTP** a
`restaurante_api` con un token de servicio. **Nunca accede a la base de datos.**
Visión general y despliegue: [README raíz](../README.md).

## Instalar y probar (sin GPU ni OpenCV)

```bash
python -m venv .venv && source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt                        # numpy + requests + pytest
pytest                                                     # 232 tests, sin GPU ni pesos de modelo
```

Las dependencias pesadas se importan **de forma perezosa**: `cv2`, `ultralytics` y `deep_sort_realtime` solo se cargan al
ejecutar sobre video real (`test_lazy_imports.py` lo garantiza).

## Ejecutar

```bash
cp .env.example .env                                       # VISION_API_URL, VISION_API_TOKEN, VIDEO_SOURCE…

# Escena sintética (sin cámara ni modelo) contra un backend en marcha — valida la comunicación completa
python -m scaneats_vision.simulate --api-url http://localhost:8000 --token "$VISION_API_TOKEN"

# Video real
pip install -r requirements-vision.txt                     # OpenCV, ultralytics (PyTorch), deep-sort-realtime
python -m scaneats_vision.pipeline --source video.mp4 --config config/zonas_ejemplo.json --fps 5 \
       --start-time 2026-09-12T11:00:00-05:00              # hora de pared real del inicio de la grabación
python -m scaneats_vision.pipeline --source rtsp://user:pass@ip:554/stream --clips --tracker deepsort
python -m scaneats_vision.pipeline --source video.mp4 --dry-run     # imprime los eventos sin llamar al backend
python -m scaneats_vision.preview_zones --source video.mp4 --config config/zonas_video.json --out zonas.png   # validar zonas
python -m scaneats_vision.pipeline --source video.mp4 --config config/zonas_video.json --dry-run --informe informe_video/   # ANALIZA TODO el video
```

Docker (independiente del backend): `docker build -t scaneats-vision . && docker run --env-file .env scaneats-vision --source /data/v.mp4`.

## Módulos

| Módulo | Responsabilidad | RF/RNF |
|---|---|---|
| `geometry.py` | Cajas, polígonos, **overlap geométrico** caja↔zona y asignación por centroide; adyacencia | RF-07 |
| `occupancy_engine.py` | Máquina `OCUPADA / POSIBLEMENTE_LIBRE / LIBRE` con período de gracia y **timeout corto para "un solo objeto sin persona"**; valida que el peor caso ≤ 45 s | RF-07, RNF-01/06 |
| `staff_classifier.py` + `color_utils.py` | **Cascada**: vestimenta (color de prenda) → si la confianza es baja, comportamiento (visita varias mesas brevemente, sin sentarse) | RF-08 |
| `table_fusion.py` | Mesas adyacentes ocupadas a la vez + exceso sobre la capacidad + cohesión espacial, con **histéresis** | RF-03 |
| `delivery_detector.py` | Entrega por **sustracción de fondo (MOG2)** sobre zonas de mesa + mesero presente + enfriamiento; varias por ocupación | RF-02 |
| `tracking.py` | SORT ligero (Python puro) y adaptador DeepSORT | — |
| `detector.py` | YOLOv8 (personas + vajilla) | — |
| `api_client.py` / `dispatcher.py` | Cliente HTTP con token, reintentos con backoff y cola ordenada en hilo aparte | RF-13 |
| `config_loader.py` | Carga el JSON de zonas/mesas (**TODO**: reemplazar por `GET /api/vision/configuracion/`; el conversor `from_api_payload` ya existe) | RF-04, RF-15 |
| `clip_recorder.py` | Búfer circular y recorte de clips por evento; los sube el cliente HTTP | RF-14 |
| `pipeline.py` | Orquestador end-to-end y CLI | todos |
| `evaluation.py` | Calcula RF-07 (≥ 90 %), RF-08 (≥ 85 %) y RNF-01 (≤ 45 s) desde anotaciones humanas | — |
| `simulate.py` | Escena sintética para demostrar y verificar la integración | — |
| `annotate.py` | Dibuja sobre cada fotograma las cajas de YOLO, el rol de cada persona (personal / cliente), el estado de cada mesa y los eventos; alimenta el video anotado y los clips | — |
| `analysis.py` | **Análisis de un video completo**: estadísticas de YOLO, ocupación por mesa, entregas, personal y **nivel de demanda** (baja / normal / alta) → `informe.html` + `informe.json` | — |
| `preview_zones.py` | Dibuja las zonas sobre un fotograma del video para validarlas a ojo | RF-04, RF-15 |

## Configuración de zonas

[`config/zonas_ejemplo.json`](config/zonas_ejemplo.json): 12 mesas en cuadrícula (mesas 4-5-6 apiladas → la fusión observada
en el local), la **zona de exclusión "Caja"** y tres meseros con delantal rojo/azul/verde. Las coordenadas son **normalizadas
(0..1)**: dibújelas sobre una captura real de su cámara. Las personas dentro de la zona de exclusión se descartan antes de
cualquier análisis (ni siquiera se rastrean): privacidad del personal de caja (RF-15).

## Procesar un video real (probado con `video.mp4`)

1. **Instalar**: `pip install -r requirements-vision.txt` (OpenCV + YOLOv8/PyTorch; los pesos `yolov8n.pt` se descargan solos).
2. **Dibujar las zonas** sobre un fotograma: escriba `config/zonas_<camara>.json` (ver `config/zonas_video.json`) y valídelo a ojo:
   `python -m scaneats_vision.preview_zones --source video.mp4 --config config/zonas_video.json --at 0.75 --out zonas.png`
   - `polygon`: la mesa **y sus sillas** (ahí se sienta la gente). `surface`: solo el **mantel/tablero**, donde aparecen los platos.
   - Bloques opcionales `delivery`, `behavior`, `cascade`, `tracker`: calibración de esa cámara (se validan al cargar).
3. **Probar sin escribir nada**: `... --dry-run --start-time 2026-09-19T15:00:00-05:00`. Revise los eventos contra el video.
4. **Enviar y guardar evidencia**: quite `--dry-run` y añada `--clips`.

Resultado con el `video.mp4` del equipo (7.6 min, 1080p, CPU): 2 286 fotogramas en ~4 min. Verdad de terreno vista a ojo
vs. lo detectado: la clienta se sienta a 1:24 y se va a 3:33 (detectado 15:01:24 → 15:03:33), el mesero le entrega el plato a 2:40
(detectado 15:02:40, MES-A por vestimenta, confianza 1.0), vuelve a sentarse a 5:03 y se va a 6:41 (detectado igual). 0 entregas falsas.

**Lo que enseñó el video real (y quedó corregido)**
* La zona de sillas hacía que un plato pesara ~0.4 % del área: se añadió `surface` (mantel) y umbrales de entrega por cámara (`0.2 %`, no el 5 % por defecto).
* Un cambio en la mesa **también ocurre al retirar** un plato o al gesticular: la entrega exige mesero presente ≤ 8 s **antes** de que empiece el cambio.
* **El color no discrimina si todos visten oscuro**: la camiseta azul marino del mesero mide H≈127, S≈50, V≈50 (casi negro) y la clienta también vestía oscuro.
  Se compensó con más peso al comportamiento (`seated_dwell_s=30`, veto absoluto al cliente sentado). **Para un despliegue real conviene un
  delantal de color vivo** (rojo, naranja, verde): así la vestimenta identifica *a quién* sin depender de heurísticas.
* Las pistas del tracker se rompían con oclusiones de ~3 s: `tracker.max_age=30` para esa cámara.

## Analizar TODO un video (informe)

```bash
python -m scaneats_vision.pipeline --source video.mp4 --config config/zonas_video.json \
       --start-time 2026-09-19T15:00:00-05:00 --dry-run --informe informe_video/
```

Genera en `informe_video/`: **`informe.html`** (ábralo en el navegador), `informe.json`, **`deteccion_yolo.mp4`** (el video con las detecciones dibujadas,
H.264) y `fotogramas/` (fotogramas anotados en cada momento clave). El informe incluye:

* **Detección con YOLOv8**: detecciones de persona, personas por fotograma, % de fotogramas con gente, confianza promedio, trayectorias del tracker.
* **Línea de tiempo** por mesa (barras = ocupada, rombos = entregas) y gráfica de personas / mesas ocupadas.
* **Ocupación por mesa**, entregas con su mesero y cómo se identificó, tiempos de espera.
* **Nivel de demanda**: cuántas mesas estuvieron ocupadas *a la vez* frente al total (regla: baja ≤ 25 %, alta ≥ 75 %, **pendiente de calibrar**) y una tabla de comparación
  con las fichas de observación del documento (baja 1.4 min · normal 5.2 min · alta 11.9 min).
* **Avisos honestos**: si hay pocas ocupaciones dice que los promedios *no son representativos*, y si la demanda es baja advierte que *no es hora pico* y que para validar
  el comportamiento bajo carga hace falta video de hora pico.

`--dry-run` no escribe nada en el backend; quítelo para enviar los eventos. Con `--clips` los clips de evidencia **también llevan las detecciones dibujadas**
(`--clips-crudos` los deja sin dibujar). Los clips y el video anotado se recodifican a **H.264** con el ffmpeg de `imageio-ffmpeg`: el navegador **no** reproduce
el `mp4v` que escribe OpenCV.

## Calibración con video real (pendiente)

Todos los umbrales son parámetros de `OccupancyConfig`, `FusionConfig`, `DeliveryConfig`, `BehaviorConfig` y `CascadeConfig`;
los valores por defecto son estimaciones iniciales y **están marcados en el código como "PENDIENTE DE CALIBRAR"**. Prioridad:
`single_object_timeout_s` (12 s) frente a `absent_with_objects_timeout_s` (35 s), los rangos HSV de las prendas
(`color_utils.py`) y `change_high`/`change_low` de las entregas. Medir con `python -m scaneats_vision.evaluation ocupacion muestras.csv`.

## Notas de despliegue

* El costo dominante es la inferencia de YOLOv8, no la lógica: 20 mesas / 60 personas cuestan ≈ 2 ms por fotograma en Python
  puro (medido: 1.6 ms; `test_pipeline.TestUsoDeRecursos` exige < 100 ms). La carga de RNF-06 con video real hay que medirla
  sobre el hardware final.
* `--fps 5` mantiene el hueco entre fotogramas en 0.2 s, muy por debajo del presupuesto de RNF-01 (`max_frame_gap_s`).
* Si el backend cae, los eventos se acumulan en cola (5 000) y se reenvían **en orden** al volver; los clips generados se
  suben después del evento que documentan.
