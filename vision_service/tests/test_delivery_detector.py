# vision_service/tests/test_delivery_detector.py
import pytest

from scaneats_vision.delivery_detector import DeliveryConfig, DeliveryDetector

CFG = DeliveryConfig()  # high .05, low .02, persist 1.5 s, ventana de mesero 20 s, enfriamiento 25 s


def correr(det, mesa, señal, desde, hasta, ocupada=True, dt=0.5):
    """señal(t) -> razón de cambio | None. Devuelve las detecciones producidas."""
    out, t = [], desde
    while t <= hasta + 1e-9:
        d = det.update(mesa, t, señal(t), ocupada)
        if d:
            out.append(d)
        t += dt
    return out


def entrega_en(t0, alto=0.12):
    """Estable, aparece algo en t0 y ahí se queda un rato."""
    return lambda t: alto if t >= t0 else 0.0


class TestDeteccion:
    def test_mesero_presente_mas_cambio_sostenido_es_una_entrega(self):
        det = DeliveryDetector(CFG)
        correr(det, 1, lambda t: 0.0, 0, 9)  # mesa estable: el detector se arma
        det.note_staff_presence(1, 9.5)
        out = correr(det, 1, entrega_en(10.0), 9.5, 20)
        assert len(out) == 1
        d = out[0]
        assert d.table_id == 1
        assert d.at == pytest.approx(10.0)  # cuándo apareció el plato
        assert d.confirmed_at == pytest.approx(10.0 + CFG.persist_s, abs=0.5)
        assert d.staff_last_seen == 9.5

    def test_el_cambio_debe_sostenerse(self):
        det = DeliveryDetector(CFG)
        correr(det, 1, lambda t: 0.0, 0, 9)
        det.note_staff_presence(1, 9.5)
        assert correr(det, 1, lambda t: 0.12 if 10 <= t < 11 else 0.0, 9.5, 30) == []  # 1 s < 1.5 s (sombra/brazo)

    def test_sin_mesero_no_hay_entrega(self):
        """Un cliente que deja su celular sobre la mesa cambia la escena pero no es una entrega."""
        det = DeliveryDetector(CFG)
        correr(det, 1, lambda t: 0.0, 0, 9)
        assert correr(det, 1, entrega_en(10.0), 9.5, 30) == []

    def test_el_mesero_debe_haber_estado_recientemente(self):
        det = DeliveryDetector(CFG)
        det.note_staff_presence(1, 0.0)
        correr(det, 1, lambda t: 0.0, 0.5, 40)
        assert correr(det, 1, entrega_en(41.0), 40.5, 60) == []  # el mesero se fue hace 41 s > 20 s

    def test_la_ventana_del_mesero_se_mide_hasta_el_inicio_del_cambio(self):
        """Un gesto del cliente 13 s después de que el mesero se fue no es una entrega (ventana de 8 s)."""
        det = DeliveryDetector(DeliveryConfig(staff_window_s=8.0))
        det.note_staff_presence(1, 0.0)
        correr(det, 1, lambda t: 0.0, 0.5, 12)
        assert correr(det, 1, entrega_en(13.0), 12.5, 30) == []
        det2 = DeliveryDetector(DeliveryConfig(staff_window_s=8.0))
        det2.note_staff_presence(1, 5.0)  # se fue 5 s antes de que apareciera el plato: sí cuenta
        correr(det2, 1, lambda t: 0.0, 0.5, 9)
        assert len(correr(det2, 1, entrega_en(10.0), 9.5, 30)) == 1

    def test_debe_estar_armado_no_dispara_si_ya_arranca_con_cambio_alto(self):
        det = DeliveryDetector(CFG)
        det.note_staff_presence(1, 0.0)
        assert correr(det, 1, lambda t: 0.3, 0.5, 20) == []  # nunca vio la mesa estable: puede ser iluminación

    def test_solo_en_mesas_ocupadas(self):
        det = DeliveryDetector(CFG)
        det.note_staff_presence(1, 0.0)
        assert correr(det, 1, entrega_en(1.0), 0.5, 10, ocupada=False) == []

    def test_mesa_tapada_por_personas_conserva_el_estado(self):
        det = DeliveryDetector(CFG)
        correr(det, 1, lambda t: 0.0, 0, 9)
        det.note_staff_presence(1, 9.5)
        # el cambio arranca en t=10; durante 10.5-11.5 la mesa queda tapada (None) y no reinicia la cuenta
        out = correr(det, 1, lambda t: None if 10.5 <= t < 11.5 else 0.12, 9.5, 20)
        assert len(out) == 1


class TestVariasEntregas:
    def test_una_mesa_admite_varias_entregas_por_ocupacion(self):
        """No siempre llega todo junto ni hay aperitivo previo."""
        det = DeliveryDetector(CFG)
        det.note_staff_presence(1, 0.0)
        # 1ª entrega en t=10; el plato se "absorbe" al fondo y la mesa se estabiliza; 2ª entrega en t=50
        señal = lambda t: 0.12 if 10 <= t < 20 else (0.12 if 50 <= t < 60 else 0.0)  # noqa: E731
        out = []
        for t in [x * 0.5 for x in range(0, 140)]:
            if 48 <= t < 51:
                det.note_staff_presence(1, t)
            if 8 <= t < 11:
                det.note_staff_presence(1, t)
            d = det.update(1, t, señal(t), True)
            if d:
                out.append(d)
        assert [round(d.at) for d in out] == [10, 50]

    def test_un_objeto_que_permanece_no_dispara_dos_veces(self):
        det = DeliveryDetector(CFG)
        det.note_staff_presence(1, 0.0)
        correr(det, 1, lambda t: 0.0, 0.5, 9)
        assert len(correr(det, 1, entrega_en(10.0), 9.5, 200)) == 1  # el plato sigue ahí 190 s

    def test_enfriamiento_entre_entregas(self):
        det = DeliveryDetector(DeliveryConfig(cooldown_s=25.0))
        det.note_staff_presence(1, 0.0)
        señal = lambda t: 0.12 if (10 <= t < 14 or 22 <= t < 26) else 0.0  # noqa: E731
        out = []
        for t in [x * 0.5 for x in range(0, 100)]:
            det.note_staff_presence(1, t) if t < 30 else None
            d = det.update(1, t, señal(t), True)
            out.append(d) if d else None
        assert len(out) == 1  # la 2ª (12 s después) cae dentro del enfriamiento de 25 s

    def test_reset_al_liberar_la_mesa(self):
        det = DeliveryDetector(CFG)
        det.note_staff_presence(1, 0.0)
        correr(det, 1, lambda t: 0.0, 0.5, 5)
        det.reset(1)
        assert correr(det, 1, entrega_en(6.0), 5.5, 20) == []  # sin contexto previo: ni mesero ni armado


class TestConfig:
    @pytest.mark.parametrize("kwargs", [{"change_low": 0.1, "change_high": 0.05}, {"change_high": 1.5}, {"change_low": -0.1}])
    def test_validaciones(self, kwargs):
        with pytest.raises(ValueError):
            DeliveryConfig(**kwargs).validate()

    def test_el_extractor_de_primer_plano_no_se_importa_sin_opencv(self):
        """La lógica pura funciona sin OpenCV; solo ForegroundExtractor lo requiere (importación perezosa)."""
        try:
            import cv2  # noqa: F401
        except ImportError:
            from scaneats_vision.delivery_detector import ForegroundExtractor

            with pytest.raises(ImportError):
                ForegroundExtractor()
