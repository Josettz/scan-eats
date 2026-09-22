# vision_service/tests/test_annotate.py
"""El anotador dibuja las detecciones sobre el fotograma (para el video de depuración y para los clips de evidencia)."""
from datetime import datetime, timezone
from pathlib import Path

import pytest

np = pytest.importorskip("numpy")
cv2 = pytest.importorskip("cv2")

from scaneats_vision.annotate import Annotator, EventTap  # noqa: E402
from scaneats_vision.config_loader import load_config  # noqa: E402
from scaneats_vision.detector import Detection  # noqa: E402
from scaneats_vision.dispatcher import EventDispatcher  # noqa: E402
from scaneats_vision.geometry import BBox  # noqa: E402
from scaneats_vision.pipeline import ScanEatsPipeline, TrackedPerson  # noqa: E402
from scaneats_vision.tracking import Track  # noqa: E402

CONFIG = Path(__file__).resolve().parent.parent / "config" / "zonas_video.json"
WALL = datetime(2026, 3, 1, tzinfo=timezone.utc)


class ApiNula:
    def __getattr__(self, nombre):
        return lambda **kw: None


def escena():
    tap = EventTap(EventDispatcher(ApiNula(), sync=True))
    pipe = ScanEatsPipeline(load_config(CONFIG), tap, color_ratio_fn=lambda c: {})
    caja = BBox(0.45, 0.20, 0.50, 0.40)
    dets = [Detection(0, 0.91, caja), Detection(41, 0.62, BBox(0.47, 0.22, 0.49, 0.25))]
    pistas = [Track(7, caja, confirmed=True)]
    for i in range(30):  # 6 s con alguien sentado: la mesa 2 queda OCUPADA
        pipe.process(i * 0.2, WALL, [TrackedPerson(7, caja)])
    return tap, pipe, dets, pistas


def test_render_dibuja_sobre_una_copia_y_no_modifica_el_original():
    tap, pipe, dets, pistas = escena()
    frame = np.full((1080, 1920, 3), 90, dtype=np.uint8)
    original = frame.copy()
    img = Annotator(None, 5.0, tap).render(frame, 6.0, dets, pistas, pipe, {2: 0.004})
    assert (frame == original).all()  # no toca el fotograma de entrada
    assert img.shape == (720, 1280, 3)  # 1280 de ancho, misma proporción
    assert (img != 90).mean() > 0.02  # se dibujó algo: zonas, cajas, textos


def test_las_cajas_de_yolo_tienen_su_color():
    tap, pipe, dets, pistas = escena()
    img = Annotator(None, 5.0, tap).render(np.zeros((1080, 1920, 3), dtype=np.uint8), 6.0, dets, pistas, pipe, {})
    assert ((img == (255, 255, 255)).all(axis=2)).any()  # persona detectada: caja blanca
    assert ((img == (255, 220, 0)).all(axis=2)).any()  # vajilla detectada: cian


def test_sin_ruta_no_se_escribe_ningun_video():
    tap, pipe, dets, pistas = escena()
    an = Annotator(None, 5.0, tap)
    an.draw(np.zeros((1080, 1920, 3), dtype=np.uint8), 6.0, dets, pistas, pipe, {})
    an.close()
    assert an.frames == 0 and an._writer is None


def test_con_ruta_se_escribe_el_video_anotado(tmp_path):
    tap, pipe, dets, pistas = escena()
    salida = tmp_path / "anotado.mp4"
    an = Annotator(str(salida), 5.0, tap)
    for _ in range(3):
        an.draw(np.zeros((1080, 1920, 3), dtype=np.uint8), 6.0, dets, pistas, pipe, {})
    an.close()
    assert an.frames == 3 and salida.exists() and salida.stat().st_size > 0


def test_el_espia_de_eventos_muestra_ocupaciones_y_entregas_y_reenvia_todo():
    reenviados = []

    class Real:
        def submit(self, method, **kw):
            reenviados.append(method)

    tap = EventTap(Real())
    tap.now = 10.0
    tap.submit("reportar_ocupacion", mesa_numero=2)
    tap.submit("reportar_entrega", mesa_numero=2, personal_identificador="MES-A", metodo="VESTIMENTA")
    tap.submit("subir_clip", ruta_clip="x")  # ruido: no se dibuja pero sí se reenvía
    assert [t for _, t in tap.eventos] == ["OCUPACION mesa 2", "ENTREGA mesa 2 por MES-A (VESTIMENTA)"]
    assert reenviados == ["reportar_ocupacion", "reportar_entrega", "subir_clip"]
