# vision_service/tests/test_table_fusion.py
import pytest

from scaneats_vision.table_fusion import (
    FusionConfig, FusionEventKind as K, TableFusionDetector, TableObservation as Obs,
)

ADY = {4: {5}, 5: {4, 6}, 6: {5}, 1: {2}, 2: {1}}
CFG = FusionConfig()  # confirm 3 s, min 3 observaciones, tolerancia de huecos 2 s, liberar 30 s


def ocupada(clientes, capacidad=4, desde=0.0, puntos=()):
    return Obs(occupied=True, customers=clientes, capacity=capacidad, occupied_since=desde, customer_points=tuple(puntos))


LIBRE = Obs(occupied=False, customers=0, capacity=4)
GRUPO_45 = {4: ocupada(3), 5: ocupada(3), 6: LIBRE}  # 6 personas en dos mesas de 4


def correr(det, desde, hasta, obs, dt=0.5):
    """Alimenta al detector; obs puede ser un dict fijo o una función de t. Devuelve [(t, evento)]."""
    out, t = [], desde
    while t <= hasta + 1e-9:
        for ev in det.update(t, obs(t) if callable(obs) else obs):
            out.append((t, ev))
        t += dt
    return out


class TestConfirmacion:
    def test_confirma_solo_despues_de_sostener_la_condicion(self):
        det = TableFusionDetector(ADY, CFG)
        ev = correr(det, 0, 10, GRUPO_45)
        assert [(e.kind, e.tables) for _, e in ev] == [(K.CONFIRMED, frozenset({4, 5}))]
        assert ev[0][0] == pytest.approx(CFG.confirm_s, abs=0.5)  # ni antes ni mucho después
        assert det.active_fusions == [frozenset({4, 5})]

    def test_un_solo_evento_por_fusion(self):
        det = TableFusionDetector(ADY, CFG)
        assert len(correr(det, 0, 120, GRUPO_45)) == 1  # 2 min de fusión sostenida = 1 evento

    def test_condicion_breve_no_confirma_gente_cruzando(self):
        det = TableFusionDetector(ADY, CFG)
        ev = correr(det, 0, 20, lambda t: GRUPO_45 if 5 <= t < 7 else {4: LIBRE, 5: LIBRE, 6: LIBRE})  # 2 s < 3 s
        assert ev == [] and det.active_fusions == []

    def test_un_hueco_corto_no_reinicia_la_cuenta(self):
        det = TableFusionDetector(ADY, CFG)
        vacio = {4: ocupada(1), 5: ocupada(1), 6: LIBRE}  # 2 personas: la condición se pierde un instante
        ev = correr(det, 0, 8, lambda t: vacio if 1.5 <= t < 2.5 else GRUPO_45)  # hueco de 1 s < 2 s
        assert [e.kind for _, e in ev] == [K.CONFIRMED]

    def test_un_hueco_largo_si_reinicia_la_cuenta(self):
        det = TableFusionDetector(ADY, CFG)
        vacio = {4: ocupada(1), 5: ocupada(1), 6: LIBRE}
        obs = lambda t: vacio if 1.5 <= t < 5.0 else GRUPO_45  # noqa: E731  (hueco de 3.5 s > 2 s)
        # Sin el hueco se habría confirmado en t=3; con el reinicio hay que volver a sostener 3 s desde t=5.
        assert correr(det, 0, 7.5, obs) == []
        ev = correr(det, 8, 9, obs)
        assert [(t, e.kind) for t, e in ev] == [(8.0, K.CONFIRMED)]

    def test_exige_un_minimo_de_observaciones(self):
        det = TableFusionDetector(ADY, FusionConfig(confirm_s=3.0, min_observations=5))
        ev = correr(det, 0, 10, GRUPO_45, dt=2.0)  # solo t=0,2,4,6,8,10 -> 6 obs; la 5ª llega en t=8
        assert ev[0][0] == pytest.approx(8.0)


