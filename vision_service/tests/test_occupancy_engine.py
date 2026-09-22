# vision_service/tests/test_occupancy_engine.py
import pytest

from scaneats_vision.occupancy_engine import (
    RNF01_MAX_LATENCY_S, OccupancyConfig, OccupancyEngine, OccupancyManager, TableState as S,
)

CFG = OccupancyConfig()
DT = 0.5


def simular(engine, hasta, escena, t0=0.0, dt=DT):
    """Avanza el motor de t0 a `hasta`. escena(t) -> (clientes, objetos). Devuelve todas las transiciones."""
    out, t = [], t0
    while t <= hasta + 1e-9:
        out.extend(engine.update(t, *escena(t)))
        t += dt
    return out


def estados(transiciones):
    return [tr.current for tr in transiciones]


def sentar(engine, desde=0.0, hasta=10.0):
    """Deja la mesa OCUPADA con clientes presentes de `desde` a `hasta`."""
    simular(engine, hasta, lambda t: (2, 1), t0=desde)
    assert engine.state == S.OCUPADA


class TestOcupar:
    def test_se_confirma_ocupada_tras_el_tiempo_de_confirmacion(self):
        e = OccupancyEngine(1, CFG)
        trans = simular(e, 10, lambda t: (2, 0))
        assert estados(trans) == [S.OCUPADA]
        assert trans[0].previous == S.LIBRE
        assert trans[0].at == pytest.approx(CFG.occupy_confirm_s, abs=DT)  # ni antes ni mucho después
        assert trans[0].since == 0.0  # hora_inicio = cuando se sentaron, no cuando se confirmó

    def test_quien_solo_pasa_no_ocupa_la_mesa(self):
        e = OccupancyEngine(1, CFG)
        trans = simular(e, 30, lambda t: (1, 0) if 5 <= t < 8 else (0, 0))  # 3 s < 4 s
        assert trans == [] and e.state == S.LIBRE

    def test_un_parpadeo_del_detector_no_reinicia_la_presencia(self):
        e = OccupancyEngine(1, CFG)
        # hueco de 2 s (< tolerancia de 2.5 s) en medio de la presencia
        trans = simular(e, 10, lambda t: (0, 0) if 2.0 <= t < 3.5 else (2, 0))
        assert estados(trans) == [S.OCUPADA]

    def test_un_hueco_mayor_a_la_tolerancia_si_reinicia_la_cuenta(self):
        e = OccupancyEngine(1, CFG)
        trans = simular(e, 8, lambda t: (0, 0) if 2.0 <= t < 5.0 else (2, 0))  # hueco de 3.5 s > 2.5 s
        assert trans == []  # tras el hueco solo hay 3 s de presencia continua (< 4 s)

    def test_se_puede_volver_a_ocupar_despues_de_liberarse(self):
        e = OccupancyEngine(1, CFG)
        sentar(e)
        simular(e, 60, lambda t: (0, 0), t0=10.5)
        assert e.state == S.LIBRE
        trans = simular(e, 80, lambda t: (3, 0), t0=61)
        assert estados(trans) == [S.OCUPADA] and e.occupied_since == pytest.approx(61.0)


class TestLiberar:
    def test_sin_clientes_pasa_a_posiblemente_libre(self):
        e = OccupancyEngine(1, CFG)
        sentar(e)
        trans = simular(e, 13, lambda t: (0, 3), t0=10.5)
        assert estados(trans) == [S.POSIBLEMENTE_LIBRE]
        assert trans[0].since == pytest.approx(10.0)  # última vez que hubo un cliente

    def test_si_el_cliente_regresa_vuelve_a_ocupada_sin_pasar_por_libre(self):
        e = OccupancyEngine(1, CFG)
        sentar(e)
        trans = simular(e, 40, lambda t: (0, 3) if t < 25 else (2, 3), t0=10.5)
        assert estados(trans) == [S.POSIBLEMENTE_LIBRE, S.OCUPADA]
        assert S.LIBRE not in estados(trans)
        assert e.occupied_since == 0.0  # sigue siendo la MISMA ocupación

    @pytest.mark.parametrize("objetos, timeout, motivo", [
        (0, CFG.empty_table_timeout_s, "vacía"),
        (1, CFG.single_object_timeout_s, "un objeto"),
        (3, CFG.absent_with_objects_timeout_s, "objetos"),
    ])
    def test_timeout_segun_lo_que_queda_sobre_la_mesa(self, objetos, timeout, motivo):
        e = OccupancyEngine(1, CFG)
        sentar(e, hasta=10.0)  # último cliente visto en t=10
        trans = simular(e, 100, lambda t: (0, objetos), t0=10.5)
        libre = trans[-1]
        assert libre.current == S.LIBRE and motivo in libre.reason
        assert libre.at - 10.0 == pytest.approx(timeout, abs=DT)
        assert libre.since == pytest.approx(10.0)  # hora_fin = última vez que hubo un cliente

    def test_el_timeout_de_objeto_solo_es_mucho_menor_que_el_de_cliente_ausente(self):
        """Decisión de diseño pendiente de calibrar: un objeto sin persona se libera MUCHO antes."""
        assert CFG.single_object_timeout_s < CFG.absent_with_objects_timeout_s / 2
        assert CFG.release_timeout_s(1) == CFG.single_object_timeout_s
        assert CFG.release_timeout_s(2) == CFG.absent_with_objects_timeout_s
        assert CFG.release_timeout_s(0) == CFG.empty_table_timeout_s

        def latencia_hasta_libre(objetos):
            e = OccupancyEngine(1, CFG)
            sentar(e)
            return [tr for tr in simular(e, 100, lambda t: (0, objetos), t0=10.5) if tr.current == S.LIBRE][0].at

        assert latencia_hasta_libre(1) + 15 < latencia_hasta_libre(4)

    def test_el_timeout_usa_los_objetos_actuales(self):
        """Si durante la ausencia se llevan los platos y solo queda uno, aplica el timeout corto."""
        e = OccupancyEngine(1, CFG)
        sentar(e, hasta=10.0)
        trans = simular(e, 100, lambda t: (0, 4) if t < 15 else (0, 1), t0=10.5)
        assert trans[-1].current == S.LIBRE
        assert trans[-1].at - 10.0 == pytest.approx(CFG.single_object_timeout_s, abs=DT)

    def test_objeto_sobre_la_mesa_sin_persona_nunca_ocupa_una_mesa_libre(self):
        e = OccupancyEngine(1, CFG)
        assert simular(e, 120, lambda t: (0, 3)) == [] and e.state == S.LIBRE

    def test_fotogramas_saltados_pueden_dar_dos_transiciones_seguidas(self):
        e = OccupancyEngine(1, CFG)
        sentar(e)
        trans = e.update(200.0, 0, 0)  # no hubo observaciones durante mucho tiempo
        assert estados(trans) == [S.POSIBLEMENTE_LIBRE, S.LIBRE]

    def test_al_liberar_se_reinicia_el_contexto(self):
        e = OccupancyEngine(1, CFG)
        sentar(e)
        simular(e, 60, lambda t: (0, 0), t0=10.5)
        assert e.state == S.LIBRE and e.occupied_since is None


