# vision_service/scaneats_vision/table_fusion.py
"""
Detección de fusión de mesas (RF-03) con histéresis.

Evidencia de fusión para un grupo de mesas ADYACENTES:
  1. todas están OCUPADAS y sus ocupaciones empezaron casi a la vez (`simultaneity_window_s`);
  2. hay más personas que la capacidad de la mesa individual más grande del grupo
     (dos parejas en dos mesas no son una fusión; un grupo de 6 repartido en dos mesas de 4, sí);
  3. (si hay posiciones) las personas forman un solo grupo espacial continuo (`cohesion_link_distance`).

Histéresis para evitar falsos positivos (gente cruzando cerca de dos mesas):
  * CONFIRMAR: la condición debe sostenerse ≥ `confirm_s` y ≥ `min_observations` fotogramas; huecos
    menores a `dropout_tolerance_s` no reinician la cuenta.
  * TERMINAR: solo si la condición desaparece ≥ `release_s` o todas las mesas del grupo se liberan.
  Confirmar es rápido y terminar es lento a propósito: una fusión no "parpadea".

Un grupo que crece ({4,5} → {4,5,6}) se confirma como un grupo nuevo; el backend lo fusiona en el MISMO
registro (un único registro por fusión).

⚠ confirm_s = 3 s es un valor inicial PENDIENTE DE CALIBRAR con video real. Ojo con el compromiso: RF-03 pide el
registro en < 5 s, así que confirm_s + latencia de la API debe quedar bajo 5 s; subir confirm_s da más
robustez ante falsos positivos a cambio de perder ese criterio.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional

from .geometry import Point, distance


@dataclass(frozen=True)
class FusionConfig:
    confirm_s: float = 3.0
    min_observations: int = 3
    dropout_tolerance_s: float = 2.0
    release_s: float = 30.0
    simultaneity_window_s: float = 90.0
    require_excess_over_capacity: bool = True
    cohesion_link_distance: float = 0.12
    """Distancia máxima (en coordenadas normalizadas) entre dos personas para considerarlas del mismo grupo.
    Solo se evalúa si el pipeline entrega posiciones de los clientes."""

    def validate(self) -> "FusionConfig":
        if self.confirm_s < 0 or self.release_s <= 0 or self.min_observations < 1:
            raise ValueError("Parámetros de histéresis inválidos.")
        if self.release_s <= self.confirm_s:
            raise ValueError("release_s debe ser mayor que confirm_s (histéresis asimétrica).")
        return self


@dataclass(frozen=True)
class TableObservation:
    occupied: bool
    customers: int
    capacity: int
    occupied_since: Optional[float] = None
    customer_points: tuple[Point, ...] = ()


class FusionEventKind(str, Enum):
    CONFIRMED = "FUSION_CONFIRMADA"
    ENDED = "FUSION_TERMINADA"


@dataclass(frozen=True)
class FusionEvent:
    kind: FusionEventKind
    tables: frozenset
    at: float
    reason: str = ""


@dataclass
class _Candidate:
    first_seen: float
    last_seen: float
    observations: int = 1
    confirmed: bool = False


class TableFusionDetector:
    def __init__(self, adjacency: dict, config: Optional[FusionConfig] = None):
        """adjacency: {table_id: {ids de mesas vecinas}} (simétrico)."""
        self.config = (config or FusionConfig()).validate()
        self.adjacency = {a: set(v) for a, v in adjacency.items()}
        for a, vecinos in list(self.adjacency.items()):
            for b in vecinos:
                self.adjacency.setdefault(b, set()).add(a)
        self._candidates: dict[frozenset, _Candidate] = {}

    # ------------------------------------------------------------------ #
    @property
    def active_fusions(self) -> list[frozenset]:
        return [k for k, c in self._candidates.items() if c.confirmed]

    def update(self, now: float, observations: dict) -> list[FusionEvent]:
        cfg = self.config
        events: list[FusionEvent] = []
        actuales = self._grupos_candidatos(observations)

        for grupo in actuales:
            c = self._candidates.get(grupo)
            if c is None or (not c.confirmed and now - c.last_seen > cfg.dropout_tolerance_s):
                c = self._candidates[grupo] = _Candidate(first_seen=now, last_seen=now, observations=0)
            c.last_seen = now
            c.observations += 1
            if not c.confirmed and now - c.first_seen >= cfg.confirm_s and c.observations >= cfg.min_observations:
                c.confirmed = True
                # Un grupo que absorbe a otros ya confirmados los reemplaza (mismo registro en el backend).
                for otro in [k for k, v in self._candidates.items() if v.confirmed and k < grupo]:
                    del self._candidates[otro]
                events.append(FusionEvent(FusionEventKind.CONFIRMED, grupo, now, "condición sostenida"))

        for clave, c in list(self._candidates.items()):
            if clave in actuales:
                continue
            if any(g >= clave for g in actuales):  # la condición sigue cubierta por un grupo mayor
                c.last_seen = now
                continue
            ausente = now - c.last_seen
            if not c.confirmed:
                if ausente > cfg.dropout_tolerance_s:
                    del self._candidates[clave]
                continue
            libres = not any(observations.get(t) and observations[t].occupied for t in clave)
            if libres or ausente >= cfg.release_s:
                del self._candidates[clave]
                restantes = clave - self._mesas_en_fusiones_activas()
                if restantes:
                    events.append(FusionEvent(
                        FusionEventKind.ENDED, frozenset(restantes), now,
                        "mesas liberadas" if libres else "la condición dejó de cumplirse",
                    ))
        return events

    # ------------------------------------------------------------------ #
    def _mesas_en_fusiones_activas(self) -> set:
        return set().union(*self.active_fusions) if self.active_fusions else set()

    def _grupos_candidatos(self, observations: dict) -> set[frozenset]:
        ocupadas = [t for t, o in observations.items() if o.occupied and t in self.adjacency]
        vistos: set = set()
        grupos: set[frozenset] = set()
        for inicio in ocupadas:
            if inicio in vistos:
                continue
            componente, pila = set(), [inicio]
            while pila:
                t = pila.pop()
                if t in componente:
                    continue
                componente.add(t)
                for vecino in self.adjacency[t]:
                    if vecino in componente or vecino not in ocupadas:
                        continue
                    if self._simultaneas(observations[t], observations[vecino]):
                        pila.append(vecino)
            vistos |= componente
            if len(componente) >= 2 and self._cumple_condicion(componente, observations):
                grupos.add(frozenset(componente))
        return grupos

    def _simultaneas(self, a: TableObservation, b: TableObservation) -> bool:
        if a.occupied_since is None or b.occupied_since is None:
            return True  # sin dato de inicio no se descarta (lo deciden las demás condiciones)
        return abs(a.occupied_since - b.occupied_since) <= self.config.simultaneity_window_s

    def _cumple_condicion(self, mesas: set, observations: dict) -> bool:
        cfg = self.config
        obs = [observations[t] for t in mesas]
        if cfg.require_excess_over_capacity:
            if sum(o.customers for o in obs) <= max(o.capacity for o in obs):
                return False
        con_posiciones = [o.customer_points for o in obs if o.customer_points]
        if len(con_posiciones) >= 2 and not self._forman_un_grupo(con_posiciones):
            return False
        return True

    def _forman_un_grupo(self, listas_de_puntos: list[tuple[Point, ...]]) -> bool:
        """¿Están todas las personas (de todas las mesas) en un solo componente conexo por cercanía?"""
        puntos = [p for lista in listas_de_puntos for p in lista]
        enlace = self.config.cohesion_link_distance
        visitados, pila = {0}, [0]
        while pila:
            i = pila.pop()
            for j in range(len(puntos)):
                if j not in visitados and distance(puntos[i], puntos[j]) <= enlace:
                    visitados.add(j)
                    pila.append(j)
        return len(visitados) == len(puntos)
