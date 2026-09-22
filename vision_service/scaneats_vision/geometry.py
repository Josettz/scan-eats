# vision_service/scaneats_vision/geometry.py
"""
Geometría pura (sin numpy/OpenCV): cajas, polígonos y asignación de detecciones a zonas de mesa.

Por qué "overlap geométrico" y no "objeto dentro de la caja":
  una persona o un plato detectados NO caen limpiamente dentro del rectángulo de la mesa
  (perspectiva, sillas, brazos sobre el borde). Comparar la caja detectada contra el POLÍGONO de la
  zona por área de intersección + centroide es mucho más robusto que exigir contención total; el
  benchmark de referencia del proyecto reporta ~94 % F1 para overlap geométrico frente a ~38 % de
  la detección directa por caja.

Todas las coordenadas son las del llamador (el pipeline usa coordenadas NORMALIZADAS 0..1).
"""
from __future__ import annotations

from dataclasses import dataclass
from functools import cached_property
from typing import Iterable, Optional, Sequence

Point = tuple[float, float]


@dataclass(frozen=True)
class BBox:
    x1: float
    y1: float
    x2: float
    y2: float

    def __post_init__(self):
        # Normaliza cajas dadas "al revés" (x2 < x1) para que area/overlap nunca sean negativos.
        if self.x2 < self.x1:
            x1, x2 = self.x2, self.x1
            object.__setattr__(self, "x1", x1)
            object.__setattr__(self, "x2", x2)
        if self.y2 < self.y1:
            y1, y2 = self.y2, self.y1
            object.__setattr__(self, "y1", y1)
            object.__setattr__(self, "y2", y2)

    @classmethod
    def from_xywh(cls, x: float, y: float, w: float, h: float) -> "BBox":
        return cls(x, y, x + w, y + h)

    @property
    def width(self) -> float:
        return self.x2 - self.x1

    @property
    def height(self) -> float:
        return self.y2 - self.y1

    @property
    def area(self) -> float:
        return self.width * self.height

    @property
    def centroid(self) -> Point:
        return ((self.x1 + self.x2) / 2.0, (self.y1 + self.y2) / 2.0)

    @property
    def foot_point(self) -> Point:
        """Punto central del borde inferior: donde una persona "pisa" (útil para personas de pie)."""
        return ((self.x1 + self.x2) / 2.0, self.y2)

    def intersection_area(self, other: "BBox") -> float:
        w = min(self.x2, other.x2) - max(self.x1, other.x1)
        h = min(self.y2, other.y2) - max(self.y1, other.y1)
        return w * h if w > 0 and h > 0 else 0.0

    def iou(self, other: "BBox") -> float:
        inter = self.intersection_area(other)
        union = self.area + other.area - inter
        return inter / union if union > 0 else 0.0

    def scaled(self, sx: float, sy: float) -> "BBox":
        return BBox(self.x1 * sx, self.y1 * sy, self.x2 * sx, self.y2 * sy)

    def as_polygon(self) -> list[Point]:
        return [(self.x1, self.y1), (self.x2, self.y1), (self.x2, self.y2), (self.x1, self.y2)]


# --------------------------------------------------------------------------- #
# Polígonos
# --------------------------------------------------------------------------- #
def polygon_area(polygon: Sequence[Point]) -> float:
    """Área por la fórmula del cordón (shoelace). Siempre ≥ 0, sin importar el sentido de los vértices."""
    n = len(polygon)
    if n < 3:
        return 0.0
    s = 0.0
    for i in range(n):
        x1, y1 = polygon[i]
        x2, y2 = polygon[(i + 1) % n]
        s += x1 * y2 - x2 * y1
    return abs(s) / 2.0


def polygon_bbox(polygon: Sequence[Point]) -> BBox:
    xs = [p[0] for p in polygon]
    ys = [p[1] for p in polygon]
    return BBox(min(xs), min(ys), max(xs), max(ys))


def point_in_polygon(point: Point, polygon: Sequence[Point]) -> bool:
    """Ray casting. Los puntos sobre el borde se consideran dentro (con tolerancia numérica)."""
    x, y = point
    n = len(polygon)
    dentro = False
    for i in range(n):
        x1, y1 = polygon[i]
        x2, y2 = polygon[(i + 1) % n]
        if _en_segmento(point, (x1, y1), (x2, y2)):
            return True
        if (y1 > y) != (y2 > y):
            x_cruce = x1 + (y - y1) * (x2 - x1) / (y2 - y1)
            if x < x_cruce:
                dentro = not dentro
    return dentro


def _en_segmento(p: Point, a: Point, b: Point, eps: float = 1e-12) -> bool:
    cruz = (p[0] - a[0]) * (b[1] - a[1]) - (p[1] - a[1]) * (b[0] - a[0])
    if abs(cruz) > eps:
        return False
    return (
        min(a[0], b[0]) - eps <= p[0] <= max(a[0], b[0]) + eps
        and min(a[1], b[1]) - eps <= p[1] <= max(a[1], b[1]) + eps
    )


