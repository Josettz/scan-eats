# vision_service/tests/test_pipeline.py
"""
Pruebas de integración del orquestador con una API falsa: detecciones sintéticas entran, eventos HTTP salen.
Ni cámara, ni GPU, ni modelos: solo la lógica del proyecto de punta a punta.
"""
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from scaneats_vision import simulate
from scaneats_vision.config_loader import VisionConfig, load_config
from scaneats_vision.dispatcher import EventDispatcher
from scaneats_vision.geometry import BBox
from scaneats_vision.occupancy_engine import OccupancyConfig
from scaneats_vision.pipeline import ScanEatsPipeline, TrackedPerson

EJEMPLO = Path(__file__).resolve().parent.parent / "config" / "zonas_ejemplo.json"
WALL0 = datetime(2026, 3, 1, 10, 0, 0, tzinfo=timezone.utc)


class ApiFalsa:
    """Registra cada llamada al backend como (método, kwargs)."""

    def __init__(self):
        self.llamadas = []

    def __getattr__(self, nombre):
        def f(**kw):
            self.llamadas.append((nombre, kw))
        return f

    def de(self, metodo):
        return [kw for m, kw in self.llamadas if m == metodo]


def color_por_nombre(crop):
    return {crop: 0.5}


def armar(config=None, **kw):
    api = ApiFalsa()
    pipe = ScanEatsPipeline(config or load_config(EJEMPLO), EventDispatcher(api, sync=True),
                            color_ratio_fn=color_por_nombre, **kw)
    return pipe, api


def caja(cx, cy, w=0.04, h=0.06):
    return BBox(cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2)


@pytest.fixture(scope="module")
def resultado():
    pipe, api = armar()
    simulate.run(pipe, WALL0)
    return pipe, api


class TestEscenaCompleta:
    """La escena de simulate.py recorre ocupación, fusión, entregas por vestimenta, transeúnte y liberación."""

    def test_ocupacion_de_la_mesa_1(self, resultado):
        _, api = resultado
        ocup = [c for c in api.de("reportar_ocupacion") if c["mesa_numero"] == 1]
        assert len(ocup) == 1
        c = ocup[0]
        assert c["hora_inicio"] == WALL0  # se sentaron en t=0
        assert (c["detectado_en"] - c["hora_inicio"]).total_seconds() == pytest.approx(4.0, abs=0.5)  # confirmación
        assert c["uid"]

    def test_el_transeunte_no_ocupa_la_mesa_2(self, resultado):
        _, api = resultado
        assert all(c["mesa_numero"] != 2 for c in api.de("reportar_ocupacion"))

    def test_una_unica_fusion_de_las_mesas_4_y_5(self, resultado):
        _, api = resultado
        fusiones = api.de("reportar_fusion")
        assert len(fusiones) == 1 and fusiones[0]["mesas"] == [4, 5]
        fin = api.de("finalizar_fusion")
        assert len(fin) == 1 and fin[0]["mesas"] == [4, 5]

    def test_entregas_atribuidas_por_vestimenta(self, resultado):
        pipe, api = resultado
        entregas = {c["mesa_numero"]: c for c in api.de("reportar_entrega")}
        assert entregas[1]["personal_identificador"] == "MES-A" and entregas[1]["metodo"] == "VESTIMENTA"
        assert entregas[4]["personal_identificador"] == "MES-B" and entregas[4]["metodo"] == "VESTIMENTA"
        assert entregas[1]["confianza"] == pytest.approx(1.0)
        # la entrega ocurrió entre la ocupación y su liberación
        assert WALL0 + timedelta(seconds=14) <= entregas[1]["hora"] <= WALL0 + timedelta(seconds=19)
        assert pipe.stats.entregas == 2 and pipe.stats.entregas_sin_atribuir == 0

    def test_todas_las_entregas_llevan_mesero(self, resultado):
        _, api = resultado
        assert all(c["personal_identificador"] for c in api.de("reportar_entrega"))  # RF-02: 100 %

    def test_la_mesa_1_se_libera_por_el_timeout_corto_de_un_solo_objeto(self, resultado):
        _, api = resultado
        libres = [c for c in api.de("cambiar_estado_ocupacion") if c["mesa_numero"] == 1 and c["estado"] == "LIBRE"]
        assert len(libres) == 1
        assert libres[0]["hora_fin"] == WALL0 + timedelta(seconds=59.75)  # último cliente visto (t=59.75)

    def test_secuencia_de_estados_de_la_mesa_1(self, resultado):
        _, api = resultado
        estados = [c["estado"] for c in api.de("cambiar_estado_ocupacion") if c["mesa_numero"] == 1]
        assert estados == ["POSIBLEMENTE_LIBRE", "LIBRE"]

    def test_las_mesas_del_grupo_grande_terminan_libres(self, resultado):
        pipe, _ = resultado
        assert all(pipe.occupancy.state(n).value == "LIBRE" for n in range(1, 13))

    def test_se_reporta_la_clasificacion_del_personal(self, resultado):
        _, api = resultado
        cls = api.de("reportar_personal_clasificado")
        assert {c["personal_identificador"] for c in cls} == {"MES-A", "MES-B"}
        assert all(c["metodo"] == "VESTIMENTA" for c in cls)
        assert len(cls) == 2  # una vez por persona, no una por fotograma

    def test_nunca_se_reporta_nada_de_la_caja(self, resultado):
        _, api = resultado
        assert all(kw.get("mesa_numero") != 99 for _, kw in api.llamadas)


