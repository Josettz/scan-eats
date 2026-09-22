# vision_service/scaneats_vision/staff_classifier.py
"""
Clasificación de personal en CASCADA (RF-08):

    1) VESTIMENTA (método principal): color de la prenda distintiva contra los perfiles de `Personal`.
       Si la confianza es suficiente -> STAFF (con identidad, si el color es único).
    2) COMPORTAMIENTO (respaldo): si la prenda no es reconocible o la confianza es baja, se usa el
       patrón de movimiento: visita varias mesas brevemente, sin sentarse, de forma repetida.
       El comportamiento distingue "personal vs. cliente"; la IDENTIDAD concreta del mesero solo se
       conoce si ese mismo track ya fue reconocido antes por vestimenta (memoria por track).

Este módulo NO carga ningún modelo: el extractor de color se inyecta (`color_ratio_fn`), así que
toda la lógica se prueba sin OpenCV/YOLO.

⚠ Los umbrales por defecto son PENDIENTES DE CALIBRAR con video del local.
"""
from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Iterable, Optional


class Role(str, Enum):
    STAFF = "STAFF"
    CUSTOMER = "CUSTOMER"
    UNKNOWN = "UNKNOWN"


class Method(str, Enum):
    VESTIMENTA = "VESTIMENTA"
    COMPORTAMIENTO = "COMPORTAMIENTO"
    NINGUNO = "NINGUNO"


@dataclass(frozen=True)
class StaffProfile:
    identificador: str
    nombre: str = ""
    color: Optional[str] = None  # ROJO, AZUL, ... (None = sin prenda distintiva registrada)
    tipo: Optional[str] = None


# --------------------------------------------------------------------------- #
# 1) Vestimenta
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class ClothingResult:
    identificador: Optional[str]  # None si el color no identifica a una única persona
    color: Optional[str]
    confidence: float
    ambiguous: bool = False  # el uniforme se reconoce, pero lo comparten varias personas

    @property
    def recognized(self) -> bool:
        return self.confidence > 0 and (self.identificador is not None or self.ambiguous)


class ClothingClassifier:
    def __init__(
        self,
        profiles: Iterable[StaffProfile],
        color_ratio_fn: Callable[[object], Optional[dict]],
        min_confidence: float = 0.55,
        min_margin: float = 0.15,
        full_coverage: float = 0.30,
    ):
        """
        color_ratio_fn(recorte) -> {color: fracción de píxeles del torso} | None (recorte inservible).
        full_coverage: fracción de torso de ese color que equivale a confianza 1.0.
        min_margin: diferencia mínima entre el mejor perfil y el segundo (evita confundir dos colores parecidos).
        """
        self.profiles = [p for p in profiles if p.color]
        self.color_ratio_fn = color_ratio_fn
        self.min_confidence = min_confidence
        self.min_margin = min_margin
        self.full_coverage = full_coverage

    def classify(self, crop) -> ClothingResult:
        if crop is None or not self.profiles:
            return ClothingResult(None, None, 0.0)
        ratios = self.color_ratio_fn(crop)
        if not ratios:
            return ClothingResult(None, None, 0.0)

        por_color: dict[str, float] = {}
        for color in {p.color for p in self.profiles}:
            por_color[color] = min(1.0, ratios.get(color, 0.0) / self.full_coverage)
        ordenados = sorted(por_color.items(), key=lambda kv: kv[1], reverse=True)
        mejor_color, confianza = ordenados[0]
        segundo = ordenados[1][1] if len(ordenados) > 1 else 0.0

        if confianza < self.min_confidence or confianza - segundo < self.min_margin:
            return ClothingResult(None, mejor_color, confianza * 0.5)  # visto, pero no concluyente: confianza baja

        con_ese_color = [p for p in self.profiles if p.color == mejor_color]
        if len(con_ese_color) == 1:
            return ClothingResult(con_ese_color[0].identificador, mejor_color, confianza)
        return ClothingResult(None, mejor_color, confianza, ambiguous=True)


# --------------------------------------------------------------------------- #
# 2) Comportamiento
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class BehaviorConfig:
    window_s: float = 300.0
    """Ventana en la que se cuentan las visitas."""
    min_distinct_tables: int = 3
    """Mesas distintas visitadas (brevemente) para considerar personal. PENDIENTE DE CALIBRAR."""
    min_visits: int = 3
    """Visitas breves totales (patrón repetido)."""
    max_visit_s: float = 60.0
    """Una visita más larga que esto ya no es "breve"."""
    seated_dwell_s: float = 90.0
    """Permanecer tanto tiempo en UNA mesa = cliente sentado."""


