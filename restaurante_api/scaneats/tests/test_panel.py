# restaurante_api/scaneats/tests/test_panel.py
"""Panel del administrador en la raíz (/): mismas métricas que la API, solo para personal con sesión."""
import re
from datetime import timedelta

from django.utils import timezone
from django.test import Client

from scaneats.models import EvidenciaVideo

from .base import ApiTestBase, t


class PanelTests(ApiTestBase):
    def cliente_con_sesion(self):
        c = Client()
        c.force_login(self.admin)
        return c

    def datos(self):
        _, (m1, m2, *_) = self.crear_salon(3)
        ana = self.crear_mesero("A", "Ana")
        o1 = self.crear_ocupacion(m1, t(15, 0), t(15, 30))
        self.crear_entrega(o1, ana, t(15, 2, 30))
        self.crear_ocupacion(m2, t(15, 10), t(15, 40))
        return o1

    def test_sin_sesion_redirige_al_login(self):  # RF-13: nada se ve sin autenticarse
        r = Client().get("/")
        self.assertEqual(r.status_code, 302)
        self.assertIn("/admin/login/", r["Location"])

    def test_usuario_sin_rol_de_personal_no_entra(self):
        c = Client()
        c.force_login(self.empleado_sin_rol)
        self.assertEqual(c.get("/").status_code, 302)

    def test_el_servicio_de_vision_no_ve_el_panel(self):
        c = Client()
        c.force_login(self.servicio)
        self.assertEqual(c.get("/").status_code, 302)

    def test_muestra_las_tres_metricas_con_los_datos(self):
        self.datos()
        r = self.cliente_con_sesion().get("/")
        self.assertEqual(r.status_code, 200)
        html = r.content.decode()
        self.assertIn("Tiempo de espera promedio", html)
        self.assertIn("2:30", html)  # 150 s de espera, en min:seg
        self.assertIn("Ana", html)  # empleado con más mesas
        self.assertIn("Mesa 1", html)  # tabla de uso
        self.assertIn("2 · Empleado con más mesas atendidas", html)

    def test_sin_datos_no_falla(self):
        r = self.cliente_con_sesion().get("/")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Aún no hay ocupaciones", r.content.decode())

    def test_filtra_por_fecha_y_valida_el_formato(self):
        self.datos()
        c = self.cliente_con_sesion()
        self.assertIn("Sin ocupaciones en el periodo", c.get("/?desde=2030-01-01").content.decode())
        r = c.get("/?desde=ayer")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Formato inválido", r.content.decode())

    def test_lista_los_clips_vigentes_y_oculta_los_vencidos(self):
        o1 = self.datos()
        EvidenciaVideo.objects.create(ocupacion=o1, s3_key="k1.mp4", duracion_s=16, expira_en=timezone.now() + timedelta(days=5))
        EvidenciaVideo.objects.create(ocupacion=o1, s3_key="k2.mp4", duracion_s=9, expira_en=timezone.now() - timedelta(days=1))
        html = self.cliente_con_sesion().get("/").content.decode()
        self.assertEqual(len(set(re.findall(r"/api/evidencia/clip/\d+/descargar/", html))), 1)  # solo el vigente (RNF-03)
        self.assertIn("Ver clip (16 s)", html)
        self.assertIn("<video", html)  # visor incrustado