class TestCondiciones:
    def test_dos_parejas_en_mesas_vecinas_no_son_una_fusion(self):
        det = TableFusionDetector(ADY, CFG)
        assert correr(det, 0, 30, {4: ocupada(2), 5: ocupada(2)}) == []  # 4 personas <= capacidad 4

    def test_mesas_no_adyacentes_no_se_fusionan(self):
        det = TableFusionDetector(ADY, CFG)
        assert correr(det, 0, 30, {4: ocupada(3), 6: ocupada(3), 5: LIBRE}) == []

    def test_ocupaciones_no_simultaneas_no_se_fusionan(self):
        det = TableFusionDetector(ADY, CFG)
        obs = {4: ocupada(3, desde=0), 5: ocupada(3, desde=600)}  # la segunda llegó 10 min después
        assert correr(det, 700, 720, obs) == []

    def test_cohesion_espacial(self):
        juntos = {4: ocupada(3, puntos=[(0.30, 0.32), (0.33, 0.33), (0.36, 0.32)]),
                  5: ocupada(3, puntos=[(0.32, 0.40), (0.35, 0.41), (0.38, 0.40)])}
        lejos = {4: ocupada(3, puntos=[(0.10, 0.10), (0.12, 0.10), (0.14, 0.10)]),
                 5: ocupada(3, puntos=[(0.80, 0.90), (0.82, 0.90), (0.84, 0.90)])}
        assert len(correr(TableFusionDetector(ADY, CFG), 0, 10, juntos)) == 1
        assert correr(TableFusionDetector(ADY, CFG), 0, 10, lejos) == []

    def test_sin_posiciones_no_se_evalua_la_cohesion(self):
        assert len(correr(TableFusionDetector(ADY, CFG), 0, 10, GRUPO_45)) == 1

    def test_la_regla_de_exceso_se_puede_desactivar(self):
        det = TableFusionDetector(ADY, FusionConfig(require_excess_over_capacity=False))
        assert len(correr(det, 0, 10, {4: ocupada(1), 5: ocupada(1)})) == 1


class TestGrupoQueCrece:
    def test_45_y_luego_456_se_confirma_el_grupo_mayor_sin_terminar_el_menor(self):
        det = TableFusionDetector(ADY, CFG)
        crecido = {4: ocupada(3), 5: ocupada(3), 6: ocupada(2)}
        ev = correr(det, 0, 30, lambda t: GRUPO_45 if t < 10 else crecido)
        assert [(e.kind, e.tables) for _, e in ev] == [
            (K.CONFIRMED, frozenset({4, 5})), (K.CONFIRMED, frozenset({4, 5, 6})),
        ]
        assert det.active_fusions == [frozenset({4, 5, 6})]  # el subconjunto se reemplazó, no se "terminó"
        assert all(e.kind != K.ENDED for _, e in ev)


class TestTerminarConHisteresis:
    def test_termina_de_inmediato_si_todas_las_mesas_se_liberan(self):
        det = TableFusionDetector(ADY, CFG)
        correr(det, 0, 10, GRUPO_45)
        ev = det.update(11.0, {4: LIBRE, 5: LIBRE, 6: LIBRE})
        assert [(e.kind, e.tables) for e in ev] == [(K.ENDED, frozenset({4, 5}))]
        assert "liberadas" in ev[0].reason and det.active_fusions == []

    def test_si_la_condicion_se_pierde_pero_siguen_ocupadas_espera_release_s(self):
        det = TableFusionDetector(ADY, CFG)
        correr(det, 0, 10, GRUPO_45)
        separadas = {4: ocupada(2), 5: ocupada(2)}  # ya no exceden la capacidad
        ev = correr(det, 10.5, 10 + CFG.release_s - 1, separadas)
        assert ev == [] and det.active_fusions  # la fusión NO parpadea: sigue vigente
        ev = correr(det, 10 + CFG.release_s - 0.5, 10 + CFG.release_s + 2, separadas)
        assert [(e.kind, e.tables) for _, e in ev] == [(K.ENDED, frozenset({4, 5}))]

    def test_si_la_condicion_regresa_antes_de_liberar_no_se_duplica_el_evento(self):
        det = TableFusionDetector(ADY, CFG)
        ev = correr(det, 0, 60, lambda t: {4: ocupada(2), 5: ocupada(2)} if 10 <= t < 25 else GRUPO_45)
        assert [e.kind for _, e in ev] == [K.CONFIRMED]

    def test_el_evento_de_fin_no_incluye_mesas_que_siguen_en_otra_fusion(self):
        adyacencia = {1: {2}, 2: {1, 3}, 3: {2}}
        det = TableFusionDetector(adyacencia, CFG)
        grande = {1: ocupada(3), 2: ocupada(3), 3: ocupada(3)}
        correr(det, 0, 10, {1: ocupada(3), 2: ocupada(3), 3: LIBRE})  # {1,2}
        correr(det, 10.5, 20, grande)  # {1,2,3} reemplaza a {1,2}
        assert det.active_fusions == [frozenset({1, 2, 3})]
        ev = det.update(21.0, {1: LIBRE, 2: LIBRE, 3: LIBRE})
        assert [e.tables for e in ev] == [frozenset({1, 2, 3})]


class TestConfig:
    def test_validaciones(self):
        with pytest.raises(ValueError):
            FusionConfig(confirm_s=5, release_s=5).validate()
        with pytest.raises(ValueError):
            FusionConfig(min_observations=0).validate()

    def test_la_adyacencia_se_simetriza(self):
        det = TableFusionDetector({1: {2}}, CFG)
        assert det.adjacency[2] == {1}
