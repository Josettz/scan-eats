# restaurante_api/scaneats/management/commands/purgar_evidencia.py
"""
RNF-03: borra automáticamente los clips cuya fecha de expiración ya pasó.

Programar a diario (cron en la EC2):
    0 3 * * *  cd /srv/restaurante_api && .venv/bin/python manage.py purgar_evidencia
Como red de seguridad, en S3 conviene además una regla de ciclo de vida que expire `evidencia/`
a los N días (N = EVIDENCIA_RETENCION_DIAS + 1).
"""
import logging

from django.core.management.base import BaseCommand
from django.utils import timezone

from scaneats.models import EvidenciaVideo
from scaneats.storage import obtener_storage

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Elimina del almacenamiento y de la base de datos los clips de evidencia vencidos."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="Solo informa qué se borraría.")

    def handle(self, *args, dry_run=False, **opciones):
        storage = obtener_storage()
        vencidas = EvidenciaVideo.objects.filter(expira_en__lte=timezone.now())
        borradas = fallidas = 0
        for ev in vencidas.iterator():
            if dry_run:
                self.stdout.write(f"[dry-run] {ev.s3_key} (venció {ev.expira_en:%Y-%m-%d %H:%M})")
                borradas += 1
                continue
            try:
                storage.borrar(ev.s3_key)
            except Exception:  # noqa: BLE001 - si falla el almacenamiento se conserva la fila para reintentar
                logger.exception("No se pudo borrar %s; se reintentará en la próxima ejecución.", ev.s3_key)
                fallidas += 1
                continue
            ev.delete()
            borradas += 1
        verbo = "se borrarían" if dry_run else "borradas"
        self.stdout.write(self.style.SUCCESS(f"Evidencias {verbo}: {borradas}. Fallidas: {fallidas}."))
