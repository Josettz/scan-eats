# vision_service/tests/test_tracking.py
import pytest

from scaneats_vision.geometry import BBox
from scaneats_vision.tracking import SortTracker, build_tracker


def caja(cx, cy, w=0.1, h=0.2):
    return BBox(cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2)


def test_conserva_el_id_de_una_persona_que_se_mueve():
    tr = SortTracker(min_hits=2)
    ids = []
    for i in range(10):
        pistas = tr.update([caja(0.2 + i * 0.03, 0.5)])
        ids += [p.track_id for p in pistas]
    assert set(ids) == {1} and len(ids) == 9  # confirma desde el 2º fotograma


def test_una_pista_nueva_solo_se_confirma_tras_min_hits():
    tr = SortTracker(min_hits=3)
    assert tr.update([caja(0.5, 0.5)]) == []
    assert tr.update([caja(0.5, 0.5)]) == []
    assert len(tr.update([caja(0.5, 0.5)])) == 1


def test_dos_personas_mantienen_ids_distintos():
    tr = SortTracker(min_hits=1)
    for i in range(6):
        pistas = tr.update([caja(0.2 + i * 0.02, 0.3), caja(0.8 - i * 0.02, 0.7)])
    assert sorted(p.track_id for p in pistas) == [1, 2]
    izq = next(p for p in pistas if p.bbox.centroid[0] < 0.5)
    assert izq.track_id == 1


def test_sobrevive_a_una_oclusion_breve_con_el_mismo_id():
    tr = SortTracker(max_age=10, min_hits=1)
    for i in range(5):
        tr.update([caja(0.2 + i * 0.03, 0.5)])
    for _ in range(3):
        tr.update([])  # el detector lo pierde 3 fotogramas
    pistas = tr.update([caja(0.2 + 8 * 0.03, 0.5)])  # reaparece donde el movimiento predecía
    assert [p.track_id for p in pistas] == [1]


def test_pierde_la_pista_pasado_max_age():
    tr = SortTracker(max_age=3, min_hits=1)
    tr.update([caja(0.5, 0.5)])
    for _ in range(5):
        tr.update([])
    assert [p.track_id for p in tr.update([caja(0.5, 0.5)])] == [2]  # una persona nueva


def test_detecciones_sin_solape_son_pistas_distintas():
    tr = SortTracker(min_hits=1)
    assert len(tr.update([caja(0.1, 0.1), caja(0.9, 0.9)])) == 2


def test_las_pistas_perdidas_se_reportan():
    tr = SortTracker(max_age=5, min_hits=1)
    tr.update([caja(0.5, 0.5)])
    tr.update([])
    assert tr.lost_track_ids == [1]


def test_build_tracker():
    assert isinstance(build_tracker("sort", max_age=5), SortTracker)
    with pytest.raises(ValueError):
        build_tracker("otro")


def test_deepsort_es_perezoso():
    """DeepSORT solo se importa al pedirlo; sin la librería el error es claro y no rompe el resto."""
    try:
        import deep_sort_realtime  # noqa: F401
    except ImportError:
        with pytest.raises(ImportError):
            build_tracker("deepsort")
