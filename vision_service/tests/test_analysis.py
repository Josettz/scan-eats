# vision_service/tests/test_analysis.py
"""Análisis del video completo: estadísticas, nivel de demanda e informe (HTML/JSON)."""
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

np = pytest.importorskip("numpy")
pytest.importorskip("cv2")

from scaneats_vision import simulate  # noqa: E402
from scaneats_vision.analysis import AnalisisVideo, FICHAS  # noqa: E402
from scaneats_vision.annotate import EventTap  # noqa: E402
from scaneats_vision.config_loader import load_config  # noqa: E402
from scaneats_vision.detector import Detection  # noqa: E402
from scaneats_vision.dispatcher import EventDispatcher  # noqa: E402
from scaneats_vision.geometry import BBox  # noqa: E402
from scaneats_vision.pipeline import ScanEatsPipeline  # noqa: E402
from scaneats_vision.tracking import Track  # noqa: E402

EJEMPLO = Path(__file__).resolve().parent.parent / "config" / "zonas_ejemplo.json"
WALL0 = datetime(2026, 3, 1, 10, 0, tzinfo=timezone.utc)


class ApiNula:
    def __getattr__(self, nombre):
        return lambda **kw: None


def correr_escena(tmp_path):
    """La escena de simulate.py (3 mesas ocupadas a la vez, 2 entregas, 1 fusión) pasada por pipeline + análisis."""
    tap = EventTap(EventDispatcher(ApiNula(), sync=True))
    cfg = load_config(EJEMPLO)
    pipe = ScanEatsPipeline(cfg, tap, color_ratio_fn=lambda c: {c: 0.5})
    an = AnalisisVideo(cfg, tap, fps_proceso=4.0, carpeta=str(tmp_path))
    imagen = np.zeros((360, 640, 3), dtype=np.uint8)
    t = 0.0
    while t <= simulate.DURACION_S:
        personas, objetos, cambio, recortes = simulate.escena(t, cfg)
        tap.now = t
        pipe.process(t, WALL0 + timedelta(seconds=t), personas, objetos, cambio, recortes)
        dets = [Detection(0, 0.9, p.bbox) for p in personas]
        pistas = [Track(p.track_id, p.bbox, confirmed=True) for p in personas]
        an.observar(t, dets, pistas, pipe, imagen)
        t += simulate.DT
    an.resolucion, an.duracion_video, an.tiempo_proceso_s = (1920, 1080), simulate.DURACION_S, 5.0
    return an


class TestResumen:
    def test_estadisticas_de_la_escena(self, tmp_path):
        r = correr_escena(tmp_path).resumen()
        assert r["yolo"]["maximo_personas_a_la_vez"] >= 8  # pareja + grupo de 6 + mesero
        assert r["yolo"]["detecciones_de_persona"] > 0 and r["yolo"]["confianza_promedio"] == pytest.approx(0.9)
        assert r["ocupacion"]["ocupaciones"] == 3
        assert r["ocupacion"]["maximo_mesas_ocupadas_a_la_vez"] == 3
        assert r["ocupacion"]["por_mesa"][1]["ocupaciones"] == 1
        assert r["ocupacion"]["por_mesa"][2]["ocupaciones"] == 0  # el transeúnte no ocupó la mesa 2

    def test_tiempos_en_segundos_de_video(self, tmp_path):
        r = correr_escena(tmp_path).resumen()
        mesa1 = next(o for o in r["ocupacion"]["detalle"] if o["mesa"] == 1)
        assert mesa1["inicio"] == pytest.approx(0.0, abs=0.3)  # se sentaron en t=0
        assert mesa1["fin"] == pytest.approx(59.75, abs=0.3)  # último cliente visto
        assert mesa1["espera_s"] == pytest.approx(15.0, abs=1.0)  # entrega en t=15
        assert {e["mesero"] for e in r["entregas"]["detalle"]} == {"MES-A", "MES-B"}
        assert r["entregas"]["espera_promedio_s"] == pytest.approx(17.5, abs=1.5)  # 15 s y 20 s

    def test_la_escena_de_3_mesas_a_la_vez_es_demanda_baja_como_la_ficha_a1(self, tmp_path):
        d = correr_escena(tmp_path).resumen()["demanda"]
        assert d["nivel"] == "baja" and d["maximo_mesas_ocupadas_a_la_vez"] == 3 and d["mesas_monitoreadas"] == 12
        assert d["fraccion_maxima_ocupada"] == pytest.approx(0.25)
        assert any("hora pico" in a for a in d["avisos"])
        assert any("Muestra pequeña" in a for a in d["avisos"])  # solo 3 ocupaciones


class TestNivelDeDemanda:
    @pytest.mark.parametrize("maxima, n, esperado", [(1, 4, "baja"), (3, 12, "baja"), (2, 4, "normal"), (5, 12, "normal"),
                                                     (3, 4, "alta"), (9, 12, "alta"), (12, 12, "alta"), (0, 12, "baja")])
    def test_reglas(self, maxima, n, esperado):
        assert AnalisisVideo._demanda(n, maxima, float(maxima), 50, 5.0)["nivel"] == esperado

    def test_el_aviso_de_muestra_pequena_solo_aparece_con_pocas_ocupaciones(self):
        pocas = AnalisisVideo._demanda(12, 2, 1.0, 3, 1.0)["avisos"]
        muchas = AnalisisVideo._demanda(12, 9, 6.0, 80, 9.0)["avisos"]
        assert any("Muestra pequeña" in a for a in pocas) and not any("Muestra pequeña" in a for a in muchas)
        assert not any("hora pico" in a for a in muchas)  # con demanda alta no se advierte "no es hora pico"

    def test_las_referencias_vienen_de_las_fichas_del_documento(self):
        assert FICHAS["baja"]["espera_min"] == 1.4 and FICHAS["normal"]["espera_min"] == 5.2 and FICHAS["alta"]["espera_min"] == 11.9


class TestInforme:
    def test_escribe_html_json_y_fotogramas(self, tmp_path):
        an = correr_escena(tmp_path)
        r = an.escribir(video_rel="deteccion_yolo.mp4")
        html = (tmp_path / "informe.html").read_text(encoding="utf-8")
        assert "Nivel de demanda" in html.replace("NIVEL DE DEMANDA", "Nivel de demanda")
        assert 'class="badge">baja' in html
        assert "Muestra pequeña" in html and "deteccion_yolo.mp4" in html
        assert "<svg" in html and "Mesa 1" in html and "MES-A" in html
        datos = json.loads((tmp_path / "informe.json").read_text(encoding="utf-8"))
        assert datos["demanda"]["nivel"] == "baja" and datos["ocupacion"]["ocupaciones"] == r["ocupacion"]["ocupaciones"]
        fotos = list((tmp_path / "fotogramas").glob("*.jpg"))
        assert 1 <= len(fotos) <= 14 and len(fotos) == len(r["fotogramas"])  # un fotograma por evento clave, acotado

    def test_el_html_escapa_los_nombres(self, tmp_path):
        an = correr_escena(tmp_path)
        an.tap.registro.append((5.0, "reportar_entrega", {"mesa_numero": 1, "personal_identificador": "<script>x</script>",
                                                         "hora": WALL0, "metodo": "VESTIMENTA", "confianza": 0.9}))
        an.escribir()
        html = (tmp_path / "informe.html").read_text(encoding="utf-8")
        assert "<script>x</script>" not in html
