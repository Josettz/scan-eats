# ScanEats

Sistema de apoyo a la gestión operativa de restaurantes: un **microservicio de visión artificial** monitorea el
estado de las mesas (libre/ocupada), detecta entregas de pedido y fusiones de mesas a partir de las cámaras de
seguridad, y un **backend Django REST** guarda los eventos y produce los reportes (tiempos de espera, desempeño del
personal, uso de mesas) para el administrador. Caso real: picantería/encebollados (el cliente paga en caja, elige mesa
y el personal lleva el pedido; nadie interactúa con ninguna app). La **caja queda fuera del monitoreo** por privacidad.

Proyecto de *Construcción de Software* — UNEMI. Requerimientos de origen: `Tarea_2_Requerimientos_ScanEats_2.pdf`.

```
                       HTTP + token de servicio                         sesión (usuario/contraseña, 30 min)
┌─────────────────────┐  ───────────────────────────▶  ┌─────────────────────┐  ◀───────────────  ┌──────────────┐
│   vision_service/   │   POST eventos / clips          │   restaurante_api/  │   GET reportes /   │ Administrador│
│  OpenCV · YOLOv8 ·  │  ◀───────────────────────────   │  Django + DRF       │   CRUD / evidencia │  / Gerente   │
│  SORT/DeepSORT      │   (nunca accede a la BD)        │  SQLite | RDS (PG)  │                    └──────────────┘
└─────────▲───────────┘                                 └─────────┬───────────┘
          │ cámaras (archivo / RTSP)                              │ clips (URL firmada)
                                                                  ▼
                                                            Amazon S3 (evidencia)
```

| Carpeta | Qué es | Tests |
|---|---|---|
| [`restaurante_api/`](restaurante_api/README.md) | Backend Django + DRF: modelo de datos, autenticación, CRUD, eventos, **3 métricas obligatorias**, evidencia (S3), retención | 121 |
| [`vision_service/`](vision_service/README.md) | Microservicio de visión: geometría, máquina de estados de ocupación, fusión, personal por vestimenta→comportamiento, entregas por sustracción de fondo, cliente HTTP | 232 |

Cada servicio tiene su `requirements.txt`, `.env.example`, README y tests. Solo se comunican por HTTP.

---

## 1. Puesta en marcha local

Requiere **Python 3.11+**. Los comandos están en bash; en PowerShell activa el entorno con `.venv\Scripts\Activate.ps1`.

### Backend (`restaurante_api/`)

```bash
cd restaurante_api
python -m venv .venv && source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt
cp .env.example .env                                       # DJANGO_DEBUG=True para desarrollo
python manage.py migrate
python manage.py cargar_demo --admin gerente --password 'Cambiar-Esta-123'   # 12 mesas + caja + 3 meseros + admin
python manage.py crear_servicio_vision                     # imprime VISION_API_TOKEN=... (se muestra una sola vez)
python manage.py runserver                                 # http://localhost:8000  (panel; inicia sesión)  ·  configuración: /admin/
```

### Microservicio de visión (`vision_service/`)

```bash
cd vision_service
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt                        # basta para lógica y tests (sin GPU ni OpenCV)
cp .env.example .env                                       # pegar VISION_API_TOKEN

# A) Sin cámara ni modelo: escena sintética contra el backend real (verifica la comunicación completa)
python -m scaneats_vision.simulate --api-url http://localhost:8000 --token <TOKEN>

# B) Con video real (instala OpenCV + YOLOv8 + PyTorch)
pip install -r requirements-vision.txt
python -m scaneats_vision.pipeline --source video.mp4 --config config/zonas_ejemplo.json \
       --api-url http://localhost:8000 --token <TOKEN> --start-time 2026-09-12T11:00:00-05:00
python -m scaneats_vision.pipeline --source rtsp://usuario:clave@192.168.1.20:554/stream --clips   # cámara en vivo
python -m scaneats_vision.pipeline --source video.mp4 --dry-run                                   # sin llamar al backend
python -m scaneats_vision.pipeline --source video.mp4 --config config/zonas_video.json --dry-run --informe informe_video/   # analiza TODO el video y genera un informe
```