class TestZonaDeExclusion:
    def test_personas_en_la_caja_no_generan_eventos_ni_se_rastrean(self):
        """RF-15: 100 % de las pruebas sin eventos sobre la caja, aunque haya gente todo el día y con uniforme."""
        pipe, api = armar()
        caja_zona = pipe.config.exclusion_zones[0].bbox
        cx, cy = (caja_zona.x1 + caja_zona.x2) / 2, (caja_zona.y1 + caja_zona.y2) / 2
        t = 0.0
        while t < 120:
            gente = [TrackedPerson(1, caja(cx, cy)), TrackedPerson(2, caja(cx + 0.03, cy)),
                     TrackedPerson(3, caja(cx, cy + 0.06))]
            pipe.process(t, WALL0 + timedelta(seconds=t), gente, [caja(cx, cy + 0.1, 0.02, 0.02)],
                         {n.numero: 0.5 for n in pipe.config.tables}, {3: "ROJO"})
            t += 0.5
        assert api.llamadas == []
        assert pipe.stats.clasificaciones == 0
        assert not pipe.classifier.behavior._tracks  # ni siquiera se siguió su comportamiento (privacidad)

    def test_quien_esta_en_la_caja_no_cuenta_como_cliente_de_la_mesa_vecina(self):
        pipe, api = armar()
        caja_z = pipe.config.exclusion_zones[0].bbox
        # persona pegada al borde de la caja
        persona = caja(caja_z.x2 - 0.01, (caja_z.y1 + caja_z.y2) / 2, 0.05, 0.1)
        for i in range(60):
            pipe.process(i * 0.5, WALL0 + timedelta(seconds=i * 0.5), [TrackedPerson(1, persona)])
        assert api.de("reportar_ocupacion") == []