def clip_polygon_to_bbox(polygon: Sequence[Point], box: BBox) -> list[Point]:
    """Sutherland–Hodgman: recorta un polígono contra el rectángulo `box`."""

    def recortar(puntos, dentro, interseccion):
        if not puntos:
            return []
        salida = []
        previo = puntos[-1]
        for actual in puntos:
            if dentro(actual):
                if not dentro(previo):
                    salida.append(interseccion(previo, actual))
                salida.append(actual)
            elif dentro(previo):
                salida.append(interseccion(previo, actual))
            previo = actual
        return salida

    def cruce_x(x):
        return lambda a, b: (x, a[1] + (x - a[0]) * (b[1] - a[1]) / (b[0] - a[0]))

    def cruce_y(y):
        return lambda a, b: (a[0] + (y - a[1]) * (b[0] - a[0]) / (b[1] - a[1]), y)

    pts = list(polygon)
    pts = recortar(pts, lambda p: p[0] >= box.x1, cruce_x(box.x1))
    pts = recortar(pts, lambda p: p[0] <= box.x2, cruce_x(box.x2))
    pts = recortar(pts, lambda p: p[1] >= box.y1, cruce_y(box.y1))
    pts = recortar(pts, lambda p: p[1] <= box.y2, cruce_y(box.y2))
    return pts


def bbox_polygon_intersection_area(box: BBox, polygon: Sequence[Point]) -> float:
    if len(polygon) < 3 or box.area <= 0:
        return 0.0
    return polygon_area(clip_polygon_to_bbox(polygon, box))


# --------------------------------------------------------------------------- #
# Zonas
# --------------------------------------------------------------------------- #
TABLE = "table"
EXCLUSION = "exclusion"


@dataclass(frozen=True)
class Zone:
    """Zona fija del salón: una mesa (kind='table') o la caja (kind='exclusion')."""

    id: object  # número de mesa o nombre de la zona de exclusión
    polygon: tuple[Point, ...]
    kind: str = TABLE

    # cached_property escribe en __dict__ directamente, por lo que funciona en dataclasses congelados.
    @cached_property
    def bbox(self) -> BBox:
        return polygon_bbox(self.polygon)

    @cached_property
    def area(self) -> float:
        return polygon_area(self.polygon)

    @property
    def is_exclusion(self) -> bool:
        return self.kind == EXCLUSION

    def contains_point(self, point: Point) -> bool:
        b = self.bbox
        if not (b.x1 <= point[0] <= b.x2 and b.y1 <= point[1] <= b.y2):
            return False  # descarte barato antes del ray casting
        return point_in_polygon(point, self.polygon)

    def overlap_ratio(self, box: BBox) -> float:
        """Fracción de la CAJA DETECTADA que cae dentro de la zona (0..1)."""
        if box.area <= 0 or box.intersection_area(self.bbox) <= 0:
            return 0.0  # las cajas envolventes ni se tocan: no hace falta recortar el polígono
        return min(1.0, bbox_polygon_intersection_area(box, self.polygon) / box.area)

    def coverage(self, box: BBox) -> float:
        """Fracción de la ZONA cubierta por la caja detectada (0..1)."""
        if self.area <= 0:
            return 0.0
        return min(1.0, bbox_polygon_intersection_area(box, self.polygon) / self.area)


@dataclass(frozen=True)
class ZoneMatch:
    zone: Zone
    overlap: float
    centroid_inside: bool


def assign_to_zone(
    box: BBox,
    zones: Iterable[Zone],
    min_overlap: float = 0.25,
    reference: str = "centroid",
) -> Optional[ZoneMatch]:
    """
    Asigna una detección a UNA zona.

    Una zona es candidata si el punto de referencia de la caja (centroide, o `foot_point`) cae dentro
    de ella, o si el overlap geométrico ≥ `min_overlap`. Entre candidatas gana la que contiene el
    centroide y, en empate, la de mayor overlap. Se evalúan juntas las mesas Y las zonas de exclusión:
    una persona en la caja NO se reasigna a la mesa vecina.
    """
    punto = box.centroid if reference == "centroid" else box.foot_point
    mejor: Optional[ZoneMatch] = None
    for zona in zones:
        overlap = zona.overlap_ratio(box)
        dentro = zona.contains_point(punto)
        if not dentro and overlap < min_overlap:
            continue
        candidata = ZoneMatch(zona, overlap, dentro)
        if mejor is None or (candidata.centroid_inside, candidata.overlap) > (mejor.centroid_inside, mejor.overlap):
            mejor = candidata
    return mejor


def bbox_gap(a: BBox, b: BBox) -> float:
    """Distancia mínima entre dos cajas (0 si se tocan o se solapan)."""
    dx = max(a.x1 - b.x2, b.x1 - a.x2, 0.0)
    dy = max(a.y1 - b.y2, b.y1 - a.y2, 0.0)
    return (dx * dx + dy * dy) ** 0.5


def zones_adjacent(a: Zone, b: Zone, max_gap: float) -> bool:
    """Dos zonas son adyacentes si el hueco entre sus cajas envolventes es ≤ `max_gap`."""
    return bbox_gap(a.bbox, b.bbox) <= max_gap


def distance(a: Point, b: Point) -> float:
    return ((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2) ** 0.5
