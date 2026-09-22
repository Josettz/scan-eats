# vision_service/scaneats_vision/evaluation.py
"""
Cálculo de las metas de precisión del documento de requerimientos a partir de anotaciones humanas.
No requiere video: recibe pares (lo que dijo el sistema, lo que observó una persona).

    RF-07  coincidencia de ocupación con observación humana  ≥ 90 %  (100 muestras)
    RF-08  mesero asociado correctamente                      ≥ 85 %  (20 entregas grabadas)
    RNF-01 latencia de actualización de estado                ≤ 45 s

CSV de ejemplo (sin encabezado):  predicho,observado     p. ej.  OCUPADA,OCUPADA

    python -m scaneats_vision.evaluation ocupacion muestras.csv
    python -m scaneats_vision.evaluation atribucion entregas.csv
"""
from __future__ import annotations

import csv
import sys
from dataclasses import dataclass
from typing import Iterable, Sequence

from .occupancy_engine import RNF01_MAX_LATENCY_S

META_OCUPACION = 0.90
META_ATRIBUCION = 0.85


@dataclass(frozen=True)
class Agreement:
    total: int
    matches: int
    target: float

    @property
    def ratio(self) -> float:
        return self.matches / self.total if self.total else 0.0

    @property
    def meets_target(self) -> bool:
        return self.total > 0 and self.ratio >= self.target

    def __str__(self) -> str:
        estado = "CUMPLE" if self.meets_target else "NO CUMPLE"
        return f"{self.matches}/{self.total} = {self.ratio:.1%} (meta ≥ {self.target:.0%}) -> {estado}"


def _coincidencia(pares: Iterable[tuple[str, str]], meta: float) -> Agreement:
    pares = [(str(a).strip().upper(), str(b).strip().upper()) for a, b in pares]
    return Agreement(len(pares), sum(1 for a, b in pares if a == b), meta)


def occupancy_agreement(pares: Iterable[tuple[str, str]]) -> Agreement:
    """RF-07: pares (estado del sistema, estado observado por una persona)."""
    return _coincidencia(pares, META_OCUPACION)


def staff_attribution_accuracy(pares: Iterable[tuple[str, str]]) -> Agreement:
    """RF-08: pares (mesero según el sistema, mesero real) — combinando vestimenta y comportamiento."""
    return _coincidencia(pares, META_ATRIBUCION)


def latency_report(latencias_s: Sequence[float]) -> dict:
    """RNF-01 / RNF-06: latencia de actualización de estado (desde el cambio real hasta el registro)."""
    if not latencias_s:
        return {"muestras": 0, "cumple": False}
    ordenadas = sorted(latencias_s)
    p95 = ordenadas[min(len(ordenadas) - 1, int(0.95 * len(ordenadas)))]
    return {
        "muestras": len(ordenadas), "maxima_s": ordenadas[-1], "p95_s": p95,
        "cumple": ordenadas[-1] <= RNF01_MAX_LATENCY_S,
    }


def _leer_csv(ruta: str) -> list[tuple[str, str]]:
    with open(ruta, newline="", encoding="utf-8") as f:
        return [(fila[0], fila[1]) for fila in csv.reader(f) if len(fila) >= 2 and not fila[0].startswith("#")]


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) != 2 or argv[0] not in {"ocupacion", "atribucion"}:
        print(__doc__)
        return 2
    pares = _leer_csv(argv[1])
    resultado = occupancy_agreement(pares) if argv[0] == "ocupacion" else staff_attribution_accuracy(pares)
    print(resultado)
    return 0 if resultado.meets_target else 1


if __name__ == "__main__":
    raise SystemExit(main())
