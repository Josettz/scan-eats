# vision_service/scaneats_vision/config_loader.py
"""
Configuración de zonas, mesas y personal que necesita el pipeline.

Formato (coordenadas NORMALIZADAS 0..1 respecto al fotograma; ver config/zonas_ejemplo.json):

    {
      "camera_id": "cam-salon-1",
      "adjacency_max_gap": 0.03,
      "tables":          [{"numero": 1, "capacidad": 2, "zona": "Salón", "polygon": [[x, y], ...], "adyacentes": [2]}],
      "exclusion_zones": [{"nombre": "Caja", "polygon": [[x, y], ...]}],
      "delivery":        {"change_high": 0.002, "change_low": 0.0007}      (opcional: umbrales de entrega de esta cámara)
      "behavior" / "cascade" / "tracker": (opcionales) parámetros de BehaviorConfig, CascadeConfig y SortTracker
      "staff":           [{"identificador": "MES-A", "nombre": "Mesero A", "prenda_color": "ROJO", "prenda_tipo": "DELANTAL"}]
    }

Cada mesa admite un `"surface"` (polígono del mantel/tablero) para medir las entregas por sustracción de fondo.
`adyacentes` es opcional: si se omite, dos mesas son adyacentes cuando el hueco entre sus cajas es
≤ `adjacency_max_gap`.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .delivery_detector import DeliveryConfig
from .geometry import EXCLUSION, TABLE, Zone, zones_adjacent
from .staff_classifier import BehaviorConfig, CascadeConfig, StaffProfile
from .tracking import SortTracker


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class TableConfig:
    numero: int
    capacidad: int
    zone: Zone
    zona: str = ""
    surface: Optional[Zone] = None
    """Superficie de la mesa (el mantel/tablero) donde aparecen los platos. Es MUCHO más pequeña que `zone`
    (que incluye sillas y piso), así que el cambio de un plato pesa más al medir la sustracción de fondo."""

    @property
    def surface_zone(self) -> Zone:
        return self.surface or self.zone


@dataclass
class VisionConfig:
    camera_id: str
    tables: list[TableConfig]
    exclusion_zones: list[Zone]
    staff: list[StaffProfile]
    adjacency: dict[int, set[int]] = field(default_factory=dict)
    delivery: dict = field(default_factory=dict)
    """Umbrales de entrega calibrados para ESTA cámara (claves de `DeliveryConfig`)."""
    behavior: dict = field(default_factory=dict)
    """Parámetros de `BehaviorConfig` para esta cámara (p. ej. seated_dwell_s)."""
    cascade: dict = field(default_factory=dict)
    """Parámetros de `CascadeConfig` para esta cámara (p. ej. clothing_accept)."""
    tracker: dict = field(default_factory=dict)
    """Parámetros del tracker SORT para esta cámara (p. ej. max_age)."""

    @property
    def surface_zones(self) -> list[Zone]:
        return [t.surface_zone for t in self.tables]

    @property
    def table_zones(self) -> list[Zone]:
        return [t.zone for t in self.tables]

    @property
    def all_zones(self) -> list[Zone]:
        """Mesas + exclusión: se asignan JUNTAS para que quien está en la caja no se cuente en la mesa vecina."""
        return self.table_zones + self.exclusion_zones

    def table(self, numero: int) -> TableConfig:
        return next(t for t in self.tables if t.numero == numero)

    # ------------------------------------------------------------------ #
    @classmethod
    def from_dict(cls, data: dict) -> "VisionConfig":
        tablas = []
        for i, t in enumerate(data.get("tables", [])):
            numero = _entero(t.get("numero"), f"tables[{i}].numero")
            capacidad = _entero(t.get("capacidad"), f"tables[{i}].capacidad")
            if capacidad < 1:
                raise ConfigError(f"tables[{i}].capacidad debe ser ≥ 1.")
            zona = Zone(numero, _poligono(t.get("polygon"), f"tables[{i}].polygon"), TABLE)
            superficie = None
            if t.get("surface") is not None:
                superficie = Zone(numero, _poligono(t["surface"], f"tables[{i}].surface"), TABLE)
            tablas.append(TableConfig(numero, capacidad, zona, t.get("zona", ""), superficie))
        if not tablas:
            raise ConfigError("La configuración necesita al menos una mesa.")
        numeros = [t.numero for t in tablas]
        if len(set(numeros)) != len(numeros):
            raise ConfigError("Hay números de mesa repetidos.")

        exclusion = [
            Zone(z.get("nombre", f"exclusion-{i}"), _poligono(z.get("polygon"), f"exclusion_zones[{i}].polygon"), EXCLUSION)
            for i, z in enumerate(data.get("exclusion_zones", []))
        ]
        personal = [
            StaffProfile(
                identificador=str(p["identificador"]).strip().upper(),
                nombre=p.get("nombre", ""),
                color=(p.get("prenda_color") or None),
                tipo=(p.get("prenda_tipo") or None),
            )
            for p in data.get("staff", [])
        ]
        adyacencia = _adyacencia(tablas, data.get("adjacency_max_gap", 0.03))
        ajustes = {}
        for clave, fabrica in (("delivery", DeliveryConfig), ("behavior", BehaviorConfig), ("cascade", CascadeConfig),
                               ("tracker", SortTracker)):
            valores = dict(data.get(clave) or {})
            try:
                objeto = fabrica(**valores)
                if hasattr(objeto, "validate"):
                    objeto.validate()
            except (TypeError, ValueError) as exc:
                raise ConfigError(f"{clave}: {exc}")
            ajustes[clave] = valores
        return cls(data.get("camera_id", "camara-1"), tablas, exclusion, personal, adyacencia, **ajustes)

    @classmethod
    def from_api_payload(cls, payload: dict, camera_id: str = "camara-1", adjacency_max_gap: float = 0.03) -> "VisionConfig":
        """Convierte la respuesta de GET /api/vision/configuracion/ (backend) al formato interno."""
        zonas = {z["id"]: z for z in payload.get("zonas", [])}
        tables, exclusion = [], []
        for m in payload.get("mesas", []):
            zona = zonas.get(m.get("zona_id"))
            if zona and zona["es_exclusion"]:
                if zona.get("region"):
                    exclusion.append({"nombre": zona["nombre"], "polygon": zona["region"]})
                continue
            if not m.get("region"):
                continue  # mesa sin región dibujada: la visión no puede monitorearla todavía
            tables.append({"numero": m["numero"], "capacidad": m["capacidad"], "polygon": m["region"],
                           "zona": zona["nombre"] if zona else ""})
        return cls.from_dict({"camera_id": camera_id, "tables": tables, "exclusion_zones": exclusion,
                              "staff": payload.get("personal", []), "adjacency_max_gap": adjacency_max_gap})


def load_config(path) -> VisionConfig:
    """
    Carga la configuración desde un JSON local.

    TODO: reemplazar por una llamada a la API de Django (GET /api/vision/configuracion/ con el token de servicio)
          y `VisionConfig.from_api_payload(respuesta)`. Ese endpoint y el conversor ya existen; falta que el
          administrador dibuje la `region` de cada mesa/zona en el backend. Hasta entonces se usa este archivo.
    """
    p = Path(path)
    try:
        return VisionConfig.from_dict(json.loads(p.read_text(encoding="utf-8")))
    except FileNotFoundError:
        raise ConfigError(f"No existe el archivo de configuración: {p}")
    except json.JSONDecodeError as exc:
        raise ConfigError(f"JSON inválido en {p}: {exc}")


# --------------------------------------------------------------------------- #
def _entero(valor, campo) -> int:
    if isinstance(valor, bool) or not isinstance(valor, int):
        raise ConfigError(f"{campo} debe ser un entero.")
    return valor


def _poligono(valor, campo) -> tuple:
    if not isinstance(valor, list) or len(valor) < 3:
        raise ConfigError(f"{campo}: se necesitan al menos 3 vértices.")
    puntos = []
    for p in valor:
        if not (isinstance(p, (list, tuple)) and len(p) == 2 and all(isinstance(c, (int, float)) for c in p)):
            raise ConfigError(f"{campo}: cada vértice debe ser [x, y].")
        if not all(0 <= c <= 1 for c in p):
            raise ConfigError(f"{campo}: las coordenadas deben estar normalizadas (0..1).")
        puntos.append((float(p[0]), float(p[1])))
    return tuple(puntos)


def _adyacencia(tablas: list[TableConfig], max_gap: float) -> dict[int, set[int]]:
    ady: dict[int, set[int]] = {t.numero: set() for t in tablas}
    for t in tablas:
        for j, otra in enumerate(tablas):
            if otra.numero != t.numero and zones_adjacent(t.zone, otra.zone, max_gap):
                ady[t.numero].add(otra.numero)
                ady[otra.numero].add(t.numero)
    return ady
