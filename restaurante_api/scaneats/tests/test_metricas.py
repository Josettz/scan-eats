# restaurante_api/scaneats/tests/test_metricas.py
"""Las 3 métricas obligatorias del profesor: RF-10 (+RF-09), RF-11 y RF-12."""
from datetime import timedelta

from django.utils import timezone

from .base import ApiTestBase, t


class TiempoEsperaTests(ApiTestBase):
    """GET /api/metricas/tiempo-espera/"""

    url = "/api/metricas/tiempo-espera/"

    def setUp(self):
        super().setUp()
        self.zona_a, (self.m1, self.m2) = self.crear_salon(2, "Frente")
        self.zona_b, _ = self.crear_salon(0, "Fondo")
        from scaneats.models import Mesa
        self.m3 = Mesa.objects.create(numero=3, capacidad=4, zona=self.zona_b)
        self.ana = self.crear_mesero()
        # Datos conocidos (espera = 1ª entrega - ocupación): m1: 60 s y 180 s  | m2: 300 s | m3: 600 s (día 2)
        o = self.crear_ocupacion(self.m1, t(10, 0, 0), t(10, 30))
        self.crear_entrega(o, self.ana, t(10, 1, 0))
        self.crear_entrega(o, self.ana, t(10, 9, 0))  # 2ª entrega: NO cambia la espera
        o = self.crear_ocupacion(self.m1, t(11, 0, 0), t(11, 30))
        self.crear_entrega(o, self.ana, t(11, 3, 0))
        o = self.crear_ocupacion(self.m2, t(10, 0, 0), t(10, 40))
        self.crear_entrega(o, self.ana, t(10, 5, 0))
        o = self.crear_ocupacion(self.m3, t(12, 0, 0, dia=2), t(12, 30, dia=2))
        self.crear_entrega(o, self.ana, t(12, 10, 0, dia=2))
        self.crear_ocupacion(self.m2, t(15, 0, 0))  # ocupación sin entrega: no cuenta para el promedio

    def get(self, **params):
        r = self.admin_client.get(self.url, params)
        self.assertEqual(r.status_code, 200, r.content)
        return r.json()

    def test_promedio_general(self):
        d = self.get()
        self.assertEqual(d["ocupaciones_con_entrega"], 4)
        self.assertEqual(d["ocupaciones_sin_entrega"], 1)
        self.assertEqual(d["promedio_segundos"], (60 + 180 + 300 + 600) / 4)
        self.assertEqual((d["minimo_segundos"], d["maximo_segundos"]), (60, 600))

    def test_filtra_por_mesa(self):
        d = self.get(mesa=self.m1.pk)
        self.assertEqual(d["promedio_segundos"], 120.0)
        self.assertEqual(d["promedio_minutos"], 2.0)
        self.assertEqual([m["mesa_numero"] for m in d["por_mesa"]], [1])

    def test_filtra_por_zona(self):
        self.assertEqual(self.get(zona=self.zona_a.pk)["promedio_segundos"], (60 + 180 + 300) / 3)
        self.assertEqual(self.get(zona=self.zona_b.pk)["promedio_segundos"], 600.0)

    def test_filtra_por_rango_de_fechas_con_datos_conocidos(self):  # criterio de verificación de RF-10
        d = self.get(desde="2026-03-01", hasta="2026-03-01")  # 'hasta' de solo fecha incluye todo ese día
        self.assertEqual(d["promedio_segundos"], (60 + 180 + 300) / 3)
        self.assertEqual(self.get(desde="2026-03-02", hasta="2026-03-02")["promedio_segundos"], 600.0)
        d = self.get(desde="2026-03-01T10:30:00+00:00", hasta="2026-03-01T11:59:00+00:00")
        self.assertEqual(d["promedio_segundos"], 180.0)  # solo la ocupación de las 11:00

    def test_por_mesa_incluye_el_desglose(self):
        por = {m["mesa_numero"]: m for m in self.get()["por_mesa"]}
        self.assertEqual(por[1]["promedio_segundos"], 120.0)
        self.assertEqual(por[1]["muestras"], 2)
        self.assertEqual(por[2]["promedio_segundos"], 300.0)
        self.assertEqual(por[3]["promedio_segundos"], 600.0)

    def test_sin_datos_devuelve_promedio_nulo_y_no_falla(self):
        d = self.get(desde="2030-01-01")
        self.assertIsNone(d["promedio_segundos"])
        self.assertEqual(d["ocupaciones_con_entrega"], 0)

    def test_parametros_invalidos(self):
        for params in ({"desde": "ayer"}, {"hasta": "31/12/2026"}, {"mesa": "abc"}, {"zona": "x"},
                       {"desde": "2026-03-05", "hasta": "2026-03-01"}):
            r = self.admin_client.get(self.url, params)
            self.assertEqual(r.status_code, 400, params)