class TestRNF01:
    """El estado debe actualizarse ≤ 45 s desde el cambio real (incluye el período de gracia)."""

    def test_el_peor_caso_de_la_configuracion_por_defecto_cabe_en_45_s(self):
        assert CFG.worst_case_latency_s() <= RNF01_MAX_LATENCY_S

    @pytest.mark.parametrize("objetos", [0, 1, 5])
    def test_latencia_medida_desde_que_se_va_el_cliente(self, objetos):
        gap = CFG.max_frame_gap_s
        e = OccupancyEngine(1, CFG)
        simular(e, 10.0, lambda t: (2, objetos), dt=gap)
        cambio_real = 10.0  # el cliente se va
        trans = simular(e, 100, lambda t: (0, objetos), t0=cambio_real + gap, dt=gap)
        libre = [tr for tr in trans if tr.current == S.LIBRE][0]
        assert libre.at - cambio_real + CFG.api_latency_budget_s <= RNF01_MAX_LATENCY_S

    def test_una_configuracion_que_viola_rnf01_se_rechaza(self):
        with pytest.raises(ValueError, match="RNF-01"):
            OccupancyConfig(absent_with_objects_timeout_s=60.0).validate()
        with pytest.raises(ValueError, match="RNF-01"):  # 40 + 3 (hueco entre fotogramas) + 3 (API) = 46 s
            OccupancyEngine(1, OccupancyConfig(max_frame_gap_s=3.0, presence_gap_tolerance_s=5.0,
                                               absent_with_objects_timeout_s=40.0))
        with pytest.raises(ValueError, match="fotograma"):  # tolerancia menor que el hueco entre fotogramas
            OccupancyConfig(max_frame_gap_s=3.0, presence_gap_tolerance_s=2.0).validate()

    def test_validaciones_de_orden(self):
        with pytest.raises(ValueError, match="MENOR"):
            OccupancyConfig(single_object_timeout_s=30.0, absent_with_objects_timeout_s=20.0).validate()
        with pytest.raises(ValueError):
            OccupancyConfig(empty_table_timeout_s=1.0, presence_gap_tolerance_s=2.0).validate()
        with pytest.raises(ValueError):
            OccupancyConfig(occupy_confirm_s=0).validate()

    def test_rnf06_veinte_mesas_simultaneas_respetan_los_45_s(self):
        """20 mesas con clientes que se van en momentos distintos: todas se liberan dentro del límite."""
        mgr = OccupancyManager(range(1, 21), CFG)
        sale = {n: 20.0 + n * 1.3 for n in range(1, 21)}  # cada mesa se vacía en un instante distinto
        objetos = {n: n % 4 for n in range(1, 21)}  # 0, 1, 2 y 3 objetos
        libre_en = {}
        t = 0.0
        while t <= 150 and len(libre_en) < 20:
            obs = {n: ((2 if t < sale[n] else 0), objetos[n]) for n in range(1, 21)}
            for tr in mgr.update(t, obs):
                if tr.current == S.LIBRE:
                    libre_en[tr.table_id] = tr.at
            t += 1.0
        assert len(libre_en) == 20
        peor = max(libre_en[n] - sale[n] for n in range(1, 21))
        assert peor + CFG.api_latency_budget_s <= RNF01_MAX_LATENCY_S


class TestManager:
    def test_una_maquina_por_mesa(self):
        mgr = OccupancyManager([1, 2, 3], CFG)
        trans = []
        for i in range(0, 12):
            trans += mgr.update(float(i), {1: (2, 0), 3: (1, 1)})  # la mesa 2 no tiene observación
        assert {tr.table_id for tr in trans} == {1, 3}
        assert (mgr.state(1), mgr.state(2), mgr.state(3)) == (S.OCUPADA, S.LIBRE, S.OCUPADA)
