# restaurante_api/scaneats/tests/test_eventos.py
"""RF-01, RF-02, RF-03, RF-09, RF-15: eventos que reporta el microservicio de visión."""
import uuid
from datetime import timedelta

from django.utils import timezone

from scaneats.models import ClasificacionPersonal, EventoEntrega, EventoOcupacion, FusionMesas
from scaneats.services import tiempo_espera_segundos

from .base import ApiTestBase, t


class OcupacionTests(ApiTestBase):
    def setUp(self):
        super().setUp()
        self.zona, self.mesas = self.crear_salon(6)

    def test_registra_ocupacion_en_menos_de_3_segundos_desde_la_deteccion(self):  # RF-01
        ahora = timezone.now()
        r = self.vision.post(
            "/api/eventos/ocupacion/",
            {"mesa_numero": 2, "hora_inicio": (ahora - timedelta(seconds=4)).isoformat(), "detectado_en": ahora.isoformat()},
            format="json",
        )
        self.assertEqual(r.status_code, 201, r.content)
        cuerpo = r.json()
        self.assertEqual(cuerpo["estado"], "OCUPADA")
        self.assertEqual(cuerpo["mesa_numero"], 2)
        self.assertLess(cuerpo["latencia_registro_ms"], 3000)
        evento = EventoOcupacion.objects.get(pk=cuerpo["id"])
        self.assertEqual(evento.zona, self.zona)  # zona congelada en el histórico
        self.assertLess(evento.latencia_registro, timedelta(seconds=3))

    def test_la_ocupacion_es_idempotente(self):
        r1 = self.vision.post("/api/eventos/ocupacion/", {"mesa_numero": 1}, format="json")
        r2 = self.vision.post("/api/eventos/ocupacion/", {"mesa_numero": 1}, format="json")  # reintento
        self.assertEqual((r1.status_code, r2.status_code), (201, 200))
        self.assertEqual(r1.json()["id"], r2.json()["id"])
        uid = str(uuid.uuid4())
        a = self.vision.post("/api/eventos/ocupacion/", {"mesa_numero": 3, "uid": uid}, format="json")
        b = self.vision.post("/api/eventos/ocupacion/", {"mesa_numero": 3, "uid": uid}, format="json")
        self.assertEqual(a.json()["id"], b.json()["id"])
        self.assertEqual(EventoOcupacion.objects.count(), 2)

    def test_mesa_inexistente_o_datos_invalidos(self):
        self.assertEqual(self.vision.post("/api/eventos/ocupacion/", {"mesa_numero": 77}, format="json").status_code, 400)
        self.assertEqual(self.vision.post("/api/eventos/ocupacion/", {}, format="json").status_code, 400)

    def test_maquina_de_estados(self):
        post = lambda estado, **kw: self.vision.post(  # noqa: E731
            "/api/eventos/ocupacion/cambiar-estado/", {"mesa_numero": 1, "estado": estado, **kw}, format="json")
        self.vision.post("/api/eventos/ocupacion/", {"mesa_numero": 1, "hora_inicio": t(10).isoformat()}, format="json")
        self.assertEqual(post("POSIBLEMENTE_LIBRE").json()["estado"], "POSIBLEMENTE_LIBRE")
        self.assertEqual(post("OCUPADA").json()["estado"], "OCUPADA")  # el cliente volvió
        self.assertEqual(post("POSIBLEMENTE_LIBRE").status_code, 200)
        r = post("LIBRE", hora_fin=t(10, 30).isoformat())
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["estado"], "LIBRE")
        self.assertEqual(EventoOcupacion.objects.get().hora_fin, t(10, 30))
        self.assertEqual(post("LIBRE").status_code, 200)  # reintento de un cierre ya aplicado
        self.assertEqual(post("OCUPADA").status_code, 409)  # LIBRE es terminal: una nueva ocupación es otro evento
        # y una mesa liberada puede ocuparse de nuevo
        self.assertEqual(self.vision.post("/api/eventos/ocupacion/", {"mesa_numero": 1}, format="json").status_code, 201)

    def test_hora_fin_anterior_al_inicio_se_rechaza(self):
        self.vision.post("/api/eventos/ocupacion/", {"mesa_numero": 1, "hora_inicio": t(10).isoformat()}, format="json")
        r = self.vision.post(
            "/api/eventos/ocupacion/cambiar-estado/",
            {"mesa_numero": 1, "estado": "LIBRE", "hora_fin": t(9).isoformat()}, format="json",
        )
        self.assertEqual(r.status_code, 409)

    def test_cambiar_estado_sin_ocupacion_abierta(self):
        r = self.vision.post("/api/eventos/ocupacion/cambiar-estado/", {"mesa_numero": 4, "estado": "LIBRE"}, format="json")
        self.assertEqual(r.status_code, 409)

    def test_historial_lo_lee_el_administrador_con_filtros(self):
        self.crear_ocupacion(self.mesas[0], t(10), t(11))
        self.crear_ocupacion(self.mesas[1], t(12))
        self.assertEqual(self.admin_client.get("/api/eventos/ocupacion/").json()["count"], 2)
        self.assertEqual(self.admin_client.get("/api/eventos/ocupacion/?abierta=true").json()["count"], 1)
        self.assertEqual(self.admin_client.get(f"/api/eventos/ocupacion/?mesa={self.mesas[0].pk}").json()["count"], 1)


