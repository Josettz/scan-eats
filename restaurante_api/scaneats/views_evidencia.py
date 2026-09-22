# restaurante_api/scaneats/views_evidencia.py
"""Evidencia de video (RF-14, RNF-03)."""
import os
import re
import uuid

from django.conf import settings
from django.http import FileResponse, Http404, HttpResponse, HttpResponseRedirect, StreamingHttpResponse
from django.urls import reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.exceptions import NotFound, ValidationError
from rest_framework.parsers import MultiPartParser
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import EventoEntrega, EventoOcupacion, EvidenciaVideo, FusionMesas
from .permissions import EsAdministrador, EsServicioVision
from .serializers import EvidenciaSerializer, EvidenciaSubirSerializer
from .storage import obtener_storage

MODELO_POR_TIPO = {"ocupacion": EventoOcupacion, "entrega": EventoEntrega, "fusion": FusionMesas}
EXTENSIONES = {".mp4", ".webm", ".mkv", ".avi", ".mov"}


def _evidencias_vigentes(tipo, evento_id):
    return EvidenciaVideo.objects.filter(**{f"{tipo}_id": evento_id}, expira_en__gt=timezone.now())


class EvidenciaSubirView(APIView):
    """
    POST /api/evidencia/  (multipart) — el microservicio sube el clip de un evento.
    Campos: clip (archivo), tipo (ocupacion|entrega|fusion), evento_id | evento_uid, timestamp?, duracion_s?
    """

    permission_classes = [EsServicioVision]
    parser_classes = [MultiPartParser]

    def post(self, request):
        s = EvidenciaSubirSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        datos = s.validated_data
        tipo, clip = datos["tipo"], datos["clip"]

        if "evento_uid" in datos:
            evento = MODELO_POR_TIPO[tipo].objects.filter(uid=datos["evento_uid"]).first()
            referencia = f"uid {datos['evento_uid']}"
        else:
            evento = MODELO_POR_TIPO[tipo].objects.filter(pk=datos["evento_id"]).first()
            referencia = f"id {datos['evento_id']}"
        if evento is None:
            raise NotFound(f"No existe el evento de {tipo} con {referencia}.")
        evento_id = evento.pk
        if clip.size > settings.CLIP_MAX_BYTES:
            raise ValidationError({"clip": f"El clip excede el máximo de {settings.CLIP_MAX_BYTES // (1024 * 1024)} MB."})
        content_type = getattr(clip, "content_type", "") or ""
        if not content_type.startswith("video/"):
            raise ValidationError({"clip": "El archivo debe ser un video (Content-Type video/*)."})

        extension = os.path.splitext(clip.name or "")[1].lower()
        extension = extension if extension in EXTENSIONES else ".mp4"
        key = f"evidencia/{tipo}/{evento_id}/{uuid.uuid4().hex}{extension}"
        clip.seek(0)
        obtener_storage().guardar(key, clip, content_type)

        evidencia = EvidenciaVideo.objects.create(
            **{tipo: evento},
            s3_key=key,
            timestamp=datos.get("timestamp") or timezone.now(),
            duracion_s=datos.get("duracion_s"),
            tamano_bytes=clip.size,
            content_type=content_type,
        )
        return Response(EvidenciaSerializer(evidencia).data, status=status.HTTP_201_CREATED)


class EvidenciaConsultarView(APIView):
    """
    GET /api/evidencia/<evento_id>/?tipo=ocupacion|entrega|fusion   (RF-14)
    Devuelve los clips VIGENTES del evento con una URL firmada de S3 (o de descarga autenticada
    en almacenamiento local). Un clip vencido ya no está disponible aunque el job de purga no haya corrido (RNF-03).
    """

    permission_classes = [EsAdministrador]

    def get(self, request, evento_id):
        tipo = request.query_params.get("tipo", "ocupacion")
        if tipo not in MODELO_POR_TIPO:
            raise ValidationError({"tipo": "Debe ser ocupacion, entrega o fusion."})
        if not MODELO_POR_TIPO[tipo].objects.filter(pk=evento_id).exists():
            raise NotFound(f"No existe el evento de {tipo} con id {evento_id}.")

        storage = obtener_storage()
        clips = []
        for ev in _evidencias_vigentes(tipo, evento_id):
            url = storage.url_firmada(ev.s3_key, settings.AWS_S3_SIGNED_URL_EXPIRES)
            if url is None:
                url = request.build_absolute_uri(reverse("evidencia-descargar", args=[ev.pk]))
            clips.append({**EvidenciaSerializer(ev).data, "url": url})
        if not clips:
            raise NotFound("No hay evidencia de video disponible para este evento (no existe o ya venció).")
        return Response({"evento": {"tipo": tipo, "id": evento_id}, "clips": clips})


class EvidenciaDescargarView(APIView):
    """GET /api/evidencia/clip/<id>/descargar/ — sirve el clip (autenticado). En S3 redirige a la URL firmada."""

    permission_classes = [EsAdministrador]

    def get(self, request, pk):
        ev = EvidenciaVideo.objects.filter(pk=pk, expira_en__gt=timezone.now()).first()
        if ev is None:
            raise Http404
        storage = obtener_storage()
        url = storage.url_firmada(ev.s3_key, settings.AWS_S3_SIGNED_URL_EXPIRES)
        if url:
            return HttpResponseRedirect(url)
        if not storage.existe(ev.s3_key):
            raise Http404
        return _servir_con_rango(request, storage.abrir(ev.s3_key), ev.content_type)


def _servir_con_rango(request, archivo, content_type, chunk=64 * 1024):
    """
    Sirve un archivo soportando `Range: bytes=a-b` (206 Partial Content). Los navegadores lo usan para reproducir
    y adelantar un video; sin esto Chrome/Safari pueden negarse a mostrarlo.
    """
    archivo.seek(0, os.SEEK_END)
    total = archivo.tell()
    archivo.seek(0)
    cabecera = request.headers.get("Range", "")
    m = re.fullmatch(r"bytes=(\d*)-(\d*)", cabecera.strip()) if cabecera else None
    if not m or (not m.group(1) and not m.group(2)):
        respuesta = FileResponse(archivo, content_type=content_type)
        respuesta["Accept-Ranges"] = "bytes"
        return respuesta

    if m.group(1):
        inicio = int(m.group(1))
        fin = int(m.group(2)) if m.group(2) else total - 1
    else:  # "bytes=-N": los últimos N bytes
        inicio, fin = max(total - int(m.group(2)), 0), total - 1
    fin = min(fin, total - 1)
    if inicio > fin or inicio >= total:
        archivo.close()
        r = HttpResponse(status=416)
        r["Content-Range"] = f"bytes */{total}"
        return r

    def generar():
        try:
            archivo.seek(inicio)
            restante = fin - inicio + 1
            while restante > 0:
                datos = archivo.read(min(chunk, restante))
                if not datos:
                    break
                restante -= len(datos)
                yield datos
        finally:
            archivo.close()

    r = StreamingHttpResponse(generar(), status=206, content_type=content_type)
    r["Content-Range"] = f"bytes {inicio}-{fin}/{total}"
    r["Content-Length"] = str(fin - inicio + 1)
    r["Accept-Ranges"] = "bytes"
    return r
