# vision_service/scaneats_vision/preview_zones.py
"""
Dibuja las zonas del JSON de configuración sobre un fotograma del video, para validarlas a ojo antes de procesar.

    python -m scaneats_vision.preview_zones --source video.mp4 --config config/zonas_video.json --at 0.5 --out zonas.png

`--at` es la posición en el video (0..1) o segundos si es > 1. Mesas en verde, zona de exclusión en rojo.
"""
from __future__ import annotations

import argparse

from .config_loader import load_config


def dibujar(frame, config):
    import cv2
    import numpy as np

    alto, ancho = frame.shape[:2]
    capa = frame.copy()

    def pts(zona):
        return np.array([[int(x * ancho), int(y * alto)] for x, y in zona.polygon], dtype=np.int32)

    for zona, color in [(t.zone, (0, 200, 0)) for t in config.tables] + [(z, (0, 0, 230)) for z in config.exclusion_zones]:
        cv2.fillPoly(capa, [pts(zona)], color)
    frame = cv2.addWeighted(capa, 0.30, frame, 0.70, 0)
    for zona, color, texto in [(t.zone, (0, 160, 0), f"Mesa {t.numero}") for t in config.tables] + \
                              [(z, (0, 0, 230), f"EXCLUSION {z.id}") for z in config.exclusion_zones]:
        p = pts(zona)
        cv2.polylines(frame, [p], True, color, 3)
        x, y = p[:, 0].min() + 8, p[:, 1].min() + 34
        cv2.putText(frame, texto, (x, y), cv2.FONT_HERSHEY_SIMPLEX, 1.0, (255, 255, 255), 4)
        cv2.putText(frame, texto, (x, y), cv2.FONT_HERSHEY_SIMPLEX, 1.0, color, 2)
    return frame


def main(argv=None) -> int:
    import cv2

    ap = argparse.ArgumentParser(description="Vista previa de zonas sobre un fotograma")
    ap.add_argument("--source", required=True)
    ap.add_argument("--config", required=True)
    ap.add_argument("--at", type=float, default=0.0, help="posición: fracción 0..1 o segundos (> 1)")
    ap.add_argument("--out", default="zonas.png")
    args = ap.parse_args(argv)
    cap = cv2.VideoCapture(args.source)
    total, fps = cap.get(cv2.CAP_PROP_FRAME_COUNT), cap.get(cv2.CAP_PROP_FPS) or 25.0
    cap.set(cv2.CAP_PROP_POS_FRAMES, int(total * args.at) if args.at <= 1 else int(args.at * fps))
    ok, frame = cap.read()
    if not ok:
        raise SystemExit("No se pudo leer el fotograma.")
    cv2.imwrite(args.out, dibujar(frame, load_config(args.config)))
    print(f"Guardado: {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