class ZonaExclusionTests(ApiTestBase):
    """RF-15: en la zona de caja NO se genera ningún evento (100 % de las pruebas)."""

    def test_ningun_evento_se_genera_sobre_la_caja(self):
        _, mesas = self.crear_salon(2)
        _, caja = self.crear_caja()
        self.crear_mesero()
        self.crear_ocupacion(mesas[0], t(10))
        for _ in range(3):  # repetido: el rechazo es sistemático
            r1 = self.vision.post("/api/eventos/ocupacion/", {"mesa_numero": 99}, format="json")
            r2 = self.vision.post("/api/eventos/entrega/", {"mesa_numero": 99, "personal_identificador": "MES-A"}, format="json")
            r3 = self.vision.post("/api/eventos/fusion/", {"mesas": [1, 99]}, format="json")
            for r in (r1, r2, r3):
                self.assertEqual(r.status_code, 422)
                self.assertEqual(r.json()["detail"].split(":")[0], "La mesa pertenece a una zona de exclusión")
        self.assertEqual(EventoOcupacion.objects.filter(mesa=caja).count(), 0)
        self.assertEqual(EventoEntrega.objects.count(), 0)
        self.assertEqual(FusionMesas.objects.count(), 0)

    def test_marcar_una_zona_como_exclusion_bloquea_sus_mesas_existentes(self):
        zona, mesas = self.crear_salon(2)
        self.assertEqual(self.vision.post("/api/eventos/ocupacion/", {"mesa_numero": 1}, format="json").status_code, 201)
        self.admin_client.patch(f"/api/zonas/{zona.pk}/", {"es_exclusion": True}, format="json")
        self.assertEqual(self.vision.post("/api/eventos/ocupacion/", {"mesa_numero": 2}, format="json").status_code, 422)


