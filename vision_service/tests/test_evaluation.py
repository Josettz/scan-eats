# vision_service/tests/test_evaluation.py
from scaneats_vision.evaluation import latency_report, main, occupancy_agreement, staff_attribution_accuracy


def test_rf07_meta_de_90_por_ciento_sobre_100_muestras():
    ok = [("OCUPADA", "OCUPADA")] * 90 + [("LIBRE", "OCUPADA")] * 10
    r = occupancy_agreement(ok)
    assert (r.total, r.matches, r.ratio) == (100, 90, 0.9) and r.meets_target
    assert not occupancy_agreement([("OCUPADA", "OCUPADA")] * 89 + [("LIBRE", "OCUPADA")] * 11).meets_target


def test_rf08_meta_de_85_por_ciento_sobre_20_entregas():
    assert staff_attribution_accuracy([("A", "A")] * 17 + [("B", "A")] * 3).meets_target  # 17/20 = 85 %
    assert not staff_attribution_accuracy([("A", "A")] * 16 + [("B", "A")] * 4).meets_target


def test_normaliza_mayusculas_y_espacios():
    assert occupancy_agreement([(" ocupada ", "OCUPADA")]).matches == 1


def test_sin_muestras_no_cumple():
    assert not occupancy_agreement([]).meets_target


def test_rnf01_latencia_maxima_45_s():
    assert latency_report([12.0, 30.5, 44.9])["cumple"]
    r = latency_report([10.0, 46.0])
    assert not r["cumple"] and r["maxima_s"] == 46.0
    assert latency_report([])["muestras"] == 0


def test_cli_devuelve_codigo_segun_la_meta(tmp_path, capsys):
    csv_ok = tmp_path / "ok.csv"
    csv_ok.write_text("# predicho,observado\n" + "OCUPADA,OCUPADA\n" * 10)
    csv_mal = tmp_path / "mal.csv"
    csv_mal.write_text("OCUPADA,LIBRE\n" * 10)
    assert main(["ocupacion", str(csv_ok)]) == 0
    assert "CUMPLE" in capsys.readouterr().out
    assert main(["atribucion", str(csv_mal)]) == 1
    assert main([]) == 2
