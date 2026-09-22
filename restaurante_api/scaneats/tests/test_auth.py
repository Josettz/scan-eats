# restaurante_api/scaneats/tests/test_auth.py
"""RF-13 (autenticación) y RNF-05 (hash con sal, sesión de 30 min)."""
import time
from datetime import timedelta

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.sessions.models import Session
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from .base import CLAVE, ApiTestBase


class LoginTests(ApiTestBase):
    url = "/api/auth/login/"

    def test_credenciales_validas_dan_acceso_en_menos_de_2_segundos(self):
        cliente = APIClient()
        t0 = time.perf_counter()
        r = cliente.post(self.url, {"username": "gerente", "password": CLAVE}, format="json")
        self.assertEqual(r.status_code, 200)
        self.assertLess(time.perf_counter() - t0, 2.0)  # RF-13: ≤ 2 s
        self.assertEqual(cliente.get("/api/auth/me/").status_code, 200)  # la sesión queda abierta

    def test_credenciales_invalidas_se_rechazan_sin_revelar_que_dato_fallo(self):
        respuestas = [
            APIClient().post(self.url, {"username": "gerente", "password": "incorrecta"}, format="json"),
            APIClient().post(self.url, {"username": "no_existe", "password": CLAVE}, format="json"),
            APIClient().post(self.url, {"username": "cocinero", "password": CLAVE}, format="json"),  # sin rol de admin
            APIClient().post(self.url, {"username": "gerente"}, format="json"),  # falta la contraseña
        ]
        for r in respuestas:
            self.assertEqual(r.status_code, 401)
        cuerpos = {str(r.json()) for r in respuestas}
        self.assertEqual(len(cuerpos), 1, "El mensaje debe ser idéntico en todos los fallos")
        self.assertNotIn("contraseña", cuerpos.pop().lower().replace("credenciales", ""))

    def test_usuario_de_servicio_no_puede_iniciar_sesion(self):
        r = APIClient().post(self.url, {"username": self.servicio.username, "password": ""}, format="json")
        self.assertEqual(r.status_code, 401)

    def test_usuario_inactivo_no_puede_iniciar_sesion(self):
        User = get_user_model()
        User.objects.create_user("baja", password=CLAVE, is_staff=True, is_active=False)
        r = APIClient().post(self.url, {"username": "baja", "password": CLAVE}, format="json")
        self.assertEqual(r.status_code, 401)

    def test_login_limita_intentos_repetidos(self):
        codigos = [
            APIClient().post(self.url, {"username": "gerente", "password": "x"}, format="json").status_code
            for _ in range(12)
        ]
        self.assertIn(429, codigos)


class SeguridadContrasenaYSesionTests(ApiTestBase):
    def test_contrasena_almacenada_con_hash_y_sal(self):  # RNF-05
        hash1 = self.admin.password
        self.assertTrue(hash1.startswith("pbkdf2_sha256$"))
        self.assertNotIn(CLAVE, hash1)
        otro = get_user_model().objects.create_user("otro", password=CLAVE)
        self.assertNotEqual(hash1.split("$")[2], otro.password.split("$")[2], "cada usuario tiene su propia sal")

    def test_sesion_configurada_para_expirar_a_los_30_minutos_de_inactividad(self):
        self.assertEqual(settings.SESSION_COOKIE_AGE, 30 * 60)
        self.assertTrue(settings.SESSION_SAVE_EVERY_REQUEST)  # plazo deslizante, no desde el login

    def test_actividad_renueva_la_caducidad_de_la_sesion(self):
        cliente = APIClient()
        cliente.post("/api/auth/login/", {"username": "gerente", "password": CLAVE}, format="json")
        Session.objects.update(expire_date=timezone.now() + timedelta(seconds=60))
        cliente.get("/api/auth/me/")
        restante = Session.objects.get().expire_date - timezone.now()
        self.assertGreater(restante, timedelta(minutes=29))

    def test_sesion_vencida_por_inactividad_es_rechazada(self):
        cliente = APIClient()
        cliente.post("/api/auth/login/", {"username": "gerente", "password": CLAVE}, format="json")
        self.assertEqual(cliente.get("/api/auth/me/").status_code, 200)
        Session.objects.update(expire_date=timezone.now() - timedelta(minutes=1))  # pasaron > 30 min sin actividad
        self.assertEqual(cliente.get("/api/auth/me/").status_code, 401)

    def test_logout_cierra_la_sesion(self):
        cliente = APIClient()
        cliente.post("/api/auth/login/", {"username": "gerente", "password": CLAVE}, format="json")
        self.assertEqual(cliente.post("/api/auth/logout/").status_code, 204)
        self.assertEqual(cliente.get("/api/auth/me/").status_code, 401)


