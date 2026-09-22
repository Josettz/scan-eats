# restaurante_api/scaneats/storage.py
"""
Almacenamiento de clips de evidencia (RF-14, RNF-03).

Dos implementaciones con la misma interfaz:
  * LocalClipStorage -> disco (desarrollo y tests).
  * S3ClipStorage    -> Amazon S3 (producción). boto3 se importa de forma perezosa.

`CLIP_STORAGE_BACKEND` decide cuál se usa.
"""
import logging
import os
from pathlib import Path

from django.conf import settings

logger = logging.getLogger(__name__)


class LocalClipStorage:
    nombre = "local"

    def __init__(self):
        self.raiz = Path(settings.CLIP_LOCAL_ROOT)

    def _ruta(self, key):
        ruta = (self.raiz / key).resolve()
        if self.raiz.resolve() not in ruta.parents:  # evita path traversal
            raise ValueError("Clave de almacenamiento inválida.")
        return ruta

    def guardar(self, key, fileobj, content_type):
        ruta = self._ruta(key)
        ruta.parent.mkdir(parents=True, exist_ok=True)
        with open(ruta, "wb") as destino:
            for chunk in iter(lambda: fileobj.read(1024 * 1024), b""):
                destino.write(chunk)

    def abrir(self, key):
        return open(self._ruta(key), "rb")

    def existe(self, key):
        return self._ruta(key).exists()

    def borrar(self, key):
        try:
            os.remove(self._ruta(key))
        except FileNotFoundError:
            pass

    def url_firmada(self, key, expira_s):
        return None  # los clips locales se sirven por la vista autenticada de descarga


class S3ClipStorage:
    nombre = "s3"

    def __init__(self):
        if not settings.AWS_STORAGE_BUCKET_NAME:
            raise RuntimeError("Defina AWS_STORAGE_BUCKET_NAME para usar CLIP_STORAGE_BACKEND=s3.")
        self.bucket = settings.AWS_STORAGE_BUCKET_NAME
        self._cliente = None

    def _crear_cliente(self):
        import boto3  # perezoso: no es necesario en desarrollo local

        # Credenciales por la cadena estándar de boto3 (rol IAM de la instancia EC2).
        return boto3.client("s3", region_name=settings.AWS_S3_REGION_NAME)

    @property
    def cliente(self):
        if self._cliente is None:
            self._cliente = self._crear_cliente()
        return self._cliente

    def guardar(self, key, fileobj, content_type):
        self.cliente.upload_fileobj(
            fileobj, self.bucket, key,
            ExtraArgs={"ContentType": content_type, "ServerSideEncryption": "AES256"},
        )

    def existe(self, key):
        try:
            self.cliente.head_object(Bucket=self.bucket, Key=key)
            return True
        except Exception:  # noqa: BLE001 - botocore ClientError 404
            return False

    def borrar(self, key):
        self.cliente.delete_object(Bucket=self.bucket, Key=key)

    def url_firmada(self, key, expira_s):
        return self.cliente.generate_presigned_url(
            "get_object", Params={"Bucket": self.bucket, "Key": key}, ExpiresIn=expira_s
        )


_INSTANCIAS = {}


def obtener_storage():
    clave = (settings.CLIP_STORAGE_BACKEND, settings.AWS_STORAGE_BUCKET_NAME, str(settings.CLIP_LOCAL_ROOT))
    if clave not in _INSTANCIAS:
        if settings.CLIP_STORAGE_BACKEND == "s3":
            _INSTANCIAS[clave] = S3ClipStorage()
        elif settings.CLIP_STORAGE_BACKEND == "local":
            _INSTANCIAS[clave] = LocalClipStorage()
        else:
            raise RuntimeError(f"CLIP_STORAGE_BACKEND desconocido: {settings.CLIP_STORAGE_BACKEND!r}")
    return _INSTANCIAS[clave]


def reiniciar_cache_storage():
    _INSTANCIAS.clear()