class EmpleadosTests(ApiTestBase):
    """GET /api/metricas/empleados/"""

    url = "/api/metricas/empleados/"

    def setUp(self):
        super().setUp()
        _, self.mesas = self.crear_salon(6)
        self.ana = self.crear_mesero("A", "Ana")
        self.beto = self.crear_mesero("B", "Beto", "AZUL")
        self.carla = self.crear_mesero("C", "Carla", "VERDE")

    def atender(self, mesero, mesa, hora, entregas=1):
        o = self.crear_ocupacion(mesa, hora, hora + timedelta(minutes=20))
        for i in range(entregas):
            self.crear_entrega(o, mesero, hora + timedelta(minutes=1 + i))

    def get(self, **params):
        r = self.admin_client.get(self.url, params)
        self.assertEqual(r.status_code, 200, r.content)
        return r.json()

    def test_identifica_al_empleado_con_mas_mesas_atendidas(self):
        for i in range(3):
            self.atender(self.ana, self.mesas[i], t(10, i * 25))
        self.atender(self.beto, self.mesas[3], t(10))
        d = self.get()
        self.assertEqual(d["maximo_mesas_atendidas"], 3)
        self.assertEqual([e["nombre"] for e in d["empleados_top"]], ["Ana"])
        self.assertFalse(d["empate"])
        self.assertEqual([(r["nombre"], r["mesas_atendidas"]) for r in d["ranking"]], [("Ana", 3), ("Beto", 1), ("Carla", 0)])

    def test_varias_entregas_a_la_misma_mesa_cuentan_una_sola_mesa(self):
        self.atender(self.ana, self.mesas[0], t(10), entregas=4)
        self.atender(self.beto, self.mesas[1], t(10), entregas=1)
        self.atender(self.beto, self.mesas[2], t(10), entregas=1)
        d = self.get()
        self.assertEqual(d["empleados_top"][0]["nombre"], "Beto")  # 2 mesas > 1 mesa aunque Ana hizo 4 entregas
        ana = next(r for r in d["ranking"] if r["nombre"] == "Ana")
        self.assertEqual((ana["mesas_atendidas"], ana["entregas"]), (1, 4))

    def test_los_empates_no_omiten_a_nadie(self):  # HU-04
        self.atender(self.ana, self.mesas[0], t(10))
        self.atender(self.ana, self.mesas[1], t(10))
        self.atender(self.beto, self.mesas[2], t(10))
        self.atender(self.beto, self.mesas[3], t(10))
        self.atender(self.carla, self.mesas[4], t(10))
        d = self.get()
        self.assertTrue(d["empate"])
        self.assertEqual({e["nombre"] for e in d["empleados_top"]}, {"Ana", "Beto"})
        self.assertEqual(len(d["empleados_top"]), 2)

    def test_respeta_el_periodo(self):
        self.atender(self.ana, self.mesas[0], t(10, dia=1))
        self.atender(self.ana, self.mesas[1], t(10, dia=1))
        self.atender(self.beto, self.mesas[2], t(10, dia=2))
        self.assertEqual(self.get(desde="2026-03-01", hasta="2026-03-01")["empleados_top"][0]["nombre"], "Ana")
        self.assertEqual(self.get(desde="2026-03-02", hasta="2026-03-02")["empleados_top"][0]["nombre"], "Beto")
        self.assertEqual(self.get()["empleados_top"][0]["nombre"], "Ana")

    def test_filtra_por_zona(self):
        from scaneats.models import Mesa
        otra = Mesa.objects.create(numero=50, capacidad=2, zona=self.crear_salon(0, "Patio")[0])
        self.atender(self.ana, self.mesas[0], t(10))
        self.atender(self.beto, otra, t(10))
        self.assertEqual(self.get(zona=otra.zona_id)["empleados_top"][0]["nombre"], "Beto")

    def test_sin_datos_no_hay_ganador(self):
        d = self.get()
        self.assertEqual((d["maximo_mesas_atendidas"], d["empleados_top"], d["empate"]), (0, [], False))
        self.assertEqual(len(d["ranking"]), 3)  # el personal activo aparece con 0