Tras la simulación, con la sesión del administrador (`POST /api/auth/login/`) se pueden consultar
`/api/metricas/tiempo-espera/`, `/api/metricas/empleados/` y `/api/metricas/uso-mesas/`.

### Tests

```bash
cd restaurante_api && pytest                     # 121 tests   (también:  python manage.py test)
cd vision_service  && pytest                     # 232 tests, sin GPU ni pesos de modelo
```

Los tests del microservicio se ejecutan **sin OpenCV/YOLOv8/DeepSORT instalados**; un test lo garantiza importando todos
los módulos en un intérprete limpio (`tests/test_lazy_imports.py`).

---

## 2. Cómo se comunican los servicios

* **URL base**: `VISION_API_URL` (vision_service) → `http://<host>:8000`; todos los endpoints cuelgan de `/api/`.
* **Autenticación (RF-13)**: cabecera `Authorization: Token <token>`. El token es del usuario de servicio
  `vision_service` (grupo `servicio_vision`), sin contraseña utilizable. Se emite/rota con
  `python manage.py crear_servicio_vision` o `POST /api/auth/servicio/token/` (solo Administrador); el anterior deja de valer.
  Al arrancar, la visión llama a `GET /api/auth/servicio/validar/` y falla rápido si el token es inválido.
* **Sin acceso a la BD**: la visión identifica mesas por **número** (`mesa_numero`) y personal por **identificador**;
  no necesita conocer ids internos.
* **Idempotencia**: cada evento lleva un `uid` (UUID). Si un reintento repite una petición cuya respuesta se perdió, el backend
  devuelve el evento ya guardado (200) en vez de duplicarlo. Además hay a lo sumo una ocupación abierta por mesa (restricción de BD).
* **Resiliencia**: reintentos con backoff exponencial + jitter (`api_client.py`); una cola con hilo aparte reenvía en orden
  hasta que el backend vuelva (`dispatcher.py`). Los 4xx de negocio (409, 422) se descartan; un 401/403 se marca como crítico.

### Endpoints

| Método y ruta | Quién | RF |
|---|---|---|
| `POST /api/auth/login/` · `POST /api/auth/logout/` · `GET /api/auth/me/` | Administrador (login es la única ruta abierta, con límite de intentos) | RF-13, RNF-05 |
| `POST /api/auth/servicio/token/` · `GET /api/auth/servicio/validar/` | Administrador · Visión | RF-13 |
| `GET/POST/PUT/PATCH/DELETE /api/zonas/` · `/api/mesas/` · `/api/personal/` | Administrador (paginado, filtros por zona/texto) | RF-04, 05, 06, 15 |
| `POST /api/eventos/ocupacion/` · `POST /api/eventos/ocupacion/cambiar-estado/` | Visión | RF-01, RF-07 |
| `POST /api/eventos/entrega/` | Visión | RF-02, RF-08 |
| `POST /api/eventos/fusion/` · `POST /api/eventos/fusion/finalizar/` | Visión | RF-03 |
| `POST /api/eventos/personal-clasificado/` | Visión | RF-08 (auditoría) |
| `GET /api/eventos/{ocupacion,entrega,fusion,personal-clasificado}/` | Administrador (historial) | — |
| **`GET /api/metricas/tiempo-espera/`** (`mesa`, `zona`, `desde`, `hasta`) | Administrador | **RF-10 (+RF-09)** |
| **`GET /api/metricas/empleados/`** (`desde`, `hasta`, `zona`) | Administrador | **RF-11** |
| **`GET /api/metricas/uso-mesas/`** (`mesa`, `zona`, `desde`, `hasta`) | Administrador | **RF-12** |
| `GET /api/evidencia/<evento_id>/?tipo=ocupacion\|entrega\|fusion` | Administrador → URL firmada de S3 | RF-14 |
| `POST /api/evidencia/` (multipart) | Visión sube el clip (`evento_uid`) | RF-14, RNF-03 |
| `GET /api/vision/configuracion/` | Visión / Administrador | (para el TODO de configuración) |
| **`GET /`** — panel web del Administrador: las 3 métricas, historial y clips (pide iniciar sesión) | Administrador | RF-10, 11, 12, 14 |
| `GET /health/` | Abierta, sin datos (sonda de uptime) | RNF-02 |

