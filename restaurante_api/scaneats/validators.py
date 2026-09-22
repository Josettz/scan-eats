# restaurante_api/scaneats/validators.py
from django.core.exceptions import ValidationError


def validar_region(valor):
    """
    Una región es un polígono en coordenadas NORMALIZADAS (0..1) respecto al fotograma:
    [[x1, y1], [x2, y2], [x3, y3], ...] con al menos 3 vértices. Así no depende de la
    resolución de la cámara. Es lo que consume el microservicio de visión.
    """
    if valor in (None, ""):
        return
    if not isinstance(valor, list) or len(valor) < 3:
        raise ValidationError("La región debe ser una lista de al menos 3 vértices [x, y].")
    for punto in valor:
        if (
            not isinstance(punto, (list, tuple))
            or len(punto) != 2
            or not all(isinstance(c, (int, float)) and not isinstance(c, bool) for c in punto)
        ):
            raise ValidationError("Cada vértice de la región debe ser un par numérico [x, y].")
        if not all(0 <= c <= 1 for c in punto):
            raise ValidationError("Las coordenadas de la región deben estar normalizadas entre 0 y 1.")
