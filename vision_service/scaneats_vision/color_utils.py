# vision_service/scaneats_vision/color_utils.py
"""
Extracción del color dominante de la prenda (RF-08, método PRINCIPAL).

`color_ratios_from_hsv` solo necesita numpy (se prueba con arreglos sintéticos).
`torso_color_ratios` recibe el recorte BGR de una persona y usa OpenCV (importación perezosa).

Convención de OpenCV: H ∈ [0, 179], S y V ∈ [0, 255]. Los nombres de color son los de
`Personal.PrendaColor` del backend (ROJO, AZUL, ...).
"""
from __future__ import annotations

from typing import Optional

COLORES = ("ROJO", "NARANJA", "AMARILLO", "VERDE", "AZUL", "MORADO", "ROSADO", "NEGRO", "BLANCO", "GRIS")

# Umbrales HSV (calibrables con fotogramas del local: la iluminación cambia bastante los valores).
V_NEGRO = 60
S_ACROMATICO = 40
V_BLANCO = 190
S_CROMATICO = 70
V_MIN_CROMATICO = 60

# (nombre, h_min inclusive, h_max exclusive) para píxeles cromáticos. El rojo cruza el 0/179.
RANGOS_HUE = (
    ("ROJO", 0, 8), ("NARANJA", 8, 20), ("AMARILLO", 20, 35), ("VERDE", 35, 85),
    ("AZUL", 85, 130), ("MORADO", 130, 150), ("ROSADO", 150, 170), ("ROJO", 170, 180),
)

# Zona del recorte de la persona que se analiza: el TORSO (evita cabeza y piernas).
TORSO_Y = (0.20, 0.60)
TORSO_X = (0.15, 0.85)


def color_ratios_from_hsv(hsv) -> dict[str, float]:
    """Fracción de píxeles de cada color (los píxeles "ni una cosa ni la otra" no suman a ninguno)."""
    import numpy as np

    hsv = np.asarray(hsv)
    if hsv.ndim == 2:  # lista de píxeles (N, 3)
        hsv = hsv.reshape(-1, 1, 3)
    total = hsv.shape[0] * hsv.shape[1]
    if total == 0:
        return {c: 0.0 for c in COLORES}
    h, s, v = (hsv[..., i].astype(np.int32) for i in range(3))

    negro = v < V_NEGRO
    acromatico = ~negro & (s < S_ACROMATICO)
    blanco = acromatico & (v > V_BLANCO)
    gris = acromatico & ~blanco
    cromatico = ~negro & (s >= S_CROMATICO) & (v >= V_MIN_CROMATICO)

    conteo = {"NEGRO": int(negro.sum()), "BLANCO": int(blanco.sum()), "GRIS": int(gris.sum())}
    for nombre, h_min, h_max in RANGOS_HUE:
        conteo[nombre] = conteo.get(nombre, 0) + int((cromatico & (h >= h_min) & (h < h_max)).sum())
    return {c: conteo.get(c, 0) / total for c in COLORES}


def torso_region(crop):
    """Recorta el torso del recorte (alto x ancho x 3) de una persona."""
    alto, ancho = crop.shape[:2]
    y1, y2 = int(alto * TORSO_Y[0]), int(alto * TORSO_Y[1])
    x1, x2 = int(ancho * TORSO_X[0]), int(ancho * TORSO_X[1])
    return crop[y1:max(y2, y1 + 1), x1:max(x2, x1 + 1)]


def torso_color_ratios(crop_bgr) -> Optional[dict[str, float]]:
    """Ratios de color del torso de un recorte BGR. None si el recorte es demasiado pequeño para ser confiable."""
    if crop_bgr is None or crop_bgr.size == 0:
        return None
    alto, ancho = crop_bgr.shape[:2]
    if alto < 24 or ancho < 12:
        return None
    import cv2  # perezoso: solo hace falta con video real

    torso = torso_region(crop_bgr)
    return color_ratios_from_hsv(cv2.cvtColor(torso, cv2.COLOR_BGR2HSV))