@dataclass(frozen=True)
class BehaviorResult:
    is_staff: bool
    is_customer: bool
    staff_score: float  # 0..1 (0.5 = justo en el umbral)
    distinct_tables: int
    visits: int


@dataclass
class _TrackHistory:
    current_table: object = None
    entered_at: float = 0.0
    visits: deque = None  # (table_id, entrada, salida)
    seated: bool = False

    def __post_init__(self):
        self.visits = deque(maxlen=200)


class BehaviorAnalyzer:
    def __init__(self, config: Optional[BehaviorConfig] = None):
        self.config = config or BehaviorConfig()
        self._tracks: dict[object, _TrackHistory] = defaultdict(_TrackHistory)

    def observe(self, track_id, now: float, table_id) -> None:
        """Registra en qué mesa (o None) está la persona `track_id` en este instante."""
        h = self._tracks[track_id]
        if table_id != h.current_table:
            if h.current_table is not None:
                h.visits.append((h.current_table, h.entered_at, now))
            h.current_table = table_id
            h.entered_at = now
        if h.current_table is not None and now - h.entered_at >= self.config.seated_dwell_s:
            h.seated = True

    def score(self, track_id, now: float) -> BehaviorResult:
        cfg = self.config
        h = self._tracks.get(track_id)
        if h is None:
            return BehaviorResult(False, False, 0.0, 0, 0)
        desde = now - cfg.window_s
        breves = [
            (mesa, entrada, salida) for mesa, entrada, salida in h.visits
            if salida >= desde and salida - entrada <= cfg.max_visit_s
        ]
        mesas = {mesa for mesa, _, _ in breves}
        progreso = min(len(mesas) / cfg.min_distinct_tables, len(breves) / cfg.min_visits)
        es_cliente = h.seated
        es_personal = progreso >= 1.0 and not es_cliente
        return BehaviorResult(es_personal, es_cliente, min(1.0, progreso / 2.0), len(mesas), len(breves))

    def forget(self, track_id) -> None:
        self._tracks.pop(track_id, None)


# --------------------------------------------------------------------------- #
# Cascada
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Classification:
    role: Role
    identificador: Optional[str]
    confidence: float
    method: Method
    clothing: Optional[ClothingResult] = None
    behavior: Optional[BehaviorResult] = None


@dataclass(frozen=True)
class CascadeConfig:
    clothing_accept: float = 0.60
    """Confianza mínima de la vestimenta para aceptarla sin recurrir al comportamiento."""
    seated_veto_clothing_max: float = 0.90
    """Un cliente sentado con una prenda parecida al uniforme NO es personal, salvo que la prenda sea inequívoca."""


class StaffClassifier:
    def __init__(
        self,
        clothing: ClothingClassifier,
        behavior: BehaviorAnalyzer,
        config: Optional[CascadeConfig] = None,
    ):
        self.clothing = clothing
        self.behavior = behavior
        self.config = config or CascadeConfig()
        self._identity_memory: dict[object, tuple[str, float]] = {}

    def classify(self, track_id, now: float, crop=None) -> Classification:
        cfg = self.config
        comportamiento = self.behavior.score(track_id, now)
        ropa = self.clothing.classify(crop) if crop is not None else None

        # 1) Vestimenta primero.
        if ropa is not None and ropa.recognized and ropa.confidence >= cfg.clothing_accept:
            if comportamiento.is_customer and ropa.confidence < cfg.seated_veto_clothing_max:
                return Classification(Role.CUSTOMER, None, 0.8, Method.COMPORTAMIENTO, ropa, comportamiento)
            if ropa.identificador:
                previo = self._identity_memory.get(track_id)
                if previo is None or ropa.confidence >= previo[1]:
                    self._identity_memory[track_id] = (ropa.identificador, ropa.confidence)
            identidad = ropa.identificador or self.remembered(track_id)
            return Classification(Role.STAFF, identidad, ropa.confidence, Method.VESTIMENTA, ropa, comportamiento)

        # 2) Respaldo: comportamiento.
        if comportamiento.is_staff:
            return Classification(
                Role.STAFF, self.remembered(track_id), comportamiento.staff_score,
                Method.COMPORTAMIENTO, ropa, comportamiento,
            )
        if comportamiento.is_customer:
            return Classification(Role.CUSTOMER, None, 0.8, Method.COMPORTAMIENTO, ropa, comportamiento)
        return Classification(Role.UNKNOWN, None, 0.0, Method.NINGUNO, ropa, comportamiento)

    def remembered(self, track_id) -> Optional[str]:
        previo = self._identity_memory.get(track_id)
        return previo[0] if previo else None

    def forget(self, track_id) -> None:
        self._identity_memory.pop(track_id, None)
        self.behavior.forget(track_id)
