# vision_service/tests/test_config_loader.py
import json
from pathlib import Path

import pytest

from scaneats_vision.config_loader import ConfigError, VisionConfig, load_config

EJEMPLO = Path(__file__).resolve().parent.parent / "config" / "zonas_ejemplo.json"


def base(**extra):
    d = {"tables": [{"numero": 1, "capacidad": 4, "polygon": [[0, 0], [0.1, 0], [0.1, 0.1]]}]}
    d.update(extra)
    return d


class TestEjemplo:
    def test_carga_el_json_de_ejemplo(self):
        cfg = load_config(EJEMPLO)
        assert len(cfg.tables) == 12
        assert [z.id for z in cfg.exclusion_zones] == ["Caja"]
        assert {p.identificador: p.color for p in cfg.staff} == {"MES-A": "ROJO", "MES-B": "AZUL", "MES-C": "VERDE"}

    def test_adyacencia_calculada_por_geometria(self):
        cfg = load_config(EJEMPLO)
        assert cfg.adjacency[4] == {5}
        assert cfg.adjacency[5] == {4, 6}  # 4-5-6 apiladas: la fusión observada en alta demanda
        assert cfg.adjacency[6] == {5}
        assert 7 not in cfg.adjacency[4]  # otra columna, con pasillo de por medio

    def test_la_caja_no_se_solapa_con_ninguna_mesa(self):
        cfg = load_config(EJEMPLO)
        caja = cfg.exclusion_zones[0]
        assert all(caja.bbox.intersection_area(t.zone.bbox) == 0 for t in cfg.tables)

    def test_todas_las_zonas_juntas(self):
        cfg = load_config(EJEMPLO)
        assert len(cfg.all_zones) == 13 and cfg.table(4).capacidad == 4


class TestValidacion:
    def test_archivo_inexistente_o_json_roto(self, tmp_path):
        with pytest.raises(ConfigError, match="No existe"):
            load_config(tmp_path / "nada.json")
        roto = tmp_path / "roto.json"
        roto.write_text("{no es json")
        with pytest.raises(ConfigError, match="JSON inválido"):
            load_config(roto)

    @pytest.mark.parametrize("mutar, mensaje", [
        (lambda d: d.update(tables=[]), "al menos una mesa"),
        (lambda d: d["tables"][0].update(polygon=[[0, 0], [1, 1]]), "3 vértices"),
        (lambda d: d["tables"][0].update(polygon=[[0, 0], [2, 0], [1, 1]]), "normalizadas"),
        (lambda d: d["tables"][0].update(capacidad=0), "capacidad"),
        (lambda d: d["tables"][0].update(numero="uno"), "entero"),
        (lambda d: d["tables"].append(dict(d["tables"][0])), "repetidos"),
        (lambda d: d.update(exclusion_zones=[{"nombre": "Caja", "polygon": [[0, 0]]}]), "vértices"),
    ])
    def test_configuraciones_invalidas(self, mutar, mensaje):
        d = base()
        mutar(d)
        with pytest.raises(ConfigError, match=mensaje):
            VisionConfig.from_dict(d)

    def test_el_identificador_del_personal_se_normaliza(self):
        cfg = VisionConfig.from_dict(base(staff=[{"identificador": " mes-a ", "prenda_color": "ROJO"}]))
        assert cfg.staff[0].identificador == "MES-A"


