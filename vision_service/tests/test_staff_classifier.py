# vision_service/tests/test_staff_classifier.py
"""Cascada vestimenta -> comportamiento (RF-08), sin cargar ningún modelo: el extractor de color es un mock."""
from unittest import mock

import pytest

from scaneats_vision.staff_classifier import (
    BehaviorAnalyzer, BehaviorConfig, CascadeConfig, ClothingClassifier, Method, Role, StaffClassifier, StaffProfile,
)

PERFILES = [
    StaffProfile("MES-A", "Mesero A", "ROJO"),
    StaffProfile("MES-B", "Mesero B", "AZUL"),
    StaffProfile("MES-C", "Mesero C", "VERDE"),
    StaffProfile("MES-Z", "Sin prenda registrada", None),
]

# "Recortes" simulados: el mock del extractor los traduce a fracciones de color del torso.
RECORTES = {
    "delantal_rojo": {"ROJO": 0.45, "NEGRO": 0.2},
    "delantal_azul": {"AZUL": 0.40},
    "camisa_roja_apenas": {"ROJO": 0.06, "GRIS": 0.5},  # poca prenda visible (de espaldas, tapado)
    "rojo_y_azul": {"ROJO": 0.30, "AZUL": 0.29},  # dos colores casi iguales: no concluyente
    "gris": {"GRIS": 0.8},
    "inservible": None,
}


def extractor(crop):
    return RECORTES[crop]


def nuevo(behavior_config=None, cascade=None, perfiles=PERFILES):
    color_fn = mock.Mock(side_effect=extractor)
    ropa = ClothingClassifier(perfiles, color_fn)
    comportamiento = BehaviorAnalyzer(behavior_config)
    return StaffClassifier(ropa, comportamiento, cascade), color_fn, comportamiento


def patrullar(comportamiento, track, mesas=(1, 2, 3, 4), desde=0.0, dur=10.0, pausa=2.0):
    """El track visita cada mesa `dur` s (visita breve) con `pausa` entre una y otra. Devuelve el instante final."""
    t = desde
    for m in mesas:
        comportamiento.observe(track, t, m)
        t += dur
        comportamiento.observe(track, t, None)
        t += pausa
    return t


class TestVestimentaPrincipal:
    def test_reconoce_al_mesero_por_su_prenda(self):
        clf, color_fn, _ = nuevo()
        r = clf.classify(7, 0.0, "delantal_rojo")
        assert (r.role, r.identificador, r.method) == (Role.STAFF, "MES-A", Method.VESTIMENTA)
        assert r.confidence == pytest.approx(1.0)  # 0.45 de torso >= cobertura completa (0.30)
        color_fn.assert_called_once_with("delantal_rojo")

    def test_distingue_entre_meseros(self):
        clf, _, _ = nuevo()
        assert clf.classify(1, 0, "delantal_rojo").identificador == "MES-A"
        assert clf.classify(2, 0, "delantal_azul").identificador == "MES-B"

    def test_un_perfil_sin_prenda_no_participa_de_la_vestimenta(self):
        assert [p.identificador for p in ClothingClassifier(PERFILES, extractor).profiles] == ["MES-A", "MES-B", "MES-C"]

    def test_uniforme_compartido_se_reconoce_como_personal_pero_sin_identidad(self):
        perfiles = [StaffProfile("A", color="ROJO"), StaffProfile("B", color="ROJO")]
        clf, _, _ = nuevo(perfiles=perfiles)
        r = clf.classify(1, 0, "delantal_rojo")
        assert (r.role, r.identificador, r.method) == (Role.STAFF, None, Method.VESTIMENTA)
        assert r.clothing.ambiguous

    def test_recorte_inservible_o_ausente(self):
        clf, color_fn, _ = nuevo()
        assert clf.classify(1, 0, "inservible").role == Role.UNKNOWN
        assert clf.classify(1, 0, None).role == Role.UNKNOWN
        assert color_fn.call_count == 1  # sin recorte ni siquiera se invoca al extractor