Todo endpoint, salvo el login y `/health/`, exige autenticación y responde **401** sin credenciales.

---

## 3. Despliegue en AWS

**Ejecutable, no solo prosa**: [`infra/terraform/`](infra/terraform/README.md) crea RDS + S3 + EC2 (+ rol IAM,
grupos de seguridad) con `terraform apply`, y su README trae el runbook paso a paso para desplegar el código
después. **No se ejecutó en esta máquina** (no hay AWS CLI ni Terraform instalados aquí, ni credenciales de
AWS): revisado a mano, hay que correr `terraform plan` y leerlo antes de `apply`. También hay `docker-compose.yml`
en la raíz para probar el backend contra PostgreSQL real **en local**, sin AWS, antes de desplegar.

Resumen de lo que crea Terraform:

1. **RDS PostgreSQL** (`db.t3.micro`), no accesible desde internet: solo desde la EC2 del backend (grupo de seguridad dedicado).
2. **S3** privado (bloqueo de acceso público, cifrado SSE-AES256) con regla de ciclo de vida que expira `evidencia/*` a
   `EVIDENCIA_RETENCION_DIAS + 1` días — red de seguridad además del comando `purgar_evidencia` (RNF-03).
3. **EC2** con **rol IAM** de instancia (sin claves guardadas en el servidor) limitado a `s3:PutObject/GetObject/DeleteObject`
   sobre `arn:aws:s3:::<bucket>/evidencia/*`. El `user_data` deja instalado Python, un servicio `systemd` (`scaneats-api`,
   con `Restart=on-failure` → RNF-02) y el cron diario de purga; el código se despliega después con `git clone` + `.env` (ver runbook).
4. **Producción real sin AWS** (`docker-compose.yml`, raíz): `docker compose up --build` levanta PostgreSQL + el backend
   con **whitenoise** sirviendo estáticos (sin nginx) y **CORS** listo si se agrega un frontend en otro origen.
5. **Caché compartida para el límite de login** (RF-13): con varios workers de gunicorn, `CACHE_BACKEND=db` (usa una tabla de
   la misma base de datos, `manage.py createcachetable`) evita que el límite de intentos se burle cayendo en otro proceso;
   `CACHE_BACKEND=redis` es la opción recomendada si el tráfico crece (necesita un endpoint de ElastiCache aparte).
6. **Microservicio de visión**: instancia separada (idealmente con GPU, o en el local si las cámaras son RTSP internas) o
   contenedor (`docker build -t scaneats-vision vision_service/`) con `VISION_API_URL=https://<dominio>` y `VISION_API_TOKEN`.
   `vision_service/` **sí puede correr en tiempo real** contra una cámara en vivo: `--source 0` (webcam) o `--source rtsp://…`
   procesan fotograma a fotograma al ritmo real (verificado en esta máquina con una webcam local), no solo archivos grabados.
7. **Disponibilidad (RNF-02)**: monitorear `GET /health/` (CloudWatch/UptimeRobot); sigue sin medirse durante 30 días reales.

---

## 4. Trazabilidad de requerimientos

