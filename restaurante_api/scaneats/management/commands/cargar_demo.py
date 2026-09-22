# restaurante_api/scaneats/management/commands/cargar_demo.py
"""
Carga datos de demostración alineados con vision_service/config/zonas_ejemplo.json:
12 mesas (como el local observado, Encebollados X), zona de salón, zona de exclusión de caja
y tres meseros con prenda distintiva. Crea también un Administrador si se indica --admin.
"""
import secrets

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.db import transaction

from scaneats.models import Mesa, Personal, Zona

CAPACIDADES = {1: 2, 2: 2, 3: 4, 4: 4, 5: 4, 6: 4, 7: 4, 8: 4, 9: 6, 10: 6, 11: 2, 12: 2}
MESEROS = [
    ("MES-A", "Mesero A", "DELANTAL", "ROJO"),
    ("MES-B", "Mesero B", "DELANTAL", "AZUL"),
    ("MES-C", "Mesero C", "DELANTAL", "VERDE"),
]


class Command(BaseCommand):
    help = "Carga mesas, zonas y personal de demostración (idempotente)."

    def add_arguments(self, parser):
        parser.add_argument("--admin", metavar="USUARIO", help="Crea/actualiza este Administrador.")
        parser.add_argument("--password", help="Contraseña del administrador (si se omite se genera una aleatoria).")

    @transaction.atomic
    def handle(self, *args, admin=None, password=None, **opciones):
        salon, _ = Zona.objects.get_or_create(nombre="Salón principal")
        caja, _ = Zona.objects.get_or_create(nombre="Caja", defaults={"es_exclusion": True})
        for numero, capacidad in CAPACIDADES.items():
            Mesa.objects.update_or_create(numero=numero, defaults={"capacidad": capacidad, "zona": salon})
        # La caja se registra como una "mesa" 99 dentro de la zona de exclusión (RF-04 exige ≥ 1 mesa por zona).
        Mesa.objects.update_or_create(numero=99, defaults={"capacidad": 1, "zona": caja})
        for ident, nombre, tipo, color in MESEROS:
            Personal.objects.update_or_create(
                identificador=ident, defaults={"nombre": nombre, "prenda_tipo": tipo, "prenda_color": color, "zona": salon}
            )
        self.stdout.write(self.style.SUCCESS("Demo cargada: 12 mesas + caja (mesa 99, exclusión) + 3 meseros."))

        if admin:
            User = get_user_model()
            password = password or secrets.token_urlsafe(12)
            usuario, _ = User.objects.get_or_create(username=admin, defaults={"is_staff": True, "is_superuser": True})
            usuario.is_staff = usuario.is_superuser = True
            usuario.set_password(password)
            usuario.save()
            self.stdout.write(f"Administrador: {admin}  contraseña: {password}")
