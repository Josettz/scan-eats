# vision_service/scaneats_vision/dispatcher.py
"""
Cola de eventos hacia el backend, en un hilo aparte para NO bloquear el procesamiento de video.

* Orden estricto (un solo trabajador): la ocupación se reporta antes que sus cambios de estado y entregas.
* Si el backend no responde (ApiUnavailable) el mismo evento se reintenta con espera creciente hasta que
  vuelva: no se pierden eventos por una caída corta. Los errores 4xx (p. ej. 409 conflicto, 422 zona de
  exclusión) se registran y se descartan porque reintentar no los arreglaría.
* Cola acotada: si se llena (caída larguísima) se descarta el evento MÁS ANTIGUO, con aviso.
* `sync=True` ejecuta en línea (pruebas y modo simple).
"""
from __future__ import annotations

import logging
import queue
import threading
import time
from typing import Callable, Optional

from .api_client import ApiAuthError, ApiError, ApiUnavailable

log = logging.getLogger(__name__)


class EventDispatcher:
    def __init__(self, api, sync: bool = False, max_queue: int = 5000, retry_wait_s: float = 10.0,
                 sleep: Callable[[float], None] = time.sleep):
        self.api = api
        self.sync = sync
        self.retry_wait_s = retry_wait_s
        self._sleep = sleep
        self._q: "queue.Queue" = queue.Queue(maxsize=max_queue)
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.enviados = 0
        self.descartados = 0
        if not sync:
            self._thread = threading.Thread(target=self._run, name="scaneats-dispatcher", daemon=True)
            self._thread.start()

    def submit(self, method: str, **kwargs) -> None:
        if self.sync:
            self._ejecutar(method, kwargs, reintentar=False)
            return
        try:
            self._q.put_nowait((method, kwargs))
        except queue.Full:
            try:
                self._q.get_nowait()  # descarta el más antiguo
                self.descartados += 1
                log.error("Cola de eventos llena: se descartó el evento más antiguo.")
            except queue.Empty:
                pass
            self._q.put_nowait((method, kwargs))

    def _ejecutar(self, method: str, kwargs: dict, reintentar: bool) -> None:
        while True:
            try:
                getattr(self.api, method)(**kwargs)
                self.enviados += 1
                return
            except ApiUnavailable as exc:
                if not reintentar or self._stop.is_set():
                    log.error("No se pudo enviar %s: %s", method, exc)
                    self.descartados += 1
                    return
                log.warning("Backend caído; %s se reintentará en %.0f s.", method, self.retry_wait_s)
                self._sleep(self.retry_wait_s)
            except ApiAuthError as exc:
                log.critical("Token de servicio rechazado (%s). Rote/actualice VISION_API_TOKEN.", exc)
                self.descartados += 1
                return
            except ApiError as exc:
                log.warning("El backend rechazó %s (se descarta, no se reintenta): %s", method, exc)
                self.descartados += 1
                return
            except Exception:  # noqa: BLE001 - un evento malformado no debe tumbar el hilo
                log.exception("Error inesperado enviando %s", method)
                self.descartados += 1
                return

    def _run(self) -> None:
        while not self._stop.is_set() or not self._q.empty():
            try:
                method, kwargs = self._q.get(timeout=0.2)
            except queue.Empty:
                continue
            self._ejecutar(method, kwargs, reintentar=True)
            self._q.task_done()

    def close(self, drain: bool = True, timeout_s: float = 30.0) -> None:
        """Detiene el hilo; con drain=True espera a que se envíe lo pendiente."""
        if self.sync or self._thread is None:
            return
        if drain:
            fin = time.monotonic() + timeout_s
            while not self._q.empty() and time.monotonic() < fin:
                time.sleep(0.05)
        self._stop.set()
        self._thread.join(timeout=2.0)