class AccesoObligatorioTests(ApiTestBase):
    """RF-13: NINGÚN endpoint (salvo login y la sonda /health/) responde sin autenticación."""

    def test_todos_los_endpoints_exigen_autenticacion(self):
        anonimo = APIClient()
        get_names = [
            ("zona-list", []), ("mesa-list", []), ("personal-list", []), ("ocupacion-list", []),
            ("entrega-list", []), ("fusion-list", []), ("clasificacion-list", []),
            ("metricas-tiempo-espera", []), ("metricas-empleados", []), ("metricas-uso-mesas", []),
            ("evidencia-consultar", [1]), ("auth-me", []), ("vision-configuracion", []), ("auth-servicio-validar", []),
        ]
        for nombre, args in get_names:
            r = anonimo.get(reverse(nombre, args=args))
            self.assertEqual(r.status_code, 401, f"GET {nombre} debería exigir autenticación")
        for nombre in ["zona-list", "mesa-list", "personal-list", "ocupacion-list", "entrega-list", "fusion-list",
                       "clasificacion-list", "auth-servicio-token", "evidencia-subir", "auth-logout"]:
            r = anonimo.post(reverse(nombre), {}, format="json")
            self.assertEqual(r.status_code, 401, f"POST {nombre} debería exigir autenticación")
        self.assertEqual(anonimo.post(reverse("ocupacion-cambiar-estado"), {}, format="json").status_code, 401)
        self.assertEqual(anonimo.post(reverse("fusion-finalizar"), {}, format="json").status_code, 401)
        self.assertEqual(anonimo.delete(reverse("mesa-detail", args=[1])).status_code, 401)

    def test_token_invalido_se_rechaza(self):
        c = APIClient()
        c.credentials(HTTP_AUTHORIZATION="Token 0000000000000000000000000000000000000000")
        self.assertEqual(c.get("/api/auth/servicio/validar/").status_code, 401)

    def test_la_sonda_de_salud_es_abierta_y_no_expone_datos(self):
        r = APIClient().get("/health/")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json(), {"status": "ok"})

    def test_roles_separados(self):
        # El microservicio no puede leer configuración ni reportes...
        self.assertEqual(self.vision.get("/api/mesas/").status_code, 403)
        self.assertEqual(self.vision.get("/api/metricas/uso-mesas/").status_code, 403)
        # ...y el Administrador no reporta eventos de visión.
        self.assertEqual(self.admin_client.post("/api/eventos/ocupacion/", {"mesa_numero": 1}, format="json").status_code, 403)


class TokenServicioTests(ApiTestBase):
    def test_token_de_servicio_valida(self):
        r = self.vision.get("/api/auth/servicio/validar/")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["valido"])

    def test_admin_rota_el_token_y_el_anterior_deja_de_valer(self):
        viejo = self.token.key
        r = self.admin_client.post("/api/auth/servicio/token/")
        self.assertEqual(r.status_code, 201)
        nuevo = r.json()["token"]
        self.assertNotEqual(viejo, nuevo)
        c_nuevo = APIClient()
        c_nuevo.credentials(HTTP_AUTHORIZATION=f"Token {nuevo}")
        self.assertEqual(c_nuevo.get("/api/auth/servicio/validar/").status_code, 200)
        self.assertEqual(self.vision.get("/api/auth/servicio/validar/").status_code, 401)

    def test_solo_el_admin_emite_tokens(self):
        self.assertEqual(self.vision.post("/api/auth/servicio/token/").status_code, 403)

    def test_usuario_de_servicio_no_tiene_contrasena_utilizable(self):
        self.assertFalse(self.servicio.has_usable_password())
