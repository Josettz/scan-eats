# restaurante_api/scaneats/tests/test_evidencia.py
"""RF-14 (consultar clip < 10 s) y RNF-03 (retención mínima 30 días, borrado automático)."""
import io
import time
from datetime import timedelta
from unittest import mock

from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.test import override_settings
from django.utils import timezone

from scaneats.models import EvidenciaVideo
from scaneats.storage import S3ClipStorage, obtener_storage, reiniciar_cache_storage

from .base import ApiTestBase, t

CLIP = b"\x00\x00\x00\x18ftypmp42" + b"\x01" * 2048


def clip(nombre="evento.mp4", tipo="video/mp4", contenido=CLIP):
    return SimpleUploadedFile(nombre, contenido, content_type=tipo)


class EvidenciaLocalTests(ApiTestBase):
    def setUp(self):
        super().setUp()
        _, (self.mesa, *_) = self.crear_salon(2)
        self.ocupacion = self.crear_ocupacion(self.mesa, t(10))
        self.entrega = self.crear_entrega(self.ocupacion, self.crear_mesero(), t(10, 5))

    def subir(self, **extra):
        datos = {"clip": clip(), "tipo": "ocupacion", "evento_id": self.ocupacion.pk, **extra}
        return self.vision.post("/api/evidencia/", datos, format="multipart")

    def test_subir_y_consultar_clip_de_un_evento_en_menos_de_10_segundos(self):  # RF-14
        r = self.subir(duracion_s=12.5)
        self.assertEqual(r.status_code, 201, r.content)
        t0 = time.perf_counter()
        r = self.admin_client.get(f"/api/evidencia/{self.ocupacion.pk}/?tipo=ocupacion")
        self.assertLess(time.perf_counter() - t0, 10)
        self.assertEqual(r.status_code, 200, r.content)
        clips = r.json()["clips"]
        self.assertEqual(len(clips), 1)
        self.assertIn("/api/evidencia/clip/", clips[0]["url"])
        self.assertEqual(clips[0]["duracion_s"], 12.5)
        # y el clip realmente se puede descargar
        d = self.admin_client.get(clips[0]["url"])
        self.assertEqual(d.status_code, 200)
        self.assertEqual(b"".join(d.streaming_content), CLIP)

    def test_el_clip_se_puede_asociar_por_uid_del_evento(self):
        # La visión no conoce los ids de la BD: identifica el evento por el uid con el que lo reportó.
        uid = "3f2b8c9e-6a3a-4f0e-9a51-0d2f3d9b7c11"
        self.ocupacion.uid = uid
        self.ocupacion.save(update_fields=["uid"])
        r = self.vision.post("/api/evidencia/", {"clip": clip(), "tipo": "ocupacion", "evento_uid": uid}, format="multipart")
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual(EvidenciaVideo.objects.get().ocupacion, self.ocupacion)
        r = self.vision.post("/api/evidencia/", {"clip": clip(), "tipo": "ocupacion", "evento_uid": "9f2b8c9e-6a3a-4f0e-9a51-0d2f3d9b7c99"}, format="multipart")
        self.assertEqual(r.status_code, 404)
        r = self.vision.post("/api/evidencia/", {"clip": clip(), "tipo": "ocupacion"}, format="multipart")
        self.assertEqual(r.status_code, 400)  # falta la referencia al evento

    def test_la_descarga_soporta_rangos_para_que_el_navegador_reproduzca_y_avance(self):
        self.subir()
        url = self.admin_client.get(f"/api/evidencia/{self.ocupacion.pk}/").json()["clips"][0]["url"]
        r = self.admin_client.get(url)
        self.assertEqual((r.status_code, r["Accept-Ranges"]), (200, "bytes"))
        r.close()  # libera el archivo (en Windows no se puede borrar un archivo abierto)
        r = self.admin_client.get(url, HTTP_RANGE="bytes=0-9")
        self.assertEqual(r.status_code, 206)
        self.assertEqual(r["Content-Range"], f"bytes 0-9/{len(CLIP)}")
        self.assertEqual(b"".join(r.streaming_content), CLIP[:10])
        r = self.admin_client.get(url, HTTP_RANGE="bytes=100-")  # hasta el final
        self.assertEqual(b"".join(r.streaming_content), CLIP[100:])
        r = self.admin_client.get(url, HTTP_RANGE="bytes=-16")  # los últimos 16 bytes
        self.assertEqual(b"".join(r.streaming_content), CLIP[-16:])
        r = self.admin_client.get(url, HTTP_RANGE=f"bytes={len(CLIP) + 5}-")  # fuera de rango
        self.assertEqual((r.status_code, r["Content-Range"]), (416, f"bytes */{len(CLIP)}"))
        r = self.admin_client.get(url, HTTP_RANGE="basura")  # rango inválido: se ignora
        self.assertEqual(r.status_code, 200)
        r.close()

    def test_soporta_clips_de_entrega_y_de_fusion(self):
        self.subir(tipo="entrega", evento_id=self.entrega.pk)
        r = self.admin_client.get(f"/api/evidencia/{self.entrega.pk}/?tipo=entrega")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(EvidenciaVideo.objects.get().entrega, self.entrega)
        self.assertEqual(self.admin_client.get(f"/api/evidencia/{self.ocupacion.pk}/?tipo=ocupacion").status_code, 404)

    def test_la_expiracion_respeta_la_politica_de_retencion(self):  # RNF-03
        self.subir()
        ev = EvidenciaVideo.objects.get()
        self.assertAlmostEqual((ev.expira_en - ev.creado_en).total_seconds(), 30 * 86400, delta=5)
        with override_settings(EVIDENCIA_RETENCION_DIAS=45):
            self.subir()
        ev2 = EvidenciaVideo.objects.exclude(pk=ev.pk).get()
        self.assertAlmostEqual((ev2.expira_en - ev2.creado_en).total_seconds(), 45 * 86400, delta=5)

    def test_un_clip_vencido_ya_no_esta_disponible(self):  # RNF-03: "más de 30 días ya no está disponible"
        self.subir()
        EvidenciaVideo.objects.update(expira_en=timezone.now() - timedelta(seconds=1))
        r = self.admin_client.get(f"/api/evidencia/{self.ocupacion.pk}/")
        self.assertEqual(r.status_code, 404)
        pk = EvidenciaVideo.objects.get().pk
        self.assertEqual(self.admin_client.get(f"/api/evidencia/clip/{pk}/descargar/").status_code, 404)

    def test_purga_borra_solo_los_clips_vencidos_de_bd_y_almacenamiento(self):
        self.subir()
        self.subir()
        vencida, vigente = EvidenciaVideo.objects.order_by("id")
        EvidenciaVideo.objects.filter(pk=vencida.pk).update(expira_en=timezone.now() - timedelta(days=1))
        storage = obtener_storage()
        self.assertTrue(storage.existe(vencida.s3_key))

        call_command("purgar_evidencia", "--dry-run", stdout=io.StringIO())
        self.assertEqual(EvidenciaVideo.objects.count(), 2)  # dry-run no borra nada

        call_command("purgar_evidencia", stdout=io.StringIO())
        self.assertEqual(list(EvidenciaVideo.objects.values_list("pk", flat=True)), [vigente.pk])
        self.assertFalse(storage.existe(vencida.s3_key))
        self.assertTrue(storage.existe(vigente.s3_key))

    def test_evento_inexistente_o_tipo_invalido(self):
        self.assertEqual(self.admin_client.get("/api/evidencia/9999/").status_code, 404)
        self.assertEqual(self.admin_client.get(f"/api/evidencia/{self.ocupacion.pk}/?tipo=otro").status_code, 400)
        self.assertEqual(self.subir(evento_id=9999).status_code, 404)

    def test_solo_acepta_videos_y_respeta_el_tamano_maximo(self):
        r = self.vision.post("/api/evidencia/", {
            "clip": clip("x.txt", "text/plain", b"hola"), "tipo": "ocupacion", "evento_id": self.ocupacion.pk}, format="multipart")
        self.assertEqual(r.status_code, 400)
        with override_settings(CLIP_MAX_BYTES=100):
            self.assertEqual(self.subir().status_code, 400)
        self.assertEqual(EvidenciaVideo.objects.count(), 0)

    def test_el_administrador_no_sube_clips_y_el_servicio_no_los_consulta(self):
        r = self.admin_client.post("/api/evidencia/", {"clip": clip(), "tipo": "ocupacion", "evento_id": self.ocupacion.pk}, format="multipart")
        self.assertEqual(r.status_code, 403)
        self.assertEqual(self.vision.get(f"/api/evidencia/{self.ocupacion.pk}/").status_code, 403)

    def test_un_clip_debe_referirse_a_un_solo_evento(self):
        from django.db import IntegrityError, transaction
        with self.assertRaises(IntegrityError), transaction.atomic():
            EvidenciaVideo.objects.create(ocupacion=self.ocupacion, entrega=self.entrega, s3_key="x", expira_en=timezone.now())
        with self.assertRaises(IntegrityError), transaction.atomic():
            EvidenciaVideo.objects.create(s3_key="x", expira_en=timezone.now())