class EntregaTests(ApiTestBase):
    def setUp(self):
        super().setUp()
        _, self.mesas = self.crear_salon(4)
        self.mesero = self.crear_mesero()
        self.ocupacion = self.crear_ocupacion(self.mesas[0], t(10, 0, 0))

    def post(self, **datos):
        base = {"mesa_numero": 1, "personal_identificador": "MES-A", "hora": t(10, 4, 30).isoformat()}
        return self.vision.post("/api/eventos/entrega/", {**base, **datos}, format="json")

    def test_registra_entrega_con_mesero_mesa_y_hora(self):
        r = self.post(metodo_identificacion="VESTIMENTA", confianza=0.91)
        self.assertEqual(r.status_code, 201, r.content)
        e = EventoEntrega.objects.get()
        self.assertEqual((e.personal, e.ocupacion), (self.mesero, self.ocupacion))
        self.assertEqual(r.json()["mesa_numero"], 1)
        self.assertEqual(r.json()["metodo_identificacion"], "VESTIMENTA")

    def test_diez_entregas_simuladas_todas_con_mesero(self):  # RF-02: 100 %
        for i in range(10):
            mesa = self.mesas[i % 4]
            if not mesa.ocupaciones.exists():
                self.crear_ocupacion(mesa, t(10, 0, 0))
            r = self.post(mesa_numero=mesa.numero, hora=t(10, 1 + i).isoformat(), uid=str(uuid.uuid4()))
            self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual(EventoEntrega.objects.count(), 10)
        self.assertEqual(EventoEntrega.objects.filter(personal__isnull=True).count(), 0)

    def test_sin_mesero_no_se_registra(self):
        r = self.vision.post("/api/eventos/entrega/", {"mesa_numero": 1}, format="json")
        self.assertEqual(r.status_code, 400)
        self.assertIn("personal_identificador", r.json())
        self.assertEqual(self.post(personal_identificador="NO-EXISTE").status_code, 400)
        self.assertEqual(EventoEntrega.objects.count(), 0)

    def test_personal_dado_de_baja_no_puede_recibir_entregas(self):
        self.mesero.activo = False
        self.mesero.save()
        self.assertEqual(self.post().status_code, 400)

    def test_una_mesa_puede_tener_varias_entregas_por_ocupacion(self):
        self.crear_mesero("MES-B", "Mesero B", "AZUL")
        self.post(hora=t(10, 3).isoformat())
        self.post(personal_identificador="MES-B", hora=t(10, 9).isoformat())
        self.post(hora=t(10, 15).isoformat())
        self.assertEqual(self.ocupacion.entregas.count(), 3)
        self.assertEqual(tiempo_espera_segundos(self.ocupacion), 180)  # RF-09: contra la PRIMERA entrega

    def test_entrega_sin_ocupacion_activa_se_rechaza(self):
        self.assertEqual(self.post(mesa_numero=2).status_code, 409)  # mesa 2 nunca se ocupó
        self.assertEqual(self.post(hora=t(9, 0).isoformat()).status_code, 409)  # antes de que se ocupara
        self.ocupacion.hora_fin = t(10, 30)
        self.ocupacion.estado = "LIBRE"
        self.ocupacion.save()
        self.assertEqual(self.post(hora=t(10, 45).isoformat()).status_code, 409)  # después de liberarse
        self.assertEqual(self.post(hora=t(10, 20).isoformat()).status_code, 201)  # reporte tardío pero dentro del intervalo

    def test_entrega_es_idempotente_por_uid(self):
        uid = str(uuid.uuid4())
        a, b = self.post(uid=uid), self.post(uid=uid)
        self.assertEqual((a.status_code, b.status_code), (201, 200))
        self.assertEqual(EventoEntrega.objects.count(), 1)

    def test_confianza_fuera_de_rango(self):
        self.assertEqual(self.post(confianza=1.5).status_code, 400)

    def test_tiempo_de_espera_coincide_con_el_calculo_manual(self):  # RF-09: margen ≤ 1 s
        self.post(hora=t(10, 4, 30).isoformat())
        self.assertEqual(tiempo_espera_segundos(self.ocupacion), 270.0)
        self.assertEqual(self.admin_client.get(f"/api/eventos/ocupacion/{self.ocupacion.pk}/").json()["espera_segundos"], 270.0)


