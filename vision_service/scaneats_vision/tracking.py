# vision_service/scaneats_vision/tracking.py
"""
Seguimiento de personas entre fotogramas.

* `SortTracker` — variante ligera de SORT en Python puro: predicción de movimiento a velocidad constante
  + asociación por IoU (voraz, de mayor a menor IoU) + vida útil de pistas (max_age / min_hits).
  Sin numpy ni scipy: cumple para 20 mesas y se prueba sin dependencias. (SORT original usa filtro de Kalman
  y algoritmo húngaro; aquí se simplifican a costa de algo de robustez ante oclusiones largas.)
* `DeepSortTracker` — adaptador de `deep-sort-realtime` (importación perezosa) para cuando se necesite
  re-identificación por apariencia (personas que se cruzan). Misma interfaz.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from .geometry import BBox


@dataclass
class Track:
    track_id: int
    bbox: BBox
    hits: int = 1
    time_since_update: int = 0
    velocity: tuple[float, float] = (0.0, 0.0)
    confirmed: bool = False


class SortTracker:
    def __init__(self, iou_threshold: float = 0.25, max_age: int = 15, min_hits: int = 2):
        self.iou_threshold = iou_threshold
        self.max_age = max_age
        self.min_hits = min_hits
        self._tracks: list[Track] = []
        self._next_id = 1

    def _predecir(self, t: Track) -> BBox:
        dx, dy = t.velocity
        k = t.time_since_update + 1
        b = t.bbox
        return BBox(b.x1 + dx * k, b.y1 + dy * k, b.x2 + dx * k, b.y2 + dy * k)

    def update(self, detections: Sequence[BBox], frame=None) -> list[Track]:
        """Devuelve las pistas confirmadas y vistas en ESTE fotograma."""
        predichas = [self._predecir(t) for t in self._tracks]
        pares = sorted(
            ((predichas[i].iou(d), i, j) for i in range(len(self._tracks)) for j, d in enumerate(detections)),
            reverse=True,
        )
        usadas_t, usadas_d = set(), set()
        for iou, i, j in pares:
            if iou < self.iou_threshold:
                break
            if i in usadas_t or j in usadas_d:
                continue
            usadas_t.add(i)
            usadas_d.add(j)
            t, d = self._tracks[i], detections[j]
            (cx0, cy0), (cx1, cy1) = t.bbox.centroid, d.centroid
            gap = t.time_since_update + 1
            vx, vy = (cx1 - cx0) / gap, (cy1 - cy0) / gap
            t.velocity = (0.6 * t.velocity[0] + 0.4 * vx, 0.6 * t.velocity[1] + 0.4 * vy)  # suavizado
            t.bbox, t.hits, t.time_since_update = d, t.hits + 1, 0
            t.confirmed = t.confirmed or t.hits >= self.min_hits

        for i, t in enumerate(self._tracks):
            if i not in usadas_t:
                t.time_since_update += 1
        for j, d in enumerate(detections):
            if j not in usadas_d:
                self._tracks.append(Track(self._next_id, d, confirmed=self.min_hits <= 1))
                self._next_id += 1
        self._tracks = [t for t in self._tracks if t.time_since_update <= self.max_age]
        return [t for t in self._tracks if t.confirmed and t.time_since_update == 0]

    @property
    def lost_track_ids(self) -> list[int]:
        return [t.track_id for t in self._tracks if t.time_since_update > 0]


class DeepSortTracker:
    """Adaptador de deep-sort-realtime. Requiere `pip install deep-sort-realtime` (ver requirements-vision.txt)."""

    def __init__(self, max_age: int = 30, n_init: int = 2):
        from deep_sort_realtime.deepsort_tracker import DeepSort  # perezoso

        self._ds = DeepSort(max_age=max_age, n_init=n_init)

    def update(self, detections: Sequence[BBox], frame=None) -> list[Track]:
        # Las cajas llegan normalizadas; DeepSORT trabaja en píxeles del fotograma dado.
        alto, ancho = (frame.shape[0], frame.shape[1]) if frame is not None else (1, 1)
        entrada = [
            ([d.x1 * ancho, d.y1 * alto, d.width * ancho, d.height * alto], 1.0, "person") for d in detections
        ]
        salida = []
        for t in self._ds.update_tracks(entrada, frame=frame):
            if not t.is_confirmed() or t.time_since_update > 0:
                continue
            l, tp, r, b = t.to_ltrb()
            salida.append(Track(int(t.track_id), BBox(l / ancho, tp / alto, r / ancho, b / alto), hits=t.hits, confirmed=True))
        return salida


def build_tracker(kind: str = "sort", **kwargs):
    if kind == "sort":
        return SortTracker(**kwargs)
    if kind == "deepsort":
        return DeepSortTracker(**kwargs)
    raise ValueError(f"Tracker desconocido: {kind!r} (use 'sort' o 'deepsort').")
