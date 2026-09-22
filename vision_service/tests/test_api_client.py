# vision_service/tests/test_api_client.py
import json
import os
from datetime import datetime, timezone

import pytest

from scaneats_vision.api_client import ApiAuthError, ApiClient, ApiError, ApiUnavailable, iso

T0 = datetime(2026, 3, 1, 10, 0, 0, tzinfo=timezone.utc)


class Resp:
    def __init__(self, status=200, body=None, headers=None):
        self.status_code = status
        self._body = body if body is not None else {}
        self.headers = headers or {}
        self.content = json.dumps(self._body).encode()
        self.text = json.dumps(self._body)

    def json(self):
        return self._body


class FakeSession:
    """Responde con una lista de respuestas/excepciones, en orden, y guarda cada petición."""

    def __init__(self, *respuestas):
        self.respuestas = list(respuestas)
        self.llamadas = []

    def request(self, method, url, **kwargs):
        self.llamadas.append({"method": method, "url": url, **kwargs})
        r = self.respuestas.pop(0) if len(self.respuestas) > 1 else self.respuestas[0]
        if isinstance(r, Exception):
            raise r
        return r


def cliente(*respuestas, **kw):
    dormidas = []
    api = ApiClient("http://api:8000/", "tok123", session=FakeSession(*respuestas), sleep=dormidas.append,
                    jitter=lambda: 1.0, **kw)
    return api, api.session, dormidas


class TestAutenticacion:
    def test_envia_el_token_de_servicio(self):
        api, s, _ = cliente(Resp(200, {"valido": True}))
        assert api.validar_token() == {"valido": True}
        c = s.llamadas[0]
        assert c["headers"]["Authorization"] == "Token tok123"
        assert c["url"] == "http://api:8000/api/auth/servicio/validar/"  # sin doble barra

    def test_exige_token(self):
        with pytest.raises(ValueError):
            ApiClient("http://x", "")

    @pytest.mark.parametrize("codigo", [401, 403])
    def test_token_rechazado_no_se_reintenta(self, codigo):
        api, s, dormidas = cliente(Resp(codigo, {"detail": "no"}))
        with pytest.raises(ApiAuthError):
            api.validar_token()
        assert len(s.llamadas) == 1 and dormidas == []


class TestReintentos:
    def test_reintenta_con_backoff_exponencial_y_termina_bien(self):
        api, s, dormidas = cliente(ConnectionError("sin red"), TimeoutError("lento"), Resp(200, {"ok": 1}),
                                   backoff_base_s=0.5)
        assert api.validar_token() == {"ok": 1}
        assert len(s.llamadas) == 3
        assert dormidas == [0.5, 1.0]  # 0.5·2⁰ y 0.5·2¹ (jitter=1)

    def test_reintenta_ante_5xx_y_429(self):
        api, s, dormidas = cliente(Resp(503), Resp(429, headers={"Retry-After": "3"}), Resp(200, {"ok": 1}))
        assert api.validar_token() == {"ok": 1}
        assert dormidas[1] == 3.0  # respeta Retry-After

    def test_el_backoff_tiene_tope(self):
        api, _, dormidas = cliente(ConnectionError(), max_retries=8, backoff_base_s=1.0, backoff_max_s=4.0)
        with pytest.raises(ApiUnavailable):
            api.validar_token()
        assert max(dormidas) == 4.0 and len(dormidas) == 8

    def test_agotados_los_reintentos_lanza_api_unavailable(self):
        api, s, dormidas = cliente(Resp(500), max_retries=3)
        with pytest.raises(ApiUnavailable, match="3 reintentos"):
            api.validar_token()
        assert len(s.llamadas) == 4 and len(dormidas) == 3  # 1 intento + 3 reintentos

    @pytest.mark.parametrize("codigo", [400, 404, 409, 422])
    def test_errores_4xx_del_negocio_no_se_reintentan(self, codigo):
        api, s, dormidas = cliente(Resp(codigo, {"detail": "rechazado"}))
        with pytest.raises(ApiError) as e:
            api.reportar_ocupacion(1, T0, T0)
        assert e.value.status == codigo and not isinstance(e.value, (ApiUnavailable, ApiAuthError))
        assert len(s.llamadas) == 1 and dormidas == []

    def test_jitter_reduce_la_espera(self):
        api = ApiClient("http://x", "t", session=FakeSession(ConnectionError()), sleep=lambda s: None,
                        jitter=lambda: 0.0, backoff_base_s=2.0)
        assert api._espera(0) == 1.0  # base · (0.5 + 0.5·jitter)


