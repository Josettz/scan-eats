# restaurante_api/scaneats/tests/test_comandos.py
"""Comandos de gestión y configuración crítica (RNF-03, RF-13)."""
import io
import os
import subprocess
import sys
from pathlib import Path

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from scaneats.models import Mesa, Personal, Zona

RAIZ_API = Path(__file__).resolve().parent.parent.parent  # restaurante_api/


class CargarDemoTests(TestCase):
    def ejecutar(self, *args):
        salida = io.StringIO()
        call_command("cargar_demo", *args, stdout=salida)
        return salida.getvalue()

    def test_carga_el_local_de_ejemplo(self):
        self.ejecutar()
        self.assertEqual(Mesa.objects.exclude(zona__es_exclusion=True).count(), 12)
        caja = Zona.objects.get(nombre="Caja")
        self.assertTrue(caja.es_exclusion)
        self.assertEqual(list(caja.mesas.values_list("numero", flat=True)), [99])
        self.assertEqual(
            {p.identificador: p.prenda_color for p in Personal.objects.all()}, {"MES-A": "ROJO", "MES-B": "AZUL", "MES-C": "VERDE"}
        )

    def test_es_idempotente(self):
        self.ejecutar()
        self.ejecutar()
        self.assertEqual((Mesa.objects.count(), Personal.objects.count(), Zona.objects.count()), (13, 3, 2))

    def test_crea_un_administrador_con_contrasena_hasheada(self):
        salida = self.ejecutar("--admin", "gerente", "--password", "Clave-Demo-123")
        u = get_user_model().objects.get(username="gerente")
        self.assertTrue(u.is_staff and u.check_password("Clave-Demo-123"))
        self.assertNotIn("Clave-Demo-123", u.password)
        self.assertIn("gerente", salida)
        r = APIClient().post("/api/auth/login/", {"username": "gerente", "password": "Clave-Demo-123"}, format="json")
        self.assertEqual(r.status_code, 200)

    def test_sin_password_genera_una_aleatoria(self):
        salida = self.ejecutar("--admin", "gerente2")
        clave = salida.split("contraseña:")[1].strip()
        self.assertGreaterEqual(len(clave), 12)
        self.assertTrue(get_user_model().objects.get(username="gerente2").check_password(clave))


class CrearServicioVisionTests(TestCase):
    def emitir(self):
        salida = io.StringIO()
        call_command("crear_servicio_vision", stdout=salida)
        return [l for l in salida.getvalue().splitlines() if l.startswith("VISION_API_TOKEN=")][0].split("=", 1)[1]

    def test_emite_un_token_que_autentica_al_microservicio(self):
        token = self.emitir()
        c = APIClient()
        c.credentials(HTTP_AUTHORIZATION=f"Token {token}")
        self.assertEqual(c.get("/api/auth/servicio/validar/").status_code, 200)

    def test_reejecutarlo_rota_el_token(self):
        viejo, nuevo = self.emitir(), self.emitir()
        self.assertNotEqual(viejo, nuevo)
        self.assertEqual(Token.objects.count(), 1)
        c = APIClient()
        c.credentials(HTTP_AUTHORIZATION=f"Token {viejo}")
        self.assertEqual(c.get("/api/auth/servicio/validar/").status_code, 401)


class ConfiguracionCriticaTests(TestCase):
    def cargar_settings(self, **env):
        entorno = {**os.environ, "DJANGO_DEBUG": "True", **env}
        return subprocess.run(
            [sys.executable, "-c", "import config.settings as s; print(s.EVIDENCIA_RETENCION_DIAS)"],
            capture_output=True, text=True, cwd=RAIZ_API, env=entorno, timeout=60,
        )

    def test_la_retencion_no_puede_ser_menor_a_30_dias(self):  # RNF-03
        r = self.cargar_settings(EVIDENCIA_RETENCION_DIAS="29")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("RNF-03", r.stderr)
        self.assertEqual(self.cargar_settings(EVIDENCIA_RETENCION_DIAS="30").stdout.strip(), "30")
        self.assertEqual(self.cargar_settings(EVIDENCIA_RETENCION_DIAS="90").stdout.strip(), "90")

    def test_en_produccion_exige_una_clave_secreta(self):
        # Valores explícitos (vacíos) para que un .env local no los rellene: el cargador nunca pisa variables ya definidas.
        env = {**os.environ, "DJANGO_SECRET_KEY": "", "DJANGO_DEBUG": "False"}
        r = subprocess.run(
            [sys.executable, "-c", "import config.settings"], capture_output=True, text=True, cwd=RAIZ_API, env=env, timeout=60
        )
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("DJANGO_SECRET_KEY", r.stderr)
