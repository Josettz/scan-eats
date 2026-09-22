# vision_service/Dockerfile — imagen independiente del backend
FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends libglib2.0-0 libgl1 && rm -rf /var/lib/apt/lists/*
COPY requirements.txt requirements-vision.txt ./
RUN pip install -r requirements-vision.txt
COPY scaneats_vision ./scaneats_vision
COPY config ./config
# Variables (VISION_API_URL, VISION_API_TOKEN, VIDEO_SOURCE...) se pasan con -e o --env-file.
ENTRYPOINT ["python", "-m", "scaneats_vision.pipeline"]
