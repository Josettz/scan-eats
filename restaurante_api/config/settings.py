# restaurante_api/config/settings.py
"""
Settings de ScanEats (backend).

Todo lo que cambia entre desarrollo y AWS se lee de variables de entorno
(ver .env.example):

* Sin DB_HOST  -> SQLite local (desarrollo / tests).
* Con DB_HOST  -> PostgreSQL (Amazon RDS).
* CLIP_STORAGE_BACKEND=local -> clips en disco; =s3 -> bucket de S3 (EC2 con rol IAM).
"""
import os
import sys
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured

BASE_DIR = Path(__file__).resolve().parent.parent


def _cargar_env(ruta):
    """Carga un .env simple (KEY=VALUE) sin pisar variables ya definidas."""
    if not ruta.exists():
        return
    for linea in ruta.read_text(encoding="utf-8").splitlines():
        linea = linea.strip()
        if not linea or linea.startswith("#") or "=" not in linea:
            continue
        clave, valor = linea.split("=", 1)
        valor = valor.strip()
        if valor[:1] in {'"', "'"} and valor.count(valor[0]) >= 2:
            valor = valor[1:valor.index(valor[0], 1)]
        else:  # sin comillas: se descartan los comentarios en la misma línea (`KEY=x  # nota`)
            valor = valor.split(" #", 1)[0].strip()
        os.environ.setdefault(clave.strip(), valor)


_cargar_env(BASE_DIR / ".env")


def env_bool(nombre, por_defecto=False):
    valor = os.environ.get(nombre)
    if valor is None:
        return por_defecto
    return valor.strip().lower() in {"1", "true", "yes", "si", "on"}


def env_list(nombre, por_defecto=""):
    return [v.strip() for v in os.environ.get(nombre, por_defecto).split(",") if v.strip()]


TESTING = "pytest" in sys.modules or "test" in sys.argv
DEBUG = env_bool("DJANGO_DEBUG", False)

SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY")
if not SECRET_KEY:
    if DEBUG or TESTING:
        SECRET_KEY = "insecure-dev-key-solo-para-desarrollo-y-tests"
    else:
        raise ImproperlyConfigured("Defina DJANGO_SECRET_KEY en el entorno (ver .env.example).")

ALLOWED_HOSTS = env_list("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1,testserver")
CSRF_TRUSTED_ORIGINS = env_list("DJANGO_CSRF_TRUSTED_ORIGINS", "")

# CORS: el panel (/) y la API se sirven del MISMO origen por defecto, así que esto no hace falta para el
# uso normal. Se deja listo por si en el futuro se agrega un frontend separado (otro dominio/puerto):
# defina CORS_ALLOWED_ORIGINS="https://miapp.com,http://localhost:5173" en el entorno. Vacío = sin CORS.
CORS_ALLOWED_ORIGINS = env_list("CORS_ALLOWED_ORIGINS", "")
CORS_ALLOW_CREDENTIALS = True  # necesario porque el login usa cookies de sesión, no solo el token

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "rest_framework",
    "rest_framework.authtoken",  # token de servicio del Microservicio de Visión
    "django_filters",
    "corsheaders",
    "scaneats",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",  # sirve /static/ sin necesitar nginx (RF-04/RNF-04: panel usable ya)
    "corsheaders.middleware.CorsMiddleware",  # debe ir antes de CommonMiddleware
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"

# --------------------------------------------------------------------------- #
# Base de datos: SQLite en local, PostgreSQL (RDS) si se define DB_HOST.
# --------------------------------------------------------------------------- #
if os.environ.get("DB_HOST"):
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": os.environ.get("DB_NAME", "scaneats"),
            "USER": os.environ.get("DB_USER", "scaneats"),
            "PASSWORD": os.environ.get("DB_PASSWORD", ""),
            "HOST": os.environ["DB_HOST"],
            "PORT": os.environ.get("DB_PORT", "5432"),
            "CONN_MAX_AGE": int(os.environ.get("DB_CONN_MAX_AGE", "60")),
            "OPTIONS": {"sslmode": os.environ.get("DB_SSLMODE", "require")},
        }
    }
else:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": os.environ.get("SQLITE_PATH", BASE_DIR / "db.sqlite3"),
        }
    }

# --------------------------------------------------------------------------- #
# Autenticación y sesiones (RF-13, RNF-05)
# --------------------------------------------------------------------------- #
# Contraseñas: hasher por defecto de Django (PBKDF2-SHA256 con sal aleatoria por usuario).
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

# RNF-05: la sesión del Administrador expira a los 30 min de INACTIVIDAD.
# SESSION_SAVE_EVERY_REQUEST renueva la caducidad en cada petición, por lo que
# el plazo es "deslizante" (30 min desde la última actividad, no desde el login).
SESSION_COOKIE_AGE = 30 * 60
SESSION_SAVE_EVERY_REQUEST = True
SESSION_EXPIRE_AT_BROWSER_CLOSE = True
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"

_produccion = not DEBUG and not TESTING
SESSION_COOKIE_SECURE = env_bool("DJANGO_COOKIE_SECURE", _produccion)
CSRF_COOKIE_SECURE = SESSION_COOKIE_SECURE
SECURE_SSL_REDIRECT = env_bool("DJANGO_SSL_REDIRECT", False)  # activar detrás del ALB/nginx con TLS
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SECURE_HSTS_SECONDS = int(os.environ.get("DJANGO_HSTS_SECONDS", "0"))
SECURE_CONTENT_TYPE_NOSNIFF = True