class TestCascadaConRespaldoDeComportamiento:
    def test_confianza_baja_de_vestimenta_cae_al_comportamiento(self):
        clf, _, comp = nuevo()
        fin = patrullar(comp, 7)  # visitó 4 mesas brevemente: patrón de mesero
        r = clf.classify(7, fin, "camisa_roja_apenas")  # la prenda casi no se ve
        assert r.role == Role.STAFF and r.method == Method.COMPORTAMIENTO
        assert r.identificador is None  # el comportamiento distingue personal/cliente, no QUIÉN es
        assert r.clothing.confidence < 0.6  # se intentó vestimenta primero y no alcanzó

    def test_prenda_no_reconocible_cae_al_comportamiento(self):
        clf, _, comp = nuevo()
        fin = patrullar(comp, 7)
        assert clf.classify(7, fin, "gris").method == Method.COMPORTAMIENTO
        assert clf.classify(7, fin, None).method == Method.COMPORTAMIENTO

    def test_dos_colores_casi_iguales_no_son_concluyentes(self):
        clf, _, _ = nuevo()
        r = clf.classify(1, 0, "rojo_y_azul")
        assert r.role == Role.UNKNOWN and r.identificador is None

    def test_sin_prenda_ni_patron_es_desconocido(self):
        clf, _, comp = nuevo()
        comp.observe(9, 0.0, 1)  # recién apareció
        assert clf.classify(9, 1.0, "gris").role == Role.UNKNOWN

    def test_la_identidad_por_vestimenta_se_recuerda_cuando_la_prenda_deja_de_verse(self):
        clf, _, comp = nuevo()
        r1 = clf.classify(7, 0.0, "delantal_rojo")
        assert r1.identificador == "MES-A"
        fin = patrullar(comp, 7)
        r2 = clf.classify(7, fin, "gris")  # de espaldas: la prenda no se reconoce
        assert (r2.role, r2.identificador, r2.method) == (Role.STAFF, "MES-A", Method.COMPORTAMIENTO)
        assert clf.remembered(7) == "MES-A"

    def test_olvidar_un_track_borra_su_identidad(self):
        clf, _, _ = nuevo()
        clf.classify(7, 0.0, "delantal_rojo")
        clf.forget(7)
        assert clf.remembered(7) is None

    def test_vestimenta_tiene_prioridad_sobre_comportamiento(self):
        clf, _, comp = nuevo()
        fin = patrullar(comp, 7)
        assert clf.classify(7, fin, "delantal_azul").method == Method.VESTIMENTA

    def test_umbral_de_aceptacion_configurable(self):
        RECORTES["azul_medio"] = {"AZUL": 0.2}  # cobertura 0.2/0.3 -> confianza 0.67
        try:
            estricto, _, _ = nuevo(cascade=CascadeConfig(clothing_accept=0.99))
            normal, _, _ = nuevo()
            assert normal.classify(2, 0, "azul_medio").method == Method.VESTIMENTA  # 0.67 ≥ 0.60
            r = estricto.classify(2, 0, "azul_medio")  # 0.67 < 0.99: no se acepta y no hay comportamiento
            assert (r.role, r.method) == (Role.UNKNOWN, Method.NINGUNO)
            assert estricto.classify(1, 0, "delantal_azul").method == Method.VESTIMENTA  # confianza 1.0 sí pasa
        finally:
            del RECORTES["azul_medio"]


class TestClienteConPrendaParecida:
    def test_cliente_sentado_con_camisa_roja_no_es_personal(self):
        clf, _, comp = nuevo()
        RECORTES["camisa_roja_media"] = {"ROJO": 0.25}  # confianza 0.83: parecida, no inequívoca
        try:
            comp.observe(50, 0.0, 3)
            comp.observe(50, 100.0, 3)  # 100 s sentado en la misma mesa
            r = clf.classify(50, 100.0, "camisa_roja_media")
            assert (r.role, r.method) == (Role.CUSTOMER, Method.COMPORTAMIENTO)
        finally:
            del RECORTES["camisa_roja_media"]

    def test_prenda_inequivoca_prevalece_sobre_el_veto(self):
        clf, _, comp = nuevo()
        comp.observe(50, 0.0, 3)
        comp.observe(50, 100.0, 3)
        assert clf.classify(50, 100.0, "delantal_rojo").role == Role.STAFF  # mesero descansando junto a una mesa


