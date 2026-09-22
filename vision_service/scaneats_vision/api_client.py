# vision_service/scaneats_vision/api_client.py
"""
Cliente HTTP hacia `restaurante_api` (único canal entre los dos servicios; nunca hay acceso directo a la BD).

* Autenticación por token de servicio: `Authorization: Token <token>` (RF-13).
* Reintentos con backoff exponencial + jitter ante caídas de red, 408/425/429 y 5xx.
* Un 401/403 o cualquier otro 4xx NO se reintenta (no se arregla esperando): se lanza `ApiError`.
* Las peticiones son idempotentes en el backend (campo `uid`), así que reintentar es seguro.
"""
from __future__ import annotations

import logging
import os
import random
import time
import uuid
from datetime import datetime, timezone
from typing import Callable, Iterable, Optional

log = logging.getLogger(__name__)

RETRIABLE_STATUS = {408, 425, 429, 500, 502, 503, 504}


class ApiError(Exception):
    def __init__(self, message: str, status: Optional[int] = None, body=None):
        super().__init__(message)
        self.status = status
        self.body = body


class ApiAuthError(ApiError):
    """401/403: token ausente, inválido o rotado."""


class ApiUnavailable(ApiError):
    """El backend no respondió correctamente tras agotar los reintentos."""


def iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


def nuevo_uid() -> str:
    return str(uuid.uuid4())


