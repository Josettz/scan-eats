# vision_service/scaneats_vision/clip_recorder.py
"""
Recorte de clips de evidencia por evento (RF-14).

Mantiene un búfer circular de fotogramas recientes. Al pedir un clip para un evento se esperan
`post_s` segundos más de video y se escribe el segmento [t - pre_s, t + post_s] a un archivo; cuando está
listo se llama a `on_ready` (el pipeline lo sube al backend por HTTP, que lo guarda en S3).

OpenCV escribe el clip (mp4v, que casi ningún navegador ni reproductor de Windows muestra) y a continuación se
RECODIFICA a H.264 con el ffmpeg que trae `imageio-ffmpeg`, para que se vea en cualquier navegador/reproductor.
Si ffmpeg no está disponible se conserva el mp4v y se avisa en el log. OpenCV e imageio-ffmpeg se importan de forma
perezosa (en pruebas se inyectan `writer_factory` y `transcode` falsos).
"""
from __future__ import annotations

import logging
import os
import tempfile
from collections import deque
from dataclasses import dataclass
from typing import Callable, Optional

log = logging.getLogger(__name__)


@dataclass
class _Request:
    tipo: str
    uid: str
    t: float
    wall: object


def _cv2_writer(path: str, fps: float, size: tuple[int, int]):
    import cv2  # perezoso

    w = cv2.VideoWriter(path, cv2.VideoWriter_fourcc(*"mp4v"), fps, size)
    if not w.isOpened():
        raise RuntimeError(f"No se pudo abrir el escritor de video en {path}")
    return w


def _a_h264(ruta: str) -> bool:
    """Recodifica `ruta` (mp4v) a H.264 yuv420p con moov al inicio, en el mismo archivo. Devuelve False si no pudo."""
    import subprocess

    try:
        import imageio_ffmpeg  # perezoso

        exe = imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:  # noqa: BLE001 - sin ffmpeg: se deja el clip original
        log.warning("imageio-ffmpeg no disponible: el clip queda en mp4v (poco compatible). pip install imageio-ffmpeg")
        return False
    temporal = ruta + ".h264.mp4"
    orden = [exe, "-y", "-loglevel", "error", "-i", ruta, "-c:v", "libx264", "-preset", "veryfast", "-crf", "26",
             "-pix_fmt", "yuv420p", "-movflags", "+faststart", "-an", temporal]
    try:
        subprocess.run(orden, check=True, capture_output=True, timeout=120)
        os.replace(temporal, ruta)
        return True
    except Exception:  # noqa: BLE001
        log.exception("Falló la recodificación a H.264; el clip queda en mp4v")
        if os.path.exists(temporal):
            os.remove(temporal)
        return False


def _shrink(frame, max_width: int):
    ancho = frame.shape[1]
    if ancho <= max_width:
        return frame
    import cv2  # perezoso

    escala = max_width / ancho
    return cv2.resize(frame, (max_width, int(frame.shape[0] * escala)))


class ClipRecorder:
    def __init__(
        self,
        on_ready: Callable[..., None],
        pre_s: float = 8.0,
        post_s: float = 8.0,
        fps: float = 5.0,
        out_dir: Optional[str] = None,
        max_width: int = 960,
        writer_factory: Callable = _cv2_writer,
        resize: Callable = _shrink,
        transcode: Optional[Callable[[str], bool]] = _a_h264,
    ):
        self.on_ready = on_ready
        self.pre_s, self.post_s, self.fps = pre_s, post_s, fps
        self.out_dir = out_dir or tempfile.gettempdir()
        self.max_width = max_width
        self._writer_factory = writer_factory
        self._resize = resize
        self._transcode = transcode
        self._buffer: deque = deque()
        self._pending: list[_Request] = []

    def add_frame(self, t: float, frame) -> None:
        self._buffer.append((t, self._resize(frame, self.max_width)))
        # Lo más antiguo que aún podría hacer falta (con margen para eventos reportados con algo de retraso).
        limite = t - (self.pre_s + 5.0 + (self.post_s if self._pending else 0.0))
        while self._buffer and self._buffer[0][0] < limite:
            self._buffer.popleft()
        for req in [r for r in self._pending if t >= r.t + self.post_s]:
            self._pending.remove(req)
            self._finalizar(req)

    def request(self, tipo: str, uid: str, t: float, wall) -> None:
        """Pide el clip del evento `uid` ocurrido en el instante `t` (reloj del pipeline)."""
        self._pending.append(_Request(tipo, uid, t, wall))

    def flush(self) -> None:
        """Al terminar el video: cierra los clips pendientes con el video disponible."""
        for req in self._pending:
            self._finalizar(req)
        self._pending.clear()

    def _finalizar(self, req: _Request) -> None:
        frames = [f for (ft, f) in self._buffer if req.t - self.pre_s <= ft <= req.t + self.post_s]
        if not frames:
            log.warning("Sin fotogramas para el clip de %s %s", req.tipo, req.uid)
            return
        os.makedirs(self.out_dir, exist_ok=True)
        ruta = os.path.join(self.out_dir, f"{req.tipo}_{req.uid}.mp4")
        try:
            alto, ancho = frames[0].shape[:2]
            w = self._writer_factory(ruta, self.fps, (ancho, alto))
            for f in frames:
                w.write(f)
            w.release()
            if self._transcode is not None:
                self._transcode(ruta)  # si falla, el clip queda en mp4v (ya avisó en el log)
        except Exception:  # noqa: BLE001 - un clip fallido no debe detener el monitoreo
            log.exception("No se pudo escribir el clip de %s %s", req.tipo, req.uid)
            return
        self.on_ready(tipo=req.tipo, uid=req.uid, path=ruta, wall=req.wall, duracion_s=len(frames) / self.fps)
