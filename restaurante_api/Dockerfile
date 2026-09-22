# restaurante_api/Dockerfile — imagen independiente del microservicio de visión
#
# ⚠ No se compiló/ejecutó en esta máquina: no hay Docker instalado aquí (verificado). Revísela con
# `docker build .` antes de confiar en ella para producción.
FROM python:3.12-slim AS base
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 DJANGO_SETTINGS_MODULE=config.settings
WORKDIR /app

# libpq-dev: cliente nativo de PostgreSQL que necesita psycopg para hablar con RDS.
RUN apt-get update && apt-get install -y --no-install-recommends libpq-dev gcc && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install -r requirements.txt

COPY . .

# Usuario sin privilegios (no correr la app como root dentro del contenedor).
RUN useradd -m scaneats && chown -R scaneats:scaneats /app
USER scaneats

# collectstatic necesita SECRET_KEY definido; en build no hay .env, así que se usa uno de relleno
# SOLO para esta fase (el contenedor en ejecución usa el real, inyectado por variables de entorno).
RUN DJANGO_SECRET_KEY=build-only-placeholder DJANGO_DEBUG=True python manage.py collectstatic --noinput

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health/')" || exit 1

# migrate en el arranque: adecuado para una sola instancia (proyecto de clase); con varias instancias
# a la vez, ejecute las migraciones aparte (`docker run ... python manage.py migrate`) antes del despliegue.
CMD ["sh", "-c", "python manage.py migrate --noinput && gunicorn config.wsgi:application --bind 0.0.0.0:8000 --workers 3 --access-logfile - --error-logfile -"]
