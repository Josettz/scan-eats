# vision_service/scaneats_vision/annotate.py
"""
Video anotado para VER lo que hace el sistema (no interviene en la lógica ni en lo que se envía al backend).

Dibuja sobre cada fotograma procesado:
  * detecciones crudas de YOLOv8: personas (caja blanca fina + confianza) y vajilla (cian);
  * personas seguidas por el tracker: caja gruesa con #id y rol — verde = PERSONAL, naranja = CLIENTE,
    gris = sin clasificar — más cómo se identificó (vestimenta / comportamiento) y el mesero;
  * cada mesa: zona, superficie (mantel) en amarillo, ESTADO (LIBRE / OCUPADA / POSIBLEMENTE_LIBRE) y el % de cambio;
  * los últimos eventos enviados al backend (ocupación, entrega, fusión...).

    python -m scaneats_vision.pipeline --source video.mp4 --config config/zonas_video.json --dry-run --annotate salida.mp4
"""
from __future__ import annotations

import logging
from collections import deque

from .clip_recorder import _a_h264
from .occupancy_engine import TableState
from .staff_classifier import Role

log = logging.getLogger(__name__)

# BGR
VERDE, NARANJA, GRIS, BLANCO, CIAN, AMARILLO, ROJO = (60, 200, 60), (0, 140, 255), (170, 170, 170), (255, 255, 255), (255, 220, 0), (0, 220, 255), (60, 60, 230)
COLOR_ESTADO = {TableState.LIBRE: (80, 180, 80), TableState.OCUPADA: (60, 60, 230), TableState.POSIBLEMENTE_LIBRE: (0, 200, 255)}


def _texto_evento(metodo: str, kw: dict):
    mesa = kw.get("mesa_numero")
    if metodo == "reportar_ocupacion":
        return f"OCUPACION mesa {mesa}"
    if metodo == "cambiar_estado_ocupacion":
        return f"mesa {mesa} -> {kw.get('estado')}"
    if metodo == "reportar_entrega":
        return f"ENTREGA mesa {mesa} por {kw.get('personal_identificador')} ({kw.get('metodo')})"
    if metodo == "reportar_fusion":
        return f"FUSION mesas {kw.get('mesas')}"
    if metodo == "finalizar_fusion":
        return f"FIN FUSION {kw.get('mesas')}"
    return None  # clasificaciones y clips no se muestran (demasiado ruido)


class EventTap:
    """Publicador "espía": deja pasar todo al publicador real y recuerda los eventos para dibujarlos."""

    def __init__(self, inner):
        self.inner = inner
        self.now = 0.0
        self.eventos: deque = deque(maxlen=50)
        self.registro: list = []  # (instante de video, método, argumentos) de TODO lo enviado; lo usa el análisis

    def submit(self, method: str, **kwargs) -> None:
        self.registro.append((self.now, method, dict(kwargs)))
        texto = _texto_evento(method, kwargs)
        if texto:
            self.eventos.append((self.now, texto))
        self.inner.submit(method, **kwargs)

    def close(self, *a, **k):
        if hasattr(self.inner, "close"):
            self.inner.close(*a, **k)