| Req. | Dónde se implementa | Dónde se prueba |
|---|---|---|
| RF-01 ocupación ≤ 3 s | `services.registrar_ocupacion`; `latencia_registro_ms` en la respuesta | `test_eventos.OcupacionTests` |
| RF-02 entrega con mesero (100 %) | `services.registrar_entrega`; `personal_identificador` obligatorio | `EntregaTests` (10 entregas simuladas) |
| RF-03 fusión, un único registro | `services.registrar_fusion`; visión: `table_fusion.py` | `FusionTests`, `test_table_fusion.py` |
| RF-04 zona con ≥ 1 mesa | `ZonaSerializer.mesas` (`allow_empty=False`) | `test_crud.ZonaTests` |
| RF-05 CRUD de mesas validado | `MesaSerializer`, `MesaViewSet` | `test_crud.MesaTests` |
| RF-06 personal sin duplicados | `PersonalSerializer.validate_identificador` (sin distinguir mayúsculas) | `test_crud.PersonalTests` |
| RF-07 ocupación ≥ 90 % | `geometry.py` + `occupancy_engine.py`; medición: `evaluation.py` | `test_geometry.py`, `test_occupancy_engine.py` |
| RF-08 vestimenta → comportamiento (≥ 85 %) | `staff_classifier.py` (cascada), `color_utils.py`; medición: `evaluation.py` | `test_staff_classifier.py`, `test_pipeline.py` |
| RF-09 tiempo de espera (± 1 s) | `services.tiempo_espera_segundos` (aritmética exacta) | `EntregaTests`, `TiempoEsperaTests` |
| RF-10 / RF-11 / RF-12 | `metricas.py` + `views_metricas.py` | `test_metricas.py` |
| RF-13 autenticación | `views_auth.py`, `permissions.py`, `DEFAULT_PERMISSION_CLASSES` | `test_auth.py` |
| RF-14 evidencia < 10 s | `views_evidencia.py`, `storage.py` (S3 / local) | `test_evidencia.py` |
| RF-15 zona de exclusión | `Zona.es_exclusion`, `services._validar_no_excluida`; visión descarta personas en la caja | `ZonaExclusionTests`, `test_pipeline.TestZonaDeExclusion` |
| RNF-01 ≤ 45 s | `OccupancyConfig.validate()` rechaza configuraciones que lo violen | `TestRNF01` |
| RNF-02 disponibilidad | `/health/`, systemd, monitoreo (despliegue) | `AccesoObligatorioTests` |
| RNF-03 retención ≥ 30 días | `EvidenciaVideo.expira_en`, `purgar_evidencia`, settings ≥ 30 | `test_evidencia.py`, `test_comandos.py` |
| RNF-04 alta en ≤ 5 pasos | Django admin + API (mesa: 2 campos; personal: 2 obligatorios + 3 opcionales) | (prueba de usabilidad con usuarios: pendiente) |
| RNF-05 hash + sesión 30 min | PBKDF2-SHA256 con sal; `SESSION_COOKIE_AGE=1800` deslizante | `SeguridadContrasenaYSesionTests` |
| RNF-06 20 mesas | 20 máquinas de estado O(1); prueba de carga lógica | `TestRNF01.test_rnf06…`, `test_pipeline.TestUsoDeRecursos` |

Las metas que dependen de **video real** (RF-07 ≥ 90 %, RF-08 ≥ 85 %, RNF-01 con 10 cámaras, RNF-06 con 20 mesas en producción)
no se pueden demostrar con datos sintéticos: `vision_service/scaneats_vision/evaluation.py` calcula esas métricas a partir de las
anotaciones humanas cuando el equipo grabe el video del local.

---

## 5. Decisiones de diseño propias (para defender ante el profesor)

**Valores numéricos inventados — TODOS pendientes de calibrar con video real del local.** Son parámetros de configuración, no constantes ocultas.

