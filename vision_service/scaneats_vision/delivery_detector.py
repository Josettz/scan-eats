# vision_service/scaneats_vision/delivery_detector.py
"""
Detección de entregas de pedido por SUSTRACCIÓN DE FONDO sobre las zonas fijas de mesa (RF-02).

No se entrena una clase "plato de comida": un modelo de sustracción de fondo (MOG2) aprende la mesa
y marca como primer plano lo que aparece de repente. Una ENTREGA es:

    un aumento sostenido de "cambio en la mesa"           (apareció algo: el plato)
      + un mesero estuvo en la mesa justo antes           (alguien de personal lo puso)
      + la mesa está OCUPADA                              (no hay entregas en mesas vacías)
      + fuera del enfriamiento de la entrega anterior     (una mesa admite VARIAS entregas por ocupación)

Se pisa con histéresis (`change_high` para subir, `change_low` para re-armar) para que un objeto que
permanece encima no dispare varias entregas.

Este archivo separa:
  * `DeliveryDetector`      -> lógica pura y testeable (recibe una razón de cambio por mesa).
  * `ForegroundExtractor`   -> OpenCV/numpy (MOG2), importación perezosa; calcula esa razón por zona.

⚠ Umbrales PENDIENTES DE CALIBRAR con video del local (iluminación, cámara, tamaño de la mesa en el fotograma).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence

from .geometry import BBox, Zone


@dataclass(frozen=True)
class DeliveryConfig:
    change_high: float = 0.05
    """Fracción de la zona que debe estar en primer plano para considerar "apareció algo"."""
    change_low: float = 0.02
    """Por debajo de esto la mesa se considera estable y el detector se re-arma."""
    persist_s: float = 1.5
    """El cambio debe sostenerse este tiempo (descarta sombras, brazos y reflejos)."""
    staff_window_s: float = 20.0
    """El mesero debe haber estado en la mesa, como máximo, tanto tiempo ANTES de que empiece el cambio."""
    cooldown_s: float = 25.0
    """Mínimo entre dos entregas de la misma mesa."""

    def validate(self) -> "DeliveryConfig":
        if not (0 <= self.change_low < self.change_high <= 1):
            raise ValueError("Se requiere 0 <= change_low < change_high <= 1.")
        return self


@dataclass(frozen=True)
class DeliveryDetection:
    table_id: object
    at: float
    """Cuándo apareció el objeto (inicio del cambio sostenido)."""
    confirmed_at: float
    staff_last_seen: float


@dataclass
class _TableSignal:
    armed: bool = False
    above_since: Optional[float] = None
    last_delivery: Optional[float] = None
    last_staff: Optional[float] = None


class DeliveryDetector:
    def __init__(self, config: Optional[DeliveryConfig] = None):
        self.config = (config or DeliveryConfig()).validate()
        self._tables: dict[object, _TableSignal] = {}

    def _sig(self, table_id) -> _TableSignal:
        return self._tables.setdefault(table_id, _TableSignal())

    def note_staff_presence(self, table_id, now: float) -> None:
        """Un miembro del personal está dentro de la zona de esta mesa en este instante."""
        self._sig(table_id).last_staff = now

    def reset(self, table_id) -> None:
        """La mesa se liberó: se descarta todo su contexto."""
        self._tables.pop(table_id, None)

    def update(self, table_id, now: float, change_ratio: Optional[float], occupied: bool) -> Optional[DeliveryDetection]:
        cfg = self.config
        s = self._sig(table_id)
        if not occupied:
            s.armed, s.above_since = False, None
            return None
        if change_ratio is None:  # mesa tapada por personas: no hay señal fiable, se conserva el estado
            return None

        if change_ratio <= cfg.change_low:
            s.armed, s.above_since = True, None
            return None
        if change_ratio < cfg.change_high or not s.armed:
            if change_ratio < cfg.change_high:
                s.above_since = None
            return None

        # change_ratio >= change_high y el detector está armado
        if s.above_since is None:
            s.above_since = now
        if now - s.above_since < cfg.persist_s:
            return None

        inicio = s.above_since
        s.armed, s.above_since = False, None  # no vuelve a disparar hasta que la mesa se estabilice
        # El mesero debe haber estado en la mesa como máximo `staff_window_s` ANTES de que empezara el cambio
        # (o seguir allí): así un gesto del cliente varios segundos después de que el mesero se fue no cuenta.
        hubo_mesero = s.last_staff is not None and inicio - s.last_staff <= cfg.staff_window_s
        enfriada = s.last_delivery is not None and now - s.last_delivery < cfg.cooldown_s
        if not hubo_mesero or enfriada:
            return None  # p. ej. un cliente dejó su celular: hubo cambio pero no fue una entrega
        s.last_delivery = now
        return DeliveryDetection(table_id, inicio, now, s.last_staff)


# --------------------------------------------------------------------------- #
# OpenCV: razón de primer plano por zona (importación perezosa)
# --------------------------------------------------------------------------- #
class ForegroundExtractor:
    """
    Modelo de fondo MOG2 + razón de píxeles en primer plano dentro de cada zona de mesa,
    excluyendo los píxeles ocupados por personas (para no confundir cuerpos con platos).
    """

    def __init__(self, history: int = 300, var_threshold: float = 32.0, min_visible_fraction: float = 0.25):
        import cv2  # noqa: F401  (falla temprano y con claridad si falta OpenCV)

        self._cv2 = cv2
        self._mog = cv2.createBackgroundSubtractorMOG2(history=history, varThreshold=var_threshold, detectShadows=True)
        self.min_visible_fraction = min_visible_fraction
        self._mask_cache: dict = {}

    def _zone_mask(self, zone: Zone, size: tuple[int, int]):
        import numpy as np

        clave = (zone.id, size)
        if clave not in self._mask_cache:
            ancho, alto = size
            pts = np.array([[int(x * ancho), int(y * alto)] for x, y in zone.polygon], dtype=np.int32)
            mascara = np.zeros((alto, ancho), dtype=np.uint8)
            self._cv2.fillPoly(mascara, [pts], 255)
            self._mask_cache[clave] = mascara
        return self._mask_cache[clave]

    def change_ratios(self, frame, zones: Sequence[Zone], person_boxes: Sequence[BBox]) -> dict:
        """
        frame: BGR. zones y person_boxes en coordenadas NORMALIZADAS.
        Devuelve {zone.id: razón 0..1 | None} (None = zona demasiado tapada para medir).
        """
        import numpy as np

        alto, ancho = frame.shape[:2]
        fg = self._mog.apply(frame)
        fg = (fg == 255).astype(np.uint8) * 255  # descarta sombras (valor 127)
        fg = self._cv2.morphologyEx(fg, self._cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))

        personas = np.zeros((alto, ancho), dtype=np.uint8)
        for b in person_boxes:
            x1, y1, x2, y2 = int(b.x1 * ancho), int(b.y1 * alto), int(b.x2 * ancho), int(b.y2 * alto)
            personas[max(y1, 0):max(y2, 0), max(x1, 0):max(x2, 0)] = 255

        salida = {}
        for zona in zones:
            mascara = self._zone_mask(zona, (ancho, alto))
            total = int(np.count_nonzero(mascara))
            visible = self._cv2.bitwise_and(mascara, self._cv2.bitwise_not(personas))
            n_visible = int(np.count_nonzero(visible))
            if total == 0 or n_visible / total < self.min_visible_fraction:
                salida[zona.id] = None
                continue
            salida[zona.id] = int(np.count_nonzero(self._cv2.bitwise_and(fg, visible))) / n_visible
        return salida