class Annotator:
    def __init__(self, ruta_salida, fps: float, tap: EventTap, ancho: int = 1280):
        """ruta_salida=None: solo dibuja (`render`), sin escribir un video completo (p. ej. para los clips de evidencia)."""
        self.ruta, self.fps, self.tap, self.ancho = ruta_salida, fps, tap, ancho
        self._writer = None
        self.frames = 0

    def begin(self, now: float) -> None:
        self.tap.now = now

    # ------------------------------------------------------------------ #
    def draw(self, frame, now: float, detecciones, pistas, pipeline, cambio: dict):
        """Dibuja y, si hay ruta de salida, agrega el fotograma al video anotado. Devuelve la imagen anotada."""
        img = self.render(frame, now, detecciones, pistas, pipeline, cambio)
        if self.ruta:
            self._escribir(img)
        return img

    def render(self, frame, now: float, detecciones, pistas, pipeline, cambio: dict):
        """Devuelve una copia del fotograma con todo dibujado (ancho `self.ancho`)."""
        import cv2
        import numpy as np

        alto, ancho = frame.shape[:2]
        img = frame.copy()
        cfg = pipeline.config

        def px(p):
            return int(p[0] * ancho), int(p[1] * alto)

        # 1) zonas de mesa (relleno tenue por estado) + superficie
        capa = img.copy()
        for t in cfg.tables:
            estado = pipeline.occupancy.state(t.numero)
            pts = np.array([px(p) for p in t.zone.polygon], np.int32)
            cv2.fillPoly(capa, [pts], COLOR_ESTADO[estado])
        img = cv2.addWeighted(capa, 0.18, img, 0.82, 0)
        for z in cfg.exclusion_zones:
            cv2.polylines(img, [np.array([px(p) for p in z.polygon], np.int32)], True, ROJO, 3)
        for t in cfg.tables:
            estado = pipeline.occupancy.state(t.numero)
            color = COLOR_ESTADO[estado]
            cv2.polylines(img, [np.array([px(p) for p in t.zone.polygon], np.int32)], True, color, 3)
            if t.surface is not None:
                cv2.polylines(img, [np.array([px(p) for p in t.surface.polygon], np.int32)], True, AMARILLO, 2)
            x, y = px((min(p[0] for p in t.zone.polygon), min(p[1] for p in t.zone.polygon)))
            delta = cambio.get(t.numero) if cambio else None
            txt = f"MESA {t.numero}: {estado.value}" + ("" if delta is None else f"  cambio {delta * 100:.2f}%")
            self._texto(img, txt, (x + 6, y + 30), color, 0.9)

        # 2) detecciones crudas de YOLO
        for d in detecciones:
            b = d.bbox
            p1, p2 = px((b.x1, b.y1)), px((b.x2, b.y2))
            if d.is_person:
                cv2.rectangle(img, p1, p2, BLANCO, 1)
                self._texto(img, f"person {d.confidence:.2f}", (p1[0], p2[1] + 22), BLANCO, 0.6)
            else:
                cv2.rectangle(img, p1, p2, CIAN, 2)
                self._texto(img, f"objeto {d.confidence:.2f}", (p1[0], p1[1] - 6), CIAN, 0.6)

        # 3) personas seguidas + clasificación
        for t in pistas:
            c = pipeline._last_class.get(t.track_id)
            rol = c.role if c else Role.UNKNOWN
            color = VERDE if rol == Role.STAFF else NARANJA if rol == Role.CUSTOMER else GRIS
            nombre = {Role.STAFF: "PERSONAL", Role.CUSTOMER: "CLIENTE", Role.UNKNOWN: "?"}[rol]
            extra = ""
            if c and rol == Role.STAFF:
                extra = f" {c.identificador or 'sin id'} · {c.method.value.lower()} {c.confidence:.2f}"
            elif c and rol == Role.CUSTOMER:
                extra = f" · {c.method.value.lower()}"
            p1, p2 = px((t.bbox.x1, t.bbox.y1)), px((t.bbox.x2, t.bbox.y2))
            cv2.rectangle(img, p1, p2, color, 4)
            y_texto = p1[1] - 10 if p1[1] > 50 else p1[1] + 32  # si la caja toca el borde superior, la etiqueta va dentro
            self._texto(img, f"#{t.track_id} {nombre}{extra}", (p1[0] + 4, y_texto), color, 0.8)

        # 4) cabecera y últimos eventos
        m, s = divmod(int(now), 60)
        self._texto(img, f"ScanEats  video {m}:{s:02d}", (ancho - 430, alto - 20), BLANCO, 1.0)  # abajo a la derecha
        recientes = [(t, x) for t, x in self.tap.eventos if now - t <= 6.0][-5:]
        for i, (t, x) in enumerate(reversed(recientes)):
            self._texto(img, f">> {x}", (16, alto - 20 - 34 * i), AMARILLO, 0.95)

        return cv2.resize(img, (self.ancho, int(alto * self.ancho / ancho)))

    def _escribir(self, img) -> None:
        import cv2

        if self._writer is None:
            h, w = img.shape[:2]
            self._writer = cv2.VideoWriter(self.ruta, cv2.VideoWriter_fourcc(*"mp4v"), self.fps, (w, h))
        self._writer.write(img)
        self.frames += 1

    @staticmethod
    def _texto(img, texto, org, color, escala):
        import cv2

        cv2.putText(img, texto, org, cv2.FONT_HERSHEY_SIMPLEX, escala, (0, 0, 0), 4, cv2.LINE_AA)
        cv2.putText(img, texto, org, cv2.FONT_HERSHEY_SIMPLEX, escala, color, 2, cv2.LINE_AA)

    def close(self) -> None:
        if self._writer is not None:
            self._writer.release()
            self._writer = None
            log.info("Video anotado: %s (%d fotogramas). Recodificando a H.264...", self.ruta, self.frames)
            if not _a_h264(self.ruta):
                log.warning("El video anotado quedó en mp4v (ábralo con VLC).")