class TestPayloads:
    def test_ocupacion(self):
        api, s, _ = cliente(Resp(201, {"id": 1}))
        api.reportar_ocupacion(3, T0, T0, uid="u-1")
        c = s.llamadas[0]
        assert (c["method"], c["url"].endswith("/api/eventos/ocupacion/")) == ("POST", True)
        assert c["json"] == {"mesa_numero": 3, "hora_inicio": T0.isoformat(), "detectado_en": T0.isoformat(), "uid": "u-1"}

    def test_cada_reporte_lleva_un_uid_para_la_idempotencia(self):
        api, s, _ = cliente(Resp(201, {}))
        api.reportar_ocupacion(1, T0, T0)
        api.reportar_ocupacion(1, T0, T0)
        u1, u2 = (c["json"]["uid"] for c in s.llamadas)
        assert u1 and u2 and u1 != u2

    def test_cambio_de_estado(self):
        api, s, _ = cliente(Resp(200, {}))
        api.cambiar_estado_ocupacion(3, "LIBRE", T0)
        assert s.llamadas[0]["json"] == {"mesa_numero": 3, "estado": "LIBRE", "hora_fin": T0.isoformat()}
        api.cambiar_estado_ocupacion(3, "POSIBLEMENTE_LIBRE")
        assert "hora_fin" not in s.llamadas[1]["json"]

    def test_entrega_con_metodo_y_confianza(self):
        api, s, _ = cliente(Resp(201, {}))
        api.reportar_entrega(2, "MES-A", T0, metodo="VESTIMENTA", confianza=1.7, uid="u")
        j = s.llamadas[0]["json"]
        assert (j["mesa_numero"], j["personal_identificador"], j["metodo_identificacion"]) == (2, "MES-A", "VESTIMENTA")
        assert j["confianza"] == 1.0  # se acota a [0, 1]

    def test_fusion_y_fin_de_fusion(self):
        api, s, _ = cliente(Resp(201, {}))
        api.reportar_fusion({5, 4}, T0, uid="u")
        assert s.llamadas[0]["json"]["mesas"] == [4, 5]
        api.finalizar_fusion([6, 5], T0)
        assert s.llamadas[1]["url"].endswith("/api/eventos/fusion/finalizar/")

    def test_personal_clasificado(self):
        api, s, _ = cliente(Resp(201, {}))
        api.reportar_personal_clasificado("COMPORTAMIENTO", 0.7, None, track_id=9, mesa_numero=2, timestamp=T0)
        j = s.llamadas[0]["json"]
        assert "personal_identificador" not in j and j["track_id"] == 9 and j["metodo"] == "COMPORTAMIENTO"

    def test_iso_asume_utc_si_la_fecha_es_ingenua(self):
        assert iso(datetime(2026, 1, 1, 12, 0)).endswith("+00:00")

    def test_subir_clip_y_borrar_el_temporal(self, tmp_path):
        clip = tmp_path / "ocupacion_u.mp4"
        clip.write_bytes(b"video")
        api, s, _ = cliente(Resp(201, {"id": 1}))
        api.subir_clip("ocupacion", "uid-9", str(clip), timestamp=T0, duracion_s=12.5)
        c = s.llamadas[0]
        assert c["data"]["evento_uid"] == "uid-9" and c["data"]["duracion_s"] == "12.5"
        assert c["files"]["clip"][0] == "ocupacion_u.mp4" and c["files"]["clip"][2] == "video/mp4"
        assert not os.path.exists(clip)  # no se acumulan clips en el disco local

    def test_subir_clip_conserva_el_archivo_si_falla(self, tmp_path):
        clip = tmp_path / "x.mp4"
        clip.write_bytes(b"video")
        api, _, _ = cliente(Resp(404, {"detail": "no existe"}))
        with pytest.raises(ApiError):
            api.subir_clip("entrega", "u", str(clip))
        assert os.path.exists(clip)