class ApiClient:
    def __init__(
        self,
        base_url: str,
        token: str,
        session=None,
        max_retries: int = 5,
        backoff_base_s: float = 0.5,
        backoff_max_s: float = 15.0,
        timeout_s: float = 5.0,
        sleep: Callable[[float], None] = time.sleep,
        jitter: Callable[[], float] = random.random,
    ):
        if not token:
            raise ValueError("Falta el token de servicio (VISION_API_TOKEN).")
        self.base_url = base_url.rstrip("/")
        self.token = token
        self._session = session
        self.max_retries = max_retries
        self.backoff_base_s = backoff_base_s
        self.backoff_max_s = backoff_max_s
        self.timeout_s = timeout_s
        self._sleep = sleep
        self._jitter = jitter

    # ------------------------------------------------------------------ #
    @property
    def session(self):
        if self._session is None:
            import requests  # perezoso

            self._session = requests.Session()
        return self._session

    def _espera(self, intento: int, retry_after: Optional[float] = None) -> float:
        if retry_after is not None:
            return min(retry_after, self.backoff_max_s)
        base = min(self.backoff_max_s, self.backoff_base_s * (2 ** intento))
        return base * (0.5 + 0.5 * self._jitter())  # jitter: evita reintentos sincronizados

    def _request(self, method: str, path: str, json=None, data=None, files=None):
        url = f"{self.base_url}/api/{path.lstrip('/')}"
        headers = {"Authorization": f"Token {self.token}", "Accept": "application/json"}
        ultimo_error = "sin respuesta"
        for intento in range(self.max_retries + 1):
            retry_after = None
            try:
                r = self.session.request(method, url, json=json, data=data, files=files, headers=headers, timeout=self.timeout_s)
            except OSError as exc:  # requests.RequestException hereda de OSError (conexión, timeout, DNS...)
                ultimo_error = f"{type(exc).__name__}: {exc}"
            else:
                if 200 <= r.status_code < 300:
                    return r.json() if r.content else None
                cuerpo = self._cuerpo(r)
                if r.status_code in (401, 403):
                    raise ApiAuthError(f"{method} {path} -> {r.status_code}", r.status_code, cuerpo)
                if r.status_code not in RETRIABLE_STATUS:
                    raise ApiError(f"{method} {path} -> {r.status_code}: {cuerpo}", r.status_code, cuerpo)
                ultimo_error = f"HTTP {r.status_code}"
                ra = r.headers.get("Retry-After") if hasattr(r, "headers") else None
                retry_after = float(ra) if ra and str(ra).replace(".", "", 1).isdigit() else None
            if intento < self.max_retries:
                espera = self._espera(intento, retry_after)
                log.warning("%s %s falló (%s); reintento %d/%d en %.1f s", method, path, ultimo_error,
                            intento + 1, self.max_retries, espera)
                self._sleep(espera)
        raise ApiUnavailable(f"{method} {path}: backend no disponible tras {self.max_retries} reintentos ({ultimo_error})")

    @staticmethod
    def _cuerpo(r):
        try:
            return r.json()
        except Exception:  # noqa: BLE001
            return getattr(r, "text", "")

    # ------------------------------------------------------------------ #
    # Endpoints del backend
    # ------------------------------------------------------------------ #
    def validar_token(self):
        return self._request("GET", "auth/servicio/validar/")

    def obtener_configuracion(self):
        return self._request("GET", "vision/configuracion/")

    def reportar_ocupacion(self, mesa_numero: int, hora_inicio: datetime, detectado_en: datetime, uid: Optional[str] = None):
        """RF-01."""
        return self._request("POST", "eventos/ocupacion/", json={
            "mesa_numero": mesa_numero, "hora_inicio": iso(hora_inicio), "detectado_en": iso(detectado_en),
            "uid": uid or nuevo_uid(),
        })

    def cambiar_estado_ocupacion(self, mesa_numero: int, estado: str, hora_fin: Optional[datetime] = None):
        cuerpo = {"mesa_numero": mesa_numero, "estado": estado}
        if hora_fin is not None:
            cuerpo["hora_fin"] = iso(hora_fin)
        return self._request("POST", "eventos/ocupacion/cambiar-estado/", json=cuerpo)

    def reportar_entrega(
        self, mesa_numero: int, personal_identificador: str, hora: datetime,
        metodo: Optional[str] = None, confianza: Optional[float] = None, uid: Optional[str] = None,
    ):
        """RF-02 (+ RF-08: método de identificación)."""
        cuerpo = {"mesa_numero": mesa_numero, "personal_identificador": personal_identificador,
                  "hora": iso(hora), "uid": uid or nuevo_uid()}
        if metodo:
            cuerpo["metodo_identificacion"] = metodo
        if confianza is not None:
            cuerpo["confianza"] = round(max(0.0, min(1.0, confianza)), 4)
        return self._request("POST", "eventos/entrega/", json=cuerpo)

    def reportar_fusion(self, mesas: Iterable[int], hora_evento: datetime, uid: Optional[str] = None):
        """RF-03."""
        return self._request("POST", "eventos/fusion/", json={
            "mesas": sorted(mesas), "hora_evento": iso(hora_evento), "uid": uid or nuevo_uid()})

    def finalizar_fusion(self, mesas: Iterable[int], hora_fin: datetime):
        return self._request("POST", "eventos/fusion/finalizar/", json={"mesas": sorted(mesas), "hora_fin": iso(hora_fin)})

    def reportar_personal_clasificado(
        self, metodo: str, confianza: float, personal_identificador: Optional[str] = None,
        track_id: Optional[int] = None, mesa_numero: Optional[int] = None, timestamp: Optional[datetime] = None,
    ):
        """RF-08: auditoría de cada identificación de personal."""
        cuerpo = {"metodo": metodo, "confianza": round(max(0.0, min(1.0, confianza)), 4)}
        if personal_identificador:
            cuerpo["personal_identificador"] = personal_identificador
        if track_id is not None:
            cuerpo["track_id"] = track_id
        if mesa_numero is not None:
            cuerpo["mesa_numero"] = mesa_numero
        if timestamp is not None:
            cuerpo["timestamp"] = iso(timestamp)
        return self._request("POST", "eventos/personal-clasificado/", json=cuerpo)

    def subir_clip(self, tipo: str, evento_uid: str, ruta_clip: str, timestamp: Optional[datetime] = None,
                   duracion_s: Optional[float] = None, borrar: bool = True):
        """
        RF-14: sube el clip de un evento (identificado por el `uid` que la visión generó al reportarlo).
        Con borrar=True el archivo temporal se elimina al subirse bien (no se acumulan clips en el disco local).
        """
        datos = {"tipo": tipo, "evento_uid": evento_uid}
        if timestamp is not None:
            datos["timestamp"] = iso(timestamp)
        if duracion_s is not None:
            datos["duracion_s"] = str(duracion_s)
        nombre = os.path.basename(ruta_clip)
        with open(ruta_clip, "rb") as f:
            respuesta = self._request("POST", "evidencia/", data=datos, files={"clip": (nombre, f, "video/mp4")})
        if borrar:
            try:
                os.remove(ruta_clip)
            except OSError:
                log.warning("No se pudo borrar el clip temporal %s", ruta_clip)
        return respuesta
