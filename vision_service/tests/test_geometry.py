# vision_service/tests/test_geometry.py
import pytest

from scaneats_vision.geometry import (
    BBox, EXCLUSION, Zone, assign_to_zone, bbox_gap, bbox_polygon_intersection_area, clip_polygon_to_bbox,
    point_in_polygon, polygon_area, polygon_bbox, zones_adjacent,
)

CUADRADO = [(0, 0), (1, 0), (1, 1), (0, 1)]


def mesa(id_, x1, y1, x2, y2, kind="table"):
    return Zone(id_, ((x1, y1), (x2, y1), (x2, y2), (x1, y2)), kind)


class TestBBox:
    def test_propiedades(self):
        b = BBox(0.1, 0.2, 0.5, 0.6)
        assert b.width == pytest.approx(0.4) and b.height == pytest.approx(0.4)
        assert b.area == pytest.approx(0.16)
        assert b.centroid == pytest.approx((0.3, 0.4))
        assert b.foot_point == pytest.approx((0.3, 0.6))

    def test_normaliza_cajas_al_reves(self):
        b = BBox(0.5, 0.6, 0.1, 0.2)
        assert (b.x1, b.y1, b.x2, b.y2) == (0.1, 0.2, 0.5, 0.6)
        assert b.area > 0

    def test_iou(self):
        a = BBox(0, 0, 2, 2)
        assert a.iou(a) == pytest.approx(1.0)
        assert a.iou(BBox(1, 0, 3, 2)) == pytest.approx(2 / 6)
        assert a.iou(BBox(5, 5, 6, 6)) == 0.0

    def test_from_xywh_y_escala(self):
        assert BBox.from_xywh(1, 2, 3, 4) == BBox(1, 2, 4, 6)
        assert BBox(0.1, 0.2, 0.3, 0.4).scaled(100, 50) == BBox(10, 10, 30, 20)


class TestPoligonos:
    def test_area_shoelace_independiente_del_sentido(self):
        assert polygon_area(CUADRADO) == pytest.approx(1.0)
        assert polygon_area(list(reversed(CUADRADO))) == pytest.approx(1.0)
        assert polygon_area([(0, 0), (4, 0), (0, 3)]) == pytest.approx(6.0)
        assert polygon_area([(0, 0), (1, 1)]) == 0.0

    def test_punto_en_poligono(self):
        assert point_in_polygon((0.5, 0.5), CUADRADO)
        assert not point_in_polygon((1.5, 0.5), CUADRADO)
        assert point_in_polygon((1.0, 0.5), CUADRADO)  # borde = dentro
        assert point_in_polygon((0, 0), CUADRADO)  # vértice = dentro

    def test_poligono_concavo(self):
        ele = [(0, 0), (2, 0), (2, 1), (1, 1), (1, 2), (0, 2)]  # en forma de L
        assert point_in_polygon((0.5, 1.5), ele)
        assert not point_in_polygon((1.5, 1.5), ele)  # en el hueco de la L

    def test_polygon_bbox(self):
        assert polygon_bbox([(1, 5), (3, 2), (2, 9)]) == BBox(1, 2, 3, 9)

    def test_recorte_contra_caja(self):
        assert polygon_area(clip_polygon_to_bbox(CUADRADO, BBox(-1, -1, 2, 2))) == pytest.approx(1.0)  # caja contiene
        assert polygon_area(clip_polygon_to_bbox(CUADRADO, BBox(0.5, 0, 2, 1))) == pytest.approx(0.5)  # mitad
        assert clip_polygon_to_bbox(CUADRADO, BBox(3, 3, 4, 4)) == []  # sin contacto
        triangulo = [(0, 0), (2, 0), (0, 2)]
        assert bbox_polygon_intersection_area(BBox(0, 0, 1, 1), triangulo) == pytest.approx(1.0)
        assert bbox_polygon_intersection_area(BBox(0, 0, 2, 2), triangulo) == pytest.approx(2.0)


class TestZona:
    def test_overlap_y_cobertura(self):
        z = mesa(1, 0, 0, 1, 1)
        assert z.overlap_ratio(BBox(0.2, 0.2, 0.4, 0.4)) == pytest.approx(1.0)  # caja totalmente dentro
        assert z.overlap_ratio(BBox(0.5, 0, 1.5, 1)) == pytest.approx(0.5)  # la mitad de la caja
        assert z.overlap_ratio(BBox(2, 2, 3, 3)) == 0.0
        assert z.overlap_ratio(BBox(0.5, 0.5, 0.5, 0.5)) == 0.0  # caja degenerada
        assert z.coverage(BBox(0, 0, 0.5, 1)) == pytest.approx(0.5)  # cubre la mitad de la zona

    def test_zona_no_rectangular(self):
        rombo = Zone(1, ((0.5, 0), (1, 0.5), (0.5, 1), (0, 0.5)))
        # La caja envolvente completa solo cubre la mitad del rombo: el overlap real lo refleja
        assert rombo.overlap_ratio(BBox(0, 0, 1, 1)) == pytest.approx(0.5)


