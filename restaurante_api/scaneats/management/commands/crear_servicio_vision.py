# restaurante_api/scaneats/management/commands/crear_servicio_vision.py
from django.core.management.base import BaseCommand

from scaneats import services


class Command(BaseCommand):
    help = (
        "Crea (si falta) el usuario de servicio del Microservicio de Visión y emite un token NUEVO "
        "(el anterior deja de valer). Copie el token a VISION_API_TOKEN en vision_service/.env."
    )

    def handle(self, *args, **opciones):
        usuario, token = services.emitir_token_servicio()
        self.stdout.write(self.style.SUCCESS(f"Usuario de servicio: {usuario.username}"))
        self.stdout.write(f"VISION_API_TOKEN={token.key}")
        self.stdout.write("Se muestra una sola vez; para rotarlo vuelva a ejecutar este comando.")