class TestDesdeLaApi:
    def test_convierte_la_respuesta_del_backend(self):
        payload = {
            "zonas": [{"id": 1, "nombre": "Salón", "es_exclusion": False, "region": None},
                      {"id": 2, "nombre": "Caja", "es_exclusion": True, "region": [[0, 0], [0.2, 0], [0.2, 0.3]]}],
            "mesas": [
                {"id": 1, "numero": 1, "capacidad": 4, "zona_id": 1, "region": [[0.3, 0.1], [0.4, 0.1], [0.4, 0.3]]},
                {"id": 2, "numero": 2, "capacidad": 2, "zona_id": 1, "region": None},  # sin región: se omite
                {"id": 3, "numero": 99, "capacidad": 1, "zona_id": 2, "region": None},  # la caja no es una mesa
            ],
            "personal": [{"identificador": "MES-A", "nombre": "A", "prenda_tipo": "DELANTAL", "prenda_color": "ROJO"}],
        }
        cfg = VisionConfig.from_api_payload(payload, camera_id="cam-9")
        assert [t.numero for t in cfg.tables] == [1]
        assert [z.id for z in cfg.exclusion_zones] == ["Caja"]
        assert cfg.staff[0].color == "ROJO" and cfg.camera_id == "cam-9"

    def test_un_json_serializable_de_ida_y_vuelta(self, tmp_path):
        p = tmp_path / "c.json"
        p.write_text(json.dumps(base(staff=[{"identificador": "X", "prenda_color": None}])))
        assert load_config(p).staff[0].color is None


class TestSuperficieYEntrega:
    def test_la_superficie_de_la_mesa_es_opcional(self):
        d = base()
        d["tables"][0]["surface"] = [[0.02, 0.02], [0.08, 0.02], [0.08, 0.06]]
        cfg = VisionConfig.from_dict(d)
        t = cfg.tables[0]
        assert t.surface is not None and t.surface_zone is t.surface
        assert t.surface_zone.id == 1  # mismo id que la mesa: el extractor de fondo devuelve {mesa: razón}
        assert t.surface_zone.area < t.zone.area
        assert cfg.surface_zones == [t.surface]

    def test_sin_superficie_se_usa_la_zona_completa(self):
        cfg = VisionConfig.from_dict(base())
        assert cfg.tables[0].surface is None and cfg.surface_zones == cfg.table_zones

    def test_superficie_invalida(self):
        d = base()
        d["tables"][0]["surface"] = [[0, 0], [5, 5], [1, 1]]
        with pytest.raises(ConfigError, match="surface"):
            VisionConfig.from_dict(d)

    def test_umbrales_de_entrega_por_camara(self):
        cfg = VisionConfig.from_dict(base(delivery={"change_high": 0.004, "change_low": 0.001}))
        assert cfg.delivery == {"change_high": 0.004, "change_low": 0.001}

    @pytest.mark.parametrize("delivery", [{"change_high": 0.001, "change_low": 0.01}, {"no_existe": 1}])
    def test_umbrales_de_entrega_invalidos(self, delivery):
        with pytest.raises(ConfigError, match="delivery"):
            VisionConfig.from_dict(base(delivery=delivery))

    def test_el_json_del_video_real_carga(self):
        real = Path(__file__).resolve().parent.parent / "config" / "zonas_video.json"
        cfg = load_config(real)
        assert len(cfg.tables) == 4 and all(t.surface for t in cfg.tables) and cfg.delivery


class TestAjustesPorCamara:
    def test_behavior_cascade_y_tracker(self):
        cfg = VisionConfig.from_dict(base(behavior={"seated_dwell_s": 30.0}, cascade={"clothing_accept": 0.85},
                                          tracker={"max_age": 30}))
        assert (cfg.behavior, cfg.cascade, cfg.tracker) == (
            {"seated_dwell_s": 30.0}, {"clothing_accept": 0.85}, {"max_age": 30})

    @pytest.mark.parametrize("clave", ["behavior", "cascade", "tracker", "delivery"])
    def test_claves_desconocidas_se_rechazan(self, clave):
        with pytest.raises(ConfigError, match=clave):
            VisionConfig.from_dict(base(**{clave: {"no_existe": 1}}))

    def test_por_defecto_estan_vacios(self):
        cfg = VisionConfig.from_dict(base())
        assert cfg.behavior == cfg.cascade == cfg.tracker == cfg.delivery == {}