| Valor | Por defecto | Dónde | Justificación |
|---|---|---|---|
| Timeout "solo queda **un objeto**, sin personas" | **12 s** | `OccupancyConfig.single_object_timeout_s` | Pendiente abierto del documento. Bastante menor que el de cliente ausente (≈ 1/3): un objeto solo se considera olvidado. |
| Timeout "cliente **ausente con objetos**" | **35 s** | `absent_with_objects_timeout_s` | RNF-01 exige actualizar ≤ 45 s *incluyendo la gracia*, así que no puede ser de minutos (ver limitación 1). |
| Timeout mesa vacía | 8 s | `empty_table_timeout_s` | Mesa sin gente ni objetos: inequívoca. |
| Confirmar ocupación | 4 s | `occupy_confirm_s` | Filtra a quien solo pasa por la mesa. |
| Tolerancia de parpadeo del detector | 2.5 s | `presence_gap_tolerance_s` | Debe superar el hueco entre fotogramas (validado). |
| Presupuesto de latencia API / hueco entre fotogramas | 3 s / 1 s | `api_latency_budget_s`, `max_frame_gap_s` | 35 + 1 + 3 = 39 s ≤ 45 s (validado al construir el motor). |
| Confirmación de fusión | 3 s (≥ 3 observaciones) | `FusionConfig.confirm_s` | Compromiso: RF-03 pide el registro en < 5 s; más histéresis = menos falsos positivos. Terminar exige 30 s (`release_s`). |
| Simultaneidad de ocupaciones para fusionar | 90 s | `simultaneity_window_s` | Dos mesas que empiezan a la vez y sobrepasan la capacidad individual. |
| Cohesión espacial del grupo | 0.12 (normalizado) | `cohesion_link_distance` | Evita fusionar dos grupos sentados en mesas vecinas. |
| Vestimenta: aceptación / cobertura completa | 0.60 / 30 % del torso | `CascadeConfig.clothing_accept`, `ClothingClassifier.full_coverage` | Umbral de la cascada vestimenta → comportamiento. |
| Comportamiento de mesero | ≥ 3 mesas distintas, ≥ 3 visitas breves (≤ 60 s) en 5 min; sentado ≥ 90 s = cliente | `BehaviorConfig` | "Visita varias mesas brevemente, sin sentarse, de forma repetida". |
| Entrega: cambio en mesa | 5 % sube / 2 % re-arma, sostenido 1.5 s; mesero visto ≤ 20 s antes; 25 s entre entregas | `DeliveryConfig` | Histéresis de la sustracción de fondo. |
| Ventana de atribución de mesero | 15 s, espera máx. 20 s | `ScanEatsPipeline` | Mesero que se ve pero aún no se identifica. |
| Rangos HSV de colores de prenda | ver `color_utils.py` | `color_utils.py` | Dependen mucho de la iluminación del local. |
| Zonas de `config/zonas_ejemplo.json` | cuadrícula de 12 mesas | `vision_service/config/` | Coordenadas de ejemplo: hay que dibujarlas sobre una captura real. |

**Decisiones de arquitectura y de datos**

1. **Sesión de administrador (cookie) y token de servicio.** Sesión de Django con caducidad *deslizante* de 30 min (RNF-05) y token DRF para la visión.
   El token del servicio **no expira por inactividad** (un servicio en reposo nocturno no debe desautenticarse); se **rota** a demanda. Una contraseña se hashea; un token no es una contraseña.
2. **`hora_inicio` = cuándo se sentaron; `detectado_en` = cuándo se confirmó.** El tiempo de espera se mide desde el inicio real; el criterio de RF-01 (≤ 3 s) mide *registro − detección* (`latencia_registro_ms`), no la latencia visual.
3. **Tiempo de espera = 1.ª entrega − ocupación** (una ocupación puede tener varias entregas). "Mesa atendida" = ocupación distinta en la que el empleado hizo ≥ 1 entrega. "Hora pico" = hora local con más ocupaciones iniciadas.
4. **`Zona ⇄ Mesa`**: la FK vive en `Mesa.zona` (nulable); RF-04 se valida en la API (una zona no se guarda sin mesas ni puede quedar vacía al mover/borrar su única mesa). **La caja se registra como una mesa** (99) dentro de la zona de exclusión, porque RF-04 exige ≥ 1 mesa por zona; el backend responde **422** a cualquier evento sobre esa zona y la visión ni siquiera rastrea a las personas allí (RF-15, doble barrera).
5. **Prenda distintiva en `Personal`** (`prenda_tipo`, `prenda_color`). Si dos meseros usan el mismo color se reconoce "personal" pero no "quién"; la identidad la resuelve la memoria del track. El comportamiento por sí solo distingue *personal vs. cliente*, no *cuál* mesero.
6. **No se inventa un mesero.** Si se detecta una entrega pero jamás se identifica a quién, no se reporta con un mesero al azar: se cuenta en `stats.entregas_sin_atribuir` (visible para calibrar). El backend exige mesero en toda entrega (RF-02).
7. **Fusión = un registro** (relación M2M `mesas` + `ocupaciones`); si crece ({4,5} → {4,5,6}) se actualiza el mismo registro. Termina al liberarse todas las mesas o tras 30 s sin cumplirse la condición.
8. **`/health/` abierta**: única excepción (además del login) a "todo exige autenticación"; no devuelve datos y sirve para el monitoreo de RNF-02.
9. **SORT-lite** en Python puro (IoU + velocidad constante) en lugar de SORT con Kalman/húngaro: suficiente para ≤ 20 mesas y testeable sin numpy/scipy; **DeepSORT** queda disponible como adaptador opcional.
10. **Agregados de métricas en Python**, no en SQL: mismo resultado exacto en SQLite y PostgreSQL. Con volúmenes de restaurante (cientos de eventos/día) es suficiente.
11. **Local vs S3**: el mismo código de evidencia funciona contra disco (desarrollo) o S3 (producción, URLs firmadas de 5 min, SSE-AES256). El clip lo sube la visión *por HTTP* (no toca S3 ni la BD).

