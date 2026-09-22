# restaurante_api/scaneats/tests/base.py
import tempfile
from datetime import datetime, timedelta, timezone as dt_tz

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import override_settings
from rest_framework.test import APIClient, APITestCase

from scaneats import services
from scaneats.models import EventoEntrega, EventoOcupacion, Mesa, Personal, Zona
from scaneats.storage import reiniciar_cache_storage

CLAVE = "Clave-Segura-123"
UTC = dt_tz.utc


def t(hora, minuto=0, segundo=0, dia=1):
    """Instante UTC de referencia (2026-03-<dia>) para armar datos de prueba deterministas."""
    return datetime(2026, 3, dia, hora, minuto, segundo, tzinfo=UTC)


class ApiTestBase(APITestCase):
    """Un Administrador (sesión), un usuario sin privilegios y el usuario de servicio con su token."""

    CLIP_BACKEND = "local"  # las pruebas de S3 lo cambian a "s3" (con boto3 simulado)

    @classmethod
    def setUpTestData(cls):
        User = get_user_model()
        cls.admin = User.objects.create_user("gerente", password=CLAVE, is_staff=True)
        cls.empleado_sin_rol = User.objects.create_user("cocinero", password=CLAVE)
        cls.servicio, cls.token = services.emitir_token_servicio()

    def setUp(self):
        cache.clear()  # el throttle de login usa la caché local
        reiniciar_cache_storage()
        self._clips = tempfile.TemporaryDirectory()
        self.addCleanup(self._clips.cleanup)
        override = override_settings(CLIP_STORAGE_BACKEND=self.CLIP_BACKEND, CLIP_LOCAL_ROOT=self._clips.name)
        override.enable()
        self.addCleanup(override.disable)
        self.admin_client = self.cliente_admin()
        self.vision = self.cliente_servicio()

    # -- clientes ----------------------------------------------------------- #
    def cliente_admin(self):
        c = APIClient()
        c.force_authenticate(user=self.admin)
        return c

    def cliente_servicio(self):
        c = APIClient()
        c.credentials(HTTP_AUTHORIZATION=f"Token {self.token.key}")
        return c

    # -- fábricas ----------------------------------------------------------- #
    def crear_salon(self, n=6, nombre="Salón"):
        zona = Zona.objects.create(nombre=nombre)
        mesas = [Mesa.objects.create(numero=i, capacidad=4, zona=zona) for i in range(1, n + 1)]
        return zona, mesas

    def crear_caja(self):
        zona = Zona.objects.create(nombre="Caja", es_exclusion=True)
        return zona, Mesa.objects.create(numero=99, capacidad=1, zona=zona)

    def crear_mesero(self, ident="MES-A", nombre="Mesero A", color="ROJO"):
        return Personal.objects.create(identificador=ident, nombre=nombre, prenda_tipo="DELANTAL", prenda_color=color)

    def crear_ocupacion(self, mesa, inicio, fin=None, estado=None):
        estado = estado or (EventoOcupacion.Estado.LIBRE if fin else EventoOcupacion.Estado.OCUPADA)
        return EventoOcupacion.objects.create(
            mesa=mesa, zona=mesa.zona, hora_inicio=inicio, hora_fin=fin, estado=estado, detectado_en=inicio,
        )

    def crear_entrega(self, ocupacion, personal, hora):
        return EventoEntrega.objects.create(ocupacion=ocupacion, personal=personal, hora=hora)

    @staticmethod
    def segundos(n):
        return timedelta(seconds=n)