class TestAsignacion:
    def test_centroide_dentro(self):
        zonas = [mesa(1, 0, 0, 0.4, 0.4), mesa(2, 0.5, 0, 0.9, 0.4)]
        m = assign_to_zone(BBox(0.1, 0.1, 0.3, 0.3), zonas)
        assert m.zone.id == 1 and m.centroid_inside

    def test_sin_zona(self):
        assert assign_to_zone(BBox(0.6, 0.6, 0.7, 0.7), [mesa(1, 0, 0, 0.4, 0.4)]) is None

    def test_overlap_rescata_cuando_el_centroide_cae_fuera(self):
        """Persona inclinada sobre la mesa: su centroide queda fuera pero el 40 % de su caja está encima."""
        zona = mesa(1, 0, 0, 0.4, 0.4)
        caja = BBox(0.30, 0.10, 0.60, 0.20)  # centroide x=0.45 (fuera); 1/3 de la caja dentro
        m = assign_to_zone(caja, [zona], min_overlap=0.25)
        assert m is not None and not m.centroid_inside and m.overlap == pytest.approx(1 / 3)
        assert assign_to_zone(caja, [zona], min_overlap=0.5) is None  # con umbral más alto no se asigna

    def test_por_que_no_basta_la_contencion_total(self):
        """Comparación con el enfoque ingenuo 'la caja debe estar TODA dentro de la zona'."""
        zona = mesa(1, 0.2, 0.2, 0.5, 0.5)
        caidas = [BBox(0.22, 0.22, 0.48, 0.48), BBox(0.15, 0.25, 0.35, 0.45), BBox(0.3, 0.3, 0.55, 0.5),
                  BBox(0.18, 0.18, 0.4, 0.4)]  # tres de cuatro se salen un poco del borde (perspectiva, sillas)

        def ingenuo(b):
            return b.x1 >= 0.2 and b.y1 >= 0.2 and b.x2 <= 0.5 and b.y2 <= 0.5

        assert sum(ingenuo(b) for b in caidas) == 1
        assert all(assign_to_zone(b, [zona]) is not None for b in caidas)

    def test_gana_la_zona_que_contiene_el_centroide(self):
        a, b = mesa("A", 0, 0, 0.5, 1), mesa("B", 0.5, 0, 1, 1)
        caja = BBox(0.3, 0.4, 0.65, 0.6)  # más área en A, pero el centroide (0.475) está en A
        assert assign_to_zone(caja, [a, b]).zone.id == "A"
        assert assign_to_zone(BBox(0.4, 0.4, 0.75, 0.6), [a, b]).zone.id == "B"  # centroide 0.575 en B

    def test_la_caja_no_se_reasigna_a_la_mesa_vecina(self):
        """Una persona en la zona de exclusión debe quedar en la exclusión, no 'contarse' en la mesa contigua."""
        caja = mesa("Caja", 0, 0, 0.3, 0.5, EXCLUSION)
        m1 = mesa(1, 0.28, 0, 0.6, 0.5)
        persona = BBox(0.20, 0.1, 0.34, 0.4)  # centroide 0.27 dentro de la caja, solapa un poco con la mesa 1
        m = assign_to_zone(persona, [m1, caja])
        assert m.zone.is_exclusion

    def test_punto_de_referencia_pies(self):
        z = mesa(1, 0, 0.5, 1, 1)
        alta = BBox(0.4, 0.1, 0.6, 0.7)  # de pie: centroide y=0.4 (fuera), pies y=0.7 (dentro)
        assert assign_to_zone(alta, [z], min_overlap=0.9, reference="centroid") is None
        assert assign_to_zone(alta, [z], min_overlap=0.9, reference="foot").zone.id == 1


class TestAdyacencia:
    def test_hueco_entre_cajas(self):
        a, b = BBox(0, 0, 1, 1), BBox(1.5, 0, 2.5, 1)
        assert bbox_gap(a, b) == pytest.approx(0.5)
        assert bbox_gap(a, BBox(0.5, 0.5, 2, 2)) == 0.0  # solapadas
        assert bbox_gap(a, BBox(2, 2, 3, 3)) == pytest.approx(2 ** 0.5)

    def test_zonas_adyacentes(self):
        a, b, c = mesa(1, 0, 0, 1, 1), mesa(2, 1.05, 0, 2, 1), mesa(3, 3, 0, 4, 1)
        assert zones_adjacent(a, b, 0.1)
        assert not zones_adjacent(a, c, 0.1)
