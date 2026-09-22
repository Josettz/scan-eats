# vision_service/scaneats_vision/settings.py
"""Ajustes del servicio desde variables de entorno / .env (ver .env.example)."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _valor(crudo: str) -> str:
    """Valor de una línea de .env: respeta las comillas y descarta comentarios en la misma línea (`KEY=x  # nota`)."""
    v = crudo.strip()
    if v[:1] in {'"', "'"} and v.count(v[0]) >= 2:
        return v[1:v.index(v[0], 1)]
    return v.split(" #", 1)[0].split("	#", 1)[0].strip()


def cargar_env(ruta=".env") -> None:
    """Carga un .env simple (KEY=VALUE) sin pisar variables ya definidas."""
    p = Path(ruta)
    if not p.exists():
        return
    for linea in p.read_text(encoding="utf-8").splitlines():
        linea = linea.strip()
        if linea and not linea.startswith("#") and "=" in linea:
            k, v = linea.split("=", 1)
            os.environ.setdefault(k.strip(), _valor(v))


def _bool(nombre: str, defecto: bool) -> bool:
    v = os.environ.get(nombre)
    return defecto if v is None else v.strip().lower() in {"1", "true", "yes", "si", "on"}


@dataclass(frozen=True)
class ServiceSettings:
    api_base_url: str = "http://localhost:8000"
    api_token: str = ""
    video_source: str = ""
    config_path: str = "config/zonas_ejemplo.json"
    yolo_model: str = "yolov8n.pt"
    detection_confidence: float = 0.35
    yolo_imgsz: int = 960
    process_fps: float = 5.0
    tracker: str = "sort"
    clips_enabled: bool = False
    clip_pre_s: float = 8.0
    clip_post_s: float = 8.0
    clip_dir: str = "clips_tmp"
    log_level: str = "INFO"

    @classmethod
    def from_env(cls) -> "ServiceSettings":
        cargar_env()
        e = os.environ.get
        return cls(
            api_base_url=e("VISION_API_URL", cls.api_base_url),
            api_token=e("VISION_API_TOKEN", ""),
            video_source=e("VIDEO_SOURCE", ""),
            config_path=e("VISION_CONFIG", cls.config_path),
            yolo_model=e("YOLO_MODEL", cls.yolo_model),
            detection_confidence=float(e("DETECTION_CONFIDENCE", cls.detection_confidence)),
            yolo_imgsz=int(e("YOLO_IMGSZ", cls.yolo_imgsz)),
            process_fps=float(e("PROCESS_FPS", cls.process_fps)),
            tracker=e("TRACKER", cls.tracker),
            clips_enabled=_bool("CLIPS_ENABLED", cls.clips_enabled),
            clip_pre_s=float(e("CLIP_PRE_S", cls.clip_pre_s)),
            clip_post_s=float(e("CLIP_POST_S", cls.clip_post_s)),
            clip_dir=e("CLIP_DIR", cls.clip_dir),
            log_level=e("LOG_LEVEL", cls.log_level),
        )