**Limitaciones conocidas (honestas)**

1. **RNF-01 vs. "cliente ausente"**: como el estado debe actualizarse ≤ 45 s *incluyendo la gracia*, una mesa cuyo cliente falta más de ~35 s con platos se marcará libre aunque vuelva. La máquina lo tolera: si regresa, `POSIBLEMENTE_LIBRE → OCUPADA` continúa la *misma* ocupación mientras no se haya confirmado LIBRE.
2. Con **un** video real (7.6 min, una cámara, una clienta y un mesero) el flujo completo funciona y coincide con lo visto a ojo (ver `vision_service/README.md`), pero **no hay cifras de exactitud**: RF-07 (100 muestras), RF-08 (20 entregas) y RNF-01 con 10 cámaras están instrumentadas (`evaluation.py`), no medidas. La vestimenta solo identifica si el uniforme es de color distintivo (en ese video mesero y clienta vestían oscuro).
3. Los clips se generan con `mp4v` (OpenCV) y se **recodifican a H.264 automáticamente** con el ffmpeg embebido de `imageio-ffmpeg`
   (verificado: se reproducen en el navegador); si esa recodificación fallara, el clip queda en `mp4v` y se avisa en el log.
4. Un mesero de espaldas y sin patrón de movimiento previo no cuenta como "personal presente" al instante de la entrega; se resuelve al identificarlo (ventana de 20 s) o se cuenta como no atribuido.
5. RNF-04 (usabilidad con 5 usuarios) y RNF-02 (uptime 30 días) requieren pruebas con personas y tiempo real; aquí solo se dejó la infraestructura (servicio `systemd` con reinicio automático, sonda `/health/`).
6. El panel web (`/`) es de solo lectura y sencillo (HTML del servidor, sin JavaScript); la configuración de mesas/zonas/personal se hace en el **Django admin** (`/admin/`) o por API. CORS ya está listo (`CORS_ALLOWED_ORIGINS`) si se agrega un frontend en otro origen.
7. **AWS y Docker nunca se ejecutaron en esta máquina** (no hay AWS CLI, Terraform ni Docker instalados aquí): el `Dockerfile` del backend, el `docker-compose.yml` y todo `infra/terraform/` están escritos y revisados a mano, pero no verificados corriendo. Antes de confiar en ellos: `docker compose config`, `docker compose up`, y `terraform plan` (leyendo el plan) antes de `apply`.
8. El modo **tiempo real** del microservicio de visión (`--source 0` o una URL RTSP) sí se verificó de punta a punta contra una webcam de esta máquina — abre la cámara, respeta el ritmo real de fotogramas por segundo y corre YOLO+tracking+clasificación sin fallar — pero **no contra una cámara IP de un restaurante real**; la reconexión automática ante cortes de señal (`run_video`) tiene lógica de reintento pero no se probó con una cámara real desconectándose.
