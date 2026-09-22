# vision_service/scaneats_vision/occupancy_engine.py
"""
Máquina de estados de ocupación de una mesa (RF-07, RNF-01).

    LIBRE ──(clientes ≥ occupy_confirm_s)──▶ OCUPADA
    OCUPADA ──(sin clientes > presence_gap_tolerance_s)──▶ POSIBLEMENTE_LIBRE
    POSIBLEMENTE_LIBRE ──(clientes de vuelta ≥ occupy_confirm_s)──▶ OCUPADA
    POSIBLEMENTE_LIBRE ──(sin clientes durante el TIMEOUT que corresponda)──▶ LIBRE

El TIMEOUT depende de qué queda sobre la mesa mientras no hay ningún cliente:
    * nada                          -> empty_table_timeout_s            (mesa claramente vacía)
    * un solo objeto                -> single_object_timeout_s          (objeto olvidado: casi seguro libre)
    * varios objetos (platos, etc.) -> absent_with_objects_timeout_s    (el cliente pudo ir al baño)

Sin persona el "objeto abandonado" se libera mucho antes que un "cliente ausente con platos".

RNF-01: el estado debe actualizarse ≤ 45 s desde el cambio real. Por eso la suma
"timeout + hueco entre fotogramas + latencia de la API" se valida al construir la configuración
(`OccupancyConfig.validate`): ningún timeout puede exceder el presupuesto.

⚠ Los valores por defecto son una PRIMERA ESTIMACIÓN, PENDIENTE DE CALIBRAR con video real del local
(ver "Pendiente de definir" en la conclusión del documento de requerimientos). Son parámetros, no constantes
enterradas en la lógica.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional

RNF01_MAX_LATENCY_S = 45.0


class TableState(str, Enum):
    OCUPADA = "OCUPADA"
    POSIBLEMENTE_LIBRE = "POSIBLEMENTE_LIBRE"
    LIBRE = "LIBRE"


@dataclass(frozen=True)
class OccupancyConfig:
    # --- ocupar -----------------------------------------------------------------
    occupy_confirm_s: float = 4.0
    """Segundos continuos con cliente(s) en la mesa para confirmar OCUPADA (filtra a quien solo pasa)."""
    presence_gap_tolerance_s: float = 2.5
    """Una detección perdida menos tiempo que esto no interrumpe la presencia (parpadeo del detector).
    Debe ser MAYOR que max_frame_gap_s: si no, un solo fotograma sin cliente reiniciaría la cuenta."""

    # --- liberar (períodos de gracia) -------------------------------------------
    empty_table_timeout_s: float = 8.0
    """Sin clientes ni objetos -> LIBRE tras este tiempo. PENDIENTE DE CALIBRAR con video real."""
    single_object_timeout_s: float = 12.0
    """Sin clientes y SOLO un objeto sobre la mesa -> LIBRE tras este tiempo (mucho menor que
    absent_with_objects_timeout_s). PENDIENTE DE CALIBRAR con video real del local."""
    absent_with_objects_timeout_s: float = 35.0
    """Sin clientes pero con varios objetos (cliente ausente con su pedido). PENDIENTE DE CALIBRAR con video real."""
    single_object_max_count: int = 1
    """Hasta cuántos objetos cuentan como "solo un objeto"."""

    # --- presupuesto de latencia (RNF-01) ---------------------------------------
    max_frame_gap_s: float = 1.0
    """Máximo hueco entre fotogramas procesados (1 / FPS de procesamiento, con margen). Con 5 FPS son 0.2 s."""
    api_latency_budget_s: float = 3.0
    """Tiempo reservado para reportar el cambio al backend (RF-01: ≤ 3 s)."""

    def worst_case_latency_s(self) -> float:
        """Peor caso desde el cambio real hasta que el backend recibe el nuevo estado."""
        timeouts = (self.empty_table_timeout_s, self.single_object_timeout_s, self.absent_with_objects_timeout_s)
        return max(timeouts + (self.occupy_confirm_s,)) + self.max_frame_gap_s + self.api_latency_budget_s

    def validate(self) -> "OccupancyConfig":
        if self.presence_gap_tolerance_s <= 0 or self.occupy_confirm_s <= 0:
            raise ValueError("occupy_confirm_s y presence_gap_tolerance_s deben ser > 0.")
        if not (self.presence_gap_tolerance_s > self.max_frame_gap_s):
            raise ValueError(
                "presence_gap_tolerance_s debe ser mayor que max_frame_gap_s "
                "(si no, un solo fotograma sin cliente reiniciaría la confirmación de ocupación)."
            )
        if not (self.presence_gap_tolerance_s < self.empty_table_timeout_s):
            raise ValueError("empty_table_timeout_s debe ser mayor que presence_gap_tolerance_s.")
        if not (self.single_object_timeout_s < self.absent_with_objects_timeout_s):
            raise ValueError(
                "single_object_timeout_s debe ser MENOR que absent_with_objects_timeout_s "
                "(un objeto solo se libera antes que un cliente ausente con su pedido)."
            )
        peor = self.worst_case_latency_s()
        if peor > RNF01_MAX_LATENCY_S:
            raise ValueError(
                f"Configuración incompatible con RNF-01: peor caso {peor:.1f} s > {RNF01_MAX_LATENCY_S:.0f} s. "
                "Reduzca los timeouts o el hueco entre fotogramas."
            )
        return self

    def release_timeout_s(self, objects: int) -> float:
        if objects <= 0:
            return self.empty_table_timeout_s
        if objects <= self.single_object_max_count:
            return self.single_object_timeout_s
        return self.absent_with_objects_timeout_s


@dataclass(frozen=True)
class OccupancyTransition:
    table_id: object
    previous: TableState
    current: TableState
    at: float
    """Instante (reloj del pipeline) en que la máquina CONFIRMÓ el cambio."""
    since: float
    """Instante real del cambio: inicio de la presencia (LIBRE→OCUPADA) o última vez que se vio un cliente
    (→ POSIBLEMENTE_LIBRE / LIBRE). Es el que se reporta como hora de inicio / fin de la ocupación."""
    reason: str


class OccupancyEngine:
    """Estado de UNA mesa. El tiempo lo inyecta el llamador (segundos), sin depender del reloj del sistema."""

    def __init__(self, table_id, config: Optional[OccupancyConfig] = None):
        self.table_id = table_id
        self.config = (config or OccupancyConfig()).validate()
        self.state = TableState.LIBRE
        self.occupied_since: Optional[float] = None  # hora_inicio de la ocupación en curso
        self._presence_start: Optional[float] = None
        self._last_customer_seen: Optional[float] = None

    # ------------------------------------------------------------------ #
    def update(self, now: float, customers: int, objects: int = 0) -> list[OccupancyTransition]:
        """
        Procesa una observación (clientes NO-personal y objetos sobre la mesa en este fotograma).
        Devuelve 0, 1 o 2 transiciones (2 si se saltaron fotogramas y se cruzaron dos umbrales).
        """
        cfg = self.config
        out: list[OccupancyTransition] = []

        if customers > 0:
            if self._last_customer_seen is None or now - self._last_customer_seen > cfg.presence_gap_tolerance_s:
                self._presence_start = now  # nueva racha de presencia
            self._last_customer_seen = now
            racha = now - (self._presence_start if self._presence_start is not None else now)
            if self.state != TableState.OCUPADA and racha >= cfg.occupy_confirm_s:
                razon = "presencia confirmada" if self.state == TableState.LIBRE else "el cliente regresó"
                since = self._presence_start if self.state == TableState.LIBRE else self.occupied_since
                if self.state == TableState.LIBRE:
                    self.occupied_since = self._presence_start
                out.append(self._move(TableState.OCUPADA, now, since, razon))
            return out

        # --- sin clientes en este fotograma -------------------------------------
        if self.state == TableState.LIBRE:
            if self._last_customer_seen is not None and now - self._last_customer_seen > cfg.presence_gap_tolerance_s:
                self._presence_start = None  # la presencia breve no llegó a confirmarse
                self._last_customer_seen = None
            return out

        ausente = now - self._last_customer_seen
        if self.state == TableState.OCUPADA and ausente >= cfg.presence_gap_tolerance_s:
            out.append(self._move(TableState.POSIBLEMENTE_LIBRE, now, self._last_customer_seen, "sin clientes en la mesa"))
        if self.state == TableState.POSIBLEMENTE_LIBRE and ausente >= cfg.release_timeout_s(objects):
            out.append(self._move(TableState.LIBRE, now, self._last_customer_seen, self._motivo_liberacion(objects)))
            self.occupied_since = self._presence_start = self._last_customer_seen = None
        return out

    # ------------------------------------------------------------------ #
    def _motivo_liberacion(self, objects: int) -> str:
        if objects <= 0:
            return "mesa vacía"
        if objects <= self.config.single_object_max_count:
            return "solo queda un objeto sin ninguna persona"
        return "cliente ausente con objetos: se agotó el período de gracia"

    def _move(self, nuevo: TableState, now: float, since: float, razon: str) -> OccupancyTransition:
        t = OccupancyTransition(self.table_id, self.state, nuevo, now, since, razon)
        self.state = nuevo
        return t


class OccupancyManager:
    """Un OccupancyEngine por mesa (RNF-06: 20 mesas simultáneas = 20 máquinas de estado O(1) por fotograma)."""

    def __init__(self, table_ids, config: Optional[OccupancyConfig] = None):
        self.config = (config or OccupancyConfig()).validate()
        self.engines = {tid: OccupancyEngine(tid, self.config) for tid in table_ids}

    def state(self, table_id) -> TableState:
        return self.engines[table_id].state

    def update(self, now: float, observations: dict) -> list[OccupancyTransition]:
        """observations: {table_id: (clientes, objetos)}. Las mesas sin observación cuentan como (0, 0)."""
        out = []
        for tid, engine in self.engines.items():
            customers, objects = observations.get(tid, (0, 0))
            out.extend(engine.update(now, customers, objects))
        return out