# Nombre de usuario / grupo del token de servicio del Microservicio de Visión.
SERVICIO_VISION_USERNAME = os.environ.get("SERVICIO_VISION_USERNAME", "vision_service")

# --------------------------------------------------------------------------- #
# DRF
# --------------------------------------------------------------------------- #
REST_FRAMEWORK = {
    # Token primero: así una petición sin credenciales responde 401 (no 403).
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "rest_framework.authentication.TokenAuthentication",
        "rest_framework.authentication.SessionAuthentication",
    ],
    # Todo exige autenticación salvo lo que se declare explícitamente abierto (login, /health/).
    "DEFAULT_PERMISSION_CLASSES": ["rest_framework.permissions.IsAuthenticated"],
    "DEFAULT_PAGINATION_CLASS": "scaneats.pagination.PaginacionEstandar",
    "DEFAULT_FILTER_BACKENDS": [
        "django_filters.rest_framework.DjangoFilterBackend",
        "rest_framework.filters.OrderingFilter",
    ],
    "DEFAULT_RENDERER_CLASSES": ["rest_framework.renderers.JSONRenderer"]
    + (["rest_framework.renderers.BrowsableAPIRenderer"] if DEBUG else []),
    "DEFAULT_THROTTLE_RATES": {"login": os.environ.get("LOGIN_THROTTLE_RATE", "10/min")},
    "PAGE_SIZE": 20,
}

# --------------------------------------------------------------------------- #
# Evidencia de video (RF-14, RNF-03)
# --------------------------------------------------------------------------- #
CLIP_STORAGE_BACKEND = os.environ.get("CLIP_STORAGE_BACKEND", "local")  # "local" | "s3"
CLIP_LOCAL_ROOT = Path(os.environ.get("CLIP_LOCAL_ROOT", BASE_DIR / "clips_locales"))
CLIP_MAX_BYTES = int(os.environ.get("CLIP_MAX_MB", "100")) * 1024 * 1024
AWS_STORAGE_BUCKET_NAME = os.environ.get("AWS_STORAGE_BUCKET_NAME", "")
AWS_S3_REGION_NAME = os.environ.get("AWS_S3_REGION_NAME", "us-east-1")
AWS_S3_SIGNED_URL_EXPIRES = int(os.environ.get("AWS_S3_SIGNED_URL_EXPIRES", "300"))
# Las credenciales NO se guardan aquí: en EC2 se usa el rol IAM de la instancia (cadena por defecto de boto3).

# RNF-03: los clips se conservan MÍNIMO 30 días. La política del restaurante puede ampliarlo, nunca reducirlo.
EVIDENCIA_RETENCION_DIAS = int(os.environ.get("EVIDENCIA_RETENCION_DIAS", "30"))
if EVIDENCIA_RETENCION_DIAS < 30:
    raise ImproperlyConfigured("EVIDENCIA_RETENCION_DIAS no puede ser menor a 30 (RNF-03).")

# --------------------------------------------------------------------------- #
# Internacionalización
# --------------------------------------------------------------------------- #
LANGUAGE_CODE = "es"
TIME_ZONE = os.environ.get("DJANGO_TIME_ZONE", "America/Guayaquil")
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATIC_ROOT.mkdir(exist_ok=True)  # whitenoise avisa si la carpeta no existe; collectstatic la llena en el build
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    # Comprime y le pone hash al nombre de cada archivo estático; whitenoise los sirve directo desde
    # gunicorn (sin nginx) con cache-control agresivo. Requiere `manage.py collectstatic` en el build.
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"},
}
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# --------------------------------------------------------------------------- #
# Caché (RF-13: límite de intentos de login)
# --------------------------------------------------------------------------- #
# En desarrollo/tests, memoria local del proceso. En producción con varios workers de gunicorn cada
# proceso tiene su propia memoria: sin una caché COMPARTIDA, el límite de intentos de login (RF-13,
# ScopedRateThrottle) se podría burlar cayendo en distintos workers. CACHE_BACKEND=db usa una tabla de
# la misma base de datos (sin infraestructura nueva); CACHE_BACKEND=redis es la opción recomendada si el
# tráfico crece (más rápida, no compite con la BD) — requiere `django-redis` y un endpoint de ElastiCache.
_cache_backend = os.environ.get("CACHE_BACKEND", "db" if _produccion else "locmem")
if _cache_backend == "redis":
    CACHES = {"default": {"BACKEND": "django.core.cache.backends.redis.RedisCache",
                          "LOCATION": os.environ.get("REDIS_URL", "redis://127.0.0.1:6379/1")}}
elif _cache_backend == "db":
    CACHES = {"default": {"BACKEND": "django.core.cache.backends.db.DatabaseCache", "LOCATION": "cache_tabla"}}
    # La tabla se crea con: python manage.py createcachetable  (ver README de despliegue)
else:
    CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {"simple": {"format": "%(asctime)s %(levelname)s %(name)s: %(message)s"}},
    "handlers": {"console": {"class": "logging.StreamHandler", "formatter": "simple"}},
    "root": {"handlers": ["console"], "level": os.environ.get("DJANGO_LOG_LEVEL", "INFO")},
}