class UsoMesasTests(ApiTestBase):
    """GET /api/metricas/uso-mesas/"""

    url = "/api/metricas/uso-mesas/"

    def setUp(self):
        super().setUp()
        self.zona, self.mesas = self.crear_salon(4)
        self.crear_caja()  # la caja jamás aparece como mesa
        # Mesa 1: 3 usos (10:xx, 10:xx, 13:xx) · Mesa 2: 1 uso · Mesa 3 y 4: sin uso.
        self.crear_ocupacion(self.mesas[0], t(15, 0), t(15, 30))   # 10:00 hora local (UTC-5)
        self.crear_ocupacion(self.mesas[0], t(15, 40), t(15, 50))  # 10:40 local
        self.crear_ocupacion(self.mesas[0], t(18, 0), t(19, 0))    # 13:00 local
        self.crear_ocupacion(self.mesas[1], t(15, 10), t(16, 10))  # 10:10 local

    def get(self, **params):
        r = self.admin_client.get(self.url, params)
        self.assertEqual(r.status_code, 200, r.content)
        return r.json()

    def test_frecuencia_por_mesa_sin_omisiones(self):  # RF-12: refleja TODOS los eventos registrados
        d = self.get()
        self.assertEqual(d["total_ocupaciones"], 4)
        frec = {m["mesa_numero"]: m["frecuencia"] for m in d["por_mesa"]}
        self.assertEqual(frec, {1: 3, 2: 1, 3: 0, 4: 0})
        self.assertNotIn(99, frec)

    def test_mesas_mas_y_menos_usadas_incluyendo_las_no_usadas(self):
        d = self.get()
        self.assertEqual([m["mesa_numero"] for m in d["mas_usadas"]], [1])
        self.assertEqual({m["mesa_numero"] for m in d["menos_usadas"]}, {3, 4})  # empate: aparecen las dos

    def test_empate_en_las_mas_usadas(self):
        self.crear_ocupacion(self.mesas[1], t(20, 0), t(20, 30))
        self.crear_ocupacion(self.mesas[1], t(21, 0), t(21, 30))  # mesa 2 llega a 3 usos
        self.assertEqual({m["mesa_numero"] for m in self.get()["mas_usadas"]}, {1, 2})

    def test_duraciones(self):
        m1 = next(m for m in self.get()["por_mesa"] if m["mesa_numero"] == 1)
        self.assertEqual(m1["tiempo_ocupada_segundos"], (30 + 10 + 60) * 60)
        self.assertEqual(m1["duracion_promedio_segundos"], (30 + 10 + 60) * 60 / 3)

    def test_horas_pico_en_hora_local(self):
        d = self.get()
        por_hora = {h["hora"]: h["ocupaciones"] for h in d["por_hora"]}
        self.assertEqual(por_hora[10], 3)  # 15:00, 15:40 y 15:10 UTC = 10:xx en Guayaquil
        self.assertEqual(por_hora[13], 1)
        self.assertEqual(d["horas_pico"], [10])
        self.assertEqual(len(d["por_hora"]), 24)

    def test_agregado_por_zona(self):
        d = self.get()
        self.assertEqual([(z["zona"], z["frecuencia"], z["mesas"]) for z in d["por_zona"]], [("Salón", 4, 4)])

    def test_filtros_por_mesa_zona_y_fecha(self):
        self.assertEqual(self.get(mesa=self.mesas[1].pk)["total_ocupaciones"], 1)
        self.assertEqual(self.get(zona=self.zona.pk)["total_ocupaciones"], 4)
        self.assertEqual(self.get(desde="2026-03-02")["total_ocupaciones"], 0)
        d = self.get(desde="2026-03-01T15:35:00+00:00", hasta="2026-03-01T18:30:00+00:00")
        self.assertEqual(d["total_ocupaciones"], 2)

    def test_sin_uso_no_hay_mesas_mas_usadas(self):
        d = self.get(desde="2030-01-01")
        self.assertEqual(d["mas_usadas"], [])
        self.assertEqual(len(d["menos_usadas"]), 4)
        self.assertEqual(d["horas_pico"], [])

    def test_ocupacion_abierta_cuenta_hasta_ahora(self):
        self.crear_ocupacion(self.mesas[2], timezone.now() - timedelta(minutes=10))
        m3 = next(m for m in self.get()["por_mesa"] if m["mesa_numero"] == 3)
        self.assertEqual(m3["frecuencia"], 1)
        self.assertGreaterEqual(m3["tiempo_ocupada_segundos"], 600)