@override_settings(AWS_STORAGE_BUCKET_NAME="scaneats-evidencia", AWS_S3_SIGNED_URL_EXPIRES=120)
class EvidenciaS3Tests(ApiTestBase):
    """El mismo flujo contra S3, con un cliente boto3 simulado (no toca la red)."""

    CLIP_BACKEND = "s3"

    def setUp(self):
        super().setUp()
        reiniciar_cache_storage()
        self.s3 = mock.MagicMock()
        self.s3.generate_presigned_url.return_value = "https://scaneats-evidencia.s3.amazonaws.com/k?X-Amz-Signature=abc"
        patcher = mock.patch.object(S3ClipStorage, "_crear_cliente", return_value=self.s3)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(reiniciar_cache_storage)
        _, (mesa, *_) = self.crear_salon(1)
        self.ocupacion = self.crear_ocupacion(mesa, t(10))

    def test_subida_a_s3_y_url_firmada(self):
        r = self.vision.post("/api/evidencia/", {"clip": clip(), "tipo": "ocupacion", "evento_id": self.ocupacion.pk}, format="multipart")
        self.assertEqual(r.status_code, 201, r.content)
        args, kwargs = self.s3.upload_fileobj.call_args
        self.assertEqual(args[1], "scaneats-evidencia")
        self.assertTrue(args[2].startswith(f"evidencia/ocupacion/{self.ocupacion.pk}/"))
        self.assertEqual(kwargs["ExtraArgs"]["ServerSideEncryption"], "AES256")

        r = self.admin_client.get(f"/api/evidencia/{self.ocupacion.pk}/")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["clips"][0]["url"].startswith("https://scaneats-evidencia.s3.amazonaws.com/"))
        params = self.s3.generate_presigned_url.call_args.kwargs
        self.assertEqual(params["ExpiresIn"], 120)
        self.assertEqual(params["Params"]["Bucket"], "scaneats-evidencia")

    def test_la_purga_borra_el_objeto_de_s3(self):
        EvidenciaVideo.objects.create(
            ocupacion=self.ocupacion, s3_key="evidencia/ocupacion/1/viejo.mp4", expira_en=timezone.now() - timedelta(days=1))
        call_command("purgar_evidencia", stdout=io.StringIO())
        self.s3.delete_object.assert_called_once_with(Bucket="scaneats-evidencia", Key="evidencia/ocupacion/1/viejo.mp4")
        self.assertEqual(EvidenciaVideo.objects.count(), 0)

    def test_si_s3_falla_se_conserva_la_fila_para_reintentar(self):
        EvidenciaVideo.objects.create(ocupacion=self.ocupacion, s3_key="k.mp4", expira_en=timezone.now() - timedelta(days=1))
        self.s3.delete_object.side_effect = RuntimeError("S3 caído")
        call_command("purgar_evidencia", stdout=io.StringIO())
        self.assertEqual(EvidenciaVideo.objects.count(), 1)

    def test_descarga_redirige_a_la_url_firmada(self):
        ev = EvidenciaVideo.objects.create(ocupacion=self.ocupacion, s3_key="k.mp4", expira_en=timezone.now() + timedelta(days=5))
        r = self.admin_client.get(f"/api/evidencia/clip/{ev.pk}/descargar/")
        self.assertEqual(r.status_code, 302)
        self.assertIn("X-Amz-Signature", r["Location"])
