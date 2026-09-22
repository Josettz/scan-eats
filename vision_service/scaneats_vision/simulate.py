# vision_service/scaneats_vision/simulate.py
"""
Simulador de escena SIN video ni modelo: genera detecciones sintéticas y las pasa por el pipeline real
(máquina de estados, fusión, cascada de personal, entregas, cliente HTTP). Sirve para:

  * verificar de punta a punta la comunicación con el backend (ver README general),
  * demostrar el flujo en la defensa sin necesidad de cámara ni GPU.

Escena (tiempo virtual, sin esperas reales):
    t=0     dos clientes se sientan en la mesa 1
    t=5     grupo de 6 personas repartido entre las mesas 4 y 5 (pegadas)      -> fusión
    t=15    Mesero A (delantal ROJO) entrega en la mesa 1                        -> entrega + vestimenta
    t=25    Mesero B (delantal AZUL) entrega en la mesa 4                        -> entrega
    t=40    alguien cruza por la mesa 2 (2 s)                                    -> NO ocupa la mesa
    t=60    los clientes de la mesa 1 se van, dejando UN objeto                  -> LIBRE por timeout corto
    t=70    el grupo grande se va                                                -> fin de fusión, mesas LIBRES

    python -m scaneats_vision.simulate --api-url http://localhost:8000 --token <TOKEN>
"""
from __future__ import annotations

import argparse
import logging
from datetime import datetime, timedelta, timezone

from .api_client import ApiClient
from .config_loader import load_config
from .dispatcher import EventDispatcher
from .geometry import BBox
from .occupancy_engine import OccupancyConfig
from .pipeline import ScanEatsPipeline, TrackedPerson, _LogApi
from .settings import ServiceSettings

DT = 0.25
DURACION_S = 110.0


def _caja(cx: float, cy: float, w: float = 0.04, h: float = 0.06) -> BBox:
    return BBox(cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2)


def escena(t: float, config):
    """Devuelve (personas, objetos, zone_change, recortes) para el instante virtual `t`."""
    z = {tb.numero: tb.zone.bbox for tb in config.tables}
    centro = lambda n: ((z[n].x1 + z[n].x2) / 2, (z[n].y1 + z[n].y2) / 2)  # noqa: E731
    personas, objetos, recortes = [], [], {}
    cambio = {tb.numero: 0.0 for tb in config.tables}

    if t < 60:  # mesa 1: pareja
        cx, cy = centro(1)
        personas += [TrackedPerson(1, _caja(cx - 0.02, cy)), TrackedPerson(2, _caja(cx + 0.02, cy))]
        objetos.append(_caja(cx, cy + 0.05, 0.02, 0.02))
    elif t < 100:  # se fueron: queda un solo objeto
        cx, cy = centro(1)
        objetos.append(_caja(cx, cy + 0.05, 0.02, 0.02))

    if 5 <= t < 70:  # grupo de 6 junto al borde común de las mesas 4 y 5
        y4, y5 = z[4].y2 - 0.03, z[5].y1 + 0.03
        cx = (z[4].x1 + z[4].x2) / 2
        for i, dx in enumerate((-0.03, 0.0, 0.03)):
            personas.append(TrackedPerson(11 + i, _caja(cx + dx, y4)))
            personas.append(TrackedPerson(14 + i, _caja(cx + dx, y5)))

    if 14 <= t < 18:  # Mesero A en la mesa 1
        cx, cy = centro(1)
        personas.append(TrackedPerson(100, _caja(cx, cy - 0.10)))
        recortes[100] = "ROJO"
    if 15 <= t < 18:
        cambio[1] = 0.12  # apareció un plato (sustracción de fondo)
    if 24 <= t < 28:  # Mesero B en la mesa 4
        cx, cy = centro(4)
        personas.append(TrackedPerson(101, _caja(cx + 0.03, cy - 0.06)))
        recortes[101] = "AZUL"
    if 25 <= t < 28:
        cambio[4] = 0.15
    if 40 <= t < 42:  # transeúnte: 2 s en la mesa 2, menos que occupy_confirm_s
        cx, cy = centro(2)
        personas.append(TrackedPerson(200, _caja(cx, cy)))
    return personas, objetos, cambio, recortes


def run(pipeline: ScanEatsPipeline, start_wall: datetime, sleep_real=None) -> None:
    t = 0.0
    while t <= DURACION_S:
        personas, objetos, cambio, recortes = escena(t, pipeline.config)
        pipeline.process(t, start_wall + timedelta(seconds=t), personas, objetos, cambio, recortes)
        t += DT
    pipeline.close()


def main(argv=None) -> int:
    base = ServiceSettings.from_env()
    ap = argparse.ArgumentParser(description="Simulador de escena para ScanEats (sin video)")
    ap.add_argument("--config", default=base.config_path)
    ap.add_argument("--api-url", default=base.api_base_url)
    ap.add_argument("--token", default=base.api_token)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    config = load_config(args.config)
    publisher = EventDispatcher(_LogApi(), sync=True) if args.dry_run else EventDispatcher(ApiClient(args.api_url, args.token))
    # Color simulado: el "recorte" es directamente el nombre del color.
    pipeline = ScanEatsPipeline(config, publisher, occupancy_config=OccupancyConfig(),
                                color_ratio_fn=lambda crop: {crop: 0.5})
    # La escena termina "ahora": así los timestamps quedan en el pasado reciente y no en el futuro.
    run(pipeline, datetime.now(timezone.utc) - timedelta(seconds=DURACION_S))
    print("Resumen:", pipeline.stats)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