class FusionTests(ApiTestBase):
    def setUp(self):
        super().setUp()
        _, self.mesas = self.crear_salon(6)
        for m in self.mesas[:5]:
            self.crear_ocupacion(m, t(10))

    def post(self, mesas, **extra):
        return self.vision.post("/api/eventos/fusion/", {"mesas": mesas, **extra}, format="json")

    def test_genera_un_unico_registro_para_las_mesas_fusionadas(self):  # RF-03
        r = self.post([4, 5])
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual(FusionMesas.objects.count(), 1)  # UNO, no uno por mesa
        f = FusionMesas.objects.get()
        self.assertCountEqual([m.numero for m in f.mesas.all()], [4, 5])
        self.assertEqual(f.ocupaciones.count(), 2)  # referencia a la ocupación de cada mesa
        self.assertEqual(r.json()["mesas_numeros"], [4, 5])
        self.assertLess(f.registrado_en - f.hora_evento, timedelta(seconds=5))

    def test_reintentos_no_duplican(self):
        uid = str(uuid.uuid4())
        self.post([4, 5], uid=uid)
        self.assertEqual(self.post([4, 5], uid=uid).status_code, 200)
        self.assertEqual(self.post([5, 4]).status_code, 200)  # mismo conjunto en otro orden, sin uid
        self.assertEqual(FusionMesas.objects.count(), 1)

    def test_una_fusion_que_crece_actualiza_el_mismo_registro(self):
        self.post([4, 5])
        r = self.post([4, 5, 3])
        self.assertEqual(r.status_code, 200)
        self.assertEqual(FusionMesas.objects.count(), 1)
        self.assertEqual(r.json()["mesas_numeros"], [3, 4, 5])
        self.assertEqual(FusionMesas.objects.get().ocupaciones.count(), 3)

    def test_fusiones_independientes_son_registros_distintos(self):
        self.post([1, 2])
        self.post([4, 5])
        self.assertEqual(FusionMesas.objects.count(), 2)

    def test_validaciones(self):
        self.assertEqual(self.post([4]).status_code, 400)  # una sola mesa no es fusión
        self.assertEqual(self.post([4, 4]).status_code, 400)  # repetidas
        self.assertEqual(self.post([4, 88]).status_code, 400)  # no existe
        self.assertEqual(self.post([4, 6]).status_code, 409)  # la 6 no está ocupada
        self.assertEqual(FusionMesas.objects.count(), 0)

    def test_finalizar_fusion_explicitamente(self):
        self.post([4, 5])
        r = self.vision.post("/api/eventos/fusion/finalizar/", {"mesas": [4]}, format="json")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(r.json()["finalizadas"]), 1)
        self.assertIsNotNone(FusionMesas.objects.get().hora_fin)
        self.assertEqual(self.post([4, 5]).status_code, 201)  # una fusión nueva es otro registro

    def test_la_fusion_termina_cuando_se_liberan_todas_sus_mesas(self):
        self.post([4, 5], hora_evento=t(10, 30).isoformat())
        cambiar = lambda n: self.vision.post(  # noqa: E731
            "/api/eventos/ocupacion/cambiar-estado/", {"mesa_numero": n, "estado": "LIBRE", "hora_fin": t(11).isoformat()}, format="json")
        cambiar(4)
        self.assertIsNone(FusionMesas.objects.get().hora_fin)  # aún queda la 5 ocupada
        cambiar(5)
        self.assertEqual(FusionMesas.objects.get().hora_fin, t(11))

    def test_historial_de_fusiones(self):
        self.post([4, 5])
        r = self.admin_client.get("/api/eventos/fusion/?activa=true")
        self.assertEqual(r.json()["count"], 1)


class ClasificacionPersonalTests(ApiTestBase):
    def test_registra_clasificacion_por_vestimenta_y_por_comportamiento(self):
        self.crear_mesero()
        r1 = self.vision.post("/api/eventos/personal-clasificado/", {
            "personal_identificador": "mes-a", "metodo": "VESTIMENTA", "confianza": 0.93, "track_id": 7}, format="json")
        r2 = self.vision.post("/api/eventos/personal-clasificado/", {
            "metodo": "COMPORTAMIENTO", "confianza": 0.6, "track_id": 8}, format="json")
        self.assertEqual((r1.status_code, r2.status_code), (201, 201), (r1.content, r2.content))
        self.assertEqual(ClasificacionPersonal.objects.count(), 2)
        self.assertEqual(self.admin_client.get("/api/eventos/personal-clasificado/?metodo=VESTIMENTA").json()["count"], 1)

    def test_rechaza_personal_inexistente_y_metodo_invalido(self):
        self.assertEqual(self.vision.post("/api/eventos/personal-clasificado/", {
            "personal_identificador": "ZZZ", "metodo": "VESTIMENTA", "confianza": 0.5}, format="json").status_code, 400)
        self.assertEqual(self.vision.post("/api/eventos/personal-clasificado/", {
            "metodo": "ADIVINANZA", "confianza": 0.5}, format="json").status_code, 400)