class TestAtribucionPendiente:
    """
    Un mesero solo cuenta como "presente" en una entrega si ya se sabe que es personal (por prenda o por su
    patrullaje). Aquí patrulla las mesas 2-5 y llega a la mesa 1 con la prenda tapada: se sabe que es personal
    (COMPORTAMIENTO) pero no QUIÉN es, hasta que la prenda se ve.
    """

    def _escena(self, colores_mesero, hasta=45.0, timeout=10.0):
        pipe, api = armar(attribution_timeout_s=timeout)
        z1 = pipe.config.table(1).zone.bbox
        cx, cy = (z1.x1 + z1.x2) / 2, (z1.y1 + z1.y2) / 2
        patrulla = [pipe.config.table(n).zone.bbox for n in (2, 3, 4, 5)]
        t = 0.0
        while t < hasta:
            gente = [TrackedPerson(1, caja(cx - 0.02, cy)), TrackedPerson(2, caja(cx + 0.02, cy))]
            if t < 12:  # patrulla: 3 s en cada una de las mesas 2, 3, 4 y 5
                zb = patrulla[int(t // 3)]
                gente.append(TrackedPerson(100, caja((zb.x1 + zb.x2) / 2, (zb.y1 + zb.y2) / 2)))
            elif t < 30:
                gente.append(TrackedPerson(100, caja(cx, cy - 0.1)))  # ya en la mesa 1
            cambio = {1: 0.12 if 15 <= t < 18 else 0.0}
            pipe.process(t, WALL0 + timedelta(seconds=t), gente, [], cambio, {100: colores_mesero(t)})
            t += 0.5
        return pipe, api

    def test_si_la_prenda_se_reconoce_tarde_la_entrega_se_reporta_al_identificar(self):
        pipe, api = self._escena(lambda t: "GRIS" if t < 20 else "ROJO")  # de espaldas hasta t=20
        entregas = api.de("reportar_entrega")
        assert len(entregas) == 1
        e = entregas[0]
        assert (e["personal_identificador"], e["metodo"]) == ("MES-A", "VESTIMENTA")
        assert e["hora"] == WALL0 + timedelta(seconds=15)  # la hora es la de la entrega, no la de la atribución
        assert pipe.stats.entregas == 1 and pipe.stats.entregas_sin_atribuir == 0

    def test_si_la_prenda_deja_de_verse_se_usa_la_identidad_recordada_y_el_metodo_es_comportamiento(self):
        # La prenda se ve al llegar (t<14) y luego el mesero se voltea: la identidad viene de la memoria del track.
        pipe, api = self._escena(lambda t: "ROJO" if t < 13 else "GRIS")
        e = api.de("reportar_entrega")[0]
        assert e["personal_identificador"] == "MES-A" and e["metodo"] == "COMPORTAMIENTO"

    def test_si_nunca_se_identifica_al_mesero_no_se_inventa_uno(self):
        pipe, api = self._escena(lambda t: "GRIS")
        assert api.de("reportar_entrega") == []
        assert pipe.stats.entregas == 0 and pipe.stats.entregas_sin_atribuir == 1  # el hueco queda medido
        comp = [c for c in api.de("reportar_personal_clasificado") if c["metodo"] == "COMPORTAMIENTO"]
        assert comp and all(c["personal_identificador"] is None for c in comp)

    def test_un_cliente_no_dispara_una_entrega(self):
        """Cambio en la mesa (p. ej. deja su celular) sin ningún personal cerca: no hay entrega."""
        pipe, api = armar()
        z1 = pipe.config.table(1).zone.bbox
        cx, cy = (z1.x1 + z1.x2) / 2, (z1.y1 + z1.y2) / 2
        for i in range(120):
            t = i * 0.5
            pipe.process(t, WALL0 + timedelta(seconds=t), [TrackedPerson(1, caja(cx, cy))], [],
                         {1: 0.12 if 20 <= t < 25 else 0.0})
        assert api.de("reportar_entrega") == [] and pipe.stats.entregas_sin_atribuir == 0


class TestUsoDeRecursos:
    def test_el_pipeline_no_bloquea_ni_reporta_de_mas_con_una_mesa_estable(self):
        pipe, api = armar()
        z = pipe.config.table(1).zone.bbox
        cx, cy = (z.x1 + z.x2) / 2, (z.y1 + z.y2) / 2
        for i in range(600):  # 5 min a 2 FPS con clientes sentados: un único evento de ocupación
            pipe.process(i * 0.5, WALL0 + timedelta(seconds=i * 0.5), [TrackedPerson(1, caja(cx, cy))])
        assert len(api.de("reportar_ocupacion")) == 1
        assert api.de("cambiar_estado_ocupacion") == []

    def test_rnf06_veinte_mesas_y_sesenta_personas_por_fotograma(self):
        """La lógica del pipeline escala: 20 mesas con 3 personas cada una, 300 fotogramas."""
        def poligono(n):  # cuadrícula de 5 columnas x 4 filas, todo dentro de 0..1
            x0, y0 = 0.02 + ((n - 1) % 5) * 0.19, 0.05 + ((n - 1) // 5) * 0.23
            return [[x0, y0], [x0 + 0.13, y0], [x0 + 0.13, y0 + 0.2], [x0, y0 + 0.2]]

        tablas = [{"numero": n, "capacidad": 4, "polygon": poligono(n)} for n in range(1, 21)]
        cfg = VisionConfig.from_dict({"tables": tablas, "staff": [{"identificador": "A", "prenda_color": "ROJO"}]})
        api = ApiFalsa()
        pipe = ScanEatsPipeline(cfg, EventDispatcher(api, sync=True), color_ratio_fn=lambda c: {},
                                occupancy_config=OccupancyConfig())
        personas = []
        for t in cfg.tables:
            b = t.zone.bbox
            cx, cy = (b.x1 + b.x2) / 2, (b.y1 + b.y2) / 2
            personas += [TrackedPerson(t.numero * 10 + k, caja(cx + (k - 1) * 0.03, cy)) for k in range(3)]
        assert len(personas) == 60
        t0 = time.perf_counter()
        for i in range(300):
            pipe.process(i * 0.2, WALL0 + timedelta(seconds=i * 0.2), personas, [], {n: 0.0 for n in range(1, 21)})
        por_fotograma_ms = (time.perf_counter() - t0) / 300 * 1000
        assert len(api.de("reportar_ocupacion")) == 20
        assert por_fotograma_ms < 100, f"{por_fotograma_ms:.1f} ms por fotograma (presupuesto a 5 FPS: 200 ms)"
