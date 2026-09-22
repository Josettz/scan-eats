# restaurante_api — Backend de ScanEats

Django 5.2 + Django REST Framework. Fuente única de verdad: modelo de datos, autenticación, reportes y evidencia.
Lo consume el microservicio de visión (reporta eventos) y el Administrador (configura y consulta reportes).
Visión general, despliegue en AWS, trazabilidad RF/RNF y decisiones de diseño: [README raíz](../README.md).

## Ejecutar

```bash
python -m venv .venv && source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt
cp .env.example .env                                       # editar; DJANGO_DEBUG=True en desarrollo
python manage.py migrate
python manage.py cargar_demo --admin gerente --password 'Cambiar-Esta-123'
python manage.py crear_servicio_vision                     # token para vision_service/.env (VISION_API_TOKEN)
python manage.py runserver
```

* Sin `DB_HOST` usa **SQLite** (`db.sqlite3`, o la ruta de `SQLITE_PATH`); con `DB_HOST` usa **PostgreSQL/RDS**.
* `DJANGO_DEBUG=False` exige `DJANGO_SECRET_KEY` (el arranque falla si falta).
* **`http://localhost:8000/`** — panel del Administrador (pide iniciar sesión): tiempo de espera, empleado con más mesas, uso de mesas, historial y clips.
* **`/admin/`** (Django admin): alta de mesas, zonas y personal.

## Tests

```bash
pytest                       # 121 tests
python manage.py test        # mismo resultado con el runner nativo de Django
```

Cubren: las 3 métricas obligatorias, CRUD, reglas de negocio (RF-04 rechaza zona sin mesas, RF-06 rechaza duplicados,
RF-13 rechaza credenciales inválidas sin revelar cuál dato falló), sesión de 30 min, zona de exclusión, eventos idempotentes,
fusión de un solo registro, evidencia contra disco y contra S3 simulado, y la purga por retención.

## Modelo de datos

`Zona` (`es_exclusion`, `region`) · `Mesa` (`numero`, `capacidad`, `zona`) · `Personal` (`identificador` único,
`prenda_tipo`, `prenda_color`, `activo`) · `EventoOcupacion` (mesa, zona congelada, `estado`, `hora_inicio`, `hora_fin`,
`detectado_en`) · `EventoEntrega` (ocupación, personal, hora; **varias por ocupación**) · `FusionMesas` (M2M a mesas y
ocupaciones, **un registro por fusión**) · `ClasificacionPersonal` (auditoría de RF-08) · `EvidenciaVideo` (a un evento,
`s3_key`, `timestamp`, `expira_en`). Usuarios: `django.contrib.auth` + `rest_framework.authtoken`.

`region` es un polígono normalizado `[[x, y], …]` (0..1) que consume la visión vía `GET /api/vision/configuracion/`.

## Ejemplos

```bash
# Login del Administrador (cookie de sesión) y las 3 métricas del profesor
curl -c cj -X POST localhost:8000/api/auth/login/ -H 'Content-Type: application/json' -d '{"username":"gerente","password":"…"}'
curl -b cj "localhost:8000/api/metricas/tiempo-espera/?zona=1&desde=2026-09-12&hasta=2026-09-12"
curl -b cj "localhost:8000/api/metricas/empleados/?desde=2026-09-01&hasta=2026-09-30"
curl -b cj "localhost:8000/api/metricas/uso-mesas/"

# Como el microservicio de visión
curl -X POST localhost:8000/api/eventos/ocupacion/ -H "Authorization: Token $TOKEN" -H 'Content-Type: application/json' \
     -d '{"mesa_numero": 3, "hora_inicio": "2026-09-12T11:00:00-05:00", "uid": "5b9c…"}'
curl -X POST localhost:8000/api/eventos/entrega/ -H "Authorization: Token $TOKEN" -H 'Content-Type: application/json' \
     -d '{"mesa_numero": 3, "personal_identificador": "MES-A", "metodo_identificacion": "VESTIMENTA", "confianza": 0.93}'
```

Las fechas `desde`/`hasta` aceptan `YYYY-MM-DD` (día completo, hora local `America/Guayaquil`) o un datetime ISO 8601.
Los empates en `empleados` y `uso-mesas` devuelven a **todos** los empatados.

## Comandos de gestión

| Comando | Para qué |
|---|---|
| `cargar_demo [--admin U --password P]` | 12 mesas + caja (zona de exclusión) + 3 meseros; idempotente |
| `crear_servicio_vision` | Crea el usuario de servicio y emite/rota su token |
| `purgar_evidencia [--dry-run]` | RNF-03: borra de S3/disco y de la BD los clips vencidos (programar con cron a diario) |

## Variables de entorno

Ver [`.env.example`](.env.example). Las relevantes: `DJANGO_SECRET_KEY`, `DJANGO_DEBUG`, `DJANGO_ALLOWED_HOSTS`,
`DB_HOST/DB_NAME/DB_USER/DB_PASSWORD` (RDS), `CLIP_STORAGE_BACKEND=local|s3`, `AWS_STORAGE_BUCKET_NAME`,
`EVIDENCIA_RETENCION_DIAS` (≥ 30; menor que 30 impide arrancar), `LOGIN_THROTTLE_RATE`.