class TestComportamiento:
    def test_patron_de_mesero_visitas_breves_a_varias_mesas(self):
        comp = BehaviorAnalyzer()
        fin = patrullar(comp, 1, mesas=(1, 2, 3))
        r = comp.score(1, fin)
        assert r.is_staff and not r.is_customer
        assert (r.distinct_tables, r.visits) == (3, 3)
        assert r.staff_score >= 0.5

    def test_pocas_mesas_no_alcanzan(self):
        comp = BehaviorAnalyzer()
        fin = patrullar(comp, 1, mesas=(1, 2))
        assert not comp.score(1, fin).is_staff

    def test_visitar_siempre_la_misma_mesa_no_es_patron_de_mesero(self):
        comp = BehaviorAnalyzer()
        fin = patrullar(comp, 1, mesas=(1, 1, 1, 1))
        r = comp.score(1, fin)
        assert not r.is_staff and r.distinct_tables == 1

    def test_quedarse_sentado_es_cliente(self):
        comp = BehaviorAnalyzer()
        comp.observe(1, 0.0, 5)
        comp.observe(1, 95.0, 5)
        r = comp.score(1, 95.0)
        assert r.is_customer and not r.is_staff

    def test_las_visitas_largas_no_cuentan(self):
        comp = BehaviorAnalyzer(BehaviorConfig(max_visit_s=60.0, seated_dwell_s=500.0))
        fin = patrullar(comp, 1, mesas=(1, 2, 3, 4), dur=80.0)  # cada visita dura 80 s > 60 s
        assert not comp.score(1, fin).is_staff

    def test_las_visitas_antiguas_salen_de_la_ventana(self):
        comp = BehaviorAnalyzer(BehaviorConfig(window_s=100.0))
        fin = patrullar(comp, 1, mesas=(1, 2, 3))
        assert comp.score(1, fin).is_staff
        assert not comp.score(1, fin + 500.0).is_staff

    def test_track_desconocido(self):
        r = BehaviorAnalyzer().score(999, 0.0)
        assert not r.is_staff and not r.is_customer and r.visits == 0

    def test_olvidar(self):
        comp = BehaviorAnalyzer()
        patrullar(comp, 1)
        comp.forget(1)
        assert comp.score(1, 100.0).visits == 0


class TestColorUtils:
    np = pytest.importorskip("numpy")

    def hsv(self, h, s, v, n=100):
        return self.np.tile(self.np.array([[[h, s, v]]], dtype=self.np.uint8), (n, 1, 1))

    def test_colores_puros(self):
        from scaneats_vision.color_utils import color_ratios_from_hsv as ratios

        assert ratios(self.hsv(0, 200, 200))["ROJO"] == 1.0
        assert ratios(self.hsv(175, 200, 200))["ROJO"] == 1.0  # el rojo cruza el 0/179
        assert ratios(self.hsv(110, 200, 200))["AZUL"] == 1.0
        assert ratios(self.hsv(60, 200, 200))["VERDE"] == 1.0
        assert ratios(self.hsv(28, 200, 200))["AMARILLO"] == 1.0
        assert ratios(self.hsv(0, 0, 20))["NEGRO"] == 1.0
        assert ratios(self.hsv(0, 5, 240))["BLANCO"] == 1.0
        assert ratios(self.hsv(0, 5, 120))["GRIS"] == 1.0

    def test_mezcla_y_normalizacion(self):
        from scaneats_vision.color_utils import color_ratios_from_hsv as ratios

        mezcla = self.np.concatenate([self.hsv(0, 200, 200, 30), self.hsv(110, 200, 200, 70)])
        r = ratios(mezcla)
        assert r["ROJO"] == pytest.approx(0.3) and r["AZUL"] == pytest.approx(0.7)
        assert sum(r.values()) <= 1.0 + 1e-9

    def test_pixeles_ambiguos_no_suman_a_ningun_color(self):
        from scaneats_vision.color_utils import color_ratios_from_hsv as ratios

        assert sum(ratios(self.hsv(0, 55, 150)).values()) == 0.0  # poco saturado pero no gris

    def test_entrada_vacia(self):
        from scaneats_vision.color_utils import color_ratios_from_hsv as ratios

        assert sum(ratios(self.np.zeros((0, 0, 3), dtype=self.np.uint8)).values()) == 0.0

    def test_torso_descarta_recortes_diminutos(self):
        from scaneats_vision.color_utils import torso_color_ratios

        assert torso_color_ratios(None) is None
        assert torso_color_ratios(self.np.zeros((10, 5, 3), dtype=self.np.uint8)) is None

    def test_region_del_torso(self):
        from scaneats_vision.color_utils import torso_region

        recorte = self.np.zeros((100, 40, 3), dtype=self.np.uint8)
        assert torso_region(recorte).shape[:2] == (40, 28)  # filas 20..60, columnas 6..34
