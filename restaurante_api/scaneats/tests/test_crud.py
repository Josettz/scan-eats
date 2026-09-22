# restaurante_api/scaneats/tests/test_crud.py
"""RF-04, RF-05, RF-06, RF-15: CRUD de configuración y sus reglas de validación."""
from scaneats.models import Mesa, Personal, Zona

from .base import ApiTestBase, t


class ZonaTests(ApiTestBase):
    def test_zona_sin_mesas_se_rechaza_con_mensaje(self):  # RF-04
        r = self.admin_client.post("/api/zonas/", {"nombre": "Terraza"}, format="json")
        self.assertEqual(r.status_code, 400)
        self.assertIn("al menos una mesa", str(r.json()["mesas"]))
        r = self.admin_client.post("/api/zonas/", {"nombre": "Terraza", "mesas": []}, format="json")
        self.assertEqual(r.status_code, 400)
        self.assertFalse(Zona.objects.filter(nombre="Terraza").exists())

    def test_zona_con_mesas_se_persiste_y_aparece_en_el_listado(self):  # RF-04
        m1, m2 = Mesa.objects.create(numero=1, capacidad=4), Mesa.objects.create(numero=2, capacidad=4)
        r = self.admin_client.post("/api/zonas/", {"nombre": "Frente", "mesas": [m1.pk, m2.pk]}, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        listado = self.admin_client.get("/api/zonas/").json()["results"]
        self.assertEqual([z["nombre"] for z in listado], ["Frente"])
        self.assertCountEqual(listado[0]["mesas"], [m1.pk, m2.pk])
        m1.refresh_from_db()
        self.assertEqual(m1.zona.nombre, "Frente")

    def test_zona_de_exclusion_de_caja(self):  # RF-15
        caja = Mesa.objects.create(numero=99, capacidad=1)
        r = self.admin_client.post(
            "/api/zonas/",
            {"nombre": "Caja", "es_exclusion": True, "mesas": [caja.pk], "region": [[0.0, 0.0], [0.2, 0.0], [0.2, 0.3]]},
            format="json",
        )
        self.assertEqual(r.status_code, 201, r.content)
        self.assertTrue(r.json()["es_exclusion"])
        self.assertEqual(self.admin_client.get("/api/zonas/?es_exclusion=true").json()["count"], 1)

    def test_region_invalida_se_rechaza(self):
        m = Mesa.objects.create(numero=1, capacidad=2)
        for region in ([[0, 0], [1, 1]], [[0, 0], [1, 1], [2, 5]], [[0, 0], [1, "a"], [1, 1]]):
            r = self.admin_client.post("/api/zonas/", {"nombre": "Z", "mesas": [m.pk], "region": region}, format="json")
            self.assertEqual(r.status_code, 400, region)

    def test_no_se_puede_vaciar_una_zona_al_editar(self):
        zona, mesas = self.crear_salon(2)
        r = self.admin_client.patch(f"/api/zonas/{zona.pk}/", {"mesas": []}, format="json")
        self.assertEqual(r.status_code, 400)
        r = self.admin_client.put(f"/api/zonas/{zona.pk}/", {"nombre": "Nuevo"}, format="json")  # PUT exige mesas
        self.assertEqual(r.status_code, 400)

    def test_editar_zona_reasigna_mesas(self):
        zona, (m1, m2) = self.crear_salon(2)
        m3 = Mesa.objects.create(numero=3, capacidad=4)
        r = self.admin_client.patch(f"/api/zonas/{zona.pk}/", {"mesas": [m2.pk, m3.pk]}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        m1.refresh_from_db()
        self.assertIsNone(m1.zona)
        self.assertEqual(Mesa.objects.get(pk=m3.pk).zona, zona)

    def test_mover_mesa_no_puede_dejar_otra_zona_vacia(self):
        zona_a, (a1,) = self.crear_salon(1, "A")
        m = Mesa.objects.create(numero=50, capacidad=2)
        r = self.admin_client.post("/api/zonas/", {"nombre": "B", "mesas": [m.pk, a1.pk]}, format="json")
        self.assertEqual(r.status_code, 400)
        self.assertIn("quedaría sin mesas", str(r.json()))

    def test_nombre_duplicado_se_rechaza(self):
        self.crear_salon(1, "Salón")
        m = Mesa.objects.create(numero=9, capacidad=2)
        r = self.admin_client.post("/api/zonas/", {"nombre": "salón", "mesas": [m.pk]}, format="json")
        self.assertEqual(r.status_code, 400)

    def test_eliminar_zona_libera_sus_mesas(self):
        zona, (m1,) = self.crear_salon(1)
        self.assertEqual(self.admin_client.delete(f"/api/zonas/{zona.pk}/").status_code, 204)
        m1.refresh_from_db()
        self.assertIsNone(m1.zona)


class MesaTests(ApiTestBase):
    def test_crud_completo(self):
        r = self.admin_client.post("/api/mesas/", {"numero": 7, "capacidad": 4}, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        pk = r.json()["id"]
        self.assertEqual(self.admin_client.get(f"/api/mesas/{pk}/").json()["numero"], 7)
        self.assertEqual(self.admin_client.patch(f"/api/mesas/{pk}/", {"capacidad": 6}, format="json").json()["capacidad"], 6)
        r = self.admin_client.put(f"/api/mesas/{pk}/", {"numero": 8, "capacidad": 2}, format="json")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.admin_client.delete(f"/api/mesas/{pk}/").status_code, 204)
        self.assertEqual(self.admin_client.get(f"/api/mesas/{pk}/").status_code, 404)

    def test_campos_obligatorios_e_invalidos_se_validan(self):  # RF-05: 100 % de los casos
        casos = [
            ({}, {"numero", "capacidad"}),
            ({"numero": 1}, {"capacidad"}),
            ({"capacidad": 4}, {"numero"}),
            ({"numero": 0, "capacidad": 4}, {"numero"}),
            ({"numero": -3, "capacidad": 4}, {"numero"}),
            ({"numero": 1, "capacidad": 0}, {"capacidad"}),
            ({"numero": 1, "capacidad": 999}, {"capacidad"}),
            ({"numero": "abc", "capacidad": 4}, {"numero"}),
            ({"numero": 1, "capacidad": 4, "zona": 12345}, {"zona"}),
        ]
        for datos, campos in casos:
            r = self.admin_client.post("/api/mesas/", datos, format="json")
            self.assertEqual(r.status_code, 400, datos)
            self.assertEqual(set(r.json()), campos, datos)
        self.assertEqual(Mesa.objects.count(), 0)

    def test_numero_duplicado(self):
        Mesa.objects.create(numero=5, capacidad=4)
        r = self.admin_client.post("/api/mesas/", {"numero": 5, "capacidad": 2}, format="json")
        self.assertEqual(r.status_code, 400)

    def test_listado_paginado_y_filtros(self):
        zona, _ = self.crear_salon(3)
        Mesa.objects.create(numero=20, capacidad=8)
        r = self.admin_client.get("/api/mesas/?page_size=2").json()
        self.assertEqual((r["count"], len(r["results"])), (4, 2))
        self.assertIsNotNone(r["next"])
        self.assertEqual(self.admin_client.get(f"/api/mesas/?zona={zona.pk}").json()["count"], 3)
        self.assertEqual(self.admin_client.get("/api/mesas/?search=20").json()["count"], 1)
        self.assertEqual(self.admin_client.get("/api/mesas/?capacidad_min=8").json()["count"], 1)

    def test_no_se_puede_mover_ni_borrar_la_unica_mesa_de_una_zona(self):
        zona, (unica,) = self.crear_salon(1)
        r = self.admin_client.patch(f"/api/mesas/{unica.pk}/", {"zona": None}, format="json")
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self.admin_client.delete(f"/api/mesas/{unica.pk}/").status_code, 409)

    def test_mesa_con_historial_no_se_elimina(self):
        _, (m1, m2) = self.crear_salon(2)
        self.crear_ocupacion(m1, t(10), t(11))
        r = self.admin_client.delete(f"/api/mesas/{m1.pk}/")
        self.assertEqual(r.status_code, 409)
        self.assertTrue(Mesa.objects.filter(pk=m1.pk).exists())


class PersonalTests(ApiTestBase):
    def datos(self, **extra):
        base = {"identificador": "MES-A", "nombre": "Ana", "turno": "MANANA", "prenda_tipo": "DELANTAL", "prenda_color": "ROJO"}
        return {**base, **extra}

    def test_crud_completo(self):
        r = self.admin_client.post("/api/personal/", self.datos(), format="json")
        self.assertEqual(r.status_code, 201, r.content)
        pk = r.json()["id"]
        self.assertEqual(r.json()["prenda_color"], "ROJO")
        self.assertEqual(self.admin_client.patch(f"/api/personal/{pk}/", {"nombre": "Ana P."}, format="json").json()["nombre"], "Ana P.")
        self.assertEqual(self.admin_client.get(f"/api/personal/{pk}/").status_code, 200)
        self.assertEqual(self.admin_client.delete(f"/api/personal/{pk}/").status_code, 204)

    def test_identificacion_duplicada_se_rechaza(self):  # RF-06
        self.assertEqual(self.admin_client.post("/api/personal/", self.datos(), format="json").status_code, 201)
        for ident in ("MES-A", "mes-a", "  Mes-A  "):  # también variantes de mayúsculas/espacios
            r = self.admin_client.post("/api/personal/", self.datos(identificador=ident, nombre="Otra"), format="json")
            self.assertEqual(r.status_code, 400, ident)
            self.assertIn("identificación", str(r.json()["identificador"]))
        self.assertEqual(Personal.objects.count(), 1)

    def test_editar_no_puede_pisar_la_identificacion_de_otro(self):
        self.admin_client.post("/api/personal/", self.datos(), format="json")
        r = self.admin_client.post("/api/personal/", self.datos(identificador="MES-B", nombre="Beto"), format="json")
        r = self.admin_client.patch(f"/api/personal/{r.json()['id']}/", {"identificador": "MES-A"}, format="json")
        self.assertEqual(r.status_code, 400)

    def test_campos_obligatorios(self):
        for datos in ({}, {"identificador": "X1"}, {"nombre": "Sin id"}, {"identificador": " ", "nombre": "N"}):
            self.assertEqual(self.admin_client.post("/api/personal/", datos, format="json").status_code, 400, datos)

    def test_prenda_invalida_se_rechaza(self):
        r = self.admin_client.post("/api/personal/", self.datos(prenda_color="FUCSIA"), format="json")
        self.assertEqual(r.status_code, 400)

    def test_filtros_de_listado(self):
        self.crear_mesero("A1", "Ana")
        self.crear_mesero("B2", "Beto", "AZUL")
        Personal.objects.filter(identificador="B2").update(activo=False)
        self.assertEqual(self.admin_client.get("/api/personal/?search=ana").json()["count"], 1)
        self.assertEqual(self.admin_client.get("/api/personal/?activo=false").json()["count"], 1)
        self.assertEqual(self.admin_client.get("/api/personal/?prenda_color=AZUL").json()["count"], 1)

    def test_empleado_con_entregas_se_da_de_baja_en_vez_de_borrarse(self):
        _, (mesa, *_) = self.crear_salon(1)
        p = self.crear_mesero()
        self.crear_entrega(self.crear_ocupacion(mesa, t(10)), p, t(10, 5))
        self.assertEqual(self.admin_client.delete(f"/api/personal/{p.pk}/").status_code, 409)
        r = self.admin_client.patch(f"/api/personal/{p.pk}/", {"activo": False}, format="json")
        self.assertFalse(r.json()["activo"])


class ConfiguracionVisionTests(ApiTestBase):
    def test_entrega_zonas_mesas_y_personal_a_la_vision(self):
        zona, mesas = self.crear_salon(2)
        self.crear_caja()
        self.crear_mesero()
        r = self.vision.get("/api/vision/configuracion/")
        self.assertEqual(r.status_code, 200)
        cfg = r.json()
        self.assertEqual({z["nombre"]: z["es_exclusion"] for z in cfg["zonas"]}, {"Salón": False, "Caja": True})
        self.assertEqual(len(cfg["mesas"]), 3)
        self.assertEqual(cfg["personal"][0]["prenda_color"], "ROJO")
